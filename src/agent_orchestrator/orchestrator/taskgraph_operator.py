# SPDX-License-Identifier: Apache-2.0
"""Authenticated operator commands on original Store; never model-callable."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ..contracts.models import Event, sha256_hex
from ..governance.permissions import Principal
from ..graph.notification_contracts import FollowupCauseRef, FollowupKind, FollowupV1, _integer, _text
from ..planning.htn.grounding import derive_id
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.taskgraph_convergence import TaskGraphConvergenceStore
from .taskgraph_policy import KERNEL_VERSION, read_installed_graph_policy


class TaskGraphOperatorGuard:
    """Same authenticated human and tenant as the real enabling command."""
    def __init__(self, store: Store, *, tenant_id: str, principal: Principal,
                 validate_read: Callable[[Principal, str], None]) -> None:
        if not isinstance(principal, Principal):
            raise ValueError("TaskGraph operator requires an authenticated human")
        self.store, self.tenant_id, self.principal = store, tenant_id, principal
        self.validate_read = validate_read

    def require(self, store: Store, mission_id: str, caller: object) -> None:
        if store is not self.store or not store.connection.in_transaction:
            raise StoreError("TASKGRAPH_OPERATOR_TRANSACTION_REQUIRED")
        mission = store.get_mission(mission_id)
        if (not isinstance(caller, Principal) or caller != self.principal
                or mission is None or mission.tenant_id != self.tenant_id):
            raise StoreError("TASKGRAPH_OPERATOR_NOT_AUTHORIZED")
        self.validate_read(caller, mission_id)
        read_installed_graph_policy(store, mission_id)
        row = store.connection.execute(
            "SELECT enabling_command_id FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)).fetchone()
        receipt = None if row is None else store.get_receipt(row[0])
        expected = sha256_hex({"kind": "EnableTaskGraphContract", "mission_id": mission_id,
                              "kernel_version": KERNEL_VERSION, "principal_id": caller.principal_id})
        if receipt is None or receipt.get("intent_hash") != expected:
            raise StoreError("TASKGRAPH_OPERATOR_NOT_ENABLE_ISSUER")


class TaskGraphOperatorService:
    def __init__(self, guard: TaskGraphOperatorGuard, *, jobs: TaskGraphConvergenceStore,
                 notifications: Any, sources: Any) -> None:
        self.guard, self.store, self.jobs = guard, guard.store, jobs
        self.notifications, self.sources = notifications, sources
        if jobs.store is not self.store or notifications.store is not self.store or sources.store is not self.store:
            raise ValueError("operator commands require the original Store")

    def matches_binding(self, commit: Any, tenant_id: str, principal: Any) -> bool:
        return commit.store is self.store and tenant_id == self.guard.tenant_id and principal == self.guard.principal

    def _replay(self, command_id: str, intent: dict[str, Any]) -> Mapping[str, Any] | None:
        _text(command_id, "command_id")
        self.guard.require(self.store, intent["mission_id"], self.guard.principal)
        old = self.store.get_receipt(command_id)
        if old is not None and old.get("intent_hash") != sha256_hex(intent):
            raise StoreConflict("TASKGRAPH_OPERATOR_COMMAND_CONFLICT")
        return old

    def _record(self, command_id: str, intent: dict[str, Any], result: dict[str, Any]) -> Mapping[str, Any]:
        mission = self.store.get_mission(intent["mission_id"])
        if mission is None:
            raise StoreError("TASKGRAPH_OPERATOR_MISSION_MISSING")
        receipt = {"version": 1, "kind": intent["kind"], "command_id": command_id,
                   "mission_id": mission.id, "intent_hash": sha256_hex(intent),
                   "principal_id": self.guard.principal.principal_id, "result": result}
        self.store.insert_receipt(commit_id=command_id, kind=intent["kind"], subject_id=mission.id,
            base_version=mission.version, proposal_hash=receipt["intent_hash"], receipt=receipt)
        identity = derive_id("tg-operator-command", command_id)
        event = self.store.append_event(Event(id=identity, type=intent["kind"], trace_id=command_id,
            mission_id=mission.id, task_id=None, attempt_id=None, actor_type="human",
            actor_id=self.guard.principal.principal_id, payload=receipt,
            idempotency_key=identity, created_at=self.store.now))
        # Restore evaluation, not old physical execution or old permission. The
        # original dispatch path must still admit any subsequent new Attempt.
        if intent["kind"] == "TaskGraphConvergenceAbandoned":
            if event.seq is None:
                raise StoreError("TASKGRAPH_OPERATOR_EVENT_NOT_PERSISTED")
            self.notifications.notifications.append_followup(FollowupV1(mission_id=mission.id,
                source_event_id=event.id, kind=FollowupKind.REEVALUATE, subject_key=mission.id,
                source_revision=result["source_revision"], cause_ref=FollowupCauseRef(kind="event",
                    id=event.id, revision=event.seq, content_hash=sha256_hex(event.to_json()))),
                now_ms=int(self.store.now * 1000))
        return receipt

    def abandon_convergence(self, mission_id: str, job_id: str, *, expected_version: int,
                            command_id: str, reason: str) -> Mapping[str, Any]:
        _text(job_id, "job_id")
        _text(reason, "reason", maximum=2048)
        _integer(expected_version, "expected_version", minimum=1)
        intent = {"kind": "TaskGraphConvergenceAbandoned", "mission_id": mission_id,
                  "job_id": job_id, "expected_version": expected_version, "reason": reason,
                  "principal_id": self.guard.principal.principal_id}
        with self.store.transaction():
            replay = self._replay(command_id, intent)
            if replay is not None:
                return replay
            job = self.jobs.abandon(mission_id, job_id, expected_version=expected_version,
                caller=self.guard.principal, command_id=command_id, now_ms=int(self.store.now * 1000))
            record = self.sources.local.sources.history.read_revision(mission_id, job.source_revision).record
            return self._record(command_id, intent, {"job_id": job.job_id, "state": job.state,
                "row_version": job.row_version, "candidate_hash": job.candidate_hash,
                "impact_hash": job.impact_hash, "source_revision": job.source_revision,
                "restored_network_hash": record.manifest_hash})

    def retry_notification(self, mission_id: str, message_id: str, *, expected_version: int,
                           command_id: str, reason: str) -> Mapping[str, Any]:
        _text(message_id, "message_id")
        _text(reason, "reason", maximum=2048)
        _integer(expected_version, "expected_version", minimum=1)
        intent = {"kind": "TaskGraphNotificationRetryAuthorized", "mission_id": mission_id,
                  "message_id": message_id, "expected_version": expected_version, "reason": reason,
                  "principal_id": self.guard.principal.principal_id}
        with self.store.transaction():
            replay = self._replay(command_id, intent)
            if replay is not None:
                return replay
            row = self.store.connection.execute(
                "SELECT * FROM taskgraph_followups WHERE mission_id=? AND message_id=?", (mission_id, message_id)).fetchone()
            if row is None or row["delivery_state"] != "BLOCKED" or row["row_version"] != expected_version:
                raise StoreConflict("TASKGRAPH_FOLLOWUP_REPAIR_CONFLICT")
            message = FollowupV1.from_json(json.loads(row["payload_json"]))
            if sha256_hex(message.to_json()) != row["payload_hash"] or message.mission_id != mission_id:
                raise StoreError("TASKGRAPH_FOLLOWUP_CORRUPT")
            self.notifications._verify_source(message)
            self.notifications.validate_current(mission_id)
            execution = self.sources.read_execution(mission_id)
            if message.kind is FollowupKind.CONVERGE:
                job = self.jobs.get_job(mission_id, message.subject_key)
                if job.state not in {"APPLIED", "ABANDONED"}:
                    self.notifications.convergence._observe(job)
            def verify(store: Store, mission: str, identity: str, command: str) -> None:
                if (store is not self.store or (mission, identity, command) != (mission_id, message_id, command_id)):
                    raise StoreError("TASKGRAPH_FOLLOWUP_REPAIR_IDENTITY_CHANGED")
                self.guard.require(store, mission, self.guard.principal)
            # All source probes above and Q13 CAS share this Store snapshot.
            # A source that still fails never gains another retry budget.
            self.notifications.notifications.reset_blocked(mission_id=mission_id, message_id=message_id,
                expected_version=expected_version, now_ms=int(self.store.now * 1000),
                repair_command_id=command_id, verify_repair=verify)
            return self._record(command_id, intent, {"message_id": message_id,
                "row_version": expected_version + 1, "state": "PENDING",
                "original_error_code": row["last_error_code"], "payload_hash": row["payload_hash"],
                "runtime_source_hash": execution.running_work.source_digest,
                "execution_policy_hash": execution.execution_policy.source_digest,
                "operation_source_hash": execution.operation_snapshot.source_digest})
