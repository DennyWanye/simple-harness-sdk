# SPDX-License-Identifier: Apache-2.0
"""Convergence observations from original Store, SDK turns and imported usage.

No provider/connector request is made here. Missing runtime profiles, orphaned
turn bindings and ambiguous Operation joins refuse a complete observation.
"""
from __future__ import annotations

from typing import Any


from ..contracts.state_machines import TERMINAL_ATTEMPT
from ..runtime.planning_operations import OperationEffect, SourceUnavailable, StoreOperationReader, build_operation_snapshot
from ..runtime.taskgraph_local_work import read_local_work
from ..storage.htn_store import HtnStore
from ..runtime.taskgraph_operation_sources import read_operation_producers
from ..storage.store import Store
from ..storage.taskgraph_attempt_inputs import TaskGraphAttemptInputStore
from ..storage.taskgraph_convergence import ConvergenceJob
from ..storage.taskgraph_store import TaskGraphStore
from .taskgraph_convergence import ConvergenceAction, ConvergenceActionKind, ConvergenceObservation


class TaskGraphRuntimeObservationReader:
    def __init__(self, orchestrator: Any, *, history: TaskGraphStore) -> None:
        if orchestrator.store is not history.store:
            raise ValueError("runtime observations require the original Store")
        self.orchestrator, self.store, self.history = orchestrator, history.store, history
        self.inputs = TaskGraphAttemptInputStore(self.store, revision_reader=history.read_revision)

    def _physical_settled(self, intent: Any) -> bool:
        # All historical rehandoffs, tools and actual imported charges participate.
        from .taskgraph_runtime_imports import TaskGraphRuntimeImports
        return TaskGraphRuntimeImports(self.orchestrator).read_subject(intent).physical_settled

    def __call__(self, store: Store, job: ConvergenceJob) -> ConvergenceObservation:
        if store is not self.store:
            raise SourceUnavailable("taskgraph_runtime_store_mismatch")
        with store.read_view():
            self.history.read_revision(job.mission_id, job.source_revision)
            targets = {item.occurrence_id: item for item in job.targets}
            task_ids = frozenset(item.task_id for item in job.targets)
            local = read_local_work(store, job.mission_id)
            subjects = local.related_subjects(task_ids)
            unsettled = set(local.blocking_subjects(task_ids))
            physical: dict[str, bool] = {}
            intents = {}
            for row in local.to_json()["intents"]:
                if row["subject_id"] not in subjects:
                    continue
                intent = store.get_intent(row["intent_id"])
                if intent is None or intent.mission_id != job.mission_id:
                    raise SourceUnavailable("taskgraph_runtime_intent_missing")
                intents[intent.subject_id] = intent
                physical[intent.subject_id] = self._physical_settled(intent)
                if not physical[intent.subject_id]:
                    unsettled.add(intent.subject_id)
            actions = []
            for row in local.to_json()["attempts"]:
                if row["attempt_id"] not in subjects:
                    continue
                attempt = store.get_attempt(row["attempt_id"])
                if attempt is None:
                    raise SourceUnavailable("taskgraph_runtime_attempt_missing")
                if attempt.status in TERMINAL_ATTEMPT and physical.get(attempt.id) is True:
                    continue
                frozen = self.inputs.get_attempt_inputs(job.mission_id, attempt.id).binding
                target = targets.get(frozen.occurrence_id)
                if target is None or target.task_id != attempt.task_id:
                    raise SourceUnavailable("taskgraph_runtime_attempt_target_ambiguous")
                if frozen.dispatch_generation != target.expected_generation:
                    # Old unresolved generations remain blockers; they cannot be
                    # reinterpreted as today's cancellation target.
                    continue
                intent = intents.get(attempt.id)
                if intent is None:
                    raise SourceUnavailable("taskgraph_runtime_attempt_intent_missing")
                foreign = any(item.lease_owner not in (None, self.orchestrator._owner)
                    and item.lease_expires_at is not None and item.lease_expires_at > store.now
                    for item in (attempt, intent))
                if not foreign:
                    actions.append(ConvergenceAction(occurrence_id=target.occurrence_id,
                        original_identity=attempt.id, kind=ConvergenceActionKind.CANCEL_ATTEMPT))
            for subject, intent in sorted(intents.items()):
                if intent.kind == "attempt" or (subject not in unsettled and physical[subject]):
                    continue
                if (intent.lease_owner not in (None, self.orchestrator._owner)
                        and intent.lease_expires_at is not None and intent.lease_expires_at > store.now):
                    continue
                related_targets = [target for target in job.targets if subject in
                    local.related_subjects(frozenset({target.task_id}))]
                if not related_targets:
                    raise SourceUnavailable("taskgraph_runtime_service_target_missing")
                # One stable cancellation command even if a Critic/Manager names
                # several affected subjects. No unrelated service is inferred.
                target = min(related_targets, key=lambda item: item.occurrence_id)
                actions.append(ConvergenceAction(occurrence_id=target.occurrence_id,
                    original_identity=intent.intent_id, kind=ConvergenceActionKind.CANCEL_SERVICE_INTENT))
            operations = build_operation_snapshot(job.mission_id, reader=StoreOperationReader(store))
            producers = read_operation_producers(store, operations)
            for producer in producers:
                if producer.task_id not in task_ids:
                    continue
                target = targets.get(producer.occurrence_id)
                if target is None or target.task_id != producer.task_id:
                    raise SourceUnavailable("taskgraph_runtime_operation_target_ambiguous")
                effect = operations.operation_effects.get(producer.operation_id)
                if effect is None:
                    raise SourceUnavailable("taskgraph_runtime_operation_effect_missing")
                if effect not in {OperationEffect.IN_FLIGHT, OperationEffect.UNRESOLVED}:
                    continue
                unsettled.add(producer.operation_id)
                binding = HtnStore(store).get_task_semantics(target.task_id, producer.contract_revision)
                if int(binding.dispatch_generation) == target.expected_generation:
                    actions.append(ConvergenceAction(occurrence_id=target.occurrence_id,
                        original_identity=producer.operation_id, kind=ConvergenceActionKind.RECONCILE_OPERATION))
            return ConvergenceObservation(job_id=job.job_id, job_version=job.row_version,
                target_occurrences=tuple(sorted(targets)), actions=tuple(actions),
                quiescent=not unsettled and not actions)
