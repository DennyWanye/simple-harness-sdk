# SPDX-License-Identifier: Apache-2.0
"""Read original settlement facts for every settled-terminal predecessor."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.htn import OccurrenceId, ReleaseCondition
from ..contracts.models import sha256_hex
from ..graph.eligibility import OccurrenceOutcome
from ..graph.settlement import SettledTerminalOccurrence
from ..graph.task_network import TaskNetworkSnapshot
from ..runtime.planning_operations import OperationEffect, SourceUnavailable, StoreOperationReader, build_operation_snapshot
from ..runtime.taskgraph_local_work import read_local_work
from ..runtime.taskgraph_operation_sources import read_operation_producers
from ..storage.taskgraph_store import TaskGraphStore
from .taskgraph_runtime_observation import TaskGraphRuntimeObservationReader


class TaskGraphSettlementReader:
    def __init__(self, orchestrator: Any, *, history: TaskGraphStore) -> None:
        self.store = history.store
        self.runtime = TaskGraphRuntimeObservationReader(orchestrator, history=history)

    def __call__(self, mission_id: str, network: TaskNetworkSnapshot,
                 outcomes: Mapping[OccurrenceId, OccurrenceOutcome], *,
                 predecessors: frozenset[OccurrenceId] | None = None) -> dict[OccurrenceId, SettledTerminalOccurrence]:
        predecessors = (frozenset(edge.before for edge in network.order_constraints
                        if edge.release_condition is ReleaseCondition.SETTLED_TERMINAL)
                        if predecessors is None else predecessors)
        if not predecessors <= {item.occurrence_id for item in network.occurrences}:
            raise SourceUnavailable("taskgraph_settlement_predecessor_set_invalid")
        if not predecessors:
            return {}
        with self.store.read_view():
            local = read_local_work(self.store, mission_id)
            operations = build_operation_snapshot(mission_id, reader=StoreOperationReader(self.store))
            producers = read_operation_producers(self.store, operations)
            facts = {}
            physical: dict[str, bool] = {}
            for predecessor in sorted(predecessors, key=str):
                outcome = outcomes.get(predecessor)
                if outcome is None:
                    raise SourceUnavailable("taskgraph_settlement_outcome_missing")
                if outcome in {OccurrenceOutcome.UNKNOWN, OccurrenceOutcome.RUNNING}:
                    continue
                # A compound exit includes work below its adopted method, including
                # optional work that really started and shared producer identities.
                related = {predecessor}
                pending = [predecessor]
                while pending:
                    parent = pending.pop()
                    for child in network.adopted_children(parent):
                        for identity in (child.occurrence_id, child.goal_occurrence_id or child.occurrence_id):
                            if identity not in related:
                                network.occurrence(identity)  # missing is integrity failure
                                related.add(identity)
                                pending.append(identity)
                task_ids = frozenset(str(network.occurrence(identity).task_id) for identity in related)
                if local.blocking_subjects(task_ids):
                    continue
                subjects = local.related_subjects(task_ids)
                intent_subjects = {row["subject_id"] for row in local.to_json()["intents"]}
                if any(row["attempt_id"] not in intent_subjects for row in local.to_json()["attempts"]
                       if row["task_id"] in task_ids):
                    raise SourceUnavailable("taskgraph_settlement_attempt_intent_missing")
                settled = True
                for row in local.to_json()["intents"]:
                    if row["subject_id"] not in subjects:
                        continue
                    if row["intent_id"] not in physical:
                        intent = self.store.get_intent(row["intent_id"])
                        if intent is None:
                            raise SourceUnavailable("taskgraph_settlement_intent_missing")
                        physical[row["intent_id"]] = self.runtime._physical_settled(intent)
                    if not physical[row["intent_id"]]:
                        settled = False
                for producer in producers:
                    if producer.task_id not in task_ids:
                        continue
                    if producer.occurrence_id not in {str(item) for item in related}:
                        raise SourceUnavailable("taskgraph_settlement_operation_target_mismatch")
                    effect = operations.operation_effects.get(producer.operation_id)
                    if effect is None:
                        raise SourceUnavailable("taskgraph_settlement_operation_missing")
                    if effect in {OperationEffect.IN_FLIGHT, OperationEffect.UNRESOLVED}:
                        settled = False
                if not settled:
                    continue
                binding = network.binding_for_occurrence(predecessor)
                facts[predecessor] = SettledTerminalOccurrence(mission_id=mission_id,
                    plan_revision=int(network.plan_revision), occurrence_id=str(predecessor),
                    contract_revision=int(binding.contract_revision), dispatch_generation=int(binding.dispatch_generation),
                    outcome=str(outcome), source_digest=sha256_hex({"local_work": local.digest,
                        "operations": operations.read_digest, "task_ids": sorted(task_ids),
                        "physical_imports_verified": sorted(key for key, value in physical.items() if value)}))
            return facts
