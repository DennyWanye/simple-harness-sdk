# SPDX-License-Identifier: Apache-2.0
"""A leaf review consumes its actual Attempt's immutable contract and inputs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts.htn import OccurrenceId, TaskSemanticBindingV1
from ..contracts.models import ContractError
from ..contracts.semantic_base import VersionedRef
from ..storage.htn_store import HtnStore
from .accepted_outputs import CarriedCriterion, declared_output_ports
from .taskgraph_dispatch import TaskGraphAttemptContext, require_taskgraph_unfenced


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphReviewOrigin:
    context: TaskGraphAttemptContext
    semantic: TaskSemanticBindingV1
    carried: tuple[CarriedCriterion, ...]
    ports: tuple[tuple[str, VersionedRef], ...]


def read_review_origin(commit: Any, mission_id: str, task_id: str,
                       result_id: str) -> TaskGraphReviewOrigin:
    from .scoped_content_review import uses_completion_protocol

    store = commit.store
    with store.read_view():
        # The result remains historical evidence while convergence owns this
        # target. It cannot become an accepted current output during that fence.
        require_taskgraph_unfenced(store, mission_id, task_id)
        result = store.get_result(result_id)
        if result is None or result.envelope.task_id != task_id:
            raise ContractError("TASKGRAPH_REVIEW_RESULT_UNAVAILABLE")
        if result.envelope.mission_id and result.envelope.mission_id != mission_id:
            raise ContractError("TASKGRAPH_REVIEW_RESULT_MISSION_MISMATCH")
        context = commit.taskgraph_attempt_context(mission_id, result.envelope.attempt_id)
        if not isinstance(context, TaskGraphAttemptContext) or context.inputs.binding.task_id != task_id:
            raise ContractError("TASKGRAPH_REVIEW_ATTEMPT_MISMATCH")
        frozen = context.inputs.binding
        semantics = HtnStore(store)
        semantic = semantics.get_task_semantics(task_id, frozen.binding_revision)
        current = semantics.task_semantics_of(mission_id, task_id)
        if (current is None or current.contract_revision != semantic.contract_revision
                or current.contract_hash != frozen.contract_hash
                or int(current.dispatch_generation) != frozen.dispatch_generation
                or int(current.input_binding_revision) != frozen.input_binding_revision):
            raise ContractError("TASKGRAPH_REVIEW_ATTEMPT_SUPERSEDED")
        occurrence = OccurrenceId(frozen.occurrence_id)
        network = context.network
        task_of = {item.occurrence_id: str(item.task_id) for item in network.occurrences}
        if task_of.get(occurrence) != task_id:
            raise ContractError("TASKGRAPH_REVIEW_OCCURRENCE_MISMATCH")
        adopted = set(network.adopted_instance_ids)
        carried = []
        for draft in network.method_instances:
            if draft.instance_id not in adopted:
                continue
            method = semantics.get_method(str(draft.method_ref.method_id), int(draft.method_ref.version)).contract
            if method.method_ref() != draft.method_ref:
                raise ContractError("TASKGRAPH_REVIEW_METHOD_HASH_MISMATCH")
            slots = {child.slot_key: child.occurrence_id for child in draft.child_bindings}
            composition = method.composition
            finalizer = slots.get(composition.finalizer_step or "")
            for link in composition.criterion_links:
                bound = slots.get(link.child_step or "") if link.child_step else finalizer
                if bound is None or bound not in task_of:
                    raise ContractError("TASKGRAPH_REVIEW_CRITERION_SOURCE_INCOMPLETE")
                if bound == occurrence:
                    carried.append(CarriedCriterion(
                        parent_task_id=str(draft.goal_id),
                        parent_criterion_id=str(link.parent_criterion_id),
                        occurrence_id=bound, task_id=task_id,
                        leaf_criterion_id=str(link.child_criterion_id or link.parent_criterion_id),
                        evidence_requirement=str(link.evidence_requirement)))
        return TaskGraphReviewOrigin(context=context, semantic=semantic, carried=tuple(carried),
                                    ports=tuple(sorted(declared_output_ports(
                                        network, occurrence,
                                        own_ports=uses_completion_protocol(store, mission_id)).items())))
