# SPDX-License-Identifier: Apache-2.0
"""Verify real original cancellation and Action reconciliation, never our notice."""
from __future__ import annotations

import json
from typing import Any

from ..contracts.models import sha256_hex
from ..contracts.state_machines import TERMINAL_ATTEMPT
from ..planning.htn.grounding import derive_id
from ..runtime.dispatch_history import read_dispatch_history
from ..runtime.planning_operations import (
    OperationEffect, SourceUnavailable, StoreOperationReader, build_operation_snapshot,
)
from ..runtime.taskgraph_local_work import read_local_work
from ..runtime.taskgraph_operation_sources import read_operation_producers
from ..storage.taskgraph_convergence import ConvergenceJob
from .taskgraph_convergence import ConvergenceAction, ConvergenceActionKind
from .taskgraph_runtime_imports import TaskGraphRuntimeImports


class TaskGraphReconciliationProof:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator
        self.store = orchestrator.store
        self.imports = TaskGraphRuntimeImports(orchestrator)

    def _foreign(self, subject: Any) -> bool:
        return (subject.lease_owner not in (None, self.orchestrator._owner)
                and subject.lease_expires_at is not None
                and subject.lease_expires_at > self.store.now)

    def require_started(self, job: ConvergenceJob) -> None:
        store = self.store
        if not store.connection.in_transaction:
            raise SourceUnavailable("taskgraph_reconciliation_transaction_required")
        local = read_local_work(store, job.mission_id)
        targets = {target.task_id: target for target in job.targets}
        subjects = local.related_subjects(frozenset(targets))
        for row in local.to_json()["intents"]:
            if row["subject_id"] not in subjects:
                continue
            intent = store.get_intent(row["intent_id"])
            if intent is None:
                raise SourceUnavailable("taskgraph_reconciliation_intent_missing")
            attempt = store.get_attempt(intent.subject_id) if intent.kind == "attempt" else None
            if intent.kind == "attempt" and (attempt is None or attempt.task_id not in targets):
                raise SourceUnavailable("taskgraph_reconciliation_attempt_missing")
            facts = self.imports.read_subject(intent)
            closed = (attempt.status in TERMINAL_ATTEMPT if attempt is not None
                      else intent.state in {"FAILED", "SETTLED"})
            if closed and facts.physical_settled:
                continue
            if self._foreign(intent) or (attempt is not None and self._foreign(attempt)):
                continue  # a live original owner remains responsible; not quiescence
            if not closed:
                raise SourceUnavailable("taskgraph_reconciliation_rights_not_withdrawn")
            affected = [target for target in job.targets if intent.subject_id in
                        local.related_subjects(frozenset({target.task_id}))]
            if not affected:
                raise SourceUnavailable("taskgraph_reconciliation_target_missing")
            kind = (ConvergenceActionKind.CANCEL_ATTEMPT if attempt is not None
                    else ConvergenceActionKind.CANCEL_SERVICE_INTENT)
            action = ConvergenceAction(occurrence_id=min(t.occurrence_id for t in affected),
                original_identity=attempt.id if attempt is not None else intent.intent_id, kind=kind)
            command_key = action.command_key(job.job_id)
            runtime = self.orchestrator.bridge_for(intent).runtime
            with runtime.uow.database.transaction(read_only=True):
                executors = read_dispatch_history(store, runtime, intent)
                found = False
                for executor in executors:
                    turn = (None if executor.expected_turn_id is None else
                            runtime.uow.read_agent_turn(executor.expected_turn_id))
                    if turn is None:
                        continue
                    found = True
                    key = (command_key if attempt is not None else
                           derive_id("tg-cancel-executor", command_key, turn.turn_id))
                    receipt = runtime.uow.read_agent_control_command(key)
                    expected = sha256_hex({"kind": "cancel_turn", "agent_id": executor.agent_id,
                                           "turn_id": turn.turn_id})
                    if (receipt is None or receipt.command_id != key or receipt.kind != "cancel_turn"
                            or receipt.agent_id != executor.agent_id
                            or receipt.target_turn_id != turn.turn_id or receipt.request_hash != expected):
                        raise SourceUnavailable("taskgraph_reconciliation_cancel_receipt_missing")
                if not found and not facts.physical_settled:
                    raise SourceUnavailable("taskgraph_reconciliation_unbound_runtime")
        snapshot = build_operation_snapshot(job.mission_id, reader=StoreOperationReader(store))
        producers = read_operation_producers(store, snapshot)
        links = {link.operation_id: link for link in snapshot.links}
        for producer in producers:
            if producer.task_id not in targets or snapshot.operation_effects[producer.operation_id] not in {
                    OperationEffect.IN_FLIGHT, OperationEffect.UNRESOLVED}:
                continue
            link = links[producer.operation_id]
            action_row = store.get_action(link.action_key)
            if action_row is None:
                raise SourceUnavailable("taskgraph_reconciliation_action_missing")
            executor = self.orchestrator._actions
            if executor is not None and link.action_key in executor._inflight:
                continue  # original process still owns a physical connector call
            if (action_row["state"] == "HANDED_OFF" and action_row.get("owner")
                    and float(action_row.get("lease_expires_at") or 0) > store.now):
                continue
            verdict = action_row.get("reconcile")
            if verdict not in {"STILL_UNKNOWN", "CONFIRMED_NOT_STARTED"}:
                raise SourceUnavailable("taskgraph_reconciliation_action_lookup_missing")
            # Original producer binds the event key to the exact physical handoff
            # ordinal. A result from an older handoff cannot release a new one.
            key = f"ActionReconciled:{link.action_key}:h{action_row['handoffs']}:{verdict}"
            event = store.connection.execute(
                "SELECT mission_id,type,payload_json FROM events WHERE idempotency_key=?", (key,)).fetchone()
            if (event is None or event[0] != job.mission_id or event[1] != "ActionReconciled"
                    or json.loads(event[2]) != {"action_key": link.action_key, "verdict": verdict,
                                              "note": (action_row.get("reconcile_note")
                                                       if verdict == "STILL_UNKNOWN" else None)}):
                raise SourceUnavailable("taskgraph_reconciliation_action_event_missing")
