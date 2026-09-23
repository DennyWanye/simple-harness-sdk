# SPDX-License-Identifier: Apache-2.0
"""Capture an existing, quiescent original plan without rewriting its history."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..contracts.models import Event, sha256_hex
from ..contracts.semantic_base import TypedRef, TypedRefKind
from ..governance.permissions import Principal
from ..graph.network_codec import encode
from ..graph.notification_contracts import FollowupCauseRef, FollowupKind, FollowupV1
from ..graph.revision_events import revision_event_payload
from ..graph.revision_records import CapturedBaselineCertificate, SourceRef
from ..planning.htn.grounding import derive_id
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.htn_store import HtnStore
from ..storage.store import Store, StoreError
from ..storage.taskgraph_followups import TaskGraphFollowupStore
from ..storage.taskgraph_store import TaskGraphStore
from .hierarchical_dispatch import HierarchicalDispatch
from .plan_commits import _network_identity
from .taskgraph_policy import InstalledGraphPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineCaptureRequest:
    mission_id: str
    revision: int
    command_id: str
    enabling_command_id: str
    principal: Principal
    manifest_hash: str
    captured_through_seq: int


class TaskGraphBaselineCapture:
    def __init__(self, dispatch: Callable[[str], HierarchicalDispatch], *, history: TaskGraphStore,
                 prove_quiescence: Callable[[Store, BaselineCaptureRequest], SourceRef]) -> None:
        """The proof producer reads actual runtime receipts; it cannot do external IO.

        Its durable reference is independently checked by the history Store's
        installed reference verifier. A local empty work set alone is insufficient.
        """
        if not callable(dispatch):
            raise ValueError("baseline capture requires the original shared Store")
        self.dispatch_for = dispatch
        self.history = history
        self.prove_quiescence = prove_quiescence

    def capture(self, store: Store, *, mission_id: str, revision: int,
                command_id: str, caller: Principal, policy: InstalledGraphPolicy) -> None:
        if store is not self.history.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_BASELINE_REQUIRES_ENABLE_TRANSACTION")
        with store.transaction() as db:
            dispatch = self.dispatch_for(mission_id)
            if dispatch.store is not store:
                raise StoreError("TASKGRAPH_BASELINE_STORE_MISMATCH")
            mission = dispatch.require_hierarchical(mission_id)
            enabled = store.get_receipt(command_id)
            if (enabled is None or enabled.get("kind") != "TaskGraphContractEnabled"
                    or enabled.get("mission_id") != mission_id or enabled.get("command_id") != command_id
                    or enabled.get("policy_hash") != policy.content_hash):
                raise StoreError("TASKGRAPH_BASELINE_ENABLE_RECEIPT_UNAVAILABLE")
            semantics = HtnStore(store)
            active = semantics.active_plan_revision(mission_id)
            if active is None or int(active.revision) != revision:
                raise StoreError("TASKGRAPH_BASELINE_ACTIVE_REVISION_CHANGED")
            if db.execute("SELECT 1 FROM taskgraph_revision_records WHERE mission_id=?", (mission_id,)).fetchone():
                raise StoreError("TASKGRAPH_BASELINE_HISTORY_EXISTS")
            # This is explicitly a present-time capture. Requirements and binding
            # bytes come from the same current snapshot; no pre-capture history is
            # reconstructed from today's rows.
            requirements = semantics.latest_requirements_revision(mission_id)
            if requirements is None:
                raise StoreError("TASKGRAPH_BASELINE_REQUIREMENTS_UNAVAILABLE")
            requirement_ref = TypedRef(kind=TypedRefKind.REQUIREMENTS,
                id=str(requirements.revision_id), revision=int(requirements.revision),
                content_hash=sha256_hex(requirements.to_json()))
            network, integrity = dispatch._read_network(mission_id, capturing_baseline=True)
            if integrity is not None:
                raise integrity
            if sha256_hex(_network_identity(network)) != active.snapshot_hash:
                raise StoreError("TASKGRAPH_BASELINE_SHAPE_CHANGED")
            document = encode(network, requirement_ref)
            facts = read_local_work(store, mission_id)
            task_ids = frozenset(task.id for task in store.list_tasks(mission_id))
            local = facts.to_json()
            if (facts.blocking_subjects(task_ids)
                    or any(row["state"] not in {"SETTLED", "FAILED"} for row in local["intents"])
                    or any(row["state"] != "SETTLED" for row in local["reservations"])
                    or any(row["state"] in {"RESERVED", "HANDED_OFF", "UNKNOWN"} for row in local["provider_grants"])
                    or any(row["unknown"] for row in local["usage"])):
                raise StoreError("TASKGRAPH_BASELINE_LOCAL_WORK_UNSETTLED")
            through = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0]
            identity = derive_id("tg-baseline-capture", command_id)
            request = BaselineCaptureRequest(mission_id=mission_id, revision=revision, command_id=identity,
                enabling_command_id=command_id, principal=caller, manifest_hash=sha256_hex(document.to_json()),
                captured_through_seq=through)
            quiescence = self.prove_quiescence(store, request)
            if not isinstance(quiescence, SourceRef):
                raise StoreError("TASKGRAPH_BASELINE_QUIESCENCE_PROOF_UNAVAILABLE")
            policy_ref = SourceRef(channel="taskgraph_policy", identity=command_id, revision=1,
                                   digest=policy.content_hash)
            receipt = {"schema_version": 1, "kind": "TaskGraphBaselineCaptured", "command_id": identity,
                "mission_id": mission_id, "revision": revision, "enabling_command_id": command_id,
                "principal_id": caller.principal_id, "manifest_hash": request.manifest_hash,
                "sdk_snapshot_hash": active.snapshot_hash, "captured_through_seq": through,
                "quiescence_ref": quiescence.to_json(), "policy_ref": policy_ref.to_json(),
                "codec_manifest_hash": document.codec_manifest_hash}
            store.insert_receipt(commit_id=identity, kind="CaptureTaskGraphBaseline", subject_id=mission_id,
                base_version=mission.version, proposal_hash=sha256_hex(receipt), receipt=receipt)
            certificate = CapturedBaselineCertificate(captured_through_seq=through,
                baseline_command_ref=SourceRef(channel="taskgraph_baseline_capture", identity=identity,
                                              revision=revision, digest=sha256_hex(receipt)),
                quiescence_ref=quiescence, policy_ref=policy_ref, codec_manifest_hash=document.codec_manifest_hash)
            event_id = derive_id("tg-revision-recorded", identity)
            event = store.append_event(Event(id=event_id, type="TaskGraphRevisionRecorded", trace_id=identity,
                mission_id=mission_id, task_id=None, attempt_id=None, actor_type="human",
                actor_id=caller.principal_id, idempotency_key=event_id, created_at=store.now,
                payload=revision_event_payload(document, source_kind="CAPTURED_BASELINE",
                    sdk_snapshot_hash=active.snapshot_hash, command_id=identity, decision_id=None, parent=None)))
            self.history.insert_revision_record(document=document, source_kind="CAPTURED_BASELINE",
                sdk_snapshot_hash=active.snapshot_hash, certificate=certificate, admission_check_id=None,
                event_id=event.id, command_id=identity, created_at=store.now)
            TaskGraphFollowupStore(store).append_followup(FollowupV1(mission_id=mission_id,
                source_event_id=event.id, kind=FollowupKind.REEVALUATE, subject_key=mission_id,
                source_revision=revision, cause_ref=FollowupCauseRef(kind="plan_revision", id=mission_id,
                    revision=revision, content_hash=request.manifest_hash)), now_ms=int(store.now * 1000))
