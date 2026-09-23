# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure candidate compilation for the H1H plan-admission seam.

This module owns no Store, event, witness, budget reservation, CAS or dispatch
operation.  It receives frozen deployment values and returns a value that a
later plan-admission/Commit step can bind to the exact same candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.evidence_state import EvidenceSnapshot
from ..contracts.htn import (
    GraphStructureBudget,
    MethodInstanceId,
    MethodRef,
    OccurrenceId,
    PlanProposal,
    RefineOperation,
    RebindInputOperation,
    CancelBranchOperation,
    ProposeSuccessorOperation,
    BindSharedGoalOperation,
    RetireMethodOperation,
    TaskForm,
    TaskRef,
)
from ..contracts.models import ContractError, sha256_hex
from ..graph.task_network import TaskNetworkSnapshot
from ..knowledge.predicates import PredicateRegistry
from ..runtime.planning_operations import RuntimeWorkSnapshot
from .htn.applicability import ApplicabilityStatus, CapabilitySnapshot, assess_method
from .htn.compiler import (
    CompilationRefused,
    RefinementCompilation,
)
from .htn.compiler import (
    compile_candidate_from_snapshot as compile_refinement_candidate,
)
from .htn.grounding import ParameterBindingsError, SharedGoalEntry, SharedGoalIndex, ground_method
from .htn.registry import MethodRegistry, SchemaCatalog, TaskTypeCatalog
from .htn.validation import DeltaProblemKind, DeltaReport, validate_delta


@dataclass(frozen=True, slots=True)
class PreviewInputs:
    """All inputs required by a reproducible candidate preview."""

    decision_id: str
    decision_hash: str
    request_id: str
    source_plan_revision: int
    source_network_hash: str
    proposal: PlanProposal
    network: TaskNetworkSnapshot
    registry: MethodRegistry
    catalog: TaskTypeCatalog
    schemas: SchemaCatalog
    evidence: EvidenceSnapshot
    predicates: PredicateRegistry
    requirements_revision: int
    budget: GraphStructureBudget
    system_identity_seed: str
    now_ms: int
    capabilities: CapabilitySnapshot
    # This is a required producer snapshot.  Callers must read running work from the
    # Store; an implicit empty value would hide an in-flight repair or hand-off.
    runtime_work: RuntimeWorkSnapshot
    repair_impact: Mapping[str, Any] | None = None
    goal_reuse_sources: tuple[Mapping[str, Any], ...] = ()
    taskgraph_contract: bool = False
    sharing_entries: tuple[SharedGoalEntry, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.taskgraph_contract) is not bool:
            raise ContractError("preview.taskgraph_contract must be a boolean")
        if self.taskgraph_contract:
            if (not isinstance(self.sharing_entries, tuple)
                    or not all(isinstance(item, SharedGoalEntry) for item in self.sharing_entries)):
                raise ContractError("TaskGraph preview requires the captured sharing index")
        elif self.sharing_entries is not None:
            raise ContractError("legacy preview does not accept TaskGraph sharing sources")
        for name in (
            "decision_id",
            "decision_hash",
            "request_id",
            "source_network_hash",
            "system_identity_seed",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractError(f"preview.{name} must be a non-empty string")
        if not isinstance(self.proposal, PlanProposal):
            raise ContractError("preview.proposal must be a PlanProposal")
        if not isinstance(self.network, TaskNetworkSnapshot):
            raise ContractError("preview.network must be a TaskNetworkSnapshot")
        if self.proposal.mission_id != self.network.mission_id:
            raise ContractError("preview proposal and network mission identities differ")
        if int(self.proposal.expected_plan_revision) != int(self.source_plan_revision):
            raise ContractError("preview proposal revision is not the frozen source revision")
        if int(self.network.plan_revision) != int(self.source_plan_revision):
            raise ContractError("preview network revision is not the frozen source revision")
        for name, expected in (
            ("registry", MethodRegistry),
            ("catalog", TaskTypeCatalog),
            ("schemas", SchemaCatalog),
            ("evidence", EvidenceSnapshot),
            ("predicates", PredicateRegistry),
            ("budget", GraphStructureBudget),
            ("capabilities", CapabilitySnapshot),
            ("runtime_work", RuntimeWorkSnapshot),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ContractError(f"preview.{name} has the wrong frozen input type")


@dataclass(frozen=True, slots=True)
class CandidatePreview:
    request_id: str
    decision_hash: str
    source_snapshot_hash: str
    compilation_hash: str
    compilation: RefinementCompilation
    delta_report: DeltaReport
    plan_shape: str
    mapped_problems: tuple[str, ...]
    required_convergence: RuntimeWorkSnapshot
    validator_id: str = "htn.validate_delta/v1"

    def __post_init__(self) -> None:
        if self.plan_shape not in {"CHECKED_VALID", "CHECKED_INVALID"}:
            raise ContractError("preview.plan_shape must be an explicit checked verdict")
        if not isinstance(self.compilation, RefinementCompilation):
            raise ContractError("preview.compilation must be the compiler result")
        if not isinstance(self.delta_report, DeltaReport):
            raise ContractError("preview.delta_report must be a DeltaReport")


@dataclass(frozen=True, slots=True)
class PreviewUnavailable:
    reason: str
    mapped_problems: tuple[str, ...] = ()
    detail: str = ""
    delta_report: DeltaReport | None = None


_PROJECTION_KIND_TO_CODE: Mapping[str, str] = {
    "CYCLE": "ORDER_CYCLE",
    "UNBOUND_PORT": "DATA_UNBOUND",
    "SINGLE_PORT_OVERBOUND": "DATA_UNBOUND",
    "SET_PORT_UNORDERED": "DATA_UNBOUND",
    "NO_GATING_CHILDREN": "COVERAGE_GAP",
    "ORPHAN_OBLIGATION": "COVERAGE_GAP",
    "UNREACHED_REQUIRED_OCCURRENCE": "COVERAGE_GAP",
    "ROOT_COVERAGE_GAP": "COVERAGE_GAP",
    "DANGLING_ENDPOINT": "STRUCTURE_INVALID",
    "MISSING_EDGE": "STRUCTURE_INVALID",
    "DUPLICATE_SLOT": "STRUCTURE_INVALID",
    "RESOURCE_CONFLICT": "STRUCTURE_INVALID",
    "BOUND_REACHED": "PLANNING_BOUND_REACHED",
    "PARTIAL_CHECK": "INTERNAL_CONTRACT_ERROR",
}
_DELTA_KIND_TO_CODE: Mapping[DeltaProblemKind, str] = {
    DeltaProblemKind.NOT_REDUCIBLE: "STRUCTURE_INVALID",
    DeltaProblemKind.PORT_UNBINDABLE: "DATA_UNBOUND",
    DeltaProblemKind.PRECONDITION_FALSE: "METHOD_INAPPLICABLE",
    DeltaProblemKind.PRECONDITION_UNKNOWN: "EVIDENCE_REQUIRED",
    DeltaProblemKind.PRECONDITION_CONFLICT: "EVIDENCE_CONFLICT",
    DeltaProblemKind.PRECONDITION_WITNESS_STALE: "REQUEST_BINDING_STALE",
    DeltaProblemKind.ROOT_COVERAGE_GAP: "COVERAGE_GAP",
    DeltaProblemKind.SIZE_BOUND: "PLANNING_BOUND_REACHED",
    DeltaProblemKind.REFINEMENT_CYCLE: "REFINEMENT_CYCLE",
    DeltaProblemKind.NOT_CHECKED: "INTERNAL_CONTRACT_ERROR",
}


def map_delta_problems(report: DeltaReport) -> tuple[str, ...]:
    """Map every typed compiler/validator problem; never infer from text."""

    if not isinstance(report, DeltaReport):
        raise ContractError("preview problem mapping requires a DeltaReport")
    mapped: list[str] = []
    for problem in report.problems:
        if problem.kind is DeltaProblemKind.PROJECTION_DEFECT:
            details = {
                str(item.kind).split(".")[-1].upper() for item in report.projection_report.problems
            }
            if not details or any(item not in _PROJECTION_KIND_TO_CODE for item in details):
                raise ContractError("unmapped projection problem in candidate preview")
            mapped.extend(_PROJECTION_KIND_TO_CODE[item] for item in sorted(details))
        else:
            try:
                mapped.append(_DELTA_KIND_TO_CODE[problem.kind])
            except KeyError as error:
                raise ContractError(f"unmapped delta problem {problem.kind!s}") from error
    return tuple(dict.fromkeys(mapped))


def map_compilation_refusal(error: CompilationRefused) -> tuple[str, ...]:
    """Keep compiler reports typed even when compilation produced no candidate.

    A diagnostic string is not a reason code. Refusals without a typed report
    remain internal failures until their producer supplies one.
    """

    mapped: list[str] = []
    if error.refinement_report is not None:
        for problem in error.refinement_report.problems:
            kind = str(problem.kind).split(".")[-1].upper()
            mapped.append("REFINEMENT_CYCLE" if kind == "CYCLE" else "INTERNAL_CONTRACT_ERROR")
    if error.projection_report is not None:
        for problem in error.projection_report.problems:
            kind = str(problem.kind).split(".")[-1].upper()
            mapped.append(_PROJECTION_KIND_TO_CODE.get(kind, "INTERNAL_CONTRACT_ERROR"))
    return tuple(dict.fromkeys(mapped)) or ("INTERNAL_CONTRACT_ERROR",)


def _source_snapshot_payload(network: TaskNetworkSnapshot) -> dict[str, Any]:
    return {
        "mission_id": str(network.mission_id),
        "plan_revision": int(network.plan_revision),
        "occurrences": [item.to_json() for item in network.occurrences],
        "task_bindings": [item.to_json() for item in network.task_bindings],
        "method_instances": [item.to_json() for item in network.method_instances],
        "adopted_instance_ids": [str(item) for item in network.adopted_instance_ids],
        "root_occurrence_ids": [str(item) for item in network.root_occurrence_ids],
        "order_constraints": [item.to_json() for item in network.order_constraints],
        "data_requirements": [item.to_json() for item in network.data_requirements],
        "obligation_coverage": [item.to_json() for item in network.obligation_coverage],
        "required_obligations": [str(item) for item in network.required_obligations],
    }


def _refined_occurrence(
    network: TaskNetworkSnapshot,
    operation: RefineOperation,
    retiring: Sequence[str],
) -> str:
    matches = [
        spec
        for spec in network.occurrences
        if str(spec.task_id) == str(operation.goal_id)
        and str(spec.obligation_id) == str(operation.obligation_id)
        and spec.form is TaskForm.COMPOUND
        and (
            (adopted := network.adopted_instance_for(spec.occurrence_id)) is None
            or str(adopted.instance_id) in set(retiring)
        )
    ]
    if len(matches) != 1:
        raise ContractError("candidate refinement does not identify exactly one open occurrence")
    return str(matches[0].occurrence_id)


def compile_candidate_from_snapshot(inputs: PreviewInputs) -> RefinementCompilation:
    """Compile one candidate using only the explicitly frozen preview inputs."""

    proposal = inputs.proposal
    if len(proposal.operations) == 1 and isinstance(proposal.operations[0], BindSharedGoalOperation):
        from .htn.graph_repair import compile_bind_existing
        return compile_bind_existing(inputs, proposal.operations[0])
    if len(proposal.operations) == 1 and isinstance(proposal.operations[0], ProposeSuccessorOperation):
        from .htn.graph_repair import compile_successor
        return compile_successor(inputs, proposal.operations[0])
    if len(proposal.operations) == 1 and isinstance(proposal.operations[0], CancelBranchOperation):
        from .htn.graph_repair import compile_cancel
        return compile_cancel(inputs, proposal.operations[0])
    if len(proposal.operations) == 1 and isinstance(proposal.operations[0], RebindInputOperation):
        from .htn.graph_repair import compile_rebind
        return compile_rebind(inputs, proposal.operations[0])
    refinements = [item for item in proposal.operations if isinstance(item, RefineOperation)]
    retirements = [item for item in proposal.operations if isinstance(item, RetireMethodOperation)]
    if len(refinements) != 1 or len(refinements) + len(retirements) != len(proposal.operations):
        raise ContractError("candidate proposal must contain one refine and optional retire")
    operation = refinements[0]
    retiring = tuple(str(item.method_instance_id) for item in retirements)
    parent = inputs.network.binding_for_task(TaskRef(str(operation.goal_id)))
    method_ref = MethodRef(
        method_id=operation.method_ref.id,
        version=operation.method_ref.version,
        content_hash=operation.method_ref.content_hash,
    )
    method = inputs.registry.definition(method_ref)
    if method is None:
        raise ContractError(f"method {method_ref!s} is unavailable in the frozen registry")
    assessment = assess_method(
        parent,
        method,
        inputs.evidence,
        inputs.capabilities,
        registry=inputs.predicates,
        now_ms=inputs.now_ms,
    )
    if assessment.status is not ApplicabilityStatus.APPLICABLE:
        raise CompilationRefused(
            f"candidate method is {assessment.status!s}",
            problems=(str(assessment.status),),
        )
    occurrence = _refined_occurrence(inputs.network, operation, retiring)
    # The fixed source reader captured these original signature entries in the
    # same view as the network. Lookup remains the original full-signature gate;
    # preview never queries a live Store or overrides NEW_WORK for repair reuse.
    sharing = SharedGoalIndex(inputs.sharing_entries) if inputs.sharing_entries is not None else None
    draft = ground_method(
        parent,
        method,
        dict(operation.bindings),
        assessment,
        catalog=inputs.catalog,
        schemas=inputs.schemas,
        sharing=sharing,
        plan_revision=inputs.network.plan_revision,
        goal_occurrence_id=OccurrenceId(occurrence),
    )
    return compile_refinement_candidate(
        draft,
        inputs.network,
        method=method,
        catalog=inputs.catalog,
        schemas=inputs.schemas,
        registry=inputs.registry,
        sharing=sharing,
        retire_instance_ids=tuple(MethodInstanceId(item) for item in retiring),
        budget=inputs.budget,
        requirements_revision=inputs.requirements_revision,
        compiled_from_proposal_id=proposal.proposal_id,
    )


def preview_candidate(
    proposal: PlanProposal, *, inputs: PreviewInputs
) -> CandidatePreview | PreviewUnavailable:
    """Purely compile, validate and hash a candidate; never mutate plan state."""

    if not isinstance(inputs, PreviewInputs):
        return PreviewUnavailable("SOURCE_UNAVAILABLE", detail="preview inputs are not typed")
    if not callable(validate_delta):
        return PreviewUnavailable("SOURCE_UNAVAILABLE", detail="candidate validator is unavailable")
    if not isinstance(proposal, PlanProposal) or proposal != inputs.proposal:
        return PreviewUnavailable("proposal_snapshot_mismatch")
    source_hash = sha256_hex(_source_snapshot_payload(inputs.network))
    if source_hash != inputs.source_network_hash:
        return PreviewUnavailable("source_snapshot_mismatch")
    try:
        compilation = compile_candidate_from_snapshot(inputs)
        methods = {
            str(ref.method_id): definition
            for ref in inputs.registry.method_refs()
            if (definition := inputs.registry.definition(ref)) is not None
        }
        report = validate_delta(
            compilation.delta,
            inputs.network,
            inputs.budget,
            task_bindings=compilation.task_bindings,
            network=compilation.network,
            registry=inputs.registry,
            catalog=inputs.catalog,
            methods=methods,
            snapshot=inputs.evidence,
            predicates=inputs.predicates,
            now_ms=inputs.now_ms,
            taskgraph_contract=inputs.taskgraph_contract,
        )
        mapped = map_delta_problems(report)
        compilation_hash = sha256_hex(
            {
                "delta": compilation.delta.to_json(),
                "network": _source_snapshot_payload(compilation.network),
            }
        )
        return CandidatePreview(
            request_id=inputs.request_id,
            decision_hash=inputs.decision_hash,
            source_snapshot_hash=source_hash,
            compilation_hash=compilation_hash,
            compilation=compilation,
            delta_report=report,
            plan_shape="CHECKED_VALID" if not mapped else "CHECKED_INVALID",
            mapped_problems=mapped,
            required_convergence=inputs.runtime_work,
        )
    except CompilationRefused as error:
        return PreviewUnavailable(
            "candidate_rejected", mapped_problems=map_compilation_refusal(error), detail=str(error)
        )
    except ParameterBindingsError as error:
        return PreviewUnavailable(
            "candidate_rejected", mapped_problems=("STRUCTURE_INVALID",), detail=str(error)
        )
    except (ContractError, KeyError, ValueError) as error:
        return PreviewUnavailable("SOURCE_UNAVAILABLE", detail=str(error))


__all__ = (
    "CandidatePreview",
    "PreviewInputs",
    "PreviewUnavailable",
    "RuntimeWorkSnapshot",
    "compile_candidate_from_snapshot",
    "map_delta_problems",
    "preview_candidate",
)
