# SPDX-License-Identifier: Apache-2.0
"""Root resolution provenance from original immutable Commit receipts.

References identify actual resolution commits, not current validity or execution
permission. Historical reads stop before the next recorded plan revision and do
not consult today's mutable adoption flag.
"""
from __future__ import annotations

import json
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.models import sha256_hex
from ..contracts.resolution import GoalResolution
from ..contracts.semantic_base import TypedRef, TypedRefKind
from ..graph.network_codec import decode
from ..runtime.planning_operations import SourceUnavailable
from ..storage.htn_store import HtnStore
from ..storage.taskgraph_store import TaskGraphStore


class TaskGraphResolutionReader:
    def __init__(self, history: TaskGraphStore) -> None:
        self.history = history
        self.store = history.store

    def __call__(self, mission_id: str, revision: int) -> tuple[dict[str, Any], ...]:
        with self.store.read_view() as db:
            record = self.history.read_revision(mission_id, revision).record
            network = decode(record.document.to_json()).snapshot
            roots = {str(item.task_id): network.binding_for_occurrence(item.occurrence_id)
                     for item in network.occurrences if item.occurrence_id in network.root_occurrence_ids}
            # The chosen revision covers its lifetime, rather than only its commit
            # instant (when a new root normally has no resolution yet).
            following = db.execute(
                "SELECT e.seq FROM taskgraph_revision_records r JOIN events e ON e.event_id=r.event_id "
                "WHERE r.mission_id=? AND r.revision>? ORDER BY r.revision LIMIT 1",
                (mission_id, revision)).fetchone()
            through = (following[0] - 1 if following is not None else db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0])
            semantics = HtnStore(self.store)
            references: dict[str, dict[str, Any]] = {}
            for row in db.execute("SELECT event_id,seq,task_id,payload_json FROM events "
                                  "WHERE mission_id=? AND type='GoalResolutionCommitted' AND seq<=? ORDER BY seq",
                                  (mission_id, through)):
                payload = json.loads(row["payload_json"])
                if type(payload.get("is_mission_root")) is not bool:
                    raise SourceUnavailable("taskgraph_resolution_root_identity_missing")
                if not payload["is_mission_root"]:
                    continue
                command_id = payload.get("command_id")
                if not isinstance(command_id, str) or not command_id:
                    raise SourceUnavailable("taskgraph_resolution_command_missing")
                receipt = semantics.find_acceptance_receipt(mission_id, command_id)
                if (receipt is None or receipt.kind != "goal_resolution" or receipt.event_id != row["event_id"]
                        or receipt.subject_id != payload.get("resolution_id")
                        or receipt.intent_hash != payload.get("intent_hash")
                        or receipt.read_set_hash != payload.get("read_set_hash")
                        or dict(receipt.output_identity) != payload.get("output_identity")):
                    raise SourceUnavailable("taskgraph_resolution_receipt_mismatch")
                stored = db.execute("SELECT * FROM goal_resolutions WHERE mission_id=? AND resolution_id=?",
                                    (mission_id, receipt.subject_id)).fetchone()
                if stored is None:
                    raise SourceUnavailable("taskgraph_resolution_body_missing")
                resolution = GoalResolution.from_json(json.loads(stored["resolution_json"]))
                body = resolution.to_json()
                digest = sha256_hex(body)
                output = dict(receipt.output_identity)
                if (canonical_json(body) != stored["resolution_json"] or digest != stored["content_hash"]
                        or output != {"kind": "goal_resolution", "subject_id": str(resolution.resolution_id),
                            "content_hash": digest, "obligation_id": resolution.obligation_id,
                            "goal_task_id": resolution.goal_task_id, "is_mission_root": True}
                        or resolution.mission_id != mission_id or resolution.goal_task_id != row["task_id"]
                        or resolution.goal_task_id != payload.get("goal_task_id")
                        or resolution.obligation_id != payload.get("obligation_id")
                        or resolution.requirements_version != payload.get("requirements_version")
                        or resolution.contract_revision != payload.get("contract_revision")):
                    raise SourceUnavailable("taskgraph_resolution_identity_mismatch")
                binding = roots.get(resolution.goal_task_id)
                if (binding is None or int(binding.contract_revision) != resolution.contract_revision
                        or resolution.requirements_version != record.document.requirements_ref.revision):
                    continue
                identity = str(resolution.resolution_id)
                if identity in references:
                    raise SourceUnavailable("taskgraph_resolution_commit_ambiguous")
                references[identity] = TypedRef(kind=TypedRefKind.RESOLUTION, id=identity,
                    revision=resolution.contract_revision, content_hash=digest).to_json()
            # A missing event must not turn a persisted root receipt into an empty
            # answer. Check the other direction, including later-revision events.
            for receipt in semantics.list_acceptance_receipts(mission_id, kind="goal_resolution"):
                if receipt.output_identity.get("is_mission_root") is not True:
                    continue
                event = db.execute("SELECT mission_id,type FROM events WHERE event_id=?", (receipt.event_id,)).fetchone()
                if event is None or tuple(event) != (mission_id, "GoalResolutionCommitted"):
                    raise SourceUnavailable("taskgraph_resolution_event_missing")
            return tuple(references[key] for key in sorted(references))
