# SPDX-License-Identifier: Apache-2.0
"""Complete structural checks with explicit, non-executable pending compounds."""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.htn import GraphStructureBudget, TaskForm
from .network_codec import NetworkDocumentV1, decode
from .task_network import TaskNetworkSnapshot
from .projection_validation import ProblemKind, ProjectionProblem, ProjectionReport, validate_execution_projection, validate_refinement_acyclic


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphStructureReport:
    pending_compounds: tuple[str, ...]
    structural_check_complete: bool
    decomposition_complete: bool
    problems: tuple[ProjectionProblem, ...]

    @property
    def ok(self) -> bool:
        return self.structural_check_complete and not self.problems


def pending_compounds(network: TaskNetworkSnapshot) -> tuple[str, ...]:
    return tuple(sorted(str(item.occurrence_id) for item in network.occurrences
        if item.form is TaskForm.COMPOUND and network.adopted_instance_for(item.occurrence_id) is None))


def taskgraph_projection_report(network: TaskNetworkSnapshot, report: ProjectionReport) -> ProjectionReport:
    """Defer only explicit unexpanded roots; all materialized structure is checked."""
    pending_roots = set(pending_compounds(network)) & {str(item) for item in network.root_occurrence_ids}
    problems = [problem for problem in report.problems
        if not (problem.kind is ProblemKind.ROOT_COVERAGE_GAP and len(problem.nodes) == 1
                and problem.nodes[0] in pending_roots)]
    for item in network.occurrences:
        if (item.form is TaskForm.COMPOUND and network.adopted_instance_for(item.occurrence_id) is not None
                and not network.adopted_children(item.occurrence_id)):
            problems.append(ProjectionProblem(kind=ProblemKind.NO_GATING_CHILDREN,
                detail="an adopted zero-work method requires an explicit reuse/verification contract",
                nodes=(str(item.occurrence_id),)))
    return ProjectionReport(budget_version=report.budget_version,
        topological_order=report.topological_order,
        problems=tuple(sorted(problems, key=lambda item: (str(item.kind), item.nodes, item.detail))))


def validate_taskgraph_structure(document: NetworkDocumentV1, budget: GraphStructureBudget) -> TaskGraphStructureReport:
    network = decode(document.to_json()).snapshot
    structural = taskgraph_projection_report(network, validate_execution_projection(network.execution_projection(), budget))
    refinement = validate_refinement_acyclic(network)
    pending = pending_compounds(network)
    problems = (*structural.problems, *refinement.problems)
    complete = not any(problem.kind in {ProblemKind.PARTIAL_CHECK, ProblemKind.BOUND_REACHED} for problem in problems)
    return TaskGraphStructureReport(pending_compounds=pending, structural_check_complete=complete,
        decomposition_complete=not pending,
        problems=tuple(sorted(problems, key=lambda item: (str(item.kind), item.nodes, item.detail))))
