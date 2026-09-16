# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deciding what to refine next, and how far (§7.2, §6.4 v1.2, TG §6).

:func:`refine` walks a planning frontier and, for each item, answers one of a
fixed set of outcomes.  It never calls a model, never writes a store and never
dispatches anything: when no admitted method fits, it returns a
:class:`MethodProposalRequest` for the layer above to take to a synthesiser, and
when a precondition is UNKNOWN it returns a read-only evidence-gathering
occurrence rather than sending a leaf out to find out the hard way.

The four rules this module exists to hold:

leaves are relative (§6.2)
    a task is a leaf when a registered operator can complete it inside one bounded
    attempt, its deliverable is checkable through declared output ports, and no
    required precondition is unresolved.  Nothing here is a leaf because someone
    marked it one.

try-first is for cheap, reversible work only (§7.2, ADaPT)
    :class:`AttemptPolicy` is ``BOUNDED_ATTEMPT_FIRST`` only for a task type whose
    declared effects are read-only or a reversible local write.  A high-risk,
    irreversible action is planned before it is attempted — "just try it once" is
    not a way to discover whether it needed planning.

fuel is per obligation and exhaustion is a bound (§6.4 v1.2, ADR-08)
    every instantiation spends one unit of the *obligation's* recursion fuel;
    changing method, parameters or agent refills nothing.  Running out returns
    ``BOUND_REACHED`` together with the structure already expanded — never
    ``UNSOLVABLE``, which would be a claim about the world rather than about this
    deployment's bounds.

an expansion that changes nothing is not progress (§6.4)
    re-expanding the same obligation with the same method and parameters is
    ``REPEATED_EXPANSION`` (the ledger's own key), and re-expanding an *ancestor's*
    goal with the same parameters against the same world snapshot is
    ``NO_STATE_CHANGE``.  Both are refused before fuel is spent, so a
    non-terminating method cannot burn a mission's budget to reach its bound.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...contracts.evidence_state import EvidenceSnapshot, TruthValue
from ...contracts.htn import (
    GoalSignature,
    GraphStructureBudget,
    MethodContract,
    MethodInstanceDraft,
    MethodRef,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    PlanRevision,
    Requiredness,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from ...contracts.models import ContractError
from ...contracts.obligations import (
    BoundReachedReport,
    ExpansionRecord,
    FuelStatus,
    ObligationLedger,
)
from ...contracts.semantic_base import (
    MAX_TEXT,
    VersionedRef,
    content_hash_of,
    text,
)
from ...graph.task_network import TaskNetworkSnapshot
from ...knowledge.predicates import PredicateRegistry
from .applicability import (
    ApplicabilityReport,
    ApplicabilityStatus,
    CapabilitySnapshot,
    assess_method,
    evaluate_condition,
)
from .grounding import (
    GroundingError,
    SharedGoalIndex,
    derive_id,
    ground_method,
)
from .registry import (
    MethodCandidate,
    MethodRegistry,
    SchemaCatalog,
    TaskTypeCatalog,
    TaskTypeSpec,
    iter_predicates,
)


class AttemptPolicy(StrEnum):
    """§7.2: may this leaf be tried before it is planned?"""

    BOUNDED_ATTEMPT_FIRST = "BOUNDED_ATTEMPT_FIRST"
    PLAN_BEFORE_ATTEMPT = "PLAN_BEFORE_ATTEMPT"


class RefinementOutcome(StrEnum):
    """What :func:`refine` decided about one frontier item."""

    REFINED = "REFINED"
    LEAF = "LEAF"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    NO_APPLICABLE_METHOD = "NO_APPLICABLE_METHOD"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    BOUND_REACHED = "BOUND_REACHED"
    REPEATED_EXPANSION = "REPEATED_EXPANSION"
    NO_STATE_CHANGE = "NO_STATE_CHANGE"
    OBLIGATION_NOT_OPENED = "OBLIGATION_NOT_OPENED"
    NOT_REFINABLE = "NOT_REFINABLE"


@dataclass(frozen=True, slots=True)
class FrontierItem:
    """One open position the planner may work on next (implementation design §6)."""

    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId
    bindings: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, spec: OccurrenceSpec, **bindings: Any) -> FrontierItem:
        return cls(
            occurrence_id=spec.occurrence_id,
            task_id=spec.task_id,
            obligation_id=spec.obligation_id,
            bindings=dict(bindings),
        )


def planning_frontier(network: TaskNetworkSnapshot) -> tuple[FrontierItem, ...]:
    """Compound occurrences in the adopted plan that no method has refined yet.

    The *planning* frontier of implementation design §6 — deliberately not the
    execution frontier, which is ``graph/eligibility.py``'s job (P2.1c) and answers
    a different question with different inputs.
    """

    projection = network.execution_projection()
    out: list[FrontierItem] = []
    for occurrence_id in sorted(projection.projected_occurrences, key=str):
        spec = network.occurrence(occurrence_id)
        if spec.form is not TaskForm.COMPOUND:
            continue
        if network.adopted_instance_for(occurrence_id) is not None:
            continue
        out.append(FrontierItem.of(spec))
    return tuple(out)


# --------------------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeafDecision:
    """§6.2: whether this task is a leaf *in the current capability and evidence*."""

    is_leaf: bool
    reason: str
    attempt_policy: AttemptPolicy
    checkable_deliverable: bool = False
    missing_capabilities: tuple[str, ...] = ()
    unresolved_preconditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", text(self.reason, "leaf.reason", limit=MAX_TEXT))


def leaf_decision(
    binding: TaskSemanticBindingV1,
    spec: TaskTypeSpec | None,
    *,
    capabilities: CapabilitySnapshot,
    snapshot: EvidenceSnapshot,
    predicates: PredicateRegistry,
    now_ms: int | None = None,
) -> LeafDecision:
    """Is there an executable operator, a checkable deliverable and no open precondition?"""

    policy = AttemptPolicy.PLAN_BEFORE_ATTEMPT
    if binding.form is TaskForm.COMPOUND:
        return LeafDecision(
            is_leaf=False,
            reason="a compound task is refined by a method, never dispatched (§6.2)",
            attempt_policy=policy,
        )
    if spec is None or spec.operator_ref is None:
        return LeafDecision(
            is_leaf=False,
            reason=(
                "no registered operator provides this task type; it is not executable here (§6.4)"
            ),
            attempt_policy=policy,
        )
    policy = (
        AttemptPolicy.BOUNDED_ATTEMPT_FIRST if spec.low_risk else AttemptPolicy.PLAN_BEFORE_ATTEMPT
    )
    missing = capabilities.missing_from(
        tuple(dict.fromkeys((*binding.capability_requirements, *spec.required_capabilities)))
    )
    checkable = bool(spec.output_ports)
    unresolved = tuple(
        request.proposition_key
        for request in unknown_predicates(
            spec.preconditions,
            parameters=binding.typed_parameters,
            predicates=predicates,
            snapshot=snapshot,
            now_ms=now_ms,
        )
    )
    if missing:
        return LeafDecision(
            is_leaf=False,
            reason=("this deployment cannot execute the operator: missing " + ", ".join(missing)),
            attempt_policy=policy,
            checkable_deliverable=checkable,
            missing_capabilities=missing,
            unresolved_preconditions=unresolved,
        )
    if not checkable:
        return LeafDecision(
            is_leaf=False,
            reason=(
                "the task type declares no output port, so its deliverable could not be "
                "checked; a leaf has to produce something an acceptance can read"
            ),
            attempt_policy=policy,
            unresolved_preconditions=unresolved,
        )
    if unresolved:
        return LeafDecision(
            is_leaf=False,
            reason=(
                "a required precondition is unresolved; gather evidence before dispatching "
                "(ADR-07: UNKNOWN is not FALSE and not TRUE)"
            ),
            attempt_policy=policy,
            checkable_deliverable=True,
            unresolved_preconditions=unresolved,
        )
    return LeafDecision(
        is_leaf=True,
        reason=(
            "a registered operator can complete this in one bounded attempt and its "
            "deliverable is readable through declared output ports"
        ),
        attempt_policy=policy,
        checkable_deliverable=True,
    )


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceOccurrenceRequest:
    """A read-only occurrence that would answer one UNKNOWN proposition.

    §7.2 / ADR-07: an unknown precondition is a reason to *look*, never a reason to
    dispatch the real work and see what happens.  The occurrence it proposes is
    always a read-only observer type, so producing the evidence cannot change the
    answer.
    """

    proposition_key: str
    predicate_ref: VersionedRef
    for_occurrence: OccurrenceId
    observer: TaskTypeSpec | None
    occurrence: OccurrenceSpec | None
    binding: TaskSemanticBindingV1 | None
    reason: str

    @property
    def satisfiable(self) -> bool:
        """Is there a registered observer that could answer this at all?"""

        return self.observer is not None


@dataclass(frozen=True, slots=True)
class UnknownProposition:
    proposition_key: str
    predicate_ref: VersionedRef
    truth: TruthValue


def unknown_predicates(
    conditions: Sequence[Any],
    *,
    parameters: Mapping[str, Any],
    predicates: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    now_ms: int | None = None,
) -> tuple[UnknownProposition, ...]:
    """The atoms of ``conditions`` whose truth is UNKNOWN right now.

    Evaluated atom by atom rather than read off the aggregate: the aggregate says
    the expression is UNKNOWN, and what an evidence request needs is *which*
    proposition to go and observe.
    """

    out: list[UnknownProposition] = []
    seen: set[str] = set()
    for atom in iter_predicates(conditions):
        evaluation = evaluate_condition(
            atom,
            registry=predicates,
            snapshot=snapshot,
            parameters=parameters,
            now_ms=now_ms,
            path="precondition",
        )
        if evaluation.truth is not TruthValue.UNKNOWN:
            continue
        keys = evaluation.unknown_propositions or (
            f"{atom.predicate_ref.id}@{atom.predicate_ref.version}#unresolved",
        )
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            out.append(
                UnknownProposition(
                    proposition_key=key,
                    predicate_ref=atom.predicate_ref,
                    truth=evaluation.truth,
                )
            )
    return tuple(out)


def evidence_requests(
    unknowns: Sequence[UnknownProposition],
    *,
    for_occurrence: OccurrenceId,
    obligation_id: ObligationId,
    catalog: TaskTypeCatalog,
    semantic_scope: str,
    contract_revision: int = 1,
) -> tuple[EvidenceOccurrenceRequest, ...]:
    """Turn UNKNOWN propositions into read-only occurrences that could answer them."""

    out: list[EvidenceOccurrenceRequest] = []
    for unknown in unknowns:
        observers = catalog.observers_for(unknown.predicate_ref)
        observer = observers[0] if observers else None
        if observer is None:
            out.append(
                EvidenceOccurrenceRequest(
                    proposition_key=unknown.proposition_key,
                    predicate_ref=unknown.predicate_ref,
                    for_occurrence=for_occurrence,
                    observer=None,
                    occurrence=None,
                    binding=None,
                    reason=(
                        f"no registered read-only observer can produce "
                        f"{unknown.predicate_ref.id!r}; the precondition stays UNKNOWN "
                        "(ADR-07: that is not a FALSE)"
                    ),
                )
            )
            continue
        occurrence_id = OccurrenceId(
            derive_id("occ", "evidence", for_occurrence, unknown.proposition_key)
        )
        task_id = TaskRef(derive_id("task", "evidence", for_occurrence, unknown.proposition_key))
        duty = ObligationId(derive_id("obl", "evidence", obligation_id, unknown.proposition_key))
        binding = TaskSemanticBindingV1(
            task_id=task_id,
            obligation_id=duty,
            contract_revision=contract_revision,  # type: ignore[arg-type]
            contract_hash=content_hash_of(
                {
                    "observer": observer.task_type_ref.to_json(),
                    "proposition": unknown.proposition_key,
                }
            ),
            form=TaskForm.PRIMITIVE,
            goal_signature=observer.goal_signature,
            typed_parameters={"proposition_key": unknown.proposition_key},
            input_ports=observer.input_ports,
            output_ports=observer.output_ports,
            operator_ref=observer.operator_ref,
            semantic_scope=semantic_scope,
            capability_requirements=observer.required_capabilities,
            resource_reads=observer.resource_reads,
            resource_writes=(),
            side_effect_kind=observer.side_effect_kind,
        )
        out.append(
            EvidenceOccurrenceRequest(
                proposition_key=unknown.proposition_key,
                predicate_ref=unknown.predicate_ref,
                for_occurrence=for_occurrence,
                observer=observer,
                occurrence=OccurrenceSpec(
                    occurrence_id=occurrence_id,
                    task_id=task_id,
                    obligation_id=duty,
                    form=TaskForm.PRIMITIVE,
                    requiredness=Requiredness.CONDITIONAL,
                ),
                binding=binding,
                reason=(
                    f"observe {unknown.predicate_ref.id!r} before deciding; the observer is "
                    "read-only, so looking cannot change the answer"
                ),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------------------
# Candidate comparison
# --------------------------------------------------------------------------------------

#: Applicable first, then the ones a bounded amount of evidence could rescue, then
#: the ones the world refuted.  Deliberately a fixed, documented order and not a
#: learned score: §10.3 owns scoring, and P2.1 must not invent one.
_STATUS_RANK: Mapping[ApplicabilityStatus, int] = {
    ApplicabilityStatus.APPLICABLE: 0,
    ApplicabilityStatus.NEEDS_EVIDENCE: 1,
    ApplicabilityStatus.CONFLICT: 2,
    ApplicabilityStatus.CAPABILITY_UNAVAILABLE: 3,
    ApplicabilityStatus.PRECONDITION_FALSE: 4,
    ApplicabilityStatus.TYPE_ERROR: 5,
}


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """One alternative method and why it is or is not selectable here."""

    candidate: MethodCandidate
    report: ApplicabilityReport
    rank: int
    selectable: bool
    reason: str

    @property
    def method(self) -> MethodContract:
        return self.candidate.method

    @property
    def method_ref(self) -> MethodRef:
        return self.candidate.method_ref


def assess_candidates(
    binding: TaskSemanticBindingV1,
    candidates: Sequence[MethodCandidate],
    *,
    snapshot: EvidenceSnapshot,
    capabilities: CapabilitySnapshot,
    predicates: PredicateRegistry,
    budget: GraphStructureBudget,
    now_ms: int | None = None,
) -> tuple[CandidateAssessment, ...]:
    """Assess every offered alternative, then keep the budget's worth of them.

    The cut is applied *after* assessment and in a deterministic order, so a
    truncated comparison never silently hides the only applicable method behind an
    inapplicable one.
    """

    assessed: list[CandidateAssessment] = []
    for candidate in candidates:
        report = assess_method(
            binding,
            candidate.method,
            snapshot,
            capabilities,
            registry=predicates,
            now_ms=now_ms,
        )
        gate = report.authorization
        selectable = report.applicable and (
            not candidate.method.applicable_when or (gate is not None and gate.allowed)
        )
        if report.applicable and not selectable:
            reason = (
                "the preconditions are TRUE but not evidence-backed; an authorisation gate "
                "accepts only observed or authoritative leaves (§6.6 rule 2)"
            )
        elif selectable:
            reason = "applicable and evidence-backed"
        else:
            reason = f"{report.status!s}"
        assessed.append(
            CandidateAssessment(
                candidate=candidate,
                report=report,
                rank=_STATUS_RANK[report.status],
                selectable=selectable,
                reason=reason,
            )
        )
    ordered = sorted(
        assessed,
        key=lambda item: (
            item.rank,
            0 if item.selectable else 1,
            len(item.report.unmet_capabilities),
            len(item.report.needs_evidence),
            item.candidate.method.method_id,
            item.candidate.method_ref.version,
        ),
    )
    return tuple(ordered[: budget.max_candidates])


# --------------------------------------------------------------------------------------
# Method proposal requests
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    method_id: str
    version: int
    status: ApplicabilityStatus
    reason: str


@dataclass(frozen=True, slots=True)
class MethodProposalRequest:
    """§7.3 source 4: nothing in the library fits, so ask for a new candidate.

    This module produces the *request* and stops.  Calling a synthesiser is a model
    call with its own budget and its own dispatch identity (§18.5), and the
    resulting proposal comes back through
    :meth:`~.registry.MethodRegistry.admit` like any other — a method the planner
    invented does not skip the admission protocol.
    """

    task_id: TaskRef
    occurrence_id: OccurrenceId
    obligation_id: ObligationId
    goal_signature: GoalSignature
    goal_type_ref: VersionedRef | None
    rejected: tuple[RejectedCandidate, ...] = ()
    suggestions: tuple[str, ...] = ()
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "occurrence_id": str(self.occurrence_id),
            "obligation_id": str(self.obligation_id),
            "goal_signature": self.goal_signature.to_json(),
            "goal_type_ref": (None if self.goal_type_ref is None else self.goal_type_ref.to_json()),
            "rejected": [
                {
                    "method_id": item.method_id,
                    "version": item.version,
                    "status": str(item.status),
                    "reason": item.reason,
                }
                for item in self.rejected
            ],
            "suggestions": list(self.suggestions),
            "reason": self.reason,
        }


# --------------------------------------------------------------------------------------
# refine
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefinementDecision:
    """What was decided about one frontier item, and everything behind it."""

    item: FrontierItem
    outcome: RefinementOutcome
    reason: str
    draft: MethodInstanceDraft | None = None
    chosen: CandidateAssessment | None = None
    candidates: tuple[CandidateAssessment, ...] = ()
    evidence: tuple[EvidenceOccurrenceRequest, ...] = ()
    proposal_request: MethodProposalRequest | None = None
    bound_report: BoundReachedReport | None = None
    leaf: LeafDecision | None = None
    fuel_remaining: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", text(self.reason, "decision.reason", limit=MAX_TEXT))


@dataclass(frozen=True, slots=True)
class RefinementReport:
    decisions: tuple[RefinementDecision, ...]

    def of_outcome(self, outcome: RefinementOutcome) -> tuple[RefinementDecision, ...]:
        return tuple(item for item in self.decisions if item.outcome is outcome)

    def for_occurrence(self, occurrence_id: OccurrenceId) -> RefinementDecision | None:
        for decision in self.decisions:
            if decision.item.occurrence_id == occurrence_id:
                return decision
        return None

    @property
    def drafts(self) -> tuple[MethodInstanceDraft, ...]:
        return tuple(item.draft for item in self.decisions if item.draft is not None)

    @property
    def outcomes(self) -> tuple[RefinementOutcome, ...]:
        return tuple(item.outcome for item in self.decisions)


def refine(
    frontier: Sequence[FrontierItem],
    *,
    network: TaskNetworkSnapshot,
    registry: MethodRegistry,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    predicates: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    capabilities: CapabilitySnapshot,
    ledger: ObligationLedger,
    budget: GraphStructureBudget,
    sharing: SharedGoalIndex | None = None,
    plan_revision: PlanRevision | None = None,
    world_snapshot_id: str | None = None,
    now_ms: int | None = None,
) -> RefinementReport:
    """Decide, for each frontier item, whether and how to expand it.

    Pure with one deliberate exception: recursion fuel is spent on the ledger, and
    only for an expansion that is actually produced.  Every refusal above — a
    repeat, an ancestor with no state change, an unauthorised gate — happens before
    the spend, so a method that cannot make progress does not consume the
    obligation's allowance on the way to saying so.
    """

    if not isinstance(network, TaskNetworkSnapshot):
        raise ContractError("refine expects a TaskNetworkSnapshot")
    if not isinstance(ledger, ObligationLedger):
        raise ContractError("refine expects an ObligationLedger")
    revision = plan_revision if plan_revision is not None else network.plan_revision
    view = network.refinement_view()
    decisions: list[RefinementDecision] = []
    for item in frontier:
        decisions.append(
            _refine_one(
                item,
                network=network,
                view_ancestors=view.ancestors_of(item.occurrence_id),
                registry=registry,
                catalog=catalog,
                schemas=schemas,
                predicates=predicates,
                snapshot=snapshot,
                capabilities=capabilities,
                ledger=ledger,
                budget=budget,
                sharing=sharing,
                plan_revision=revision,
                world_snapshot_id=world_snapshot_id or snapshot.snapshot_id,
                now_ms=now_ms,
            )
        )
    return RefinementReport(decisions=tuple(decisions))


def _refine_one(
    item: FrontierItem,
    *,
    network: TaskNetworkSnapshot,
    view_ancestors: frozenset[OccurrenceId],
    registry: MethodRegistry,
    catalog: TaskTypeCatalog,
    schemas: SchemaCatalog,
    predicates: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    capabilities: CapabilitySnapshot,
    ledger: ObligationLedger,
    budget: GraphStructureBudget,
    sharing: SharedGoalIndex | None,
    plan_revision: PlanRevision,
    world_snapshot_id: str,
    now_ms: int | None,
) -> RefinementDecision:
    binding = network.binding_for_occurrence(item.occurrence_id)
    spec = _task_type_for(binding, catalog)

    if binding.form is TaskForm.PRIMITIVE:
        decision = leaf_decision(
            binding,
            spec,
            capabilities=capabilities,
            snapshot=snapshot,
            predicates=predicates,
            now_ms=now_ms,
        )
        if decision.unresolved_preconditions and spec is not None:
            unknowns = unknown_predicates(
                spec.preconditions,
                parameters=binding.typed_parameters,
                predicates=predicates,
                snapshot=snapshot,
                now_ms=now_ms,
            )
            return RefinementDecision(
                item=item,
                outcome=RefinementOutcome.NEEDS_EVIDENCE,
                reason=decision.reason,
                evidence=evidence_requests(
                    unknowns,
                    for_occurrence=item.occurrence_id,
                    obligation_id=item.obligation_id,
                    catalog=catalog,
                    semantic_scope=binding.semantic_scope,
                    contract_revision=int(binding.contract_revision),
                ),
                leaf=decision,
            )
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.LEAF if decision.is_leaf else RefinementOutcome.NOT_REFINABLE,
            reason=decision.reason,
            leaf=decision,
        )

    # §6.1 / CR#6: a duty has to have been opened before anything is accounted
    # against it.  Saying so is a refinement outcome, not a crash inside the ledger:
    # the repair is to apply the increment's ``obligation_openings``, and a caller
    # that skipped that step needs to be told which duty it skipped.
    if item.obligation_id not in ledger.obligation_ids():
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.OBLIGATION_NOT_OPENED,
            reason=(
                f"duty {item.obligation_id!s} has no ledger account; open the increment's "
                "ObligationOpening against its parent before refining this sub-goal (§6.1)"
            ),
        )

    goal_type_ref = None if spec is None else spec.task_type_ref
    if goal_type_ref is None:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.NO_APPLICABLE_METHOD,
            reason=(
                f"no registered compound task type carries goal signature "
                f"{binding.goal_signature.signature_id!r} v{binding.goal_signature.version}"
            ),
            proposal_request=MethodProposalRequest(
                task_id=item.task_id,
                occurrence_id=item.occurrence_id,
                obligation_id=item.obligation_id,
                goal_signature=binding.goal_signature,
                goal_type_ref=None,
                reason="the goal type itself is not registered",
            ),
        )

    candidates = registry.candidates_for(goal_type_ref, mission_id=network.mission_id)
    assessed = assess_candidates(
        binding,
        candidates,
        snapshot=snapshot,
        capabilities=capabilities,
        predicates=predicates,
        budget=budget,
        now_ms=now_ms,
    )
    selectable = [entry for entry in assessed if entry.selectable]
    if not selectable:
        return _no_method(
            item,
            binding,
            goal_type_ref,
            assessed,
            catalog=catalog,
            predicates=predicates,
            snapshot=snapshot,
            now_ms=now_ms,
        )

    chosen = selectable[0]
    try:
        draft = ground_method(
            binding,
            chosen.method,
            item.bindings,
            chosen.report,
            catalog=catalog,
            schemas=schemas,
            sharing=sharing,
            plan_revision=plan_revision,
            goal_occurrence_id=item.occurrence_id,
            world_snapshot_id=world_snapshot_id,
        )
    except GroundingError as error:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.NOT_REFINABLE,
            reason=str(error),
            candidates=assessed,
            chosen=chosen,
        )

    repeat = _ancestor_repeat(
        draft, network=network, ancestors=view_ancestors, world_snapshot_id=world_snapshot_id
    )
    if repeat is not None:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.NO_STATE_CHANGE,
            reason=repeat,
            candidates=assessed,
            chosen=chosen,
            fuel_remaining=ledger.remaining_fuel(item.obligation_id),
        )

    expansion = ExpansionRecord(
        method_id=chosen.method.method_id,
        parameters_digest=draft.parameters_digest(),
        task_id=str(item.task_id),
    )
    fuel = ledger.consume_fuel(item.obligation_id, expansion=expansion)
    if fuel.status is FuelStatus.REPEATED_EXPANSION:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.REPEATED_EXPANSION,
            reason=fuel.reason,
            candidates=assessed,
            chosen=chosen,
            fuel_remaining=fuel.remaining_fuel,
        )
    if fuel.status is FuelStatus.BOUND_REACHED:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.BOUND_REACHED,
            reason=(
                "recursion fuel for this obligation is exhausted; the structure expanded so "
                "far is kept and reported as a bound, not as an impossible goal (ADR-08)"
            ),
            candidates=assessed,
            chosen=chosen,
            bound_report=BoundReachedReport(
                obligation_id=item.obligation_id,
                expansions=ledger.expansion_keys(item.obligation_id),
                open_child_obligation_ids=_open_children(network, item.occurrence_id),
            ),
            fuel_remaining=0,
        )
    registry.note_trial_use(chosen.method_ref, mission_id=network.mission_id)
    return RefinementDecision(
        item=item,
        outcome=RefinementOutcome.REFINED,
        reason=(
            f"refined with {chosen.method.method_id}@{chosen.method_ref.version}; "
            f"{fuel.remaining_fuel} fuel left on obligation {item.obligation_id!s}"
        ),
        draft=draft,
        chosen=chosen,
        candidates=assessed,
        fuel_remaining=fuel.remaining_fuel,
    )


def _no_method(
    item: FrontierItem,
    binding: TaskSemanticBindingV1,
    goal_type_ref: VersionedRef,
    assessed: Sequence[CandidateAssessment],
    *,
    catalog: TaskTypeCatalog,
    predicates: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    now_ms: int | None,
) -> RefinementDecision:
    """No selectable method: gather evidence if that could help, otherwise ask for one."""

    needing = [
        entry for entry in assessed if entry.report.status is ApplicabilityStatus.NEEDS_EVIDENCE
    ]
    if needing:
        unknowns: list[UnknownProposition] = []
        seen: set[str] = set()
        for entry in needing:
            for unknown in unknown_predicates(
                entry.method.applicable_when,
                parameters=binding.typed_parameters,
                predicates=predicates,
                snapshot=snapshot,
                now_ms=now_ms,
            ):
                if unknown.proposition_key in seen:
                    continue
                seen.add(unknown.proposition_key)
                unknowns.append(unknown)
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.NEEDS_EVIDENCE,
            reason=(
                f"{len(needing)} candidate method(s) rest on propositions nothing has "
                "observed yet; gather evidence rather than guess (ADR-07)"
            ),
            candidates=tuple(assessed),
            evidence=evidence_requests(
                unknowns,
                for_occurrence=item.occurrence_id,
                obligation_id=item.obligation_id,
                catalog=catalog,
                semantic_scope=binding.semantic_scope,
                contract_revision=int(binding.contract_revision),
            ),
        )
    unauthorized = [
        entry
        for entry in assessed
        if entry.report.status is ApplicabilityStatus.APPLICABLE and not entry.selectable
    ]
    if unauthorized:
        return RefinementDecision(
            item=item,
            outcome=RefinementOutcome.NOT_AUTHORIZED,
            reason=(
                "every applicable method's TRUE rests on a constant or an unresolved leaf; "
                "an empty or unsupported expression never opens a gate (§6.6 rule 2)"
            ),
            candidates=tuple(assessed),
        )
    return RefinementDecision(
        item=item,
        outcome=RefinementOutcome.NO_APPLICABLE_METHOD,
        reason=(
            f"{len(assessed)} method(s) are offered for {goal_type_ref.id!r} and none applies here"
        ),
        candidates=tuple(assessed),
        proposal_request=MethodProposalRequest(
            task_id=item.task_id,
            occurrence_id=item.occurrence_id,
            obligation_id=item.obligation_id,
            goal_signature=binding.goal_signature,
            goal_type_ref=goal_type_ref,
            rejected=tuple(
                RejectedCandidate(
                    method_id=entry.candidate.method.method_id,
                    version=entry.candidate.method_ref.version,
                    status=entry.report.status,
                    reason=entry.reason,
                )
                for entry in assessed
            ),
            reason=(
                "no admitted method applies; a synthesiser may propose one, and it goes "
                "through the same admission protocol (§7.3)"
            ),
        ),
    )


def _task_type_for(binding: TaskSemanticBindingV1, catalog: TaskTypeCatalog) -> TaskTypeSpec | None:
    """The registered task type whose goal signature this binding carries."""

    matches = [
        spec
        for spec in catalog.task_types()
        if spec.form is binding.form
        and spec.goal_signature.signature_id == binding.goal_signature.signature_id
        and spec.goal_signature.version == binding.goal_signature.version
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    raise ContractError(
        f"goal signature {binding.goal_signature.signature_id!r} "
        f"v{binding.goal_signature.version} is claimed by "
        f"{len(matches)} registered task types; a signature names one type"
    )


def _ancestor_repeat(
    draft: MethodInstanceDraft,
    *,
    network: TaskNetworkSnapshot,
    ancestors: frozenset[OccurrenceId],
    world_snapshot_id: str,
) -> str | None:
    """§6.4: re-expanding an ancestor's goal with nothing changed is not progress.

    The obligation ledger's own key catches a repeat *within* one duty.  This
    catches the other shape: a recursive method whose child duty re-expands the
    parent's goal with the same parameters against the same world snapshot, which
    would otherwise recurse to the bound and report BOUND_REACHED as though the
    deployment were too small for the problem.
    """

    signature = (
        draft.method_ref.method_id,
        draft.parameters_digest(),
        world_snapshot_id,
    )
    for ancestor in sorted(ancestors, key=str):
        instance = network.adopted_instance_for(ancestor)
        if instance is None:
            continue
        if (
            instance.method_ref.method_id,
            instance.parameters_digest(),
            instance.world_snapshot_id or world_snapshot_id,
        ) == signature:
            return (
                f"occurrence {ancestor!s} above this one already expanded "
                f"{draft.method_ref.method_id!r} with the same parameters against the same "
                "world snapshot; expanding it again changes no state (§6.4)"
            )
    return None


def _open_children(
    network: TaskNetworkSnapshot, occurrence_id: OccurrenceId
) -> tuple[ObligationId, ...]:
    """The duties already opened beneath this occurrence, for a BOUND_REACHED report."""

    out: list[ObligationId] = []
    pending = [occurrence_id]
    seen: set[OccurrenceId] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for child in network.adopted_children(current):
            spec = network.occurrence(child.occurrence_id)
            if spec.obligation_id not in out:
                out.append(spec.obligation_id)
            pending.append(child.occurrence_id)
    return tuple(out)


def bounded_attempt_admissible(spec: TaskTypeSpec) -> AttemptPolicy:
    """§7.2: the ADaPT try-first rule, stated as one function.

    A high-risk irreversible action never qualifies, whatever its cost estimate
    says: deciding by trying is exactly what must not happen to a send, a payment
    or an external state write.
    """

    return (
        AttemptPolicy.BOUNDED_ATTEMPT_FIRST if spec.low_risk else AttemptPolicy.PLAN_BEFORE_ATTEMPT
    )


__all__ = (
    "AttemptPolicy",
    "CandidateAssessment",
    "EvidenceOccurrenceRequest",
    "FrontierItem",
    "LeafDecision",
    "MethodProposalRequest",
    "RefinementDecision",
    "RefinementOutcome",
    "RefinementReport",
    "RejectedCandidate",
    "UnknownProposition",
    "assess_candidates",
    "bounded_attempt_admissible",
    "evidence_requests",
    "leaf_decision",
    "planning_frontier",
    "refine",
    "unknown_predicates",
)
