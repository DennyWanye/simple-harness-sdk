# SPDX-License-Identifier: Apache-2.0
"""Pure TaskGraph convergence scope over complete original Operation facts.

Admission may compile a change needing convergence. Only original Commit, after
its durable job proves quiescence, may apply it. Unrelated work stays visible in
the original full snapshot and is never rewritten into an empty successful read.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.models import ContractError
from ..runtime.planning_operations import OperationSnapshot
from ..runtime.taskgraph_operation_sources import OperationProducer
from .convergence import compute_convergence_impact
from .network_codec import NetworkDocumentV1


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningConvergenceScope:
    mission_id: str
    base_revision: int
    candidate_hash: str
    target_occurrences: frozenset[str]
    operation_digest: str
    unresolved_operations: tuple[str, ...]


def planning_convergence_scope(before: NetworkDocumentV1, candidate: NetworkDocumentV1,
                               operations: OperationSnapshot,
                               producers: tuple[OperationProducer, ...]) -> PlanningConvergenceScope:
    if not operations.complete_read or operations.mission_id != before.mission_id:
        raise ContractError("TASKGRAPH_OPERATION_SOURCE_INCOMPLETE")
    impact = compute_convergence_impact(before, candidate)
    from .network_codec import decode
    network = decode(before.to_json()).snapshot
    members = {str(item.occurrence_id): item for item in network.occurrences}
    origins = {item.operation_id: item for item in producers}
    if len(origins) != len(producers) or set(origins) != {item.operation_id for item in operations.links}:
        raise ContractError("TASKGRAPH_OPERATION_SOURCE_AMBIGUOUS")
    targets = frozenset(item.occurrence_id for item in impact.targets)
    unresolved = []
    for operation_id in operations.unresolved:
        origin = origins.get(operation_id)
        member = None if origin is None else members.get(origin.occurrence_id)
        if origin is None or member is None or str(member.task_id) != origin.task_id:
            raise ContractError("TASKGRAPH_OPERATION_OCCURRENCE_UNMAPPED")
        binding = network.binding_for_occurrence(member.occurrence_id)
        if (origin.contract_revision != int(binding.contract_revision)
                or not 0 <= origin.plan_revision <= before.revision):
            raise ContractError("TASKGRAPH_OPERATION_PRODUCER_STALE")
        if origin.occurrence_id in targets:
            unresolved.append(operation_id)
    return PlanningConvergenceScope(mission_id=before.mission_id, base_revision=before.revision,
        candidate_hash=impact.candidate_hash, target_occurrences=targets,
        operation_digest=operations.read_digest, unresolved_operations=tuple(sorted(unresolved)))
