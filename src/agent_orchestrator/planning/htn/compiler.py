# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Compiling one grounded method into a checked plan increment (§18.3, TG §5.2).

:func:`compile_refinement` walks the ten steps of the implementation design §5.2
in order and returns a
:class:`~agent_orchestrator.contracts.htn.ProposedPlanDelta` — the only shape a
Commit accepts (§18.3).  It commits nothing, creates no Agent, touches no store
and calls no model; step 10 of the design is precisely "hand it to the single
Commit service", and this module stops one step short of that on purpose.

The three structural decisions it implements, from §24.1:

decision 2
    a compound occurrence compiles to an ``entry``/``exit`` pair of logical gates,
    an external ``P before Q`` becomes ``P.exit → Q.entry``, and a compound with no
    gating children still spans ``entry → exit``.  The gates themselves are built
    by :meth:`~agent_orchestrator.graph.task_network.TaskNetworkSnapshot.execution_projection`;
    what the compiler does is produce a network whose projection has them, and
    then check that it does.
decision 3
    DATA travels through declared ports.  A step argument that reads
    ``OutputValue(step, port)`` becomes a ``DataRequirement`` between the two
    occurrences, and the ports must be declared by the two task types.
decision 9
    a slot that reuses an accepted result keeps its own occurrence.  The reused
    goal occurrence is *referenced*, never re-created and never deleted, and a
    ``REUSE_ACCEPTED`` slot must name the exact acceptance it rests on.

Refusal, not repair: an uncovered root criterion, a cyclic projection, an unbound
required port or a size bound each raise :class:`CompilationRefused` carrying the
structured report.  Silently dropping the offending edge would turn a defect in
the plan into a defect in the execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...contracts.htn import (
    BudgetInheritance,
    DataRequirement,
    GraphStructureBudget,
    MethodContract,
    MethodInstanceDraft,
    MethodInstanceId,
    MissionRef,
    ObligationCoverage,
    ObligationId,
    ObligationOpening,
    ObligationRelation,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    PortCardinality,
    ProposedPlanDelta,
    ReadItem,
    ReadItemKind,
    ReleaseCondition,
    ReusePolicy,
    SemanticReadSet,
    SourceRevisionPolicy,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    mission_ref,
)
from ...contracts.models import ContractError
from ...contracts.obligations import ObligationAccountView, ObligationLedger
from ...contracts.semantic_base import TypedRef, content_hash_of, identifier, index
from ...graph.projection_validation import (
    ProblemKind,
    ProjectionReport,
    validate_execution_projection,
    validate_refinement_acyclic,
)
from ...graph.task_network import DEFAULT_PROJECTION_BUDGET, TaskNetworkSnapshot
from .grounding import (
    GroundingError,
    SharedGoalIndex,
    SlotPlan,
    child_task_bindings,
    data_flows,
    plan_slots,
)
from .registry import MethodRegistry, SchemaCatalog, TaskTypeCatalog

#: The default policies a planned DATA edge carries when the caller names none.
#: They are ids into the deployment's policy tables, not behaviour defined here.
DEFAULT_ASSURANCE_POLICY = "assurance.default"
DEFAULT_FRESHNESS_POLICY = "freshness.default"

#: The slice of the parent's remaining fuel a newly opened child duty inherits when
#: the caller names no grant (§6.1).  One unit: decomposition redistributes the
#: parent's allowance and never creates any, so the default is the smallest share
#: that lets the child be worked on at all.
DEFAULT_CHILD_FUEL_SHARE = 1


@dataclass(frozen=True, slots=True)
class BudgetRequirement:
    """What this increment would cost the plan's structural budget (ADR-08)."""

    obligation_id: ObligationId
    new_occurrences: int
    new_primitive_occurrences: int
    new_compound_occurrences: int
    referenced_occurrences: int
    new_order_constraints: int
    new_data_requirements: int
    max_fan_out: int

    def to_json(self) -> dict[str, Any]:
        return {
            "obligation_id": str(self.obligation_id),
            "new_occurrences": self.new_occurrences,
            "new_primitive_occurrences": self.new_primitive_occurrences,
            "new_compound_occurrences": self.new_compound_occurrences,
            "referenced_occurrences": self.referenced_occurrences,
            "new_order_constraints": self.new_order_constraints,
            "new_data_requirements": self.new_data_requirements,
            "max_fan_out": self.max_fan_out,
        }


@dataclass(frozen=True, slots=True)
class RefinementCompilation:
    """Everything one compilation produced.

    ``ProposedPlanDelta`` deliberately carries no task bindings — TG §3.2 keeps
    those in their own record — so the bindings the increment creates travel here,
    beside the delta, and the caller hands both to the Commit service in one
    transaction.
    """

    delta: ProposedPlanDelta
    task_bindings: tuple[TaskSemanticBindingV1, ...]
    adopted_instance_id: MethodInstanceId
    parent_binding: TaskSemanticBindingV1
    budget_requirement: BudgetRequirement
    projection_report: ProjectionReport
    refinement_report: ProjectionReport
    network: TaskNetworkSnapshot
    shared_occurrences: tuple[OccurrenceId, ...] = ()
    #: Duties this increment opens (§6.1, CR#6).  Empty when every slot refines the
    #: parent duty, which is the common case and the one that keeps recursion fuel
    #: meaningful.  The same tuple is carried on ``delta.obligation_openings``.
    new_obligations: tuple[ObligationOpening, ...] = ()
    steps: tuple[str, ...] = ()


class CompilationRefused(ContractError):
    """The increment did not pass a structural check and was not produced.

    Carries the reports so the caller can say *which* class of defect stopped it —
    a cycle, an unbound port, a coverage gap and a size bound are four different
    repairs and must not arrive as one boolean.
    """

    def __init__(
        self,
        message: str,
        *,
        projection_report: ProjectionReport | None = None,
        refinement_report: ProjectionReport | None = None,
        problems: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.projection_report = projection_report
        self.refinement_report = refinement_report
        self.problems = tuple(problems)

    def kinds(self) -> frozenset[ProblemKind]:
        kinds: set[ProblemKind] = set()
        for report in (self.projection_report, self.refinement_report):
            if report is not None:
                kinds |= set(report.kinds)
        return frozenset(kinds)


def compile_refinement(
    draft: MethodInstanceDraft,
    current: TaskNetworkSnapshot,
    *,
    method: MethodContract,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    registry: MethodRegistry | None = None,
    sharing: SharedGoalIndex | None = None,
    reuse_acceptances: Mapping[str, TypedRef] | None = None,
    slot_authorizations: Mapping[str, TypedRef] | None = None,
    slot_grants: Mapping[str, str] | None = None,
    retire_instance_ids: Sequence[MethodInstanceId] = (),
    budget: GraphStructureBudget = DEFAULT_PROJECTION_BUDGET,
    requirements_revision: int = 0,
    delta_id: str | None = None,
    compiled_from_proposal_id: str | None = None,
) -> ProposedPlanDelta:
    """§18.3: emit the partial order, the data bindings, the coverage and the read-set.

    The §18.3 shape is ``(draft, current) -> ProposedPlanDelta``; the deployment
    facts the compilation is decided against are keyword arguments so the same
    call is reproducible from a receipt.  Use :func:`compile_refinement_bundle`
    when the caller also needs the task bindings the increment creates.
    """

    return compile_refinement_bundle(
        draft,
        current,
        method=method,
        catalog=catalog,
        schemas=schemas,
        registry=registry,
        sharing=sharing,
        reuse_acceptances=reuse_acceptances,
        slot_authorizations=slot_authorizations,
        slot_grants=slot_grants,
        retire_instance_ids=retire_instance_ids,
        budget=budget,
        requirements_revision=requirements_revision,
        delta_id=delta_id,
        compiled_from_proposal_id=compiled_from_proposal_id,
    ).delta


def compile_refinement_bundle(
    draft: MethodInstanceDraft,
    current: TaskNetworkSnapshot,
    *,
    method: MethodContract,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    registry: MethodRegistry | None = None,
    sharing: SharedGoalIndex | None = None,
    reuse_acceptances: Mapping[str, TypedRef] | None = None,
    slot_authorizations: Mapping[str, TypedRef] | None = None,
    slot_grants: Mapping[str, str] | None = None,
    retire_instance_ids: Sequence[MethodInstanceId] = (),
    budget: GraphStructureBudget = DEFAULT_PROJECTION_BUDGET,
    requirements_revision: int = 0,
    delta_id: str | None = None,
    compiled_from_proposal_id: str | None = None,
) -> RefinementCompilation:
    """The ten steps of implementation design §5.2, in order."""

    if not isinstance(draft, MethodInstanceDraft):
        raise ContractError("compile_refinement expects a MethodInstanceDraft")
    if not isinstance(current, TaskNetworkSnapshot):
        raise ContractError("compile_refinement expects a TaskNetworkSnapshot")
    if not isinstance(method, MethodContract):
        raise ContractError("compile_refinement expects a MethodContract")
    steps: list[str] = []

    # -- step 1: freeze the input versions ---------------------------------------
    if draft.method_ref != method.method_ref():
        raise CompilationRefused(
            f"the draft refines {draft.method_ref.method_id!r} at content hash "
            f"{draft.method_ref.content_hash[:12]}…, but the method supplied hashes to "
            f"{method.method_ref().content_hash[:12]}…; a definition is pinned by its hash"
        )
    parent_binding = _require_parent(current, draft)
    steps.append("1 froze requirements, method definition and network revision")

    # -- step 2: parameters, signature, operators, admission ----------------------
    if registry is not None:
        mission = mission_ref(current.mission_id, "mission_id")
        if not registry.retrievable(draft.method_ref, mission_id=mission):
            registration = registry.registration(draft.method_ref)
            status = "unregistered" if registration is None else str(registration.status)
            raise CompilationRefused(
                f"method {draft.method_ref.method_id!r} is {status} for mission "
                f"{mission!s}; only an admitted or trial-admitted method may be compiled "
                "(§7.3)"
            )
    parameters = {binding.name: binding.value for binding in draft.grounded_parameters}
    _check_parameters_agree(draft, parent_binding, parameters)
    try:
        plans = plan_slots(
            parent_binding,
            method,
            parameters,
            instance_id=draft.instance_id,
            catalog=catalog,
            sharing=sharing,
            reuse_acceptances=dict(reuse_acceptances or {}),
        )
    except GroundingError as error:
        raise CompilationRefused(str(error)) from error
    _check_slots_match_draft(draft, plans)
    steps.append("2 checked parameters, goal signature, operators and method admission")

    # -- step 3: stable occurrences, only for the frontier being expanded ---------
    by_slot = {plan.identity.slot_key: plan for plan in plans}
    new_bindings = child_task_bindings(
        draft,
        method,
        parent_binding,
        catalog=catalog,
        schemas=schemas,
        sharing=sharing,
        reuse_acceptances=dict(reuse_acceptances or {}),
    )
    binding_by_task = {binding.task_id: binding for binding in new_bindings}
    occurrences: list[OccurrenceSpec] = []
    referenced: list[OccurrenceId] = []
    shared: list[OccurrenceId] = []
    for plan in plans:
        if plan.shared:
            referenced.append(plan.bound_occurrence_id)
            shared.append(plan.bound_occurrence_id)
            continue
        binding = binding_by_task[plan.identity.task_id]
        occurrences.append(
            OccurrenceSpec(
                occurrence_id=plan.identity.occurrence_id,
                task_id=plan.identity.task_id,
                obligation_id=binding.obligation_id,
                form=plan.spec.form,
                requiredness=plan.requiredness,
            )
        )
    steps.append(f"3 derived {len(occurrences)} stable occurrence(s) from (instance, slot)")

    # -- step 4: reused slots keep their occurrence and name their acceptance -----
    for plan in plans:
        if not plan.shared:
            continue
        if not _contains(current, plan.bound_occurrence_id):
            raise CompilationRefused(
                f"slot {plan.identity.slot_key!r} binds shared occurrence "
                f"{plan.bound_occurrence_id!s}, which this network does not contain"
            )
        if plan.reuse_policy is ReusePolicy.REUSE_ACCEPTED and plan.acceptance_ref is None:
            raise CompilationRefused(
                f"slot {plan.identity.slot_key!r} reuses an accepted result but names no "
                "acceptance; reuse binds an exact Acceptance (TG decision 9)"
            )
    parent_occurrence = draft.effective_goal_occurrence_id
    if parent_occurrence not in referenced:
        referenced.append(parent_occurrence)
    steps.append(
        f"4 referenced {len(shared)} shared occurrence(s) without re-creating or deleting any"
    )

    # -- step 5: ORDER, DATA ports and the compound entry / exit gates ------------
    order_constraints = _compile_order(method, by_slot)
    data_requirements = _compile_data(method, by_slot, catalog)
    steps.append(
        f"5 expanded {len(order_constraints)} ORDER and {len(data_requirements)} DATA edge(s)"
    )

    # -- step 7 (first half): the composition obligations -------------------------
    coverage = _compile_coverage(method, by_slot, parent_binding)
    new_obligations = _new_obligations(
        method,
        by_slot,
        parent_binding,
        authorizations=dict(slot_authorizations or {}),
        grants=dict(slot_grants or {}),
    )

    # -- steps 6 and 8: the structural checks, on the merged network --------------
    merged = _merge(
        current,
        draft=draft,
        parent_binding=parent_binding,
        new_bindings=new_bindings,
        occurrences=tuple(occurrences),
        order_constraints=order_constraints,
        data_requirements=data_requirements,
        coverage=coverage,
        retire_instance_ids=tuple(retire_instance_ids),
    )
    projection = merged.execution_projection()
    projection_report = validate_execution_projection(projection, budget)
    refinement_report = validate_refinement_acyclic(merged)
    steps.append("6 checked resource read/write threats on the merged network")

    # -- step 7 (second half): root coverage and the duties it rests on -----------
    _check_obligations_accounted(draft, new_obligations, parent_binding)
    gaps = _coverage_gaps(method, parent_binding, coverage)
    if gaps:
        raise CompilationRefused(
            f"method {method.method_id!r} leaves criteria {', '.join(gaps)} of obligation "
            f"{parent_binding.obligation_id!s} uncovered; all leaves passing does not "
            "satisfy the parent (§8.1)",
            projection_report=projection_report,
            refinement_report=refinement_report,
            problems=tuple(f"uncovered criterion {item}" for item in gaps),
        )
    steps.append(
        "7 built the composition obligations "
        f"({len(new_obligations)} new duty/duties) and checked root coverage"
    )

    fatal = [
        problem
        for problem in projection_report.problems
        if problem.kind is not ProblemKind.PARTIAL_CHECK
        and not _is_open_frontier_gap(problem, merged)
    ]
    if fatal or refinement_report.problems:
        raise CompilationRefused(
            "the compiled increment does not pass the structural checks: "
            + "; ".join(
                sorted(
                    {
                        f"{problem.kind!s}: {problem.detail}"
                        for problem in (*fatal, *refinement_report.problems)
                    }
                )
            ),
            projection_report=projection_report,
            refinement_report=refinement_report,
            problems=tuple(
                sorted({str(problem.kind) for problem in (*fatal, *refinement_report.problems)})
            ),
        )
    steps.append("8 ran cycle, missing-edge, duplicate-slot, port and size checks")

    # -- step 9: the delta, the budget requirement and the semantic read-set ------
    read_set = build_read_set(
        current,
        draft=draft,
        parent_binding=parent_binding,
        plans=plans,
        requirements_revision=requirements_revision,
    )
    identifier_ = delta_id or _derive_delta_id(draft, current.plan_revision)
    delta = ProposedPlanDelta(
        delta_id=identifier_,
        mission_id=current.mission_id,
        base_plan_revision=current.plan_revision,
        read_set=read_set,
        method_instances=(draft,),
        occurrences=tuple(occurrences),
        retired_instance_ids=tuple(retire_instance_ids),
        order_constraints=order_constraints,
        data_requirements=data_requirements,
        obligation_coverage=coverage,
        obligation_openings=new_obligations,
        referenced_occurrences=tuple(dict.fromkeys(referenced)),
        compiled_from_proposal_id=compiled_from_proposal_id,
    )
    delta.assert_consistent_with({binding.task_id: binding for binding in merged.task_bindings})
    requirement = _budget_requirement(
        parent_binding, occurrences, order_constraints, data_requirements, shared
    )
    steps.append("9 emitted the ProposedPlanDelta, the budget requirement and the read-set")
    # -- step 10 is the Commit service.  This module stops here, by design.
    steps.append("10 not taken here: the single Commit service accepts the delta")

    return RefinementCompilation(
        delta=delta,
        task_bindings=new_bindings,
        adopted_instance_id=draft.instance_id,
        parent_binding=_adopting(parent_binding, draft.instance_id),
        budget_requirement=requirement,
        projection_report=projection_report,
        refinement_report=refinement_report,
        network=merged,
        shared_occurrences=tuple(dict.fromkeys(shared)),
        new_obligations=new_obligations,
        steps=tuple(steps),
    )


# --------------------------------------------------------------------------------------
# The individual compilation steps
# --------------------------------------------------------------------------------------


def _is_open_frontier_gap(problem: Any, merged: TaskNetworkSnapshot) -> bool:
    """Is this coverage gap simply a root nobody has refined yet?

    ``validate_execution_projection`` checks root coverage over the *whole*
    network, which is the right question to ask before a mission is declared done
    and the wrong one to ask of an increment: a root that has not been refined at
    all is the planning frontier, not a defect, and refusing every increment until
    every root is covered would make incremental planning impossible.  The
    increment's own coverage is still checked, by step 7 above, against the
    obligation it refines.

    The root is matched out of the problem text because ``ProjectionProblem``
    carries no structured subject for this kind; it is a narrow, local read of a
    message this module does not own.
    """

    if problem.kind is not ProblemKind.ROOT_COVERAGE_GAP:
        return False
    for root in merged.root_occurrence_ids:
        if merged.adopted_instance_for(root) is None and f"{str(root)!r}" in problem.detail:
            return True
    return False


def _contains(network: TaskNetworkSnapshot, occurrence_id: OccurrenceId) -> bool:
    try:
        network.occurrence(occurrence_id)
    except KeyError:
        return False
    return True


def _require_parent(
    current: TaskNetworkSnapshot, draft: MethodInstanceDraft
) -> TaskSemanticBindingV1:
    occurrence = draft.effective_goal_occurrence_id
    try:
        spec = current.occurrence(occurrence)
    except KeyError as error:
        raise CompilationRefused(
            f"the draft refines occurrence {occurrence!s}, which this network does not contain"
        ) from error
    if spec.task_id != draft.goal_id:
        raise CompilationRefused(
            f"occurrence {occurrence!s} belongs to task {spec.task_id!s}, not to the "
            f"draft's goal {draft.goal_id!s}"
        )
    if spec.form is not TaskForm.COMPOUND:
        raise CompilationRefused(
            f"occurrence {occurrence!s} is primitive; only a compound task is refined by a "
            "method (§6.2)"
        )
    binding = current.binding_for_task(spec.task_id)
    if binding.obligation_id != draft.obligation_id:
        raise CompilationRefused(
            f"the draft claims obligation {draft.obligation_id!s} but the task binding says "
            f"{binding.obligation_id!s}; the binding is the authority (TG §3.2)"
        )
    return binding


def _check_parameters_agree(
    draft: MethodInstanceDraft,
    parent: TaskSemanticBindingV1,
    parameters: Mapping[str, Any],
) -> None:
    """The draft's parameters must agree with the goal it claims to refine.

    A grounding may add bindings the task does not carry — that is how a planner
    supplies a choice — but it may not *contradict* the task's own typed
    parameters.  Without this check a draft grounded against one goal would compile
    cleanly against another goal of the same type, and the resulting occurrences
    would carry one task's identity and another's arguments.
    """

    disagreeing = sorted(
        name
        for name, value in parameters.items()
        if name in parent.typed_parameters and parent.typed_parameters[name] != value
    )
    if disagreeing:
        raise CompilationRefused(
            f"the draft's parameter(s) {', '.join(disagreeing)} disagree with the typed "
            f"parameters of task {parent.task_id!s}; this draft was grounded against a "
            "different goal"
        )
    del draft


def _check_slots_match_draft(draft: MethodInstanceDraft, plans: Sequence[SlotPlan]) -> None:
    """The draft and a fresh planning of the same method must agree slot for slot.

    They are derived from the same inputs, so a disagreement means the draft was
    built against different parameters, a different sharing index or a different
    catalogue — and compiling it would attach this method's structure to another
    grounding's identities.
    """

    expected = {plan.identity.slot_key: plan.bound_occurrence_id for plan in plans}
    actual = {binding.slot_key: binding.occurrence_id for binding in draft.child_bindings}
    if expected != actual:
        differing = sorted(set(expected) ^ set(actual)) or sorted(
            key for key in expected if expected[key] != actual.get(key)
        )
        raise CompilationRefused(
            "the draft's child bindings disagree with a fresh grounding of the same method "
            f"at slot(s) {', '.join(differing)}; the draft was built against other inputs"
        )


def _compile_order(
    method: MethodContract, by_slot: Mapping[str, SlotPlan]
) -> tuple[OrderConstraint, ...]:
    """TG decision 1 / §24.1 decision 2: ORDER releases on acceptance.

    The projection turns each of these into ``before.exit → after.entry``; nothing
    here linearises the remaining steps.  Two steps with no declared order stay
    unordered, which is the whole point of a partial order.
    """

    out: list[OrderConstraint] = []
    for ordering in method.ordering:
        before = by_slot.get(ordering.before)
        after = by_slot.get(ordering.after)
        if before is None or after is None:
            raise CompilationRefused(
                f"the method orders {ordering.before!r} before {ordering.after!r}, but one "
                "of them is not a slot of this instance"
            )
        if before.bound_occurrence_id == after.bound_occurrence_id:
            raise CompilationRefused(
                f"steps {ordering.before!r} and {ordering.after!r} bind the same shared "
                "occurrence; an ordering between them cannot be satisfied"
            )
        out.append(
            OrderConstraint(
                before=before.bound_occurrence_id,
                after=after.bound_occurrence_id,
                release_condition=ReleaseCondition.ACCEPTED,
            )
        )
    return tuple(out)


def _compile_data(
    method: MethodContract,
    by_slot: Mapping[str, SlotPlan],
    catalog: TaskTypeCatalog,
) -> tuple[DataRequirement, ...]:
    """TG decision 3: one ``DataRequirement`` per declared port-to-port link."""

    out: list[DataRequirement] = []
    for producer_step, output_port, consumer_step, input_port in data_flows(method):
        producer = by_slot.get(producer_step)
        consumer = by_slot.get(consumer_step)
        if producer is None or consumer is None:
            raise CompilationRefused(
                f"the method binds {producer_step}.{output_port} into "
                f"{consumer_step}.{input_port}, but one of the steps is not a slot"
            )
        producer_spec = catalog.require(producer.step.task_type_ref)
        consumer_spec = catalog.require(consumer.step.task_type_ref)
        declared_output = producer_spec.output_port(output_port)
        declared_input = consumer_spec.input_port(input_port)
        if declared_output is None:
            raise CompilationRefused(
                f"task type {producer_spec.task_type_ref.id!r} declares no output port "
                f"{output_port!r}"
            )
        if declared_input is None:
            raise CompilationRefused(
                f"task type {consumer_spec.task_type_ref.id!r} declares no input port "
                f"{input_port!r}"
            )
        if declared_output.schema_ref != declared_input.schema_ref:
            raise CompilationRefused(
                f"port {producer_step}.{output_port} publishes schema "
                f"{declared_output.schema_ref.id!r} v{declared_output.schema_ref.version} but "
                f"{consumer_step}.{input_port} expects "
                f"{declared_input.schema_ref.id!r} v{declared_input.schema_ref.version}; "
                "schema compatibility is exact match or a registered declaration "
                "(TG decision 3)"
            )
        if producer.bound_occurrence_id == consumer.bound_occurrence_id:
            raise CompilationRefused(
                f"steps {producer_step!r} and {consumer_step!r} bind the same occurrence; a "
                "data requirement connects two different occurrences"
            )
        out.append(
            DataRequirement(
                requirement_id=_derive_requirement_id(
                    producer.bound_occurrence_id,
                    output_port,
                    consumer.bound_occurrence_id,
                    input_port,
                ),
                producer_occurrence=producer.bound_occurrence_id,
                output_port=output_port,
                consumer_occurrence=consumer.bound_occurrence_id,
                input_port=input_port,
                schema_ref=declared_output.schema_ref,
                assurance_policy_ref=DEFAULT_ASSURANCE_POLICY,
                freshness_policy_ref=DEFAULT_FRESHNESS_POLICY,
                source_revision_policy=SourceRevisionPolicy.PINNED,
            )
        )
    # No duplicate-port check here on purpose.  Within one method a consumer's input
    # port *is* the argument name, so a dict cannot hold two of them, and the two
    # cases that really can overbind a port are each owned by a layer that can see
    # them: ``ProposedPlanDelta`` refuses two requirements into one port inside a
    # delta, and ``validate_execution_projection`` reports SINGLE_PORT_OVERBOUND when
    # a second delta binds a port an earlier one already filled.  A third copy here
    # would only be dead code that reads like a guarantee.
    return tuple(out)


def _compile_coverage(
    method: MethodContract,
    by_slot: Mapping[str, SlotPlan],
    parent: TaskSemanticBindingV1,
) -> tuple[ObligationCoverage, ...]:
    """§6.3: the parent criterion → child criterion map becomes a coverage claim."""

    return coverage_from_slots(
        method,
        {key: plan.bound_occurrence_id for key, plan in by_slot.items()},
        obligation=parent.obligation_id,
    )


def coverage_from_slots(
    method: MethodContract,
    by_slot: Mapping[str, OccurrenceId],
    *,
    obligation: ObligationId,
) -> tuple[ObligationCoverage, ...]:
    """The coverage claims of one adopted method instance, from slot → occurrence.

    A link with no ``child_step`` is covered by the composition itself, which is
    the finalizer slot when the method declares one — a claim that has to land on
    a real occurrence, because coverage is what the root Resolution is checked
    against.

    Public since P2.3c part 2c: ``obligation_coverage`` is **not** a persisted
    column, so a network read back from the store has to re-derive it from the
    adopted instances and their contracts.  Without that, the claims of round one
    were gone by round two and a second refinement was refused with
    ``root_coverage_gap`` — a plan deeper than one level could never be committed.
    One implementation, two callers.
    """

    finalizer = by_slot.get(method.composition.finalizer_step or "")
    grouped: dict[str, list[OccurrenceId]] = {}
    for link in method.composition.criterion_links:
        bound = by_slot.get(link.child_step or "") if link.child_step else finalizer
        if bound is None:
            raise CompilationRefused(
                f"criterion {link.parent_criterion_id!r} is covered by "
                + (
                    f"step {link.child_step!r}, which is not a slot"
                    if link.child_step
                    else "the composition, but the method declares no finalizer step"
                )
            )
        bucket = grouped.setdefault(link.parent_criterion_id, [])
        if bound not in bucket:
            bucket.append(bound)
    if not grouped:
        return ()
    covered_by: list[OccurrenceId] = []
    criteria: list[str] = []
    for criterion in sorted(grouped):
        criteria.append(criterion)
        for occurrence in grouped[criterion]:
            if occurrence not in covered_by:
                covered_by.append(occurrence)
    return (
        ObligationCoverage(
            obligation_id=obligation,
            criterion_ids=tuple(criteria),
            covered_by=tuple(covered_by),
        ),
    )


def _new_obligations(
    method: MethodContract,
    by_slot: Mapping[str, SlotPlan],
    parent: TaskSemanticBindingV1,
    *,
    authorizations: Mapping[str, TypedRef],
    grants: Mapping[str, str],
) -> tuple[ObligationOpening, ...]:
    """The duties this increment opens, one per slot that derives a new one (CR#6).

    §6.1 draws the line at ``obligation_relation``.  A ``refines_parent`` step *is*
    part of the parent's responsibility: it carries the parent's ``obligation_id``,
    spends the parent's allowance, and opens nothing — which is exactly what stops
    decomposition from minting retry budget, and what makes recursion fuel bound a
    recursive method's depth.  Only an ``independent_authorized`` step is genuinely
    new work, and it may not be opened on the planner's say-so: the contract
    requires an ``authorization_ref``, so a slot whose authority the caller has not
    supplied is refused here rather than opened without one.

    A separate grant is available for such a duty when the caller names the grant
    that made it.  It is never available to a refinement — the contract refuses
    that combination outright, and this function never builds one, because a
    refinement that could ask for its own grant would be the §6.1 reset by another
    route.
    """

    out: list[ObligationOpening] = []
    for step in method.steps:
        plan = by_slot.get(step.local_id)
        if plan is None or plan.shared:
            continue
        slot = plan.identity.slot_key
        relation = step.obligation_relation
        if plan.bound_obligation_id == parent.obligation_id:
            # A refinement opens nothing — and may not buy its way out of that by
            # naming a grant, which is checked here rather than after the `continue`
            # so the refusal covers the case that has no opening to attach it to.
            if slot in grants:
                raise CompilationRefused(
                    f"slot {slot!r} refines the parent duty and therefore spends the "
                    "parent's allowance; a separate grant would hand decomposition a "
                    "fresh retry budget (§6.1)"
                )
            continue
        authority = authorizations.get(slot)
        if relation is ObligationRelation.INDEPENDENT_AUTHORIZED and authority is None:
            raise CompilationRefused(
                f"slot {slot!r} adds independently authorised work, which needs the "
                "authority that authorised it; planning alone does not create "
                "responsibility (§6.1)"
            )
        grant = grants.get(slot)
        if grant is not None and relation is not ObligationRelation.INDEPENDENT_AUTHORIZED:
            raise CompilationRefused(
                f"slot {slot!r} refines the parent duty and therefore spends the parent's "
                "allowance; a separate grant would hand decomposition a fresh retry "
                "budget (§6.1)"
            )
        try:
            out.append(
                ObligationOpening(
                    obligation_id=plan.bound_obligation_id,
                    parent_obligation_id=parent.obligation_id,
                    relation=relation,
                    requirement_refs=parent.requirement_refs,
                    goal_signature=plan.spec.goal_signature,
                    budget_inheritance=(
                        BudgetInheritance.SEPARATE_GRANT
                        if grant is not None
                        else BudgetInheritance.INHERIT_PARENT_FUEL_SHARE
                    ),
                    fuel_share=None if grant is not None else DEFAULT_CHILD_FUEL_SHARE,
                    grant_ref=grant,
                    authorization_ref=authority,
                )
            )
        except ContractError as error:
            raise CompilationRefused(f"slot {slot!r} cannot open its duty: {error}") from error
    return tuple(out)


def _check_obligations_accounted(
    draft: MethodInstanceDraft,
    openings: Sequence[ObligationOpening],
    parent: TaskSemanticBindingV1,
) -> None:
    """Every child duty is the parent's, a shared one, or one this increment opens.

    Without this the compiler could emit a slot whose ``obligation_id`` nobody has
    opened an account for, and the *next* refinement of that sub-goal would fail
    inside the ledger with "obligation is not registered" — a defect in this
    increment surfacing two steps later, in a different module.
    """

    opened = {item.obligation_id for item in openings}
    unaccounted = sorted(
        str(binding.obligation_id)
        for binding in draft.child_bindings
        if binding.obligation_id != parent.obligation_id
        and binding.obligation_id not in opened
        and binding.reuse_policy is ReusePolicy.NEW_WORK
    )
    if unaccounted:
        raise CompilationRefused(
            "these child duties are neither the parent's nor opened by this increment: "
            + ", ".join(unaccounted)
            + "; a duty nobody opened has no retry allowance and no funding (§6.1)"
        )


class DemandNotAdmissible(ContractError):
    """Nobody in this delta is asking for a duty it opens, or may ask for it.

    A separate class from the fuel refusals: "the parent cannot afford this child"
    and "no adopted slot wants this child" are different defects with different
    repairs, and §7.4 forbids reporting two causes under one name.
    """


@dataclass(frozen=True, slots=True)
class DemandAdmission:
    """Who is asking for a newly opened duty's work, and on what authority.

    TG implementation design §9.2 gives a demand its identity as
    ``DemandRef(consumer_instance, slot, obligation, producer, state)``: the thing
    that wants the work is a **slot of an adopted method instance**, not the Mission
    and not the model.  §6.1 adds that a genuinely new responsibility is created by
    an explicit command and records its relation to its parent.  This record is the
    two of those put together, and it is what the commit path writes down.
    """

    obligation_id: ObligationId
    parent_obligation_id: ObligationId
    relation: ObligationRelation
    #: ``method_slot`` — an adopted slot asked for it; ``authorization`` — a separate
    #: authority did.  The Mission-root case never arrives through a plan delta.
    requester_kind: str
    method_instance_id: str | None = None
    slot_key: str | None = None
    occurrence_id: str | None = None
    authorization_ref: TypedRef | None = None

    def requester(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.requester_kind}
        if self.method_instance_id is not None:
            payload["method_instance_id"] = self.method_instance_id
        if self.slot_key is not None:
            payload["slot_key"] = self.slot_key
        if self.occurrence_id is not None:
            payload["occurrence_id"] = self.occurrence_id
        if self.authorization_ref is not None:
            payload["authorization_ref"] = self.authorization_ref.to_json()
        return payload


def demand_admissions_for(delta: ProposedPlanDelta) -> tuple[DemandAdmission, ...]:
    """Which slot is asking for each duty this delta opens, or refuse the delta.

    P2.3c part 2d, decision 3.  Until now nothing on the production path ever
    admitted a demand, so every NEW_WORK child this delta opened was permanently
    undispatchable: ``has_admitted_demand`` stayed false and the readiness gate --
    correctly -- withheld it.  The fix is not to loosen that gate (TG §6 and §24.1
    decision 9 both require it) but to answer the question it asks.

    The rules are deterministic and there is no default pass:

    ``REFINES_PARENT``
        exactly one ``ChildBinding`` in this same delta must name the opened duty.
        That slot is the consumer, and its ``(instance_id, slot_key,
        occurrence_id)`` is the ``DemandRef`` TG §9.2 describes.  No such slot means
        nobody adopted the work, and TG §3.2 is explicit that a candidate which is
        not in the approved execution scope gets **no** real demand -- so the delta
        is refused rather than opening a duty nobody asked for.  Two such slots is
        the shared-work case, which registers its own second ``DemandRef`` through
        the commit path rather than riding on this one.

    ``INDEPENDENT_AUTHORIZED``
        the opening's own ``authorization_ref`` is the requester.  It does not
        inherit the parent's demand, because it is not refining the parent's work.

    The parent's own account is checked by the caller, which holds the ledger:
    refining a duty nobody demands would manufacture demand out of nothing.
    """

    by_duty: dict[str, list[tuple[str, str, str]]] = {}
    for draft in delta.method_instances:
        for binding in draft.child_bindings:
            by_duty.setdefault(str(binding.obligation_id), []).append(
                (str(binding.instance_id), str(binding.slot_key), str(binding.occurrence_id))
            )
    out: list[DemandAdmission] = []
    for opening in delta.obligation_openings:
        duty = str(opening.obligation_id)
        if opening.relation is ObligationRelation.INDEPENDENT_AUTHORIZED:
            if opening.authorization_ref is None:  # pragma: no cover - contract enforces it
                raise DemandNotAdmissible(
                    f"the independently authorised duty {duty} names no authority; "
                    "planning alone does not create responsibility (§6.1)"
                )
            out.append(
                DemandAdmission(
                    obligation_id=opening.obligation_id,
                    parent_obligation_id=opening.parent_obligation_id,
                    relation=opening.relation,
                    requester_kind="authorization",
                    authorization_ref=opening.authorization_ref,
                )
            )
            continue
        slots = by_duty.get(duty, [])
        if not slots:
            raise DemandNotAdmissible(
                f"no adopted slot in this delta asks for the work of {duty}; a refinement "
                "creates the duty its own method instance adopted, and an opening nobody "
                "adopted would be a duty with no consumer (TG §3.2, §9.2)"
            )
        if len(slots) > 1:
            raise DemandNotAdmissible(
                f"{len(slots)} slots of this delta bind {duty}; a second consumer registers "
                "its own DemandRef through the commit path rather than sharing this one "
                "(§24.1 decision 9)"
            )
        instance_id, slot_key, occurrence = slots[0]
        out.append(
            DemandAdmission(
                obligation_id=opening.obligation_id,
                parent_obligation_id=opening.parent_obligation_id,
                relation=opening.relation,
                requester_kind="method_slot",
                method_instance_id=instance_id,
                slot_key=slot_key,
                occurrence_id=occurrence,
            )
        )
    return tuple(out)


def apply_obligation_openings(
    ledger: ObligationLedger,
    delta: ProposedPlanDelta,
    *,
    granted_fuel: Mapping[str, int] | None = None,
    admit: Callable[[DemandAdmission, ObligationLedger], ObligationAccountView] | None = None,
) -> tuple[ObligationAccountView, ...]:
    """Open every duty the delta asks for, and admit the demand that asked for it.

    The Commit service does this inside the same transaction as the rest of the
    delta; this function exists so the planning side can do it in one call and so a
    test can show that a newly opened sub-goal is immediately refinable.

    P2.3c part 2d, decision 3: opening and admitting are one step, because TG
    implementation design §7.3 puts ``validate_data_bindings_and_demand`` inside
    ``commit_plan_revision`` itself.  Splitting them would leave a window in which
    the plan is committed, the occurrences are materialised and the duty is open,
    but nothing is dispatchable -- which is the state part 2c's smoke ended in.
    :func:`demand_admissions_for` decides *who* is asking; this function additionally
    refuses to refine a duty that nobody demands, because a child cannot inherit an
    interest its parent does not hold.

    ``admit`` lets the Commit service route the admission through its own audited
    entry point (``CommitService.admit_obligation_demand``, which writes the
    ``ObligationDemandAdmitted`` event) while the rules above stay in this one place;
    the dry run that checks a command before the transaction leaves it unset and just
    flips the bit on a ledger it throws away.
    """

    grants = dict(granted_fuel or {})
    admissions = {str(item.obligation_id): item for item in demand_admissions_for(delta)}
    out: list[ObligationAccountView] = []
    for opening in delta.obligation_openings:
        parent_account = ledger.account(opening.parent_obligation_id)
        admission = admissions[str(opening.obligation_id)]
        if (
            admission.relation is ObligationRelation.REFINES_PARENT
            and not parent_account.has_admitted_demand
        ):
            raise DemandNotAdmissible(
                f"the parent duty {opening.parent_obligation_id!s} has no admitted demand, so "
                f"there is nobody for {opening.obligation_id!s} to inherit an interest from; "
                "refining work nobody wants is refused rather than assumed (§6.1)"
            )
        ledger.open_from(
            opening,
            parent_account,
            granted_fuel=grants.get(str(opening.obligation_id)),
        )
        out.append(
            ledger.admit_demand(opening.obligation_id)
            if admit is None
            else admit(admission, ledger)
        )
    return tuple(out)


def _coverage_gaps(
    method: MethodContract,
    parent: TaskSemanticBindingV1,
    coverage: Sequence[ObligationCoverage],
) -> tuple[str, ...]:
    wanted = set(parent.goal_signature.coverage_criteria)
    if not wanted:
        return ()
    claimed: set[str] = set()
    for claim in coverage:
        if claim.obligation_id == parent.obligation_id:
            claimed.update(claim.criterion_ids)
    del method
    return tuple(sorted(wanted - claimed))


def _adopting(
    binding: TaskSemanticBindingV1, instance_id: MethodInstanceId
) -> TaskSemanticBindingV1:
    """The parent binding with this instance adopted, and its plan identity bumped."""

    payload = binding.to_json()
    payload["adopted_method_instance_id"] = str(instance_id)
    return TaskSemanticBindingV1.from_json(payload)


def _merge(
    current: TaskNetworkSnapshot,
    *,
    draft: MethodInstanceDraft,
    parent_binding: TaskSemanticBindingV1,
    new_bindings: Sequence[TaskSemanticBindingV1],
    occurrences: Sequence[OccurrenceSpec],
    order_constraints: Sequence[OrderConstraint],
    data_requirements: Sequence[DataRequirement],
    coverage: Sequence[ObligationCoverage],
    retire_instance_ids: Sequence[MethodInstanceId],
) -> TaskNetworkSnapshot:
    """The network as it would look once this increment is committed.

    Building it is how the compiler checks the increment *in context*: a cycle or a
    resource clash between a new occurrence and an existing one is only visible on
    the merged graph, which is the same reason §24.1 decision 8 insists a commit
    re-validates on the current transaction state.
    """

    retired = set(retire_instance_ids)
    adopted = [
        instance_id for instance_id in current.adopted_instance_ids if instance_id not in retired
    ]
    adopted.append(draft.instance_id)
    # TG §9.3: retiring a method retires the *membership* it created.  The ORDER and
    # DATA edges between its slots belong to that membership, so they go with it —
    # left behind they would name occurrences the adopted plan no longer projects,
    # which the validator correctly reads as missing edges.
    orphaned = _orphaned_occurrences(current, retired, keep=adopted)
    # P2.3q / N10a: an accepted read-only leaf the replacement binds with
    # ``share_active`` is not an orphan — the new instance still needs it.  Without
    # this, ``shared_goal_index`` could name the occurrence and ``_merge`` would
    # drop it, which is the ``binds slot … to unknown occurrence`` P2.3n closed by
    # excluding *every* retiring child (and so never reused an accepted facts leaf).
    kept_by_share = {
        child.occurrence_id
        for child in draft.child_bindings
        if child.reuse_policy is not ReusePolicy.NEW_WORK
    }
    # Edges of the *retired* membership, including those that land on a shared
    # leaf, go with the membership.  The replacement compiles its own ORDER/DATA.
    # When nothing is retiring, this set is empty so a later method that merely
    # shares a live goal does not drop the first consumer's edges.
    retired_children: frozenset[OccurrenceId] = frozenset()
    if retired:
        dropped: set[OccurrenceId] = set()
        for instance in current.method_instances:
            if instance.instance_id in retired:
                dropped.update(child.occurrence_id for child in instance.child_bindings)
        retired_children = frozenset(dropped)
    orphaned = frozenset(orphaned - kept_by_share)
    # P2.3j: the orphaned occurrences leave the network *with* the membership, not
    # only their edges.  Left in, they would still be counted as live work — the
    # commit side funds a revision against every occurrence the network names — and
    # the retired instance would still bind slots to them, which is the shape the
    # store's plan-preservation check (§9.4) already accounts for as "retired
    # children".  The retired instance itself leaves too: a network is read back
    # from the adopted memberships only, and a re-read that kept the instance
    # would name occurrences the revision no longer holds.
    kept_occurrences = tuple(
        spec for spec in current.occurrences if spec.occurrence_id not in orphaned
    )
    orphaned_tasks = {
        spec.task_id for spec in current.occurrences if spec.occurrence_id in orphaned
    } - {spec.task_id for spec in kept_occurrences}
    bindings: list[TaskSemanticBindingV1] = []
    for binding in current.task_bindings:
        if binding.task_id in orphaned_tasks:
            continue
        if binding.task_id == parent_binding.task_id:
            bindings.append(_adopting(parent_binding, draft.instance_id))
            continue
        if binding.adopted_method_instance_id in retired:
            payload = binding.to_json()
            payload["adopted_method_instance_id"] = None
            bindings.append(TaskSemanticBindingV1.from_json(payload))
            continue
        bindings.append(binding)
    bindings.extend(new_bindings)
    instances = [
        instance
        for instance in current.method_instances
        if instance.instance_id != draft.instance_id and instance.instance_id not in retired
    ]
    instances.append(draft)
    try:
        return TaskNetworkSnapshot(
            mission_id=current.mission_id,
            plan_revision=PlanRevision(int(current.plan_revision) + 1),
            occurrences=(*kept_occurrences, *occurrences),
            task_bindings=tuple(bindings),
            method_instances=tuple(instances),
            adopted_instance_ids=tuple(adopted),
            root_occurrence_ids=current.root_occurrence_ids,
            order_constraints=(
                *(
                    constraint
                    for constraint in current.order_constraints
                    if constraint.before not in retired_children
                    and constraint.after not in retired_children
                ),
                *order_constraints,
            ),
            data_requirements=(
                *(
                    requirement
                    for requirement in current.data_requirements
                    if requirement.producer_occurrence not in retired_children
                    and requirement.consumer_occurrence not in retired_children
                ),
                *data_requirements,
            ),
            typed_edges=tuple(
                edge
                for edge in current.typed_edges
                if not _edge_names_any(edge, retired_children, orphaned_tasks, retired)
            ),
            obligation_coverage=(
                *(
                    claim
                    for claim in current.obligation_coverage
                    if not any(covered in orphaned for covered in claim.covered_by)
                ),
                *coverage,
            ),
            required_obligations=current.required_obligations,
        )
    except ContractError as error:
        raise CompilationRefused(
            f"the increment does not merge into the current network: {error}"
        ) from error


def _edge_names_any(
    edge: Any,
    occurrences: frozenset[OccurrenceId],
    tasks: set[TaskRef],
    instances: set[MethodInstanceId],
) -> bool:
    """Whether a typed edge touches anything the retirement removes."""

    for side in (edge.source, edge.target):
        kind = str(getattr(side.kind, "value", side.kind))
        if kind == "occurrence" and OccurrenceId(side.id) in occurrences:
            return True
        if kind == "task" and TaskRef(side.id) in tasks:
            return True
        if kind == "method_instance" and MethodInstanceId(side.id) in instances:
            return True
    return False


def _orphaned_occurrences(
    current: TaskNetworkSnapshot,
    retired: set[MethodInstanceId],
    *,
    keep: Sequence[MethodInstanceId],
) -> frozenset[OccurrenceId]:
    """Occurrences that only the retired instances opened.

    An occurrence a surviving adopted instance also binds — a shared sub-goal, for
    example — is *not* orphaned: §8.3 is explicit that one consumer leaving must not
    cancel work another consumer still needs.
    """

    if not retired:
        return frozenset()
    surviving: set[OccurrenceId] = set()
    dropped: set[OccurrenceId] = set()
    kept = set(keep)
    for instance in current.method_instances:
        if instance.instance_id in retired:
            dropped.update(child.occurrence_id for child in instance.child_bindings)
        elif instance.instance_id in kept:
            surviving.update(child.occurrence_id for child in instance.child_bindings)
    return frozenset(dropped - surviving)


def _budget_requirement(
    parent: TaskSemanticBindingV1,
    occurrences: Sequence[OccurrenceSpec],
    order_constraints: Sequence[OrderConstraint],
    data_requirements: Sequence[DataRequirement],
    shared: Sequence[OccurrenceId],
) -> BudgetRequirement:
    fan_out: dict[OccurrenceId, int] = {}
    for constraint in order_constraints:
        fan_out[constraint.before] = fan_out.get(constraint.before, 0) + 1
    for requirement in data_requirements:
        producer = requirement.producer_occurrence
        fan_out[producer] = fan_out.get(producer, 0) + 1
    return BudgetRequirement(
        obligation_id=parent.obligation_id,
        new_occurrences=len(occurrences),
        new_primitive_occurrences=sum(1 for spec in occurrences if spec.form is TaskForm.PRIMITIVE),
        new_compound_occurrences=sum(1 for spec in occurrences if spec.form is TaskForm.COMPOUND),
        referenced_occurrences=len(set(shared)),
        new_order_constraints=len(order_constraints),
        new_data_requirements=len(data_requirements),
        max_fan_out=max(fan_out.values(), default=0),
    )


def build_read_set(
    current: TaskNetworkSnapshot,
    *,
    draft: MethodInstanceDraft,
    parent_binding: TaskSemanticBindingV1,
    plans: Sequence[SlotPlan],
    requirements_revision: int = 0,
) -> SemanticReadSet:
    """ADR-13: what this compilation actually read, beside the integer gate.

    A precondition witness is listed as an observation read **only when it names a
    stored :class:`~...contracts.evidence_state.ValidityWitness`**, and an acceptance a
    slot reuses is a read too — otherwise a retracted acceptance could be reused by a
    delta that never mentioned it.

    P2.3c part 2c: this used to list *every* precondition witness, with the
    ``condition_digest`` as the read's id.  A condition digest is not a subject the
    store holds — the FACT channel resolves an id as an observation record or as a
    validity witness — so the read was unresolvable by construction, and once P2.3c
    part 1's review unified the eleven channels, **every** refinement of a method with
    preconditions was refused ``READ_SET_UNRESOLVED``.  That is the real-model smoke's
    blocker, and it had nothing to do with what the Planner wrote.

    Dropping those entries loses nothing: the digests are frozen on the
    ``MethodInstanceDraft`` itself with the truth they were selected under, and §6.6
    rule 3's ``recheck_method_instance`` is what compares them to the world later.  A
    read-set entry is a promise that *this* store can re-check the subject, and a
    promise it cannot keep is worse than no promise at all.
    """

    goal_reads = [
        ReadItem(
            kind=ReadItemKind.TASK,
            id=str(parent_binding.task_id),
            semantic_revision=int(parent_binding.contract_revision),
            content_hash=parent_binding.contract_hash,
        )
    ]
    method_reads = [
        ReadItem(
            kind=ReadItemKind.METHOD,
            id=draft.method_ref.method_id,
            semantic_revision=draft.method_ref.version,
            content_hash=draft.method_ref.content_hash,
        )
    ]
    observation_reads = [
        ReadItem(
            kind=ReadItemKind.FACT,
            id=str(witness.witness_ref.id),
            semantic_revision=int(witness.witness_ref.revision),
            content_hash=str(witness.witness_ref.content_hash),
        )
        for witness in draft.precondition_witnesses
        if witness.witness_ref is not None
    ]
    acceptance_reads = [
        ReadItem(
            kind=ReadItemKind.ACCEPTANCE,
            id=plan.acceptance_ref.id,
            semantic_revision=plan.acceptance_ref.revision,
            content_hash=plan.acceptance_ref.content_hash,
        )
        for plan in plans
        if plan.acceptance_ref is not None
    ]
    for plan in plans:
        if not plan.shared:
            continue
        spec = current.occurrence(plan.bound_occurrence_id)
        binding = current.binding_for_task(spec.task_id)
        goal_reads.append(
            ReadItem(
                kind=ReadItemKind.TASK,
                id=str(binding.task_id),
                semantic_revision=int(binding.contract_revision),
                content_hash=binding.contract_hash,
            )
        )
    return SemanticReadSet(
        requirements_revision=index(requirements_revision, "requirements_revision"),
        goal_revisions=tuple(dict.fromkeys(goal_reads)),
        method_revisions=tuple(method_reads),
        observation_revisions=tuple(observation_reads),
        acceptance_revisions=tuple(acceptance_reads),
    )


def _derive_delta_id(draft: MethodInstanceDraft, base: PlanRevision) -> str:
    digest = content_hash_of([str(draft.instance_id), int(base)])
    return f"delta-{digest[:32]}"


def _derive_requirement_id(
    producer: OccurrenceId, output_port: str, consumer: OccurrenceId, input_port: str
) -> str:
    digest = content_hash_of([str(producer), output_port, str(consumer), input_port])
    return f"data-{digest[:32]}"


def unbound_required_ports(
    network: TaskNetworkSnapshot,
) -> tuple[tuple[OccurrenceId, str], ...]:
    """Required single-valued input ports with no data requirement feeding them.

    A convenience over the same facts :func:`validate_execution_projection` reports,
    for callers that want the list rather than the problem text.
    """

    bound = {
        (requirement.consumer_occurrence, requirement.input_port)
        for requirement in network.data_requirements
    }
    out: list[tuple[OccurrenceId, str]] = []
    projection = network.execution_projection()
    for occurrence_id in sorted(projection.projected_occurrences, key=str):
        binding = network.binding_for_occurrence(occurrence_id)
        for port in binding.input_ports:
            if not port.required:
                continue
            if port.cardinality is PortCardinality.SET and port.ordering is None:
                continue
            if (occurrence_id, port.port_key) not in bound:
                out.append((occurrence_id, port.port_key))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class RootNetwork:
    """A one-occurrence starting network, for the first refinement of a mission.

    Building the seed by hand in every caller is how two callers end up disagreeing
    about whether the root is its own occurrence; this makes it one function.
    """

    mission_id: MissionRef
    root_task: TaskSemanticBindingV1
    plan_revision: PlanRevision = PlanRevision(0)
    root_occurrence_id: OccurrenceId | None = None
    extra_occurrences: tuple[OccurrenceSpec, ...] = ()
    extra_bindings: tuple[TaskSemanticBindingV1, ...] = ()
    required_obligations: tuple[ObligationId, ...] = field(default_factory=tuple)

    def snapshot(self) -> TaskNetworkSnapshot:
        root = self.root_occurrence_id or OccurrenceId(str(self.root_task.task_id))
        return TaskNetworkSnapshot(
            mission_id=mission_ref(self.mission_id, "mission_id"),
            plan_revision=self.plan_revision,
            occurrences=(
                OccurrenceSpec(
                    occurrence_id=root,
                    task_id=self.root_task.task_id,
                    obligation_id=self.root_task.obligation_id,
                    form=self.root_task.form,
                ),
                *self.extra_occurrences,
            ),
            task_bindings=(self.root_task, *self.extra_bindings),
            root_occurrence_ids=(root,),
            required_obligations=self.required_obligations or (self.root_task.obligation_id,),
        )


def task_ref_of(binding: TaskSemanticBindingV1) -> TaskRef:
    return TaskRef(identifier(str(binding.task_id), "task_id"))


__all__ = (
    "DEFAULT_ASSURANCE_POLICY",
    "DEFAULT_FRESHNESS_POLICY",
    "BudgetRequirement",
    "CompilationRefused",
    "DemandNotAdmissible",
    "DemandAdmission",
    "apply_obligation_openings",
    "demand_admissions_for",
    "RefinementCompilation",
    "RootNetwork",
    "build_read_set",
    "compile_refinement",
    "compile_refinement_bundle",
    "coverage_from_slots",
    "task_ref_of",
    "unbound_required_ports",
)
