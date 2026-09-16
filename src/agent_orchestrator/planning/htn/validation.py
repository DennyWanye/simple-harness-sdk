# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Judgements about a compiled plan increment, and the minimal HDDL export.

Two things live here, and they answer different questions.

:func:`validate_delta`
    implementation design §3.3: "cycle detection only proves acyclicity; method
    reducibility, port bindability, precondition satisfiability and root coverage
    are checked separately."  :mod:`...graph.projection_validation` owns the
    structural half; this function adds the four semantic ones and reports every
    class on its own, so "the plan is not reducible" never arrives disguised as
    "the plan has a cycle".
:func:`to_hddl`
    §7.4 / §8.3: the *minimal* interface P2.2's PANDA backend consumes.  It
    exports only the fragment this adapter can honestly claim — typed objects,
    STRIPS preconditions and effects, total-order and partial-order methods — and
    returns :class:`UnsupportedFeature` for everything else rather than
    approximating it.  Reuse is exported as an explicit "the result is already
    available" method and every occurrence keeps its mapping into the
    decomposition witness, because a shortened action list is not a valid plan for
    the original task network (§8.3, TG §12).

Nothing here decides; every function returns a report.  Refusing is the caller's
move, and the caller needs to know which of the four checks refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...contracts.evidence_state import EvidenceSnapshot, TruthValue
from ...contracts.htn import (
    AnyCondition,
    ArrayValue,
    ConstantValue,
    DataRequirement,
    GraphStructureBudget,
    MethodContract,
    MethodInstanceDraft,
    ObjectValue,
    OccurrenceId,
    OrderConstraint,
    PortCardinality,
    ProposedPlanDelta,
    ReleaseCondition,
    ReusePolicy,
    SourceRevisionPolicy,
    TaskForm,
    TaskSemanticBindingV1,
    condition_digest,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import MAX_TEXT, text
from ...graph.projection_validation import (
    ProblemKind,
    ProjectionReport,
    validate_execution_projection,
    validate_refinement_acyclic,
)
from ...graph.task_network import DEFAULT_PROJECTION_BUDGET, TaskNetworkSnapshot
from ...knowledge.predicates import PredicateRegistry
from .applicability import evaluate_condition
from .registry import MethodRegistry, TaskTypeCatalog, iter_conditions

#: The HDDL subset this export claims.  Named so a receipt can record which
#: fragment a witness was produced against, the way the PANDA adapter does.
HDDL_FRAGMENT = "strips-typed-partial-order"


class DeltaProblemKind(StrEnum):
    """One value per class of semantic defect.  Structural ones keep their own kinds."""

    NOT_REDUCIBLE = "not_reducible"
    PORT_UNBINDABLE = "port_unbindable"
    PRECONDITION_FALSE = "precondition_false"
    PRECONDITION_UNKNOWN = "precondition_unknown"
    PRECONDITION_CONFLICT = "precondition_conflict"
    PRECONDITION_WITNESS_STALE = "precondition_witness_stale"
    ROOT_COVERAGE_GAP = "root_coverage_gap"
    SIZE_BOUND = "size_bound"
    PROJECTION_DEFECT = "projection_defect"
    REFINEMENT_CYCLE = "refinement_cycle"
    NOT_CHECKED = "not_checked"


class PreconditionClass(StrEnum):
    """§18.3: "precondition satisfiability, classified" — four answers, not a boolean."""

    SATISFIED = "SATISFIED"
    REFUTED = "REFUTED"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    CONFLICTED = "CONFLICTED"
    NOT_CHECKED = "NOT_CHECKED"


_TRUTH_TO_CLASS: Mapping[TruthValue, PreconditionClass] = {
    TruthValue.TRUE: PreconditionClass.SATISFIED,
    TruthValue.FALSE: PreconditionClass.REFUTED,
    TruthValue.UNKNOWN: PreconditionClass.NEEDS_EVIDENCE,
    TruthValue.CONFLICT: PreconditionClass.CONFLICTED,
}

_CLASS_TO_PROBLEM: Mapping[PreconditionClass, DeltaProblemKind] = {
    PreconditionClass.REFUTED: DeltaProblemKind.PRECONDITION_FALSE,
    PreconditionClass.NEEDS_EVIDENCE: DeltaProblemKind.PRECONDITION_UNKNOWN,
    PreconditionClass.CONFLICTED: DeltaProblemKind.PRECONDITION_CONFLICT,
}


@dataclass(frozen=True, slots=True)
class DeltaProblem:
    kind: DeltaProblemKind
    detail: str
    subjects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", text(self.detail, "problem.detail", limit=MAX_TEXT))


@dataclass(frozen=True, slots=True)
class PreconditionVerdict:
    instance_id: str
    condition_digest: str
    recorded: TruthValue
    current: TruthValue
    classification: PreconditionClass


@dataclass(frozen=True, slots=True)
class DeltaReport:
    """What the four semantic checks and the two structural ones found."""

    problems: tuple[DeltaProblem, ...]
    projection_report: ProjectionReport
    refinement_report: ProjectionReport
    preconditions: tuple[PreconditionVerdict, ...] = ()
    unbound_ports: tuple[tuple[str, str], ...] = ()
    irreducible_occurrences: tuple[OccurrenceId, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def of_kind(self, kind: DeltaProblemKind) -> tuple[DeltaProblem, ...]:
        return tuple(problem for problem in self.problems if problem.kind is kind)

    @property
    def kinds(self) -> frozenset[DeltaProblemKind]:
        return frozenset(problem.kind for problem in self.problems)


def merge_delta(
    delta: ProposedPlanDelta,
    current: TaskNetworkSnapshot,
    task_bindings: Sequence[TaskSemanticBindingV1],
) -> TaskNetworkSnapshot:
    """The network the delta describes, so it can be judged in context.

    Separate from the compiler's own merge because a validator must be able to
    check a delta it did not build — a proposal arriving from another session, or
    one replayed from a receipt.
    """

    retired = set(delta.retired_instance_ids)
    adopted = [
        instance_id for instance_id in current.adopted_instance_ids if instance_id not in retired
    ]
    known_tasks = {binding.task_id for binding in task_bindings}
    bindings: list[TaskSemanticBindingV1] = []
    for binding in current.task_bindings:
        if binding.task_id in known_tasks:
            continue
        if binding.adopted_method_instance_id in retired:
            payload = binding.to_json()
            payload["adopted_method_instance_id"] = None
            bindings.append(TaskSemanticBindingV1.from_json(payload))
            continue
        bindings.append(binding)
    bindings.extend(task_bindings)
    instances = list(current.method_instances)
    known_instances = {instance.instance_id for instance in instances}
    for draft in delta.method_instances:
        if draft.instance_id not in known_instances:
            instances.append(draft)
        if draft.instance_id not in retired:
            adopted.append(draft.instance_id)
    occurrence_ids = {spec.occurrence_id for spec in current.occurrences}
    new_occurrences = tuple(
        spec for spec in delta.occurrences if spec.occurrence_id not in occurrence_ids
    )
    return TaskNetworkSnapshot(
        mission_id=current.mission_id,
        plan_revision=current.plan_revision,
        occurrences=(*current.occurrences, *new_occurrences),
        task_bindings=tuple(bindings),
        method_instances=tuple(instances),
        adopted_instance_ids=tuple(dict.fromkeys(adopted)),
        root_occurrence_ids=current.root_occurrence_ids,
        order_constraints=(*current.order_constraints, *delta.order_constraints),
        data_requirements=(*current.data_requirements, *delta.data_requirements),
        typed_edges=current.typed_edges,
        obligation_coverage=(*current.obligation_coverage, *delta.obligation_coverage),
        required_obligations=current.required_obligations,
    )


def validate_delta(
    delta: ProposedPlanDelta,
    current: TaskNetworkSnapshot,
    budget: GraphStructureBudget = DEFAULT_PROJECTION_BUDGET,
    *,
    task_bindings: Sequence[TaskSemanticBindingV1] = (),
    network: TaskNetworkSnapshot | None = None,
    registry: MethodRegistry | None = None,
    catalog: TaskTypeCatalog | None = None,
    methods: Mapping[str, MethodContract] | None = None,
    snapshot: EvidenceSnapshot | None = None,
    predicates: PredicateRegistry | None = None,
    now_ms: int | None = None,
) -> DeltaReport:
    """Reducibility, port bindability, precondition class, root coverage and size.

    Every optional argument is an input the check *needs*; without it the matching
    check reports ``NOT_CHECKED`` rather than passing.  A report that silently
    skipped the evidence check would be indistinguishable from one where the
    evidence was fine.
    """

    if not isinstance(delta, ProposedPlanDelta):
        raise ContractError("validate_delta expects a ProposedPlanDelta")
    if not isinstance(current, TaskNetworkSnapshot):
        raise ContractError("validate_delta expects a TaskNetworkSnapshot")
    merged = network if network is not None else merge_delta(delta, current, task_bindings)
    projection = merged.execution_projection()
    projection_report = validate_execution_projection(projection, budget)
    refinement_report = validate_refinement_acyclic(merged)

    problems: list[DeltaProblem] = []
    for problem in projection_report.problems:
        if problem.kind is ProblemKind.PARTIAL_CHECK:
            continue
        kind = DeltaProblemKind.PROJECTION_DEFECT
        if problem.kind is ProblemKind.BOUND_REACHED:
            kind = DeltaProblemKind.SIZE_BOUND
        elif problem.kind is ProblemKind.ROOT_COVERAGE_GAP:
            kind = DeltaProblemKind.ROOT_COVERAGE_GAP
        elif problem.kind in (
            ProblemKind.UNBOUND_PORT,
            ProblemKind.SINGLE_PORT_OVERBOUND,
            ProblemKind.SET_PORT_UNORDERED,
        ):
            kind = DeltaProblemKind.PORT_UNBINDABLE
        problems.append(
            DeltaProblem(
                kind=kind, detail=f"{problem.kind!s}: {problem.detail}", subjects=problem.nodes
            )
        )
    for problem in refinement_report.problems:
        problems.append(
            DeltaProblem(
                kind=DeltaProblemKind.REFINEMENT_CYCLE,
                detail=problem.detail,
                subjects=problem.nodes,
            )
        )

    irreducible = _check_reducibility(delta, merged, registry, problems)
    unbound = _check_ports(delta, merged, problems)
    verdicts = _check_preconditions(delta, methods, snapshot, predicates, problems, now_ms=now_ms)
    _check_coverage(delta, merged, problems)
    del catalog
    return DeltaReport(
        problems=tuple(sorted(problems, key=lambda item: (str(item.kind), item.detail))),
        projection_report=projection_report,
        refinement_report=refinement_report,
        preconditions=verdicts,
        unbound_ports=unbound,
        irreducible_occurrences=irreducible,
    )


def _check_reducibility(
    delta: ProposedPlanDelta,
    merged: TaskNetworkSnapshot,
    registry: MethodRegistry | None,
    problems: list[DeltaProblem],
) -> tuple[OccurrenceId, ...]:
    """A compound occurrence no method could ever refine is a dead end, not a frontier.

    Left unrefined on purpose is fine — that is the planning frontier.  Having *no
    retrievable method at all* for its goal type is different: nothing later in the
    run can change it, and a plan that contains one can never close.
    """

    if registry is None:
        problems.append(
            DeltaProblem(
                kind=DeltaProblemKind.NOT_CHECKED,
                detail="reducibility was not checked: no method registry was supplied",
            )
        )
        return ()
    irreducible: list[OccurrenceId] = []
    for spec in delta.occurrences:
        if spec.form is not TaskForm.COMPOUND:
            continue
        if merged.adopted_instance_for(spec.occurrence_id) is not None:
            continue
        binding = merged.binding_for_task(spec.task_id)
        goal_type_id = binding.goal_signature.signature_id
        offered = [
            candidate
            for candidate in registry.method_refs()
            if (definition := registry.definition(candidate)) is not None
            and definition.goal_type_ref.id == goal_type_id
            and registry.retrievable(candidate, mission_id=merged.mission_id)
        ]
        if offered:
            continue
        irreducible.append(spec.occurrence_id)
        problems.append(
            DeltaProblem(
                kind=DeltaProblemKind.NOT_REDUCIBLE,
                detail=(
                    f"compound occurrence {spec.occurrence_id!s} has goal signature "
                    f"{goal_type_id!r}, for which this mission can retrieve no method"
                ),
                subjects=(str(spec.occurrence_id),),
            )
        )
    return tuple(irreducible)


def _check_ports(
    delta: ProposedPlanDelta,
    merged: TaskNetworkSnapshot,
    problems: list[DeltaProblem],
) -> tuple[tuple[str, str], ...]:
    """Every required input port of a new occurrence must have a producer."""

    bound = {
        (requirement.consumer_occurrence, requirement.input_port)
        for requirement in merged.data_requirements
    }
    unbound: list[tuple[str, str]] = []
    for spec in delta.occurrences:
        binding = merged.binding_for_task(spec.task_id)
        for port in binding.input_ports:
            if not port.required:
                continue
            if (spec.occurrence_id, port.port_key) in bound:
                continue
            unbound.append((str(spec.occurrence_id), port.port_key))
            problems.append(
                DeltaProblem(
                    kind=DeltaProblemKind.PORT_UNBINDABLE,
                    detail=(
                        f"required input port {port.port_key!r} of "
                        f"{spec.occurrence_id!s} has no producer in this plan"
                    ),
                    subjects=(str(spec.occurrence_id), port.port_key),
                )
            )
        if port_problem := _set_port_problem(binding):
            problems.append(port_problem)
    return tuple(sorted(set(unbound)))


def _set_port_problem(binding: TaskSemanticBindingV1) -> DeltaProblem | None:
    for label, ports in (("input", binding.input_ports), ("output", binding.output_ports)):
        for port in ports:
            if port.cardinality is PortCardinality.SET and port.ordering is None:
                return DeltaProblem(
                    kind=DeltaProblemKind.PORT_UNBINDABLE,
                    detail=(
                        f"set {label} port {port.port_key!r} of {binding.task_id!s} declares "
                        "no ordering; a set port defines its order (TG §4.3)"
                    ),
                    subjects=(str(binding.task_id), port.port_key),
                )
    return None


def _check_preconditions(
    delta: ProposedPlanDelta,
    methods: Mapping[str, MethodContract] | None,
    snapshot: EvidenceSnapshot | None,
    predicates: PredicateRegistry | None,
    problems: list[DeltaProblem],
    *,
    now_ms: int | None,
) -> tuple[PreconditionVerdict, ...]:
    """Re-evaluate each frozen witness and classify it four ways (§6.6 rule 3)."""

    if methods is None or snapshot is None or predicates is None:
        if delta.method_instances:
            problems.append(
                DeltaProblem(
                    kind=DeltaProblemKind.NOT_CHECKED,
                    detail=(
                        "precondition satisfiability was not checked: the method "
                        "definitions, the evidence snapshot or the predicate registry "
                        "were not supplied"
                    ),
                )
            )
        return ()
    verdicts: list[PreconditionVerdict] = []
    for draft in delta.method_instances:
        method = methods.get(draft.method_ref.method_id)
        if method is None:
            problems.append(
                DeltaProblem(
                    kind=DeltaProblemKind.NOT_CHECKED,
                    detail=(
                        f"method {draft.method_ref.method_id!r} was not supplied; its "
                        "witnesses were not re-evaluated"
                    ),
                    subjects=(str(draft.instance_id),),
                )
            )
            continue
        by_digest = {condition_digest(item): item for item in method.applicable_when}
        parameters = {binding.name: binding.value for binding in draft.grounded_parameters}
        for witness in draft.precondition_witnesses:
            condition = by_digest.get(witness.condition_digest)
            if condition is None:
                problems.append(
                    DeltaProblem(
                        kind=DeltaProblemKind.PRECONDITION_WITNESS_STALE,
                        detail=(
                            f"instance {draft.instance_id!s} carries a witness for a "
                            "condition this method version does not declare"
                        ),
                        subjects=(str(draft.instance_id), witness.condition_digest),
                    )
                )
                continue
            evaluation = evaluate_condition(
                condition,
                registry=predicates,
                snapshot=snapshot,
                parameters=parameters,
                now_ms=now_ms,
                path="validate_delta",
            )
            classification = _TRUTH_TO_CLASS[evaluation.truth]
            verdicts.append(
                PreconditionVerdict(
                    instance_id=str(draft.instance_id),
                    condition_digest=witness.condition_digest,
                    recorded=witness.truth,
                    current=evaluation.truth,
                    classification=classification,
                )
            )
            problem_kind = _CLASS_TO_PROBLEM.get(classification)
            if problem_kind is not None:
                problems.append(
                    DeltaProblem(
                        kind=problem_kind,
                        detail=(
                            f"instance {draft.instance_id!s} rests on a precondition that is "
                            f"now {evaluation.truth!s} (recorded {witness.truth!s})"
                        ),
                        subjects=(str(draft.instance_id), witness.condition_digest),
                    )
                )
    return tuple(verdicts)


def _check_coverage(
    delta: ProposedPlanDelta,
    merged: TaskNetworkSnapshot,
    problems: list[DeltaProblem],
) -> None:
    """Every root criterion must be claimed by an occurrence this plan contains."""

    projected = merged.execution_projection().projected_occurrences
    for root in merged.root_occurrence_ids:
        spec = merged.occurrence(root)
        wanted = set(merged.binding_for_task(spec.task_id).goal_signature.coverage_criteria)
        if not wanted:
            continue
        covered: set[str] = set()
        for claim in merged.obligation_coverage:
            if claim.obligation_id != spec.obligation_id:
                continue
            if any(item not in projected for item in claim.covered_by):
                continue
            covered.update(claim.criterion_ids)
        missing = sorted(wanted - covered)
        if missing:
            problems.append(
                DeltaProblem(
                    kind=DeltaProblemKind.ROOT_COVERAGE_GAP,
                    detail=(
                        f"root occurrence {root!s} leaves criteria {', '.join(missing)} of "
                        f"obligation {spec.obligation_id!s} uncovered"
                    ),
                    subjects=(str(root), *missing),
                )
            )
    del delta


# --------------------------------------------------------------------------------------
# The minimal HDDL export (§7.4, §8.3)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnsupportedFeature:
    """§7.4: features outside the fragment are refused, never approximated."""

    features: tuple[str, ...]
    detail: str
    fragment: str = HDDL_FRAGMENT

    @property
    def supported(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class HddlExport:
    """A domain / problem pair plus the mapping P2.2's verifier needs.

    ``occurrence_map`` is not decoration.  §8.3 and TG §12 require the exported
    model to keep every task occurrence and its place in the decomposition
    witness, so a runtime that bound two occurrences to one result cannot pass the
    shortened trace off as a plan for the original network.
    """

    domain_text: str
    problem_text: str
    occurrence_map: Mapping[str, str]
    reused_occurrences: tuple[str, ...] = ()
    fragment: str = HDDL_FRAGMENT

    @property
    def supported(self) -> bool:
        return True


def _hddl_name(value: str) -> str:
    """A safe HDDL identifier for an arbitrary id."""

    out = "".join(character if character.isalnum() else "_" for character in value)
    return out.lower().strip("_") or "x"


@dataclass(frozen=True, slots=True)
class _ExportNode:
    """One occurrence as the exported model sees it."""

    occurrence_id: OccurrenceId
    label: str
    signature: str
    form: TaskForm
    #: The adopted instance that decomposes it, when it is a decomposed compound.
    instance: MethodInstanceDraft | None = None
    #: True when a slot binds an already accepted result instead of doing the work.
    reused: bool = False


def _export_roots(delta: ProposedPlanDelta) -> tuple[OccurrenceId, ...]:
    """The goals this delta refines — the only top-level tasks of the problem."""

    return tuple(
        dict.fromkeys(draft.effective_goal_occurrence_id for draft in delta.method_instances)
    )


def _collect_export_nodes(
    delta: ProposedPlanDelta,
    network: TaskNetworkSnapshot,
) -> tuple[tuple[_ExportNode, ...], tuple[str, ...]]:
    """Walk the adopted refinement from the delta's roots, in a stable order.

    Returns the nodes to export and any features that stopped the export.  A
    compound occurrence with no adopted method is one of those features: an
    abstract task no method decomposes is not a model a planner can solve, and
    emitting it anyway — or quietly giving it an empty method — would be claiming
    that doing nothing achieves the goal.
    """

    by_delta = {draft.effective_goal_occurrence_id: draft for draft in delta.method_instances}
    reused: set[OccurrenceId] = set()
    for draft in (*delta.method_instances, *network.method_instances):
        for binding in draft.child_bindings:
            if binding.reuse_policy is not ReusePolicy.NEW_WORK:
                reused.add(binding.occurrence_id)

    nodes: list[_ExportNode] = []
    seen: set[OccurrenceId] = set()
    problems: list[str] = []
    pending = list(_export_roots(delta))
    while pending:
        occurrence_id = pending.pop(0)
        if occurrence_id in seen:
            continue
        seen.add(occurrence_id)
        try:
            spec = network.occurrence(occurrence_id)
        except KeyError:
            problems.append("occurrence-outside-network")
            continue
        semantics = network.binding_for_task(spec.task_id)
        label = _hddl_name(f"occ_{occurrence_id}")
        signature = _hddl_name(semantics.goal_signature.signature_id)
        if occurrence_id in reused:
            nodes.append(_ExportNode(occurrence_id, label, signature, spec.form, reused=True))
            continue
        if spec.form is TaskForm.PRIMITIVE:
            nodes.append(_ExportNode(occurrence_id, label, signature, spec.form))
            continue
        instance = by_delta.get(occurrence_id) or network.adopted_instance_for(occurrence_id)
        if instance is None:
            problems.append("unexpanded-compound-occurrence")
            continue
        nodes.append(_ExportNode(occurrence_id, label, signature, spec.form, instance=instance))
        pending.extend(child.occurrence_id for child in instance.child_bindings)
    return tuple(nodes), tuple(dict.fromkeys(problems))


def to_hddl(
    delta: ProposedPlanDelta,
    *,
    network: TaskNetworkSnapshot,
    methods: Mapping[str, MethodContract] | None = None,
    domain_name: str = "sh-delta",
) -> HddlExport | UnsupportedFeature:
    """Export the supported fragment of one increment, or say what is unsupported.

    The model is fully ground: every occurrence is a domain ``:constant`` and every
    method's ``:task`` names one of them, so there are no free variables and each
    abstract task has at least one method that decomposes it.  There is exactly one
    abstract task type, ``t_available``, with three shapes of method:

    * a *primitive* occurrence is decomposed into its one action;
    * a *decomposed compound* occurrence is decomposed into its children, with the
      ORDER and DATA edges between them as ``:ordering`` and nothing else — §7.4 is
      explicit that a partial order is not silently linearised;
    * a *reused* occurrence is decomposed into nothing, guarded by
      ``(accepted_result …)``, which is the explicit "the result already exists"
      method §8.3 and TG §12 require instead of a trace with one action fewer.

    The initial task network contains only the goals this delta refines; the rest
    of the network is reached by decomposition, because listing a parent and its
    children side by side would ask for the children to be executed twice.

    Anything outside the fragment — a disjunctive precondition, a structured or
    numeric parameter, a set-valued port, a revision-following data policy, a
    non-acceptance release condition, or a compound occurrence nobody has refined —
    is reported by :class:`UnsupportedFeature` and the export does not happen.
    """

    if not isinstance(delta, ProposedPlanDelta):
        raise ContractError("to_hddl expects a ProposedPlanDelta")
    unsupported = list(unsupported_features(delta, network=network, methods=methods))
    if not delta.method_instances:
        unsupported.append("no-decomposition-to-export")
    nodes, walk_problems = _collect_export_nodes(delta, network)
    unsupported.extend(item for item in walk_problems if item not in unsupported)
    if unsupported:
        return UnsupportedFeature(
            features=tuple(unsupported),
            detail=(
                f"this increment uses semantics outside {HDDL_FRAGMENT!r}: {', '.join(unsupported)}"
            ),
        )

    roots = _export_roots(delta)
    occurrence_map = {str(node.occurrence_id): node.label for node in nodes}
    constants = sorted(node.label for node in nodes)
    actions = sorted(
        {node.signature for node in nodes if node.form is TaskForm.PRIMITIVE and not node.reused}
    )
    reused = tuple(sorted(str(node.occurrence_id) for node in nodes if node.reused))

    method_blocks: list[str] = []
    for node in sorted(nodes, key=lambda item: item.label):
        if node.reused:
            method_blocks.append(
                f"  (:method m_reuse_{node.label}\n"
                f"    :parameters ()\n"
                f"    :task (t_available {node.label})\n"
                f"    :precondition (accepted_result {node.label})\n"
                f"    :subtasks ())"
            )
        elif node.instance is None:
            method_blocks.append(
                f"  (:method m_do_{node.label}\n"
                f"    :parameters ()\n"
                f"    :task (t_available {node.label})\n"
                f"    :subtasks (and\n"
                f"      (t1 (a_{node.signature} {node.label}))))"
            )
        else:
            method_blocks.append(_hddl_decomposition(node, occurrence_map, network))

    domain_text = "\n".join(
        [
            f"(define (domain {_hddl_name(domain_name)})",
            "  (:requirements :strips :typing :hierarchy "
            ":negative-preconditions :method-preconditions)",
            "  (:types occurrence - object)",
            "  (:constants",
            *[f"    {label} - occurrence" for label in constants],
            "  )",
            "  (:predicates",
            "    (done ?o - occurrence)",
            "    (accepted_result ?o - occurrence)",
            "  )",
            "",
            "  (:task t_available :parameters (?o - occurrence))",
            "",
            *method_blocks,
            "",
            *[
                f"  (:action a_{signature}\n"
                f"    :parameters (?o - occurrence)\n"
                f"    :precondition ()\n"
                f"    :effect (done ?o))"
                for signature in actions
            ],
            ")",
        ]
    )
    problem_text = "\n".join(
        [
            f"(define (problem {_hddl_name(str(delta.delta_id))})",
            f"  (:domain {_hddl_name(domain_name)})",
            "  (:htn",
            "    :parameters ()",
            "    :subtasks (and",
            *[
                f"      (root{position} (t_available {occurrence_map[str(root)]}))"
                for position, root in enumerate(roots)
                if str(root) in occurrence_map
            ],
            "    ))",
            "  (:init",
            *[f"    (accepted_result {occurrence_map[item]})" for item in reused],
            "  )",
            ")",
        ]
    )
    return HddlExport(
        domain_text=domain_text,
        problem_text=problem_text,
        occurrence_map=dict(sorted(occurrence_map.items())),
        reused_occurrences=reused,
    )


def _hddl_decomposition(
    node: _ExportNode,
    occurrence_map: Mapping[str, str],
    network: TaskNetworkSnapshot,
) -> str:
    """One compound occurrence's method: its children and the order between them."""

    assert node.instance is not None
    labels: dict[str, str] = {}
    subtasks: list[str] = []
    for position, binding in enumerate(node.instance.child_bindings):
        child = str(binding.occurrence_id)
        target = occurrence_map.get(child)
        if target is None:
            continue
        task_label = f"t{position}"
        labels[child] = task_label
        subtasks.append(f"      ({task_label} (t_available {target}))")
    orderings = _hddl_orderings(network, labels)
    body = [
        f"  (:method m_{_hddl_name(str(node.instance.instance_id))}",
        "    :parameters ()",
        f"    :task (t_available {node.label})",
        "    :subtasks (and",
        *subtasks,
        "    )",
    ]
    if orderings:
        body.append("    :ordering (and")
        body.extend(orderings)
        body.append("    ))")
    else:
        # No declared order between these children: the method is genuinely
        # unordered and says so by omitting :ordering (TG §6 / §7.4).
        body.append("  )")
    return "\n".join(body)


def _hddl_orderings(network: TaskNetworkSnapshot, labels: Mapping[str, str]) -> list[str]:
    """Partial order only.  §7.4: a partial order is never silently linearised."""

    pairs: list[tuple[str, str]] = []
    for constraint in network.order_constraints:
        before = labels.get(str(constraint.before))
        after = labels.get(str(constraint.after))
        if before and after and (before, after) not in pairs:
            pairs.append((before, after))
    for requirement in network.data_requirements:
        before = labels.get(str(requirement.producer_occurrence))
        after = labels.get(str(requirement.consumer_occurrence))
        if before and after and (before, after) not in pairs:
            pairs.append((before, after))
    return [f"      (< {before} {after})" for before, after in sorted(pairs)]


def unsupported_features(
    delta: ProposedPlanDelta,
    *,
    network: TaskNetworkSnapshot,
    methods: Mapping[str, MethodContract] | None = None,
) -> tuple[str, ...]:
    """The semantic items of this increment the fragment cannot express."""

    features: list[str] = []

    def note(feature: str) -> None:
        if feature not in features:
            features.append(feature)

    for requirement in delta.data_requirements:
        _note_data_feature(requirement, note)
    for constraint in delta.order_constraints:
        _note_order_feature(constraint, note)
    known = {spec.occurrence_id for spec in network.occurrences}
    for spec in delta.occurrences:
        if spec.occurrence_id not in known:
            continue
        binding = network.binding_for_task(spec.task_id)
        for port in (*binding.input_ports, *binding.output_ports):
            if port.cardinality is PortCardinality.SET:
                note("set-valued-ports")
        for value in binding.typed_parameters.values():
            if isinstance(value, (dict, list)):
                note("structured-objects")
            elif isinstance(value, float):
                note("numeric-fluents")
    for draft in delta.method_instances:
        for parameter in draft.grounded_parameters:
            if isinstance(parameter.value, (dict, list)):
                note("structured-objects")
            elif isinstance(parameter.value, float):
                note("numeric-fluents")
        method = (methods or {}).get(draft.method_ref.method_id)
        if method is None:
            continue
        for condition in method.applicable_when:
            for node in iter_conditions(condition):
                if isinstance(node, AnyCondition):
                    note("disjunctive-preconditions")
        for step in method.steps:
            for argument in step.arguments.values():
                if isinstance(argument, (ObjectValue, ArrayValue)):
                    note("structured-objects")
                if isinstance(argument, ConstantValue) and isinstance(argument.value, float):
                    note("numeric-fluents")
    return tuple(features)


def _note_data_feature(requirement: DataRequirement, note: Any) -> None:
    if requirement.source_revision_policy is SourceRevisionPolicy.FOLLOW_AUTHORIZED_REVISION:
        note("revision-following-data")


def _note_order_feature(constraint: OrderConstraint, note: Any) -> None:
    if constraint.release_condition is not ReleaseCondition.ACCEPTED:
        note("non-acceptance-release-conditions")


__all__ = (
    "HDDL_FRAGMENT",
    "DeltaProblem",
    "DeltaProblemKind",
    "DeltaReport",
    "HddlExport",
    "PreconditionClass",
    "PreconditionVerdict",
    "UnsupportedFeature",
    "merge_delta",
    "to_hddl",
    "unsupported_features",
    "validate_delta",
)
