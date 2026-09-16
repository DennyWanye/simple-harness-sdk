# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Readiness as a pure judgement, and the one gate that builds a dispatch receipt (P2.1c).

Annex TG §8 replaces "every dependency is COMPLETED" with three separate sets and
one structured verdict:

``PlanningFrontier``
    work the *planner* owes: compound tasks still to be refined, preconditions
    still to be witnessed, approvals still to be obtained.
``ExecutionFrontier``
    primitive occurrences of the currently adopted plan whose semantic
    dependencies are met — candidates, not permissions.
``AdmittedDispatch``
    what the dispatch transaction admits after re-checking identity, budget and
    physical capacity.  **Not computed here**: this module never reads a budget,
    a lease or a resource pool (plan §23, P2.1c: "不接 allocator；不调账本").

:func:`evaluate_readiness` returns one :class:`ReadinessReason` out of twelve, and
the twelve never collapse into "the task failed".  TG §8.2 is explicit that a
missing input, an unreachable observation service and an external operation whose
effect is UNKNOWN are three different answers needing three different responses —
wait for a producer, retry an observation, reconcile an ``OperationId``.

Two refusals in particular are load-bearing:

* a ``form=compound`` task is intercepted by its *form*, never by a status
  string.  Plan §18.5 hard constraint 4 says the legacy ``TaskStatus.READY`` is a
  rebuildable display index and may not walk a compound into the Worker path, so
  :func:`evaluate_readiness` answers ``NEEDS_REFINEMENT`` for a compound whatever
  ``TaskView.legacy_status`` says, and :func:`legacy_ready_is_not_eligibility`
  spells the same rule out for the allocator wiring in P2.3c;
* a missing :class:`~..contracts.htn.TaskSemanticBindingV1` is ``GRAPH_INTEGRITY``.
  §18.5: a new-mode Task without a semantic binding is corruption, not a legacy
  fallback, and corruption does not silently degrade into "dispatch it anyway".

Everything here is a pure function of values the caller passes in.  There is no
store, no transaction, no model call and no ledger write; the read-set a report
carries is what makes a cached verdict checkable later (:func:`stale_after`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn, SupportsIndex

from ..artifacts.input_bindings import (
    InputManifest,
    ResolutionProblemKind,
    ResolutionResult,
)
from ..contracts.evidence_state import (
    Availability,
    PreconditionPhase,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
    phase_check_points,
)
from ..contracts.htn import (
    AbsenceRead,
    ContractRevision,
    DispatchGeneration,
    InputBindingRevision,
    MethodInstanceId,
    MissionRef,
    ObligationId,
    OccurrenceId,
    PlanRevision,
    ReadItem,
    ReadItemKind,
    ReleaseCondition,
    ScopeEpochRead,
    SemanticReadSet,
    SupportSetRead,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from ..contracts.models import ContractError
from ..contracts.obligations import ObligationAccountView, ObligationLifecycle
from ..contracts.resolution import (
    ApprovalDecision,
    ApprovalState,
    EffectOutcome,
    OperationCurrentState,
    OperationEnvelope,
)
from ..contracts.semantic_base import (
    TypedRefKind,
    content_hash_of,
    flag,
    identifier,
    index,
)
from .projection_validation import GraphIntegrityError
from .task_network import TaskNetworkSnapshot

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


class ReadinessReason(StrEnum):
    """TG §8.2 / implementation annex §6: one value per class of answer.

    They are deliberately not ranked by severity and never merged.  "The producer
    has not finished" (``WAITING_DATA``), "we could not ask" (``OBSERVER_UNAVAILABLE``)
    and "we asked and the world is in an unknown state" (``WAITING_OPERATION_UNKNOWN``)
    lead to three different operator actions, so they stay three values.
    """

    NOT_SELECTED = "NOT_SELECTED"
    NEEDS_REFINEMENT = "NEEDS_REFINEMENT"
    WAITING_ORDER = "WAITING_ORDER"
    WAITING_DATA = "WAITING_DATA"
    WAITING_EVIDENCE = "WAITING_EVIDENCE"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    STALE_BINDING = "STALE_BINDING"
    READY_CANDIDATE = "READY_CANDIDATE"
    #: An external effect that is not settled yet and conflicts with this work.
    #: TG §11.5: keep reconciling the original ``OperationId`` and block the
    #: conflicting successors — do not re-send and do not call it a failure.
    WAITING_OPERATION_UNKNOWN = "WAITING_OPERATION_UNKNOWN"
    #: The observation service could not answer.  Not being able to read a fact is
    #: not the fact being false (AER §8.2 dimension 4).
    OBSERVER_UNAVAILABLE = "OBSERVER_UNAVAILABLE"
    #: The network itself is damaged (§18.5, TG §14.3).  Never a fallback.
    GRAPH_INTEGRITY = "GRAPH_INTEGRITY"
    #: §11.5: the witness's scope epoch moved, its deadline passed, or its support
    #: is no longer CURRENT.  Recompute within bounds; an old TRUE is not reusable.
    VALIDITY_RECHECK_PENDING = "VALIDITY_RECHECK_PENDING"


#: TG §8.1 layer 1: what the planner still owes.  ``GRAPH_INTEGRITY`` is not here
#: — a damaged graph is repaired, not planned around — and neither are the
#: reasons that are simply "someone else's work has not finished".
PLANNING_REASONS: frozenset[ReadinessReason] = frozenset(
    {
        ReadinessReason.NEEDS_REFINEMENT,
        ReadinessReason.WAITING_EVIDENCE,
        ReadinessReason.OBSERVER_UNAVAILABLE,
        ReadinessReason.VALIDITY_RECHECK_PENDING,
        ReadinessReason.WAITING_APPROVAL,
    }
)

#: TG §8.1 layer 2.
EXECUTION_REASONS: frozenset[ReadinessReason] = frozenset({ReadinessReason.READY_CANDIDATE})

#: TG §8.1 layer 3 is decided by the dispatch transaction, not by a reason code.
#: Empty by construction, and asserted empty by the suite: no readiness verdict is
#: by itself an admission (plan §24.1 decision 6).
ADMITTED_DISPATCH_REASONS: frozenset[ReadinessReason] = frozenset()


#: An effect nobody can yet call done or not done, so new work that conflicts with
#: it has to wait.  ``NOT_HANDED_OFF`` is absent on purpose: nothing left the
#: orchestrator, so there is no real-world effect to conflict with.  This is a
#: narrower question than :attr:`OperationCurrentState.settled`, which also asks
#: whether the *money* has stopped moving.
UNSETTLED_EFFECTS: frozenset[EffectOutcome] = frozenset(
    {EffectOutcome.PENDING, EffectOutcome.PARTIAL, EffectOutcome.UNKNOWN}
)


class OccurrenceOutcome(StrEnum):
    """What the world says about one occurrence, for the ORDER release test.

    A *reading*, like :class:`~.task_network.RelationRow` — the storable facts are
    ``Acceptance`` / ``GoalResolution`` / the Task state machine, and this is the
    projection of them that an ORDER edge needs.
    """

    RUNNING = "RUNNING"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    #: Terminal for some other reason (superseded, abandoned, externally closed).
    SETTLED_OTHER = "SETTLED_OTHER"
    #: We do not know how it ended.  TG decision 1: this never settles anything.
    UNKNOWN = "UNKNOWN"


_TERMINAL_OUTCOMES: frozenset[OccurrenceOutcome] = frozenset(
    {
        OccurrenceOutcome.ACCEPTED,
        OccurrenceOutcome.FAILED,
        OccurrenceOutcome.CANCELLED,
        OccurrenceOutcome.SETTLED_OTHER,
    }
)


class DispatchCandidacy(StrEnum):
    """Whether a *new* dispatch of this occurrence is wanted at all right now.

    Deliberately **not** ``contracts.resolution.CandidatePolicy``: that contract is
    §10.2's versioned "how many alternatives may one goal carry, and what does
    synthesis cost" record.  This is the other half of the implementation annex
    §6 condition "当前 Attempt/候选策略允许" — whether *this* occurrence wants
    another attempt right now.  The two share a phrase, not a meaning, so they
    keep separate names rather than one name with two jobs.
    """

    ALLOWS_DISPATCH = "ALLOWS_DISPATCH"
    RUNNING_WORK_EXISTS = "RUNNING_WORK_EXISTS"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    WITHDRAWN = "WITHDRAWN"


def order_released(outcome: OccurrenceOutcome, condition: ReleaseCondition) -> bool:
    """TG decision 1: an ORDER edge releases on *acceptance*, not on "it stopped".

    ``settled_terminal`` exists for clean-up and convergence contracts and is the
    only condition a FAILED or CANCELLED predecessor satisfies — and an UNKNOWN
    outcome satisfies neither, because "we do not know whether it happened" is not
    a settlement.
    """

    if outcome is OccurrenceOutcome.ACCEPTED:
        return True
    if outcome is OccurrenceOutcome.UNKNOWN:
        return False
    if condition is ReleaseCondition.SETTLED_TERMINAL:
        return outcome in _TERMINAL_OUTCOMES
    return False


def no_approval_required() -> ApprovalState:
    """The default approval state: this work needs none."""

    return ApprovalState(decision=ApprovalDecision.NOT_REQUIRED)


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskView:
    """The combined read of one occurrence: §18.5's strongly typed ``TaskView``.

    ``binding`` is optional only so this module can *report* a missing semantic
    binding as ``GRAPH_INTEGRITY`` instead of raising somewhere a caller would be
    tempted to catch and ignore.  ``legacy_status`` is carried for diagnostics and
    is never read by a gate.
    """

    occurrence_id: OccurrenceId
    task_id: TaskRef
    binding: TaskSemanticBindingV1 | None = None
    legacy_status: str = ""
    approval: ApprovalState = field(default_factory=no_approval_required)
    #: The version of the authority record ``approval`` was read at, so a changed
    #: approval invalidates a cached readiness (:func:`stale_after`).
    approval_revision: int = 0
    dispatch_candidacy: DispatchCandidacy = DispatchCandidacy.ALLOWS_DISPATCH

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "occurrence_id",
            OccurrenceId(identifier(self.occurrence_id, "view.occurrence_id")),
        )
        object.__setattr__(self, "task_id", TaskRef(identifier(self.task_id, "view.task_id")))
        if self.binding is not None and not isinstance(self.binding, TaskSemanticBindingV1):
            raise ContractError("view.binding must be a TaskSemanticBindingV1 or None")
        if not isinstance(self.legacy_status, str):
            raise ContractError("view.legacy_status must be a string")
        if not isinstance(self.approval, ApprovalState):
            raise ContractError("view.approval must be a contracts.resolution.ApprovalState")
        object.__setattr__(
            self, "approval_revision", index(self.approval_revision, "view.approval_revision")
        )


@dataclass(frozen=True, slots=True)
class ActivePlanView:
    """The frozen read of the active plan revision this judgement is made against.

    The maps are deliberately *authoritative current values*, not copies of what
    the binding says: comparing the binding's ``dispatch_generation`` against the
    plan's tells the caller the cached binding expired (``STALE_BINDING``), which
    is the whole point of TG §11.5's "revoke the old generation's eligibility".
    """

    snapshot: TaskNetworkSnapshot
    mission_admits_work: bool = True
    requirements_revision: int = 0
    manager_epoch: int = 0
    budget_grant_revision: int = 0
    scope_epochs: Mapping[str, int] = field(default_factory=dict)
    dispatch_generations: Mapping[OccurrenceId, int] = field(default_factory=dict)
    input_binding_revisions: Mapping[TaskRef, int] = field(default_factory=dict)
    obligation_accounts: Mapping[ObligationId, ObligationAccountView] = field(default_factory=dict)
    #: Set by the caller when :func:`..projection_validation.require_topological_order`
    #: refused the projection.  Present here so a damaged graph stops *every*
    #: dispatch in the scope rather than only the task that happens to be asked about.
    integrity_error: GraphIntegrityError | None = None
    _projected: frozenset[OccurrenceId] = field(
        default_factory=frozenset, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, TaskNetworkSnapshot):
            raise ContractError("plan.snapshot must be a TaskNetworkSnapshot")
        object.__setattr__(
            self, "mission_admits_work", flag(self.mission_admits_work, "plan.mission_admits_work")
        )
        for name in ("requirements_revision", "manager_epoch", "budget_grant_revision"):
            object.__setattr__(self, name, index(getattr(self, name), f"plan.{name}"))
        object.__setattr__(
            self, "_projected", self.snapshot.execution_projection().projected_occurrences
        )

    @property
    def mission_id(self) -> MissionRef:
        return self.snapshot.mission_id

    @property
    def plan_revision(self) -> PlanRevision:
        return self.snapshot.plan_revision

    @property
    def adopted_occurrences(self) -> frozenset[OccurrenceId]:
        """The occurrences the *adopted* methods reach from the roots (TG §6)."""

        return self._projected

    def is_adopted(self, occurrence: OccurrenceId) -> bool:
        return occurrence in self._projected


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """One real-world operation whose effect readiness has to take into account.

    The frozen semantics and the mutable control record stay apart exactly as
    AER §12.2 requires: :class:`OperationEnvelope` says *what* was requested and
    :class:`OperationCurrentState` says where it has got to.
    """

    envelope: OperationEnvelope
    state: OperationCurrentState
    #: The occurrences this operation's unsettled effect conflicts with.  Empty
    #: means "nothing here waits on it"; the conflict analysis itself belongs to
    #: the operation layer, not to a readiness gate.
    conflicts_with: frozenset[OccurrenceId] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, OperationEnvelope):
            raise ContractError("pending_operation.envelope must be an OperationEnvelope")
        if not isinstance(self.state, OperationCurrentState):
            raise ContractError("pending_operation.state must be an OperationCurrentState")
        if self.state.operation_id != self.envelope.operation_id:
            raise ContractError(
                "pending_operation.state describes another operation than its envelope"
            )
        object.__setattr__(self, "conflicts_with", frozenset(self.conflicts_with))

    @property
    def effect_outcome(self) -> EffectOutcome:
        return self.state.effect_outcome

    @property
    def effect_settled(self) -> bool:
        """Whether the *effect* has stopped moving.  Says nothing about the money."""

        return self.effect_outcome not in UNSETTLED_EFFECTS


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """The facts side of the judgement: witnesses, observability, live effects.

    ``witnesses`` is keyed by ``PreconditionRef.condition_digest`` so a witness
    cannot be attached to a precondition it was not computed for.
    """

    witnesses: Mapping[str, ValidityWitness] = field(default_factory=dict)
    #: False when the observation service could not be reached at all.  It is a
    #: separate answer from "the fact is unknown" (AER §8.2).
    observer_available: bool = True
    pending_operations: tuple[PendingOperation, ...] = ()
    support_sets: tuple[SupportSetRead, ...] = ()
    #: TG §11.2: "there is no conflicting writer" is itself a read, over a range
    #: that has a version.  Recorded in the report's read-set as an ``AbsenceRead``.
    operation_range_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observer_available",
            flag(self.observer_available, "evidence.observer_available"),
        )
        object.__setattr__(
            self,
            "operation_range_revision",
            index(self.operation_range_revision, "evidence.operation_range_revision"),
        )
        object.__setattr__(self, "pending_operations", tuple(self.pending_operations))
        object.__setattr__(self, "support_sets", tuple(self.support_sets))


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadinessDetail:
    """One concrete thing the gate found.  ``code`` is stable; ``message`` is prose."""

    code: str
    subject: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """The structured verdict.  It changes nothing and authorises nothing.

    ``mission_id`` and ``plan_revision`` name the plan the verdict was computed
    against, so :func:`admit_for_dispatch` can refuse a report and a plan view
    that did not come from the same read.
    """

    reason: ReadinessReason
    occurrence_id: OccurrenceId
    task_id: TaskRef
    read_set: SemanticReadSet
    mission_id: MissionRef
    plan_revision: PlanRevision
    details: tuple[ReadinessDetail, ...] = ()
    evaluated_at_ms: int = 0

    @property
    def ready(self) -> bool:
        return self.reason is ReadinessReason.READY_CANDIDATE

    @property
    def detail_codes(self) -> tuple[str, ...]:
        return tuple(detail.code for detail in self.details)

    @property
    def data_problem_kinds(self) -> frozenset[ResolutionProblemKind]:
        """The ``input_bindings`` refusal kinds this report is carrying, unmerged."""

        kinds: set[ResolutionProblemKind] = set()
        for detail in self.details:
            try:
                kinds.add(ResolutionProblemKind(detail.code))
            except ValueError:
                continue
        return frozenset(kinds)


# --------------------------------------------------------------------------------------
# The gates.  Each returns ``(reason, details)`` or ``None``.
# --------------------------------------------------------------------------------------

_GateResult = tuple[ReadinessReason, tuple[ReadinessDetail, ...]] | None


@dataclass(frozen=True, slots=True)
class _Context:
    view: TaskView
    plan: ActivePlanView
    resolutions: Mapping[OccurrenceId, OccurrenceOutcome]
    evidence: EvidenceView
    input_result: ResolutionResult | None
    now_ms: int


def _refused(reason: ReadinessReason, *details: ReadinessDetail) -> _GateResult:
    return (reason, tuple(details))


def _integrity_gate(context: _Context) -> _GateResult:
    """§18.5 / TG §14.3: corruption is reported as corruption."""

    plan = context.plan
    view = context.view
    if plan.integrity_error is not None:
        return _refused(
            ReadinessReason.GRAPH_INTEGRITY,
            ReadinessDetail(
                code="projection_not_orderable",
                subject=str(plan.mission_id),
                message=plan.integrity_error.diagnose(),
            ),
        )
    try:
        spec = plan.snapshot.occurrence(view.occurrence_id)
    except KeyError:
        return _refused(
            ReadinessReason.GRAPH_INTEGRITY,
            ReadinessDetail(
                code="unknown_occurrence",
                subject=str(view.occurrence_id),
                message="this plan revision does not contain the occurrence being judged",
            ),
        )
    if view.binding is None:
        return _refused(
            ReadinessReason.GRAPH_INTEGRITY,
            ReadinessDetail(
                code="semantic_binding_missing",
                subject=str(view.task_id),
                message=(
                    "a new-mode task without a TaskSemanticBindingV1 is corruption, "
                    "not a legacy fallback (§18.5)"
                ),
            ),
        )
    if view.binding.task_id != view.task_id or spec.task_id != view.task_id:
        return _refused(
            ReadinessReason.GRAPH_INTEGRITY,
            ReadinessDetail(
                code="binding_task_mismatch",
                subject=str(view.task_id),
                message=(
                    f"occurrence {spec.occurrence_id!s} names task {spec.task_id!s} and the "
                    f"binding names {view.binding.task_id!s}"
                ),
            ),
        )
    if spec.form is not view.binding.form:
        return _refused(
            ReadinessReason.GRAPH_INTEGRITY,
            ReadinessDetail(
                code="form_mismatch",
                subject=str(view.task_id),
                message=f"occurrence says {spec.form!s}, binding says {view.binding.form!s}",
            ),
        )
    return None


def _refinement_gate(context: _Context) -> _GateResult:
    """Plan §18.5 hard constraint 4: the gate is ``form``, not a status string."""

    binding = context.view.binding
    if binding is None or binding.form is not TaskForm.COMPOUND:
        return None
    return _refused(
        ReadinessReason.NEEDS_REFINEMENT,
        ReadinessDetail(
            code="form_compound",
            subject=str(binding.task_id),
            message=(
                "a compound task is refined by a MethodInstance, never dispatched to a "
                f"Worker; the legacy status {context.view.legacy_status!r} is only a "
                "rebuildable display index (§18.5 hard constraint 4)"
            ),
        ),
    )


def _selection_gate(context: _Context) -> _GateResult:
    """Implementation annex §6: the mission must allow work and the duty must be live.

    A duty with **no account record at all** is refused rather than waved through:
    no account means no admitted demand, and "we could not find the ledger entry"
    is not evidence that someone asked for this work.
    """

    plan = context.plan
    view = context.view
    if not plan.mission_admits_work:
        return _refused(
            ReadinessReason.NOT_SELECTED,
            ReadinessDetail(
                code="mission_does_not_admit_work",
                subject=str(plan.mission_id),
                message="the mission is not accepting new work in this revision",
            ),
        )
    if not plan.is_adopted(view.occurrence_id):
        return _refused(
            ReadinessReason.NOT_SELECTED,
            ReadinessDetail(
                code="not_in_adopted_plan",
                subject=str(view.occurrence_id),
                message=(
                    "the occurrence is not reachable from a root through the adopted "
                    "methods of this plan revision"
                ),
            ),
        )
    binding = view.binding
    if binding is not None:
        record = plan.obligation_accounts.get(binding.obligation_id)
        if record is None:
            return _refused(
                ReadinessReason.NOT_SELECTED,
                ReadinessDetail(
                    code="obligation_account_missing",
                    subject=str(binding.obligation_id),
                    message=(
                        "no account record was read for this duty; no account means no "
                        "admitted demand, and a missing ledger entry is not a permission"
                    ),
                ),
            )
        if record.lifecycle is not ObligationLifecycle.UNSATISFIED:
            return _refused(
                ReadinessReason.NOT_SELECTED,
                ReadinessDetail(
                    code=f"obligation_{record.lifecycle!s}",
                    subject=str(binding.obligation_id),
                    message="the duty behind this occurrence is no longer outstanding",
                ),
            )
        if not record.has_admitted_demand:
            return _refused(
                ReadinessReason.NOT_SELECTED,
                ReadinessDetail(
                    code="obligation_demand_not_admitted",
                    subject=str(binding.obligation_id),
                    message=(
                        "the duty is outstanding but no demand for it is currently "
                        "admitted (TG decision 9)"
                    ),
                ),
            )
    if view.dispatch_candidacy is not DispatchCandidacy.ALLOWS_DISPATCH:
        return _refused(
            ReadinessReason.NOT_SELECTED,
            ReadinessDetail(
                code=f"dispatch_candidacy_{view.dispatch_candidacy!s}",
                subject=str(view.occurrence_id),
                message="the current attempt / candidate policy does not want a new dispatch",
            ),
        )
    return None


def _order_gate(context: _Context) -> _GateResult:
    """TG decision 1: every ORDER predecessor must have reached its release condition."""

    problems: list[ReadinessDetail] = []
    for constraint in context.plan.snapshot.order_constraints:
        if constraint.after != context.view.occurrence_id:
            continue
        outcome = context.resolutions.get(constraint.before)
        if outcome is None:
            problems.append(
                ReadinessDetail(
                    code="order_outcome_unobserved",
                    subject=str(constraint.before),
                    message=(
                        "no outcome has been observed for the predecessor; an unobserved "
                        "predecessor is waiting, not released"
                    ),
                )
            )
            continue
        if order_released(outcome, constraint.release_condition):
            continue
        code = (
            "order_outcome_unknown"
            if outcome is OccurrenceOutcome.UNKNOWN
            else "order_not_released"
        )
        problems.append(
            ReadinessDetail(
                code=code,
                subject=str(constraint.before),
                message=(
                    f"predecessor outcome {outcome!s} does not satisfy release condition "
                    f"{constraint.release_condition!s}"
                ),
            )
        )
    if problems:
        return (ReadinessReason.WAITING_ORDER, tuple(problems))
    return None


def _data_gate(context: _Context) -> _GateResult:
    """TG §4.3: every declared input resolved to one disclosable, accepted identity."""

    binding = context.view.binding
    if binding is None:
        return None
    result = context.input_result
    if result is None:
        return _refused(
            ReadinessReason.WAITING_DATA,
            ReadinessDetail(
                code="input_resolution_missing",
                subject=str(binding.task_id),
                message=(
                    "no input resolution was supplied; a missing input is its own answer, "
                    "not a task failure (TG §8.2)"
                ),
            ),
        )
    problems = [
        ReadinessDetail(
            code=str(problem.kind),
            subject=problem.input_port or ",".join(problem.input_ports),
            message=problem.detail,
        )
        for problem in result.problems
    ]
    manifest = result.manifest
    if manifest is None:
        problems.append(
            ReadinessDetail(
                code="manifest_absent",
                subject=str(binding.task_id),
                message="the resolver produced no manifest for this consumer",
            )
        )
    else:
        if not manifest.is_frozen:
            problems.append(
                ReadinessDetail(
                    code="manifest_not_frozen",
                    subject=str(binding.task_id),
                    message=(
                        f"{len(manifest.pending)} input(s) are still symbolic, so the "
                        "dispatch has no identity to hash (implementation annex §4.3)"
                    ),
                )
            )
        if not manifest.required_ports_satisfied(binding):
            problems.append(
                ReadinessDetail(
                    code="required_port_not_firmly_bound",
                    subject=str(binding.task_id),
                    message=(
                        "a required input port has only a provisional binding; a "
                        "provisional input may run but never claims DATA is met (TG §4.3)"
                    ),
                )
            )
    if problems:
        return (ReadinessReason.WAITING_DATA, tuple(problems))
    return None


def _freshness_code(witness: ValidityWitness, current_epoch: int, now_ms: int) -> str:
    """Which half of :meth:`ValidityWitness.is_fresh_for` said no — for the detail only."""

    if witness.scope_epoch != current_epoch:
        return "witness_epoch_stale"
    if witness.not_after_ms is not None and now_ms >= witness.not_after_ms:
        return "witness_deadline_passed"
    if witness.freshness is not Validity.CURRENT:
        return f"witness_freshness_{witness.freshness!s}"
    return "witness_not_fresh"


def _witness_verdict(
    witness: ValidityWitness | None,
    digest: str,
    *,
    consumer_task: TaskRef,
    scope_epochs: Mapping[str, int],
    now_ms: int,
) -> tuple[ReadinessReason, ReadinessDetail] | None:
    """§11.5 / AER §8.1: is *this* witness permission for *this* task to start, now?

    The checks follow ``artifacts.input_bindings._check_witness`` so a witness that
    would be refused when binding an input is refused here too: purpose, then the
    consumer it was issued to, then the epoch barrier via the contract's own
    :meth:`ValidityWitness.is_fresh_for` (epoch, deadline and freshness together),
    then readability, then what it actually concluded.  The epoch barrier comes
    before truth and decision because §11.5 requires checking it *before* reusing a
    cached conclusion — until it is recomputed, the cached verdict says nothing.
    """

    if witness is None:
        return (
            ReadinessReason.WAITING_EVIDENCE,
            ReadinessDetail(
                code="witness_missing",
                subject=digest,
                message="no ValidityWitness was supplied for this START precondition",
            ),
        )
    if witness.purpose is not WitnessPurpose.START:
        return (
            ReadinessReason.WAITING_EVIDENCE,
            ReadinessDetail(
                code="witness_purpose_not_start",
                subject=digest,
                message=(
                    f"the witness was issued for {witness.purpose!s}; a dispatch consumes a "
                    "purpose=START witness and a witness is not a transferable token (§11.5)"
                ),
            ),
        )
    if witness.consumer_ref.kind is not TypedRefKind.TASK or witness.consumer_ref.id != str(
        consumer_task
    ):
        return (
            ReadinessReason.WAITING_EVIDENCE,
            ReadinessDetail(
                code="witness_consumer_mismatch",
                subject=witness.witness_id,
                message=(
                    f"the witness was issued to {witness.consumer_ref.kind!s} "
                    f"{witness.consumer_ref.id!r}, not to task {str(consumer_task)!r}; it is "
                    "that consumer's permission, not this one's (§11.5)"
                ),
            ),
        )
    current_epoch = scope_epochs.get(witness.scope_id)
    if current_epoch is None:
        return (
            ReadinessReason.VALIDITY_RECHECK_PENDING,
            ReadinessDetail(
                code="witness_epoch_unknown",
                subject=witness.scope_id,
                message="this plan view cannot confirm the witness's scope epoch (I19)",
            ),
        )
    if not witness.is_fresh_for(now_ms=now_ms, current_scope_epoch=current_epoch):
        return (
            ReadinessReason.VALIDITY_RECHECK_PENDING,
            ReadinessDetail(
                code=_freshness_code(witness, current_epoch, now_ms),
                subject=witness.witness_id,
                message=(
                    f"the witness was taken at scope epoch {witness.scope_epoch} (now "
                    f"{current_epoch}), expires at {witness.not_after_ms} and is "
                    f"{witness.freshness!s}; recompute rather than reuse the old TRUE"
                ),
            ),
        )
    if (
        witness.decision is WitnessDecision.UNAVAILABLE
        or witness.availability is Availability.UNAVAILABLE
    ):
        return (
            ReadinessReason.OBSERVER_UNAVAILABLE,
            ReadinessDetail(
                code="witness_unavailable",
                subject=witness.witness_id,
                message="the source could not be read; that is not the proposition being false",
            ),
        )
    if witness.truth is not TruthValue.TRUE:
        return (
            ReadinessReason.WAITING_EVIDENCE,
            ReadinessDetail(
                code=f"witness_truth_{witness.truth!s}",
                subject=witness.witness_id,
                message="the admissible evidence does not support the precondition",
            ),
        )
    if witness.decision is not WitnessDecision.USABLE:
        return (
            ReadinessReason.WAITING_EVIDENCE,
            ReadinessDetail(
                code=f"witness_decision_{witness.decision!s}",
                subject=witness.witness_id,
                message="the witness is not usable for execution",
            ),
        )
    return None


#: Within the evidence gate the answers are ranked, so the verdict does not depend
#: on the order the preconditions happen to be declared in.
_EVIDENCE_PRECEDENCE: tuple[ReadinessReason, ...] = (
    ReadinessReason.OBSERVER_UNAVAILABLE,
    ReadinessReason.VALIDITY_RECHECK_PENDING,
    ReadinessReason.WAITING_EVIDENCE,
)


def start_preconditions(binding: TaskSemanticBindingV1) -> tuple[str, ...]:
    """§6.6 v1.3 / TG §9: the precondition digests that must hold *before* starting.

    An undeclared phase defaults to SELECT (and is re-checked at ACCEPT), so it is
    included; a precondition declared MAINTAIN or ACCEPT only is not a start gate.
    """

    return tuple(
        ref.condition_digest
        for ref in binding.precondition_refs
        if PreconditionPhase.SELECT in phase_check_points(ref.phase)
    )


def _evidence_gate(context: _Context) -> _GateResult:
    """TG §9 + §11.5: START preconditions, observability, and the epoch barrier."""

    evidence = context.evidence
    if not evidence.observer_available:
        return _refused(
            ReadinessReason.OBSERVER_UNAVAILABLE,
            ReadinessDetail(
                code="observation_service_unavailable",
                subject=str(context.view.occurrence_id),
                message=(
                    "the observation service could not be reached; readiness is unknown, "
                    "which is not the same as the task being blocked or failed (TG §8.2)"
                ),
            ),
        )
    binding = context.view.binding
    if binding is None:
        return None
    found: dict[ReadinessReason, list[ReadinessDetail]] = {}
    for digest in start_preconditions(binding):
        verdict = _witness_verdict(
            evidence.witnesses.get(digest),
            digest,
            consumer_task=binding.task_id,
            scope_epochs=context.plan.scope_epochs,
            now_ms=context.now_ms,
        )
        if verdict is None:
            continue
        reason, detail = verdict
        found.setdefault(reason, []).append(detail)
    for reason in _EVIDENCE_PRECEDENCE:
        if reason in found:
            return (reason, tuple(found[reason]))
    return None


def _approval_gate(context: _Context) -> _GateResult:
    """An approval that is pending, denied or expired is a state to wait on.

    ``ApprovalState.is_effective`` is the contract's own "approved *now*" test, so
    a grant whose ``expires_at_ms`` has passed is refused here rather than read as
    a standing permission (invariant I09).
    """

    approval = context.view.approval
    if approval.decision is ApprovalDecision.NOT_REQUIRED:
        return None
    if approval.is_effective(now_ms=context.now_ms):
        return None
    code = (
        "approval_expired_grant"
        if approval.decision is ApprovalDecision.GRANTED
        else f"approval_{approval.decision!s}"
    )
    return _refused(
        ReadinessReason.WAITING_APPROVAL,
        ReadinessDetail(
            code=code,
            subject=str(context.view.occurrence_id),
            message="the approval this work needs is not in hand right now",
        ),
    )


def _stale_gate(context: _Context) -> _GateResult:
    """TG §11.5: a re-plan revokes the old generation's eligibility."""

    binding = context.view.binding
    if binding is None:
        return None
    problems: list[ReadinessDetail] = []
    current_generation = context.plan.dispatch_generations.get(context.view.occurrence_id)
    if current_generation is not None and current_generation != int(binding.dispatch_generation):
        problems.append(
            ReadinessDetail(
                code="dispatch_generation_moved",
                subject=str(context.view.occurrence_id),
                message=(
                    f"the binding was read at generation {int(binding.dispatch_generation)}; "
                    f"the plan is at {current_generation}"
                ),
            )
        )
    current_revision = context.plan.input_binding_revisions.get(binding.task_id)
    if current_revision is not None and current_revision != int(binding.input_binding_revision):
        problems.append(
            ReadinessDetail(
                code="input_binding_revision_moved",
                subject=str(binding.task_id),
                message=(
                    f"the binding was read at input revision "
                    f"{int(binding.input_binding_revision)}; the plan is at {current_revision}"
                ),
            )
        )
    if problems:
        return (ReadinessReason.STALE_BINDING, tuple(problems))
    return None


def _operation_gate(context: _Context) -> _GateResult:
    """TG §11.5: block the successors of an unsettled effect; never re-send blindly."""

    problems = [
        ReadinessDetail(
            code=f"operation_{operation.effect_outcome!s}",
            subject=str(operation.envelope.operation_id),
            message=(
                "an external effect that is not settled conflicts with this work; keep "
                "reconciling the original OperationId (TG §11.5)"
            ),
        )
        for operation in context.evidence.pending_operations
        if not operation.effect_settled and context.view.occurrence_id in operation.conflicts_with
    ]
    if problems:
        return (ReadinessReason.WAITING_OPERATION_UNKNOWN, tuple(problems))
    return None


# --------------------------------------------------------------------------------------
# Gate order.  Declared once, and the precedence list is derived from it.
# --------------------------------------------------------------------------------------

#: Gate function names in the order they run, each with the reasons it can produce
#: (in *its* internal precedence order).  The names are resolved through the module
#: globals at call time, so a test may substitute one gate and see the difference.
_GATE_SEQUENCE: tuple[tuple[str, tuple[ReadinessReason, ...]], ...] = (
    ("_integrity_gate", (ReadinessReason.GRAPH_INTEGRITY,)),
    ("_refinement_gate", (ReadinessReason.NEEDS_REFINEMENT,)),
    ("_selection_gate", (ReadinessReason.NOT_SELECTED,)),
    ("_order_gate", (ReadinessReason.WAITING_ORDER,)),
    ("_data_gate", (ReadinessReason.WAITING_DATA,)),
    ("_evidence_gate", _EVIDENCE_PRECEDENCE),
    ("_approval_gate", (ReadinessReason.WAITING_APPROVAL,)),
    ("_stale_gate", (ReadinessReason.STALE_BINDING,)),
    ("_operation_gate", (ReadinessReason.WAITING_OPERATION_UNKNOWN,)),
)


def gate_precedence() -> tuple[ReadinessReason, ...]:
    """The reasons in the order the gates can actually produce them.

    :data:`READINESS_PRECEDENCE` is this, and the suite asserts the two agree — so
    the documented order cannot drift away from the order the code runs in.
    """

    return tuple(reason for _name, reasons in _GATE_SEQUENCE for reason in reasons) + (
        ReadinessReason.READY_CANDIDATE,
    )


#: The order the gates run in.  A report names the *first* gate that refused, so
#: the caller gets the most actionable answer rather than a list of everything
#: that happens to be wrong downstream of it.
READINESS_PRECEDENCE: tuple[ReadinessReason, ...] = gate_precedence()


# --------------------------------------------------------------------------------------
# The read-set
# --------------------------------------------------------------------------------------


def _method_read(plan: ActivePlanView, binding: TaskSemanticBindingV1) -> tuple[ReadItem, ...]:
    occurrence_binding = binding.occurrence_binding
    if occurrence_binding is None:
        return ()
    try:
        draft = plan.snapshot.instance(occurrence_binding.method_instance_id)
    except KeyError:
        return ()
    return (
        ReadItem(
            kind=ReadItemKind.METHOD,
            id=str(draft.instance_id),
            semantic_revision=int(draft.plan_revision),
            content_hash=content_hash_of(draft.to_json()),
        ),
    )


def _acceptance_reads(manifest: InputManifest | None) -> tuple[ReadItem, ...]:
    if manifest is None:
        return ()
    seen: dict[str, ReadItem] = {}
    for item in manifest.bindings:
        seen.setdefault(
            item.acceptance_id,
            ReadItem(
                kind=ReadItemKind.ACCEPTANCE,
                id=item.acceptance_id,
                semantic_revision=item.support_revision,
                content_hash=item.content_hash,
            ),
        )
    return tuple(seen[key] for key in sorted(seen))


def _task_control_reads(
    view: TaskView, plan: ActivePlanView, binding: TaskSemanticBindingV1
) -> tuple[ReadItem, ...]:
    """The two dispatch-control values the stale gate read, in the TASK lane.

    A :class:`~..contracts.htn.ReadItem` is ``(kind, id, semantic_revision,
    content_hash)`` with no sub-field, so one TASK entry can carry exactly one
    revision — and this task has three of them (contract, dispatch generation,
    input binding).  They are therefore kept apart by namespaced ids,
    ``<task>#dispatch_generation`` and ``<task>#input_binding_revision``, rather
    than by overwriting one another in a single entry.  Both are genuinely TASK
    reads; only the id spelling is a workaround.
    """

    task = str(binding.task_id)
    generation = plan.dispatch_generations.get(view.occurrence_id, int(binding.dispatch_generation))
    revision = plan.input_binding_revisions.get(
        binding.task_id, int(binding.input_binding_revision)
    )

    def channel(name: str, value: int) -> ReadItem:
        return ReadItem(
            kind=ReadItemKind.TASK,
            id=f"{task}#{name}",
            semantic_revision=value,
            content_hash=content_hash_of({"task": task, "channel": name, "value": value}),
        )

    return (
        channel("dispatch_generation", generation),
        channel("input_binding_revision", revision),
    )


def _obligation_reads(plan: ActivePlanView, binding: TaskSemanticBindingV1) -> tuple[ReadItem, ...]:
    """The duty the selection gate read, in its own OBLIGATION lane.

    ``ObligationAccountView`` carries no revision counter, so ``semantic_revision``
    stays 0 and the content hash is what moves: it covers the lifecycle *and*
    ``has_admitted_demand``, because a withdrawn demand and a cancelled duty are
    two different changes that must both invalidate a cached verdict.  A duty with
    no account record at all hashes to a distinct "absent" payload rather than to
    the same value as a live one.
    """

    account = plan.obligation_accounts.get(binding.obligation_id)
    return (
        ReadItem(
            kind=ReadItemKind.OBLIGATION,
            id=str(binding.obligation_id),
            semantic_revision=0,
            content_hash=content_hash_of(
                {
                    "lifecycle": None if account is None else str(account.lifecycle),
                    "has_admitted_demand": None if account is None else account.has_admitted_demand,
                }
            ),
        ),
    )


def _authority_reads(view: TaskView, binding: TaskSemanticBindingV1) -> tuple[ReadItem, ...]:
    """The approval the approval gate read, in its own AUTHORITY lane.

    The subject is the task the approval is about; ``semantic_revision`` is the
    authority record's version and the hash covers the decision, the grantor and
    the expiry — so a grant that merely expired is as much a change as one that
    was revoked.
    """

    return (
        ReadItem(
            kind=ReadItemKind.AUTHORITY,
            id=str(binding.task_id),
            semantic_revision=view.approval_revision,
            content_hash=content_hash_of(view.approval.to_json()),
        ),
    )


def build_read_set(
    view: TaskView,
    plan: ActivePlanView,
    evidence: EvidenceView,
    input_result: ResolutionResult | None,
) -> SemanticReadSet:
    """ADR-13 / TG §11.2: exactly what this judgement depended on, with versions.

    Besides the obvious contract, method, fact and acceptance revisions it records
    the four *control* values the gates read, each in the lane the contract gives
    it: dispatch generation and input binding revision in the TASK lane, the duty's
    lifecycle and admitted demand in ``obligation_revisions``, the approval in
    ``authority_revisions``.  A stale duty and a revoked authority fail a commit for
    different reasons, so they are re-read differently — and :func:`stale_after`
    now covers every axis this report claims to have checked.

    The absence entry is not decoration either: "there is no conflicting unsettled
    writer" is a read over a range, and without its range version a conflicting
    operation registered after the scan would leave the cached verdict looking
    current.
    """

    binding = view.binding
    goal_revisions: tuple[ReadItem, ...] = ()
    method_revisions: tuple[ReadItem, ...] = ()
    observation_revisions: tuple[ReadItem, ...] = ()
    obligation_revisions: tuple[ReadItem, ...] = ()
    authority_revisions: tuple[ReadItem, ...] = ()
    scope_ids: set[str] = set()
    absences: tuple[AbsenceRead, ...] = ()
    if binding is not None:
        goal_revisions = (
            ReadItem(
                kind=ReadItemKind.TASK,
                id=str(binding.task_id),
                semantic_revision=int(binding.contract_revision),
                content_hash=binding.contract_hash,
            ),
            *_task_control_reads(view, plan, binding),
        )
        method_revisions = _method_read(plan, binding)
        obligation_revisions = _obligation_reads(plan, binding)
        authority_revisions = _authority_reads(view, binding)
        facts: list[ReadItem] = []
        for digest in start_preconditions(binding):
            witness = evidence.witnesses.get(digest)
            if witness is None:
                continue
            facts.append(
                ReadItem(
                    kind=ReadItemKind.FACT,
                    id=witness.witness_id,
                    semantic_revision=witness.support_revision,
                    content_hash=content_hash_of(witness.to_json()),
                )
            )
            scope_ids.add(witness.scope_id)
        observation_revisions = tuple(sorted(facts, key=lambda item: item.id))
        scope_ids.add(binding.semantic_scope)
        absences = (
            AbsenceRead(
                predicate="conflicting_unsettled_operation",
                scope_id=binding.semantic_scope,
                range_revision=evidence.operation_range_revision,
            ),
        )
    return SemanticReadSet(
        requirements_revision=plan.requirements_revision,
        goal_revisions=goal_revisions,
        method_revisions=method_revisions,
        observation_revisions=observation_revisions,
        acceptance_revisions=_acceptance_reads(
            None if input_result is None else input_result.manifest
        ),
        obligation_revisions=obligation_revisions,
        authority_revisions=authority_revisions,
        manager_epoch=plan.manager_epoch,
        budget_grant_revision=plan.budget_grant_revision,
        support_sets=evidence.support_sets,
        scope_epochs=tuple(
            ScopeEpochRead(scope_id=scope, validity_epoch=plan.scope_epochs[scope])
            for scope in sorted(scope_ids)
            if scope in plan.scope_epochs
        ),
        absences=absences,
    )


# --------------------------------------------------------------------------------------
# evaluate_readiness
# --------------------------------------------------------------------------------------


def evaluate_readiness(
    view: TaskView,
    plan: ActivePlanView,
    resolutions: Mapping[OccurrenceId, OccurrenceOutcome],
    evidence: EvidenceView,
    input_result: ResolutionResult | None,
    *,
    now_ms: int,
) -> ReadinessReport:
    """TG §8.2: one structured reason, no model call, no ledger write, no state change.

    The gates run in :data:`_GATE_SEQUENCE` order — which is where
    :data:`READINESS_PRECEDENCE` comes from — and the first refusal wins, so the
    answer is the most actionable one rather than a pile of consequences.  Note
    that the compound interception runs *before* the selection gate: refusing to
    dispatch a compound is a safety property that must not depend on whether the
    occurrence is currently adopted.  Membership is applied by the frontier queries.
    """

    context = _Context(
        view=view,
        plan=plan,
        resolutions=resolutions,
        evidence=evidence,
        input_result=input_result,
        now_ms=index(now_ms, "now_ms"),
    )
    read_set = build_read_set(view, plan, evidence, input_result)

    def report(reason: ReadinessReason, details: tuple[ReadinessDetail, ...]) -> ReadinessReport:
        return ReadinessReport(
            reason=reason,
            occurrence_id=view.occurrence_id,
            task_id=view.task_id,
            read_set=read_set,
            mission_id=plan.mission_id,
            plan_revision=plan.plan_revision,
            details=details,
            evaluated_at_ms=context.now_ms,
        )

    scope = globals()
    for name, _reasons in _GATE_SEQUENCE:
        outcome = scope[name](context)
        if outcome is None:
            continue
        reason, details = outcome
        return report(reason, details)
    return report(ReadinessReason.READY_CANDIDATE, ())


# --------------------------------------------------------------------------------------
# Cache invalidation
# --------------------------------------------------------------------------------------


def _items_changed(recorded: Sequence[ReadItem], current: Sequence[ReadItem]) -> bool:
    by_id = {(item.kind, item.id): item for item in current}
    for item in recorded:
        observed = by_id.get((item.kind, item.id))
        if observed is None:
            return True
        if observed.semantic_revision != item.semantic_revision:
            return True
        if observed.content_hash != item.content_hash:
            return True
    return False


def stale_after(report: ReadinessReport, current_versions: SemanticReadSet) -> bool:
    """Implementation annex §6: a cached readiness dies when any read-set item moves.

    Only the items the report actually recorded are compared, and a *wider* current
    observation is not by itself a change — but an item that vanished is, because
    "it is no longer there" is exactly the kind of change a cached TRUE must not
    survive.  The admission transaction re-verifies these versions anyway; this
    function is what lets a listing page refuse to trust itself in the meantime.
    """

    recorded = report.read_set
    if recorded.requirements_revision != current_versions.requirements_revision:
        return True
    if recorded.manager_epoch != current_versions.manager_epoch:
        return True
    if recorded.budget_grant_revision != current_versions.budget_grant_revision:
        return True
    for left, right in (
        (recorded.goal_revisions, current_versions.goal_revisions),
        (recorded.method_revisions, current_versions.method_revisions),
        (recorded.observation_revisions, current_versions.observation_revisions),
        (recorded.acceptance_revisions, current_versions.acceptance_revisions),
        (recorded.obligation_revisions, current_versions.obligation_revisions),
        (recorded.authority_revisions, current_versions.authority_revisions),
    ):
        if _items_changed(left, right):
            return True
    supports = {item.support_set_id: item for item in current_versions.support_sets}
    for support in recorded.support_sets:
        observed = supports.get(support.support_set_id)
        if observed is None:
            return True
        if (observed.revision, observed.member_digest) != (
            support.revision,
            support.member_digest,
        ):
            return True
    epochs = {item.scope_id: item.validity_epoch for item in current_versions.scope_epochs}
    for scope in recorded.scope_epochs:
        if epochs.get(scope.scope_id) != scope.validity_epoch:
            return True
    ranges = {
        (item.predicate, item.scope_id): item.range_revision for item in current_versions.absences
    }
    for absence in recorded.absences:
        if ranges.get((absence.predicate, absence.scope_id)) != absence.range_revision:
            return True
    return False


# --------------------------------------------------------------------------------------
# The controlled constructor
# --------------------------------------------------------------------------------------


class NotEligible(RuntimeError):
    """:func:`admit_for_dispatch` was called on something that is not a ready candidate."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class EligibilityGateBypassed(RuntimeError):
    """Someone tried to obtain an :class:`EligiblePrimitiveTask` around the gate.

    Plan §18.5: the Scheduler's new-mode input is this type, and the old
    ``allocate()`` READY judgement may not walk around it.  A type anybody can
    instantiate — or copy, replace a field on, or round-trip through pickle — is
    not a gate, so every one of those paths is refused.
    """


_ADMISSION_TOKEN = object()

#: True only while :func:`admit_for_dispatch` is building the one record it was
#: asked for.  A ``ContextVar`` rather than a module global so concurrent tasks and
#: threads cannot see one another's admission window.
_ADMITTING: ContextVar[bool] = ContextVar("eligibility_admitting", default=False)


@dataclass(frozen=True, slots=True)
class EligiblePrimitiveTask:
    """TG §8.3: the frozen identity of one dispatch intent.

    It binds the task contract revision and hash, the local dispatch generation,
    the input manifest hash, the adopted method instance, the declared requirement
    ids, the acceptance ids the inputs came from, and the semantic read-set the
    verdict was computed over — so the admission transaction can re-verify every
    one of them instead of trusting a list page from a few minutes ago.

    **This is not a security token.**  It records that a controlled check passed at
    ``admitted_at_ms``; it grants nothing.  Permissions, preconditions and external
    object versions all change, so the dispatch transaction re-checks the adopted
    relation, identity, quota and physical capacity, and the tool / Provider handoff
    re-checks the live ones again before anything leaves the process (TG §8.3, plan
    §24.1 decision 6).

    Because it is not a capability it must also not be *derivable* from one: the
    admission mark is not an init field, so it cannot be passed to the constructor,
    and ``replace`` / ``copy`` / ``pickle`` all refuse rather than hand back a
    record carrying an admission nobody checked.
    """

    mission_id: MissionRef
    plan_revision: PlanRevision
    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId
    contract_revision: ContractRevision
    contract_hash: str
    dispatch_generation: DispatchGeneration
    input_binding_revision: InputBindingRevision
    input_manifest_hash: str
    method_instance_id: MethodInstanceId | None
    requirement_refs: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    read_set: SemanticReadSet
    admitted_at_ms: int
    #: Written by :func:`admit_for_dispatch` after construction.  ``init=False`` so
    #: no caller can supply it and no ``replace`` can carry it across.
    _token: Any = field(init=False, default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _ADMITTING.get():
            raise EligibilityGateBypassed(
                "an EligiblePrimitiveTask is built only by admit_for_dispatch() from a "
                "READY_CANDIDATE report (plan §18.5)"
            )

    @property
    def gate_passed(self) -> bool:
        """Whether the admission mark is the one this module hands out."""

        return self._token is _ADMISSION_TOKEN

    def _refuse(self, operation: str) -> NoReturn:
        raise EligibilityGateBypassed(
            f"{operation} would produce an EligiblePrimitiveTask that no gate admitted; "
            "re-run evaluate_readiness() and admit_for_dispatch() instead (plan §18.5)"
        )

    # ``dataclasses.replace`` delegates here on Python 3.13+; on 3.11/3.12 it calls
    # the constructor, which the ``_ADMITTING`` guard refuses.  Both paths raise the
    # same error so the refusal does not depend on the interpreter version.
    def __replace__(self, **changes: object) -> NoReturn:
        self._refuse("replace()")

    def __copy__(self) -> NoReturn:
        self._refuse("copy()")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        self._refuse("deepcopy()")

    def __reduce__(self) -> NoReturn:
        self._refuse("pickling")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        self._refuse("pickling")


def _same_origin(report: ReadinessReport, plan: ActivePlanView) -> None:
    """Refuse a report and a plan view that did not come from the same read.

    Admitting a dispatch on a verdict computed against a different plan revision or
    a different requirements revision is how a re-plan gets executed with the old
    answer.  The check is cheap and the mismatch is always a caller bug.
    """

    mismatches: list[str] = []
    if report.mission_id != plan.mission_id:
        mismatches.append(f"mission {report.mission_id!s} vs {plan.mission_id!s}")
    if int(report.plan_revision) != int(plan.plan_revision):
        mismatches.append(f"plan revision {int(report.plan_revision)} vs {int(plan.plan_revision)}")
    read_set = report.read_set
    for label, recorded, current in (
        ("requirements_revision", read_set.requirements_revision, plan.requirements_revision),
        ("manager_epoch", read_set.manager_epoch, plan.manager_epoch),
        ("budget_grant_revision", read_set.budget_grant_revision, plan.budget_grant_revision),
    ):
        if recorded != current:
            mismatches.append(f"{label} {recorded} vs {current}")
    for scope in read_set.scope_epochs:
        current_epoch = plan.scope_epochs.get(scope.scope_id)
        if current_epoch != scope.validity_epoch:
            mismatches.append(
                f"scope {scope.scope_id} epoch {scope.validity_epoch} vs {current_epoch}"
            )
    if mismatches:
        raise NotEligible(
            "the readiness report and the plan view are not from the same read: "
            + "; ".join(mismatches)
        )


def admit_for_dispatch(
    report: ReadinessReport,
    view: TaskView,
    plan: ActivePlanView,
    manifest: InputManifest,
    *,
    now_ms: int,
) -> EligiblePrimitiveTask:
    """The only way to build an :class:`EligiblePrimitiveTask`.

    Raises :class:`NotEligible` when the report is not ``READY_CANDIDATE``, when it
    judged another occurrence, when it was not computed against *this* plan view,
    or when the manifest belongs to another consumer, and lets
    :class:`~..artifacts.input_bindings.ManifestNotFrozen` propagate when an input
    is still symbolic — a dispatch with an unresolved input has no identity an
    ``Acceptance`` could quote.
    """

    if report.reason is not ReadinessReason.READY_CANDIDATE:
        raise NotEligible(f"readiness is {report.reason!s}; only READY_CANDIDATE may be admitted")
    if report.occurrence_id != view.occurrence_id or report.task_id != view.task_id:
        raise NotEligible(f"the report judged {report.occurrence_id!s}, not {view.occurrence_id!s}")
    _same_origin(report, plan)
    binding = view.binding
    if binding is None or binding.form is not TaskForm.PRIMITIVE:
        raise NotEligible("only a primitive task with a semantic binding may be dispatched")
    if not isinstance(manifest, InputManifest):
        raise NotEligible("admit_for_dispatch needs the consumer's InputManifest")
    if manifest.consumer_task_ref != view.task_id:
        raise NotEligible(
            f"the manifest belongs to {manifest.consumer_task_ref!s}, not {view.task_id!s}"
        )
    occurrence_binding = binding.occurrence_binding
    # Hash the manifest *before* opening the admission window, so an unfrozen
    # manifest raises ManifestNotFrozen without the guard ever being lifted.
    manifest_hash = manifest.manifest_hash()
    marker = _ADMITTING.set(True)
    try:
        admitted = EligiblePrimitiveTask(
            mission_id=plan.mission_id,
            plan_revision=plan.plan_revision,
            occurrence_id=view.occurrence_id,
            task_id=view.task_id,
            obligation_id=binding.obligation_id,
            contract_revision=binding.contract_revision,
            contract_hash=binding.contract_hash,
            dispatch_generation=binding.dispatch_generation,
            input_binding_revision=binding.input_binding_revision,
            input_manifest_hash=manifest_hash,
            method_instance_id=(
                None if occurrence_binding is None else occurrence_binding.method_instance_id
            ),
            requirement_refs=binding.requirement_refs,
            acceptance_ids=tuple(item.id for item in _acceptance_reads(manifest)),
            read_set=report.read_set,
            admitted_at_ms=index(now_ms, "now_ms"),
        )
    finally:
        _ADMITTING.reset(marker)
    object.__setattr__(admitted, "_token", _ADMISSION_TOKEN)
    return admitted


@dataclass(frozen=True, slots=True)
class LegacyReadyVerdict:
    """Why the legacy ``TaskStatus`` cannot stand in for an eligibility check."""

    admits_dispatch: bool
    gate_reason: ReadinessReason | None
    explanation: str


def legacy_ready_is_not_eligibility(
    legacy_status: str, *, form: TaskForm | None = None
) -> LegacyReadyVerdict:
    """Plan §18.5 hard constraint 4, spelled out for the P2.3c allocator wiring.

    ``TaskStatus.READY`` is a rebuildable display index in the new mode.  It never
    produces an :class:`EligiblePrimitiveTask`, so ``admits_dispatch`` is False for
    every status string.  For a ``form=compound`` task the allocator's answer is
    ``NEEDS_REFINEMENT`` — the interception is the form from the semantic binding,
    not the semantics version number and not the status string.  For a primitive
    the answer is "ask :func:`evaluate_readiness`", which is why ``gate_reason`` is
    None rather than a guessed refusal.
    """

    status = legacy_status if isinstance(legacy_status, str) else str(legacy_status)
    if form is TaskForm.COMPOUND:
        return LegacyReadyVerdict(
            admits_dispatch=False,
            gate_reason=ReadinessReason.NEEDS_REFINEMENT,
            explanation=(
                f"a compound task with legacy status {status!r} must be refused with "
                "NEEDS_REFINEMENT; the gate is form=compound from the semantic binding"
            ),
        )
    return LegacyReadyVerdict(
        admits_dispatch=False,
        gate_reason=None,
        explanation=(
            f"legacy status {status!r} is a rebuildable display index, not an eligibility "
            "check; run evaluate_readiness() and admit_for_dispatch() to obtain an "
            "EligiblePrimitiveTask"
        ),
    )


# --------------------------------------------------------------------------------------
# The three frontiers (pure queries)
# --------------------------------------------------------------------------------------


def _members(
    plan: ActivePlanView,
    reports: Mapping[OccurrenceId, ReadinessReport],
    reasons: frozenset[ReadinessReason],
) -> tuple[tuple[OccurrenceId, ...], Mapping[ReadinessReason, tuple[OccurrenceId, ...]]]:
    grouped: dict[ReadinessReason, list[OccurrenceId]] = {}
    for occurrence in sorted(reports, key=str):
        if not plan.is_adopted(occurrence):
            continue
        reason = reports[occurrence].reason
        if reason not in reasons:
            continue
        grouped.setdefault(reason, []).append(occurrence)
    members = tuple(
        occurrence for reason in READINESS_PRECEDENCE for occurrence in grouped.get(reason, ())
    )
    return members, {key: tuple(value) for key, value in grouped.items()}


@dataclass(frozen=True, slots=True)
class PlanningFrontier:
    """TG §8.1 layer 1: compounds to refine, preconditions to witness, approvals to get."""

    occurrences: tuple[OccurrenceId, ...]
    by_reason: Mapping[ReadinessReason, tuple[OccurrenceId, ...]]

    @classmethod
    def compute(
        cls, plan: ActivePlanView, reports: Mapping[OccurrenceId, ReadinessReport]
    ) -> PlanningFrontier:
        members, grouped = _members(plan, reports, PLANNING_REASONS)
        return cls(occurrences=members, by_reason=grouped)


@dataclass(frozen=True, slots=True)
class ExecutionFrontier:
    """TG §8.1 layer 2: adopted primitive occurrences whose semantic gates all passed.

    Membership is a candidacy, not a permission: nothing here has been reserved and
    no budget has been consulted.
    """

    occurrences: tuple[OccurrenceId, ...]
    by_reason: Mapping[ReadinessReason, tuple[OccurrenceId, ...]]

    @classmethod
    def compute(
        cls, plan: ActivePlanView, reports: Mapping[OccurrenceId, ReadinessReport]
    ) -> ExecutionFrontier:
        members, grouped = _members(plan, reports, EXECUTION_REASONS)
        return cls(occurrences=members, by_reason=grouped)


@dataclass(frozen=True, slots=True)
class AdmittedDispatch:
    """TG §8.1 layer 3 — decided by the dispatch transaction, not by this module.

    Budget, leases and physical capacity are not readable from a graph snapshot, so
    this module never fills it in.  :meth:`not_decided_here` exists so a caller can
    say "layer 3 is still empty" in types instead of in a comment.
    """

    admitted: tuple[EligiblePrimitiveTask, ...] = ()

    @classmethod
    def not_decided_here(cls) -> AdmittedDispatch:
        return cls()


__all__ = (
    "ADMITTED_DISPATCH_REASONS",
    "EXECUTION_REASONS",
    "PLANNING_REASONS",
    "READINESS_PRECEDENCE",
    "UNSETTLED_EFFECTS",
    "ActivePlanView",
    "AdmittedDispatch",
    "DispatchCandidacy",
    "EligibilityGateBypassed",
    "EligiblePrimitiveTask",
    "EvidenceView",
    "ExecutionFrontier",
    "LegacyReadyVerdict",
    "NotEligible",
    "OccurrenceOutcome",
    "PendingOperation",
    "PlanningFrontier",
    "ReadinessDetail",
    "ReadinessReason",
    "ReadinessReport",
    "TaskView",
    "admit_for_dispatch",
    "build_read_set",
    "evaluate_readiness",
    "gate_precedence",
    "legacy_ready_is_not_eligibility",
    "no_approval_required",
    "order_released",
    "stale_after",
    "start_preconditions",
)
