# SPDX-License-Identifier: Apache-2.0
"""Capture actual quiescence in the original receipt/event store, never APPLIED."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from ..contracts.models import Event, sha256_hex
from ..graph.revision_records import BaselineProofContext, SourceRef
from ..planning.htn.grounding import derive_id
from ..runtime.planning_operations import StoreOperationReader, build_operation_snapshot
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.store import Store, StoreError
from .taskgraph_baseline import BaselineCaptureRequest
from .taskgraph_runtime_imports import TaskGraphRuntimeImports


class TaskGraphBaselineProof:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator, self.store = orchestrator, orchestrator.store
        self.runtime = TaskGraphRuntimeImports(orchestrator)

    def __call__(self, store: Store, request: BaselineCaptureRequest) -> SourceRef:
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_BASELINE_PROOF_TRANSACTION_REQUIRED")
        enabled = store.get_receipt(request.enabling_command_id)
        enabling_intent = {"kind": "EnableTaskGraphContract", "mission_id": request.mission_id,
            "kernel_version": "taskgraph-exec-v2", "principal_id": request.principal.principal_id}
        if (enabled is None or enabled.get("kind") != "TaskGraphContractEnabled"
                or enabled.get("mission_id") != request.mission_id
                or enabled.get("intent_hash") != sha256_hex(enabling_intent)
                or store.last_event_seq(request.mission_id) != request.captured_through_seq):
            raise StoreError("TASKGRAPH_BASELINE_PROOF_ENABLE_BINDING_CHANGED")
        local = read_local_work(store, request.mission_id)
        tasks = frozenset(task.id for task in store.list_tasks(request.mission_id))
        rows = local.to_json()
        if (local.blocking_subjects(tasks)
                or any(row["state"] not in {"SETTLED", "FAILED"} for row in rows["intents"])
                or any(row["state"] != "SETTLED" for row in rows["reservations"])
                or any(row["state"] in {"RESERVED", "HANDED_OFF", "UNKNOWN"} for row in rows["provider_grants"])
                or any(row["unknown"] for row in rows["usage"])):
            raise StoreError("TASKGRAPH_BASELINE_PROOF_LOCAL_WORK_UNSETTLED")
        runtime = self.runtime(store, request.mission_id)
        if any(item["physical_settled"] is not True
               for item in runtime.value.to_json()["document"]["subjects"]):
            raise StoreError("TASKGRAPH_BASELINE_PROOF_RUNTIME_UNSETTLED")
        operations = build_operation_snapshot(request.mission_id, reader=StoreOperationReader(store))
        if operations.unresolved:
            raise StoreError("TASKGRAPH_BASELINE_PROOF_OPERATIONS_UNSETTLED")
        identity = derive_id("tg-baseline-quiescence", request.command_id)
        context = BaselineProofContext(mission_id=request.mission_id, revision=request.revision,
            command_id=request.command_id, enabling_command_id=request.enabling_command_id,
            manifest_hash=request.manifest_hash, captured_through_seq=request.captured_through_seq)
        body = {"version": 1, "kind": "TaskGraphBaselineQuiescence", "proof_id": identity,
            "capture": asdict(context), "principal_id": request.principal.principal_id,
            "enabling_receipt_hash": sha256_hex(enabled), "local_work_hash": local.digest,
            "runtime_source_id": runtime.source_id, "runtime_source_hash": runtime.source_digest,
            "runtime_through_seq": runtime.through_seq, "operations_hash": operations.read_digest}
        digest = sha256_hex(body)
        mission = store.get_mission(request.mission_id)
        if mission is None:
            raise StoreError("MISSION_NOT_FOUND")
        store.insert_receipt(commit_id=identity, kind="TaskGraphBaselineQuiescence",
            subject_id=request.mission_id, base_version=mission.version, proposal_hash=digest, receipt=body)
        store.append_event(Event(id=derive_id("tg-baseline-quiescence-event", identity),
            type="TaskGraphBaselineQuiescenceRecorded", trace_id=request.command_id,
            mission_id=request.mission_id, task_id=None, attempt_id=None,
            actor_type="human", actor_id=request.principal.principal_id,
            payload={"proof_id": identity, "proof_hash": digest, "capture": asdict(context)},
            idempotency_key=identity, created_at=store.now))
        return SourceRef(channel="taskgraph_baseline_quiescence", identity=identity,
                         revision=request.revision, digest=digest)


def verify_baseline_proof(connection: sqlite3.Connection, reference: SourceRef,
                          context: BaselineProofContext) -> bool:
    """Historical verification of the actual capture, not today's quiescence."""
    row = connection.execute("SELECT kind,subject_id,proposal_hash,receipt_json FROM commit_receipts WHERE commit_id=?",
                             (reference.identity,)).fetchone()
    if row is None or tuple(row[:3]) != ("TaskGraphBaselineQuiescence", context.mission_id, reference.digest):
        return False
    body = json.loads(row[3])
    expected_keys = {"version", "kind", "proof_id", "capture", "principal_id", "enabling_receipt_hash",
                     "local_work_hash", "runtime_source_id", "runtime_source_hash", "runtime_through_seq", "operations_hash"}
    if (not isinstance(body, dict) or set(body) != expected_keys
            or type(body["version"]) is not int or body["version"] != 1
            or body["kind"] != "TaskGraphBaselineQuiescence" or body["proof_id"] != reference.identity
            or reference.channel != "taskgraph_baseline_quiescence" or reference.revision != context.revision
            or body["capture"] != asdict(context) or sha256_hex(body) != reference.digest
            or type(body["runtime_through_seq"]) is not int
            or body["runtime_through_seq"] != context.captured_through_seq):
        return False
    enabled_row = connection.execute("SELECT receipt_json FROM commit_receipts WHERE commit_id=?",
                                     (context.enabling_command_id,)).fetchone()
    if enabled_row is None or sha256_hex(json.loads(enabled_row[0])) != body["enabling_receipt_hash"]:
        return False
    event = connection.execute("SELECT seq,mission_id,actor_id,payload_json FROM events "
        "WHERE type='TaskGraphBaselineQuiescenceRecorded' AND idempotency_key=?", (reference.identity,)).fetchone()
    return (event is not None and event[0] > context.captured_through_seq
            and event[1] == context.mission_id and event[2] == body["principal_id"]
            and json.loads(event[3]) == {"proof_id": reference.identity, "proof_hash": reference.digest,
                                       "capture": asdict(context)})
