# SPDX-License-Identifier: Apache-2.0
"""Exact-subject convergence commands over the existing runtime and ledgers."""
from __future__ import annotations

from typing import Any

from simple_harness.agents import AgentConfig
from simple_harness.agents.base import input_hash_for
from simple_harness.agents.contracts import _message_from_json
from simple_harness.contracts import canonical_json

from ..contracts.models import Event, jsonable, sha256_hex
from ..contracts.state_machines import AttemptStatus, TERMINAL_ATTEMPT
from ..planning.htn.grounding import derive_id
from ..runtime.planning_operations import SourceUnavailable, StoreOperationReader, build_operation_snapshot
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.htn_store import HtnStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.store import Store, StoreConflict, StoreError
from ..storage.taskgraph_attempt_inputs import TaskGraphAttemptInputStore
from ..storage.taskgraph_convergence import ConvergenceJob, TaskGraphConvergenceStore
from ..storage.taskgraph_store import TaskGraphStore
from .taskgraph_convergence import ConvergenceAction, ConvergenceActionKind, ConvergenceObservation


class TaskGraphRuntimeCommands:
    """Use with an actual complete H1 observation reader, never an empty fallback.

    The recorded events prove that a command was requested, not that physical
    execution stopped. Only the installed reader can prove quiescence afterwards.
    """
    def __init__(self, orchestrator: Any, *, jobs: TaskGraphConvergenceStore,
                 history: TaskGraphStore) -> None:
        self.orchestrator = orchestrator
        self.store = orchestrator.store
        if jobs.store is not self.store or history.store is not self.store:
            raise ValueError("convergence requires the original runtime and complete observation reader")
        from .taskgraph_runtime_observation import TaskGraphRuntimeObservationReader
        self.jobs, self.history = jobs, history
        self._observe = TaskGraphRuntimeObservationReader(orchestrator, history=history)
        self.inputs = TaskGraphAttemptInputStore(self.store, revision_reader=history.read_revision)

    def observe(self, store: Store, job: ConvergenceJob) -> ConvergenceObservation:
        if store is not self.store:
            raise StoreError("TASKGRAPH_CONVERGENCE_STORE_MISMATCH")
        return self._observe(store, job)

    def _target(self, job: ConvergenceJob, action: ConvergenceAction, command_key: str) -> Any:
        current = self.jobs.get_job(job.mission_id, job.job_id)
        if (current.row_version != job.row_version or current.state not in {"FENCED", "WAITING", "READY"}
                or command_key != action.command_key(job.job_id)):
            raise StoreConflict("TASKGRAPH_CONVERGENCE_COMMAND_STALE")
        targets = [item for item in current.targets if item.occurrence_id == action.occurrence_id]
        if len(targets) != 1:
            raise StoreError("TASKGRAPH_CONVERGENCE_TARGET_MISSING")
        target = targets[0]
        binding = HtnStore(self.store).task_semantics_of(job.mission_id, target.task_id)
        if binding is None or int(binding.dispatch_generation) != target.expected_generation:
            raise StoreConflict("TASKGRAPH_CONVERGENCE_GENERATION_STALE")
        self.history.read_revision(job.mission_id, job.source_revision)
        return target

    def _record_request(self, job: ConvergenceJob, action: ConvergenceAction,
                        command_key: str, source: dict[str, Any]) -> None:
        identity = derive_id("tg-convergence-command", command_key)
        payload = {"job_id": job.job_id, "occurrence_id": action.occurrence_id,
                   "original_identity": action.original_identity, "kind": str(action.kind),
                   "command_key": command_key, "source": source}
        # The SDK/action ledger remains authoritative. This is a provenance link
        # in the original event store, not a second Operation/completion ledger.
        existing = self.store.connection.execute("SELECT payload_json FROM events WHERE event_id=?", (identity,)).fetchone()
        if existing is not None:
            import json
            if json.loads(existing[0]) != payload:
                raise StoreConflict("TASKGRAPH_CONVERGENCE_COMMAND_CONFLICT")
            return
        self.store.append_event(Event(id=identity, type="TaskGraphConvergenceCommandRequested", trace_id=command_key,
            mission_id=job.mission_id, task_id=None, attempt_id=None, actor_type="system",
            actor_id=self.orchestrator._owner, payload=payload, idempotency_key=identity, created_at=self.store.now))

    async def cancel_attempt(self, job: ConvergenceJob, action: ConvergenceAction, *, command_key: str) -> None:
        if self.store.connection.in_transaction or action.kind is not ConvergenceActionKind.CANCEL_ATTEMPT:
            raise StoreError("TASKGRAPH_CANCEL_COMMAND_INVALID")
        with self.store.transaction():
            target = self._target(job, action, command_key)
            frozen = self.inputs.get_attempt_inputs(job.mission_id, action.original_identity).binding
            attempt = self.store.get_attempt(action.original_identity)
            intent = self.store.get_intent(frozen.intent_id)
            if (attempt is None or intent is None or attempt.task_id != target.task_id
                    or frozen.occurrence_id != target.occurrence_id
                    or frozen.dispatch_generation != target.expected_generation):
                raise StoreError("TASKGRAPH_CANCEL_ATTEMPT_IDENTITY_MISMATCH")
            for subject in (attempt, intent):
                if (subject.lease_owner not in (None, self.orchestrator._owner)
                        and subject.lease_expires_at is not None and subject.lease_expires_at > self.store.now):
                    raise StoreConflict("TASKGRAPH_CANCEL_FOREIGN_OWNER")
            if attempt.status not in TERMINAL_ATTEMPT:
                self.orchestrator.commit._close_attempt(attempt, AttemptStatus.CANCELLED,
                    reason="taskgraph_convergence:" + job.job_id)
                intent = self.store.get_intent(frozen.intent_id)
                if intent is None:
                    raise StoreError("TASKGRAPH_CANCEL_INTENT_MISSING")
            # A SUBMITTED intent and its budget stay open for original collection.
            # Do not call task-wide stop, infer zero usage or settle unknown charges.
            agent_id, turn_id = intent.agent_id, intent.expected_turn_id
            if turn_id is None:
                if intent.state == "SUBMITTED":
                    raise StoreError("TASKGRAPH_CANCEL_TURN_IDENTITY_MISSING")
                self._record_request(job, action, command_key,
                    {"attempt_id": attempt.id, "intent_id": intent.intent_id, "turn_id": None})
                return
            if agent_id is None:
                raise StoreError("TASKGRAPH_CANCEL_AGENT_IDENTITY_MISSING")
        runtime = self.orchestrator.bridge_for(intent).runtime
        turn = runtime.uow.read_agent_turn(turn_id)
        agent = runtime.uow.read_agent_binding(agent_id)
        if turn is None:
            # Agent creation and Turn submission are distinct original stages.
            # Cancellation can land between them; do not fabricate cancel_turn
            # for a Turn the SDK never accepted. Verify its complete inventory.
            if not self._observe._physical_settled(intent):
                raise SourceUnavailable("taskgraph_cancel_unsubmitted_runtime_unresolved")
            with self.store.transaction():
                self._target(job, action, command_key)
                self._record_request(job, action, command_key, {"attempt_id": attempt.id,
                    "intent_id": intent.intent_id, "agent_id": agent_id,
                    "expected_turn_id": turn_id, "turn_id": None})
            return
        message = _message_from_json(dict(intent.config["message"]))
        if (turn.agent_id != agent_id or turn.input_id != frozen.input_id
                or sha256_hex(intent.config["message"]) != frozen.frozen_input_hash
                or turn.input_hash != input_hash_for(message)
                or canonical_json(jsonable(turn.input_json)) != canonical_json({"message": message.to_dict()})
                or agent is None or agent.creation_key != frozen.creation_key
                or canonical_json(jsonable(agent.config_json)) != canonical_json(
                    AgentConfig.from_json(dict(intent.config["agent_config"])).to_json())):
            raise SourceUnavailable("taskgraph_cancel_runtime_identity_mismatch")
        # Existing runtime authorizes the agent owner, durably writes a per-turn
        # control command and signals its cooperative token. Pending is not stopped.
        await runtime.cancel_turn(agent_id, turn_id, command_id=command_key, wait_timeout=0.0)
        original = runtime.uow.read_agent_control_command(command_key)
        expected_hash = sha256_hex({"kind": "cancel_turn", "agent_id": agent_id, "turn_id": turn_id})
        if (original is None or original.kind != "cancel_turn" or original.agent_id != agent_id
                or original.target_turn_id != turn_id or original.request_hash != expected_hash):
            raise SourceUnavailable("taskgraph_cancel_command_receipt_missing")
        with self.store.transaction():
            self._record_request(job, action, command_key, {"attempt_id": attempt.id,
                "intent_id": intent.intent_id, "agent_id": agent_id, "turn_id": turn_id,
                "runtime_command_id": original.command_id, "request_hash": original.request_hash,
                "control_generation": original.control_generation})

    async def cancel_service_intent(self, job: ConvergenceJob, action: ConvergenceAction, *, command_key: str) -> None:
        if self.store.connection.in_transaction or action.kind is not ConvergenceActionKind.CANCEL_SERVICE_INTENT:
            raise StoreError("TASKGRAPH_CANCEL_SERVICE_COMMAND_INVALID")
        with self.store.transaction():
            target = self._target(job, action, command_key)
            intent = self.store.get_intent(action.original_identity)
            if (intent is None or intent.kind == "attempt" or intent.mission_id != job.mission_id
                    or intent.subject_id not in read_local_work(self.store, job.mission_id).related_subjects(
                        frozenset({target.task_id}))):
                raise StoreError("TASKGRAPH_CANCEL_SERVICE_IDENTITY_MISMATCH")
            if (intent.lease_owner not in (None, self.orchestrator._owner)
                    and intent.lease_expires_at is not None and intent.lease_expires_at > self.store.now):
                raise StoreConflict("TASKGRAPH_CANCEL_FOREIGN_OWNER")
            # This is execution-right withdrawal, not a settlement. FAILED keeps
            # the original reservation and makes the existing late-accounting
            # importer responsible for any still-running SDK request.
            if intent.state not in {"SETTLED", "FAILED"}:
                intent = self.orchestrator.commit.settle_intent(intent.intent_id, "FAILED")
            source = {"intent_id": intent.intent_id, "subject_id": intent.subject_id,
                      "agent_id": intent.agent_id, "turn_id": intent.expected_turn_id}
        runtime = self.orchestrator.bridge_for(intent).runtime
        from ..runtime.dispatch_history import read_dispatch_history
        from .taskgraph_runtime_imports import TaskGraphRuntimeImports
        with self.store.read_view(), runtime.uow.database.transaction(read_only=True):
            executors = read_dispatch_history(self.store, runtime, intent)
        receipts = []
        for executor in executors:
            turn = None if executor.expected_turn_id is None else runtime.uow.read_agent_turn(executor.expected_turn_id)
            if turn is None:
                # Whole-history negative inventory is checked after cancellation;
                # an unsubmitted current executor cannot hide an old live one.
                continue
            agent = runtime.uow.read_agent_binding(str(executor.agent_id))
            message = _message_from_json(dict(executor.config["message"]))
            if (agent is None or turn.agent_id != executor.agent_id or turn.input_id != executor.input_id
                    or agent.creation_key != executor.creation_key or agent.owner_scope != runtime.owner_scope
                    or sha256_hex(executor.config["message"]) != executor.input_hash
                    or turn.input_hash != input_hash_for(message)
                    or canonical_json(jsonable(turn.input_json)) != canonical_json({"message": message.to_dict()})
                    or canonical_json(jsonable(agent.config_json)) != canonical_json(
                        AgentConfig.from_json(dict(executor.config["agent_config"])).to_json())):
                raise SourceUnavailable("taskgraph_cancel_service_runtime_identity_mismatch")
            executor_command = derive_id("tg-cancel-executor", command_key, turn.turn_id)
            await runtime.cancel_turn(agent.agent_id, turn.turn_id, command_id=executor_command, wait_timeout=0.0)
            original = runtime.uow.read_agent_control_command(executor_command)
            expected_hash = sha256_hex({"kind": "cancel_turn", "agent_id": agent.agent_id, "turn_id": turn.turn_id})
            if (original is None or original.kind != "cancel_turn" or original.agent_id != agent.agent_id
                    or original.target_turn_id != turn.turn_id or original.request_hash != expected_hash):
                raise SourceUnavailable("taskgraph_cancel_service_command_receipt_missing")
            receipts.append({"agent_id": agent.agent_id, "turn_id": turn.turn_id,
                "runtime_command_id": original.command_id, "request_hash": original.request_hash})
        # Read-only verification can retain UNKNOWN even after every actual
        # cancel request has a receipt. Neither FAILED nor those receipts settle it.
        TaskGraphRuntimeImports(self.orchestrator).read_subject(intent)
        source["executors"] = receipts
        with self.store.transaction():
            self._target(job, action, command_key)
            self._record_request(job, action, command_key, source)
            current = self.store.get_intent(intent.intent_id)
            if (current is None or current.creation_key != intent.creation_key
                    or current.expected_turn_id != intent.expected_turn_id):
                raise StoreConflict("TASKGRAPH_CANCEL_SERVICE_CHANGED")
            if self._observe._physical_settled(current):
                # Original provider facts, complete effects and imported usage
                # have now been checked. Never release an UNKNOWN hold on intent
                # status alone and never substitute zero usage for a missing Turn.
                self.orchestrator.commit.settle_subject(current.subject_id, current.mission_id)

    async def reconcile_operation(self, job: ConvergenceJob, action: ConvergenceAction, *, command_key: str) -> None:
        if self.store.connection.in_transaction or action.kind is not ConvergenceActionKind.RECONCILE_OPERATION:
            raise StoreError("TASKGRAPH_RECONCILE_COMMAND_INVALID")
        executor = self.orchestrator._actions
        if executor is None:
            raise SourceUnavailable("taskgraph_action_executor_unavailable")
        with self.store.transaction():
            target = self._target(job, action, command_key)
            snapshot = build_operation_snapshot(job.mission_id, reader=StoreOperationReader(self.store))
            link = PlanningAdmissionStore(self.store).get_operation_action_link(action.original_identity)
            if (link is None or action.original_identity not in snapshot.operation_effects
                    or link.get("mission_id") != job.mission_id or link.get("producer_task_id") != target.task_id
                    or link.get("producer_htn_occurrence_id") != target.occurrence_id):
                raise SourceUnavailable("taskgraph_operation_target_identity_mismatch")
            # Join the producer's exact semantic revision; do not parse an id or
            # pick the latest action by task/connector/target text.
            binding = HtnStore(self.store).get_task_semantics(target.task_id, int(link["producer_contract_revision"]))
            if int(binding.dispatch_generation) != target.expected_generation:
                raise StoreConflict("TASKGRAPH_OPERATION_GENERATION_STALE")
            self._record_request(job, action, command_key, {"operation_id": action.original_identity,
                "action_key": link["action_key"], "action_id": link["action_id"],
                "action_version": link["action_version"], "params_hash": link["params_hash"],
                "idempotency_key": link["idempotency_key"], "provenance_receipt_id": link["provenance_receipt_id"]})
        await executor.reconcile_one(str(link["action_key"]), allow_rehandoff=False)
