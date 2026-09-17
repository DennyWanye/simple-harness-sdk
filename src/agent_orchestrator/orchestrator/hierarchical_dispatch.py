# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3b: the assembly layer of the hierarchical mode (§14, §7.2, TG §7/§8).

``event_handler.py`` is a 6000-line single class, and §18.2's line about it is an
instruction not to make that worse: *"only assemble and call the new
planning/repair/commit modules; do not keep piling every algorithm into this
file"*.  So everything the new mode needs between "the Planner answered" and
"one plan revision is committed" lives here, and the event handler holds one
optional collaborator and five ``if hierarchical`` branches.

Four things this module is responsible for, and one it deliberately is not:

* **One round of planning.**  :meth:`HierarchicalDispatch.apply_planner_reply`
  runs ``parse_plan_proposal`` → ``assess_method`` / ``ground_method`` →
  ``compile_refinement_bundle`` → ``commit_plan_revision``.  A refusal whose
  reason a *recompilation* could fix is retried by compiling again against the
  freshly read snapshot — at most ``compile_attempts`` times in total — and then
  the round stops with a ``PlanCommitRefused`` event.  There is no rebase here
  and no unbounded retry: ADR-13 / C19 say a hierarchical proposal is handed back
  to its author, never replayed on its behalf.
* **Reading the plan through the projection.**  ``list``, ready, terminal, the
  concurrency count and the root review all go through
  :class:`~..graph.task_network.TaskNetworkSnapshot` and
  :func:`~..graph.eligibility.evaluate_readiness` (TG §7 last paragraph).  The
  string ``TaskStatus.READY`` is a rebuildable display index in this mode and is
  never a criterion — :attr:`TaskView.legacy_status` carries it for diagnostics
  only.
* **Compound tasks advance without a Worker.**  :func:`next_compound_phase` is a
  pure reducer over typed inputs; :meth:`advance_compound_phases` records what it
  says.  No Attempt is created for a compound, ever (TG §7), and
  :meth:`intercept_worker_dispatch` refuses one with ``NEEDS_REFINEMENT`` before
  the allocator's old READY entry can walk it into the Worker path (§18.5 rule 4).
* **Inputs come from the manifest.**  :meth:`attempt_inputs` asks
  :mod:`..artifacts.versioning` for the ``InputManifest`` path, so an ORDER-only
  predecessor contributes nothing (§24.1 decisions 3 and 4).

What it is *not*: an authority.  Every write still goes through
``CommitService``; every judgement still comes from ``graph/``; a missing
semantic binding is reported as :class:`GraphIntegrityError` rather than quietly
handled, because §18.5 calls that corruption and not a legacy fallback.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from ..artifacts.input_bindings import (
    AcceptedOutput,
    AcceptedOutputsIndex,
    InputManifest,
    ManifestNotFrozen,
    ResolutionPolicy,
    ResolutionResult,
    TargetRules,
)
from ..artifacts.versioning import UpstreamInput, manifest_upstream_inputs, resolve_input_manifest
from ..contracts import TERMINAL_MISSION, ContractError, Event, TaskStatus, ids
from ..contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from ..contracts.htn import (
    MissionRef,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    PlanRevision,
    PortCardinality,
    ReadItem,
    ReadItemKind,
    RefineOperation,
    ScopeEpochRead,
    SemanticReadSet,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    condition_digest,
)
from ..contracts.obligations import ObligationAccountView
from ..contracts.resolution import (
    CriterionVerdict,
    GoalResolution,
    GoalResolutionId,
    RequirementsRevision,
    ResolutionCriterion,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewVerdict,
    WorkspaceAccess,
)
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..graph.eligibility import (
    ActivePlanView,
    EligiblePrimitiveTask,
    EvidenceView,
    ExecutionFrontier,
    NotEligible,
    OccurrenceOutcome,
    PlanningFrontier,
    ReadinessReason,
    ReadinessReport,
    TaskView,
    admit_for_dispatch,
    evaluate_readiness,
    legacy_ready_is_not_eligibility,
)
from ..graph.projection_validation import GraphIntegrityError, require_topological_order
from ..graph.task_network import GATING_REQUIREDNESS, TaskNetworkSnapshot
from ..knowledge.validity import CONDITION_REASON_PREFIX as WITNESS_CONDITION_PREFIX
from ..knowledge.validity import acceptance_subject, condition_subject
from ..planning.htn.applicability import ApplicabilityStatus, assess_method
from ..planning.htn.compiler import (
    RefinementCompilation,
    RootNetwork,
    compile_refinement_bundle,
)
from ..planning.htn.grounding import (
    SharedGoalEntry,
    SharedGoalIndex,
    SharingSignature,
    ground_method,
)
from ..planning.planner import parse_method_proposal, parse_plan_proposal
from ..storage.htn_store import HtnStore, PlanCommitReceipt
from ..storage.obligation_store import ObligationStore
from ..storage.store import StoreError
from ..verification.acceptance_rules import CompoundFacts, ExecutionPosture, IndependenceFacts
from .accepted_outputs import (
    accepted_output_from_json,
)
from .accepted_outputs import (
    stored_coverage as _stored_coverage,
)
from .plan_commits import (
    HIERARCHICAL_SEMANTICS,
    PLAN_REVISION_COMMITTED,
    CommitPlanCommand,
    PlanCommitRejected,
    PlanPrincipal,
    semantics_of,
)
from .resolution_commits import (
    CommitGoalResolutionCommand,
    ResolutionCommitRejected,
    ResolutionPrincipal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..contracts import Mission
    from ..contracts.htn import PlanProposal
    from ..planning.htn.registry import AdmissionReceipt
    from ..storage.store import Store
    from .commit_service import CommitService

#: The two event types this module appends.  Both are *new* names: §18.5 rule 3
#: lets the hierarchical mode add event types and forbids it to change the bytes
#: of an existing one, so nothing here is ever written on a legacy Mission.
PLAN_COMMIT_REFUSED = "PlanCommitRefused"
PLAN_INTEGRITY_FAILED = "PlanIntegrityFailed"
DISPATCH_INTERCEPTED = "HierarchicalDispatchIntercepted"
COMPOUND_PHASE_CHANGED = "CompoundPhaseChanged"
#: P2.3c part 2: one occurrence was *not* dispatched this cycle, and the structured
#: reason why.  A separate name from ``DISPATCH_INTERCEPTED`` because that one is the
#: safety refusal of a compound and this one is the ordinary "not yet" of the readiness
#: gate; an operator reading a Mission that is not moving has to be able to tell
#: "the plan is wrong" from "the producer has not finished".
DISPATCH_WITHHELD = "HierarchicalDispatchWithheld"
#: P2.3c part 2: the Mission's root GoalResolution was offered and refused.  Recorded
#: because "the Mission did not complete" is not a diagnosis — which rule refused it is.
ROOT_RESOLUTION_REFUSED = "RootGoalResolutionRefused"
#: P2.3c part 2c (review F1): a hierarchical Mission reached the scheduler on a
#: deployment that never installed this assembly.  The gates live here and the
#: occurrence rows are written by the Commit Service, so the two can be out of step;
#: when they are, the Mission is refused rather than handed to the legacy allocator,
#: and this is the record of the refusal.  One event per Mission — the condition is a
#: deployment fact, so it does not change from cycle to cycle and repeating it every
#: cycle would drown the log it is supposed to explain.
ASSEMBLY_MISSING = "HierarchicalAssemblyMissing"
#: P2.3c part 2c: the loop went idle while this Mission still had occurrences every
#: gate withheld.  It is a *record*, not a verdict: the Mission is left exactly where
#: it is (no status change, nothing cancelled), because "this process has nothing left
#: to do" and "this Mission can never progress" are different statements and only the
#: first one is known here — a demand admitted, an approval granted or an observation
#: recorded from outside would make the very same plan runnable.  What it does end is
#: the silence: the withheld reasons are written down where an operator reads them
#: rather than left to be re-derived from a Mission that simply stopped moving.
MISSION_STALLED = "HierarchicalMissionStalled"
#: P2.3c part 2c: a licence could not be recorded because the consumer already holds a
#: START row for this scope epoch and support revision.  It is a *storage* limit, not a
#: judgement about the work — and it is recorded rather than swallowed, because the
#: occurrence that needed the licence then waits for a reason nobody could otherwise see.
WITNESS_KEY_TAKEN = "HierarchicalWitnessKeyTaken"

#: P2.3c part 3a.  The root ``MISSION_FINAL`` review package was cut, and an earlier
#: one was retired.  The two names live *here*, beside the reader that has to honour
#: them (:meth:`HierarchicalDispatch.live_root_review_package`), rather than in
#: ``orchestrator.root_review`` which writes them: "which review does the root
#: resolve from" is a question this module already answers, and an answer that could
#: not see a supersede record would resolve from an anchor the world has moved past.
ROOT_REVIEW_CUT = "HierarchicalRootReviewCut"
ROOT_REVIEW_SUPERSEDED = "HierarchicalRootReviewSuperseded"
#: P2.3c part 2c: one MethodSynthesizer round's outcome.  A refused proposal writes
#: nothing to the registry and nothing to the plan, so without this event the only
#: trace of the round would be its token cost.
SYNTHESIS_ROUND_RECORDED = "MethodSynthesisRoundRecorded"
#: G1 (Host acceptance runner): why each registered method was refused for each still
#: open goal, at the plan revision the Planner was asked against.  The four-axis
#: report was computed for the *prompt* and thrown away, so after a run nobody could
#: say why a method had not been chosen — the runner had to re-derive it with a probe.
#: One record per ``(mission, plan_revision)``: the assessment is a function of the
#: world at that revision, so a second round against the same revision has nothing new
#: to say and the idempotency key makes that explicit rather than appending a twin.
METHOD_APPLICABILITY_ASSESSED = "MethodApplicabilityAssessed"

#: How many refusals one :data:`METHOD_APPLICABILITY_ASSESSED` payload carries.  Far
#: larger than the prompt's own cap (that one protects the model's attention; this one
#: only stops a pathological registry from writing an unbounded row), and a payload
#: that hits it says so in ``truncated`` instead of quietly ending.
MAX_RECORDED_REFUSALS = 200

#: How many times one Planner reply may be compiled in total.  Two means: compile,
#: and if the commit was refused for a reason a fresh snapshot could fix, compile
#: once more against that snapshot.  It is a small number on purpose — a third
#: attempt against a Mission that is moving underneath the proposer is a busy
#: loop, and the honest answer is to hand the round back with a named reason.
DEFAULT_COMPILE_ATTEMPTS = 2

#: The refusals a *recompilation against the current snapshot* can plausibly fix:
#: the four staleness gates, the structural ones that compare the increment against
#: the plan it was compiled from, and the two budget ones.
#:
#: Everything else is deliberately absent.  A forged principal, a payload conflict,
#: a terminal Mission, a missing semantic binding, an unresolvable read, a delta the
#: compiler did not mark commit-ready and an unresolved OR say something about the
#: *request* — recompiling against a newer snapshot changes none of them, and
#: retrying would only hide where the defect is.
RECOMPILABLE_REFUSALS: frozenset[str] = frozenset(
    {
        "GRAPH_VERSION_STALE",
        "PLAN_REVISION_STALE",
        "MANAGER_EPOCH_STALE",
        "READ_SET_STALE",
        "STRUCTURE_INVALID",
        # The increment did not preserve what the plan it was compiled from held: a
        # second compilation against the plan as it is *now* is exactly the answer.
        "PLAN_NOT_PRESERVED",
        "BOUND_REACHED",
        "BUDGET_INSUFFICIENT",
        "BUDGET_REQUIREMENT_MISMATCH",
    }
)

#: P2.3c part 2c: the ``assess_method`` refusals a *different method* could route
#: around, and therefore the only ones that make a MethodSynthesizer round the right
#: repair (§7.3).  ``NEEDS_EVIDENCE`` and ``CONFLICT`` are deliberately absent: the
#: first is answered by looking (:meth:`HierarchicalDispatch.run_evidence_round`) and
#: the second by settling the contradiction, and synthesising a method while the
#: question is open would be inventing a way past the check that is open.
SYNTHESIS_WORTHY_REFUSALS: frozenset[Any] = frozenset(
    {
        ApplicabilityStatus.PRECONDITION_FALSE,
        ApplicabilityStatus.CAPABILITY_UNAVAILABLE,
        ApplicabilityStatus.TYPE_ERROR,
    }
)

#: P2.3d / defect D2b: how many OBSERVED readings of one proposition by one observer,
#: with the truth still UNKNOWN afterwards, make "look again" a non-answer.  Two is the
#: smallest number that can tell a first reading apart from a repeat.
DEFAULT_EVIDENCE_SATURATION_ROUNDS = 2

#: Readiness answers that mean "the planner still owes something about the facts
#: or the authority", as opposed to "a method has not been chosen yet".
_WAIT_REASONS: frozenset[ReadinessReason] = frozenset(
    {
        ReadinessReason.WAITING_EVIDENCE,
        ReadinessReason.OBSERVER_UNAVAILABLE,
        ReadinessReason.VALIDITY_RECHECK_PENDING,
        ReadinessReason.WAITING_APPROVAL,
    }
)


def is_hierarchical(mission: Mission) -> bool:
    """Whether one Mission runs under the new semantics (§18.5 rule 1)."""

    return semantics_of(mission) == HIERARCHICAL_SEMANTICS


class PlanIntegrityError(GraphIntegrityError):
    """Corruption that is *not* a cycle: the plan's meaning is incomplete (§18.5).

    A :class:`~..graph.projection_validation.GraphIntegrityError` so that every
    caller which already stops the scope on a damaged projection stops on this too
    (§24.1 decision 11).  Its message is its own, because the base class's
    "N node(s) could not be ordered; cycle: unknown" would describe the wrong
    defect: nothing here is unordered — a *meaning* is absent, and telling an
    operator to look for a cycle would send them to the wrong place.
    """

    def __init__(self, mission_id: str, code: str, subjects: Sequence[str], message: str) -> None:
        self.mission_id = str(mission_id)
        self.code = str(code)
        self.subjects = tuple(sorted(str(item) for item in subjects))
        self._message = str(message)
        super().__init__(self.subjects, ())

    def __str__(self) -> str:
        return self._message

    def diagnose(self) -> str:
        """A description a person can act on.  Never an order a machine can run."""

        return self._message


def missing_bindings(mission_id: str, task_ids: Sequence[str]) -> PlanIntegrityError:
    """§18.5: a Task in the plan with no ``TaskSemanticBindingV1`` is corruption."""

    named = sorted(str(item) for item in task_ids)
    return PlanIntegrityError(
        mission_id,
        "semantic_binding_missing",
        [f"task:{item}" for item in named],
        f"graph integrity failure in mission {mission_id}: {len(named)} task(s) in the plan "
        f"carry no semantic binding [{', '.join(named) or 'none recorded'}]. In the "
        "hierarchical mode that is corruption and not a legacy fallback (§18.5); the plan "
        "cannot be executed until a binding exists for every member.",
    )


def root_not_identified(mission_id: str, task_ids: Sequence[str]) -> PlanIntegrityError:
    """No plan revision, and not exactly one root goal to seed one from."""

    named = sorted(str(item) for item in task_ids)
    return PlanIntegrityError(
        mission_id,
        "root_not_identified",
        [f"task:{item}" for item in named],
        f"graph integrity failure in mission {mission_id}: a Mission with no plan revision "
        f"seeds its network from exactly one root goal, and this one has {len(named)} "
        f"semantic binding(s) [{', '.join(named) or 'none'}]. Choosing one of several would "
        "be this module inventing a root; none at all is the missing-binding corruption.",
    )


# --------------------------------------------------------------------------------------
# compound phases (TG §7)
# --------------------------------------------------------------------------------------


class CompoundPhase(StrEnum):
    """TG §7's typed phase of a compound task.

    The coarse ``TaskStatus`` stays as display; this is the transition function
    ``next_compound_task`` the annex asks for.  It exists so a compound can be
    planned and accepted *without* a Worker Attempt: none of these transitions
    needs one, and creating a fake one to satisfy the old state machine is the
    exact bug TG §7 forbids.
    """

    PLANNING_READY = "planning_ready"
    REFINING = "refining"
    WAITING_CHILDREN = "waiting_children"
    COMPOSITION_REVIEW = "composition_review"
    RESOLUTION_COMMITTED = "resolution_committed"
    EVIDENCE_OR_AUTHORITY_WAIT = "evidence_or_authority_wait"


#: TG §7's display mapping.  Read by an operator and by the API; never by a gate.
COMPOUND_DISPLAY_STATUS: Mapping[CompoundPhase, TaskStatus] = {
    CompoundPhase.PLANNING_READY: TaskStatus.READY,
    CompoundPhase.REFINING: TaskStatus.ACTIVE,
    CompoundPhase.WAITING_CHILDREN: TaskStatus.ACTIVE,
    CompoundPhase.COMPOSITION_REVIEW: TaskStatus.VERIFYING,
    CompoundPhase.RESOLUTION_COMMITTED: TaskStatus.COMPLETED,
    CompoundPhase.EVIDENCE_OR_AUTHORITY_WAIT: TaskStatus.BLOCKED,
}


def next_compound_phase(
    occurrence: OccurrenceSpec,
    network: TaskNetworkSnapshot,
    report: ReadinessReport,
    *,
    child_outcomes: Mapping[OccurrenceId, OccurrenceOutcome],
    resolved: bool,
) -> CompoundPhase:
    """The phase of one compound occurrence, from typed inputs only (TG §7).

    Deliberately total and deliberately ignorant of ``TaskStatus``: the inputs are
    the occurrence's membership, the adopted method instance, the readiness verdict
    and the children's outcomes.  A caller that passes a legacy status string in
    gets the same answer, which is the property the suite pins down.
    """

    if not isinstance(occurrence, OccurrenceSpec):
        raise ContractError("next_compound_phase expects an OccurrenceSpec")
    if occurrence.form is not TaskForm.COMPOUND:
        raise ContractError(
            f"{occurrence.occurrence_id!s} is {occurrence.form!s}; only a compound occurrence "
            "has a typed phase (a primitive is driven by its Attempts, TG §7)"
        )
    if resolved:
        # An adopted GoalResolution is the end of the compound, whatever else is
        # still open: TG §7 says a COMPLETED compound does not go back.
        return CompoundPhase.RESOLUTION_COMMITTED
    if report.reason in _WAIT_REASONS:
        return CompoundPhase.EVIDENCE_OR_AUTHORITY_WAIT
    adopted = network.adopted_instance_for(occurrence.occurrence_id)
    if adopted is None:
        return CompoundPhase.PLANNING_READY
    children = network.adopted_children(occurrence.occurrence_id)
    known = {spec.occurrence_id for spec in network.occurrences}
    if any(binding.occurrence_id not in known for binding in children):
        # A slot the adopted method opened whose occurrence is not in the plan yet:
        # the refinement is committed but not fully expanded.
        return CompoundPhase.REFINING
    gating = [
        binding.occurrence_id for binding in children if binding.requiredness in GATING_REQUIREDNESS
    ]
    if any(
        child_outcomes.get(child, OccurrenceOutcome.UNKNOWN) is not OccurrenceOutcome.ACCEPTED
        for child in gating
    ):
        return CompoundPhase.WAITING_CHILDREN
    # TG §6.2 / §24.1 decision 2: the composition review waits for the *children*,
    # not for the parent, which is what makes it terminate at all.
    return CompoundPhase.COMPOSITION_REVIEW


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanRefusal:
    """One refused delivery of a plan commit, kept so the round can explain itself."""

    attempt: int
    reason: str
    detail: str
    recompilable: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "reason": self.reason,
            "detail": self.detail,
            "recompilable": self.recompilable,
        }


@dataclass(frozen=True, slots=True)
class PlanRoundOutcome:
    """What one Planner reply achieved: a receipt, or a named sequence of refusals."""

    proposal_id: str
    receipt: PlanCommitReceipt | None = None
    refusals: tuple[PlanRefusal, ...] = ()

    @property
    def committed(self) -> bool:
        return self.receipt is not None

    @property
    def attempts(self) -> int:
        return len(self.refusals) + (1 if self.committed else 0)

    @property
    def last_reason(self) -> str | None:
        return self.refusals[-1].reason if self.refusals else None


@dataclass(frozen=True, slots=True)
class DispatchInterception:
    """A compound that the old READY entry would have dispatched (§18.5 rule 4)."""

    task_id: str
    occurrence_id: str | None
    reason: str
    explanation: str


@dataclass(frozen=True, slots=True)
class RootResolutionInputs:
    """The read facts one Mission-root ``GoalResolution`` command is built from.

    ``reason`` non-empty means the facts are *not* complete and names which one is
    missing.  A missing review anchor is reported rather than fabricated: the root
    resolution is formed out of the success formula plus the final acceptance, so a
    system that writes its own review package has decided the answer it was meant to
    check (§6.3, §8.1, AER §6.1).
    """

    reason: str = ""
    detail: str = ""
    occurrence_id: str = ""
    task_id: str = ""
    obligation_id: str = ""
    contract_revision: int = 1
    method_instance_id: str | None = None
    requirements: RequirementsRevision | None = None
    package: ReviewPackage | None = None
    record: ReviewRecord | None = None
    witness_id: str = ""
    contributions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.reason


@dataclass(frozen=True, slots=True)
class RootResolutionOutcome:
    """Whether the Mission's root resolution was formed this cycle, and why not."""

    committed: bool
    reason: str = ""
    detail: str = ""
    resolution_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "reason": self.reason,
            "detail": self.detail,
            "resolution_id": self.resolution_id,
        }


@dataclass(frozen=True, slots=True)
class DispatchRefusal:
    """Why one occurrence is not competing for a Worker this cycle.

    The reason is a :class:`~..graph.eligibility.ReadinessReason` and the detail codes
    are the gate's own, unmerged: "the producer has not finished" (``WAITING_DATA``),
    "we could not ask" (``OBSERVER_UNAVAILABLE``) and "this compound still needs a
    method" (``NEEDS_REFINEMENT``) call for three different repairs, and the scheduler
    is the wrong place to collapse them into "not ready".
    """

    task_id: str
    occurrence_id: str
    reason: ReadinessReason
    detail_codes: tuple[str, ...] = ()
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "occurrence_id": self.occurrence_id,
            "reason": str(self.reason),
            "detail_codes": list(self.detail_codes),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DispatchAdmissions:
    """What :func:`~..scheduling.allocator.allocate_v2` is handed for one Mission.

    Three maps and nothing else: the semantic binding of every Task row (its absence
    is corruption, not a legacy fallback), the admissions
    :func:`~..graph.eligibility.admit_for_dispatch` built for the occurrences that
    passed every gate, and a named refusal for each one that did not.  Holding the
    refusals beside the admissions is the point — a Mission that is not moving has to
    be able to say why without anybody re-deriving it.
    """

    plan_revision: int
    bindings: Mapping[str, TaskSemanticBindingV1] = field(default_factory=dict)
    readiness: Mapping[str, EligiblePrimitiveTask] = field(default_factory=dict)
    refusals: tuple[DispatchRefusal, ...] = ()

    def admission_for(self, task_id: str) -> EligiblePrimitiveTask | None:
        return self.readiness.get(task_id)

    def refusal_for(self, task_id: str) -> DispatchRefusal | None:
        for item in self.refusals:
            if item.task_id == task_id:
                return item
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "plan_revision": int(self.plan_revision),
            "admitted": sorted(self.readiness),
            "refusals": [item.to_json() for item in self.refusals],
        }


@dataclass(frozen=True, slots=True)
class NetworkView:
    """One read of a Mission's plan: the snapshot plus what readiness said about it."""

    network: TaskNetworkSnapshot
    plan: ActivePlanView
    views: Mapping[OccurrenceId, TaskView]
    reports: Mapping[OccurrenceId, ReadinessReport]
    outcomes: Mapping[OccurrenceId, OccurrenceOutcome]
    resolved: frozenset[OccurrenceId] = frozenset()
    #: The acceptance projection and the two witness key spaces this read judged the
    #: plan against (review F8).  They used to be computed inside ``read()`` and
    #: dropped on the floor, so ``admissions()`` read them a *second* time and the
    #: readiness report it reported came from one snapshot while the manifest it
    #: hashed came from another.  Carrying them makes the docstring's "one read" a
    #: fact about the code rather than a claim about it.
    accepted: AcceptedOutputsIndex | None = None
    witnesses: Mapping[str, ValidityWitness] = field(default_factory=dict)
    #: consumer task id → condition digest → the START-precondition licence this read
    #: judged against (P2.3c part 2c; the sibling of ``licences`` on the DATA lane).
    starts: Mapping[str, Mapping[str, ValidityWitness]] = field(default_factory=dict)
    licences: Mapping[str, Mapping[str, ValidityWitness]] = field(default_factory=dict)

    @property
    def planning_frontier(self) -> PlanningFrontier:
        return PlanningFrontier.compute(self.plan, self.reports)

    @property
    def execution_frontier(self) -> ExecutionFrontier:
        return ExecutionFrontier.compute(self.plan, self.reports)

    def report_for(self, occurrence: OccurrenceId) -> ReadinessReport:
        return self.reports[occurrence]


def _typed(kind: TypedRefKind, identifier: str) -> TypedRef:
    """A typed reference to something this library already holds by id."""

    return TypedRef(
        kind=kind, id=str(identifier), revision=1, content_hash=content_hash_of(str(identifier))
    )


class PlanningWorld(Protocol):
    """The declarations one deployment brings to a compilation.

    A narrow structural type on purpose: this module needs a task-type catalog, a
    schema catalog, a method registry, a predicate registry and the two evidence
    reads — and nothing else.  Anything wider would make the assembly layer depend
    on how a deployment happens to be configured.
    """

    @property
    def catalog(self) -> Any: ...

    @property
    def schemas(self) -> Any: ...

    @property
    def registry(self) -> Any: ...

    @property
    def predicates(self) -> Any: ...

    def snapshot(self) -> Any: ...

    def capabilities(self) -> Any: ...


# --------------------------------------------------------------------------------------
# the dispatcher
# --------------------------------------------------------------------------------------


@dataclass
class HierarchicalDispatch:
    """Assembly for one deployment's hierarchical Missions.

    Holds no state about a Mission: every method reads the store, because the plan
    is the store's and a cached snapshot is exactly the stale read ADR-13 spends a
    whole gate refusing.
    """

    store: Store
    commit: CommitService
    planning: PlanningWorld | None = None
    compile_attempts: int = DEFAULT_COMPILE_ATTEMPTS
    target_rules: TargetRules | None = None
    resolution_policy: ResolutionPolicy = field(default_factory=ResolutionPolicy)
    #: P2.3d / defect D2b: how many times one observer may record an OBSERVED reading
    #: of one proposition, with the truth still UNKNOWN afterwards, before looking again
    #: stops counting as an answer.  See :meth:`goals_needing_method`.
    evidence_saturation_rounds: int = DEFAULT_EVIDENCE_SATURATION_ROUNDS

    def __post_init__(self) -> None:
        if int(self.compile_attempts) < 1:
            raise ContractError("compile_attempts must be at least 1")
        if int(self.evidence_saturation_rounds) < 1:
            raise ContractError("evidence_saturation_rounds must be at least 1")

    # ---------------------------------------------------------------- reading the plan
    def semantics(self) -> HtnStore:
        return HtnStore(self.store)

    def mission(self, mission_id: str) -> Mission:
        found = self.store.get_mission(mission_id)
        if found is None:
            raise ContractError(f"mission {mission_id!r} does not exist")
        return found

    def require_hierarchical(self, mission_id: str) -> Mission:
        mission = self.mission(mission_id)
        if not is_hierarchical(mission):
            raise ContractError(
                f"mission {mission_id!r} runs under {semantics_of(mission)!r}; the hierarchical "
                "assembly is not an entry point for a legacy Mission (§18.5 rule 1)"
            )
        return mission

    def network(self, mission_id: str) -> TaskNetworkSnapshot:
        """The active plan revision, read back as a typed network — or corruption.

        This is the *execution* read, so a task in the plan with no
        ``TaskSemanticBindingV1`` raises :class:`PlanIntegrityError`.  §18.5: in
        this mode a missing binding is corruption, and treating it as "then use the
        legacy rules" is how a Mission silently runs half in each mode.

        The reading side that only has to *explain* a Mission goes through
        :meth:`read` with ``tolerate_integrity=True``, which carries the same error
        on the :class:`ActivePlanView` instead of raising: every verdict in the
        scope then reads ``GRAPH_INTEGRITY`` and nothing is dispatched (§24.1
        decision 11).  Two behaviours, one read, and the caller says which it is.
        """

        snapshot, integrity = self._read_network(mission_id)
        if integrity is not None:
            raise integrity
        return snapshot

    def _read_network(
        self, mission_id: str
    ) -> tuple[TaskNetworkSnapshot, PlanIntegrityError | None]:
        """The plan as it can be read, plus what was wrong with it.

        An occurrence whose task has no meaning cannot be *in* a
        :class:`TaskNetworkSnapshot` — construction checks that every endpoint names
        something the snapshot contains — so it is left out and reported.  Leaving
        it out is not tolerating it: the returned error makes every occurrence in the
        scope refuse, which is a stronger answer than one task failing.
        """

        semantics = self.semantics()
        active = semantics.active_plan_revision(mission_id)
        if active is None:
            # Before the first refinement there is no plan revision yet, and the
            # network is the Mission's own root goal.  Building that seed by hand in
            # every caller is how two callers end up disagreeing about whether the
            # root is its own occurrence, which is why the compiler owns the shape.
            return self._read_seed_network(mission_id)
        revision = int(active.revision)
        members = semantics.list_plan_memberships(mission_id, revision)
        bindings: dict[TaskRef, TaskSemanticBindingV1] = {}
        missing: list[str] = []
        occurrences: list[OccurrenceSpec] = []
        for spec in members:
            task_id = str(spec.task_id)
            binding = semantics.task_semantics_of(mission_id, task_id)
            if binding is None:
                missing.append(task_id)
                continue
            bindings[TaskRef(task_id)] = binding
            occurrences.append(spec)
        known = {spec.occurrence_id for spec in occurrences}
        instances = tuple(
            draft
            for draft in semantics.list_method_instances(mission_id)
            if TaskRef(str(draft.goal_id)) in bindings
        )
        adopted = tuple(
            draft.instance_id
            for draft in instances
            if semantics.method_instance_state(mission_id, str(draft.instance_id)) == "ADOPTED"
        )
        child_ids = {
            child.occurrence_id
            for draft in instances
            if draft.instance_id in set(adopted)
            for child in draft.child_bindings
        }
        roots = tuple(
            spec.occurrence_id for spec in occurrences if spec.occurrence_id not in child_ids
        )
        snapshot = TaskNetworkSnapshot(
            mission_id=MissionRef(mission_id),
            plan_revision=PlanRevision(revision),
            occurrences=tuple(occurrences),
            task_bindings=tuple(bindings[TaskRef(str(spec.task_id))] for spec in occurrences),
            method_instances=instances,
            adopted_instance_ids=adopted,
            root_occurrence_ids=roots,
            order_constraints=tuple(
                item
                for item in semantics.list_order_constraints(mission_id, revision)
                if item.before in known and item.after in known
            ),
            data_requirements=tuple(
                item
                for item in semantics.list_data_requirements(mission_id, revision)
                if item.producer_occurrence in known and item.consumer_occurrence in known
            ),
            required_obligations=tuple(
                dict.fromkeys(
                    spec.obligation_id for spec in occurrences if spec.occurrence_id in roots
                )
            ),
            # P2.3c part 2c: re-derived, because it is not a column.  See
            # :func:`~.accepted_outputs.stored_coverage` — round one's claims were
            # simply absent from a
            # network read back out of the store, so a *second* refinement round was
            # refused with ``root_coverage_gap`` and no plan could ever go two levels
            # deep.
            obligation_coverage=_stored_coverage(semantics, instances, adopted, bindings),
        )
        return snapshot, (missing_bindings(mission_id, missing) if missing else None)

    def seed_network(self, mission_id: str) -> TaskNetworkSnapshot:
        """The one-occurrence starting network of a Mission with no plan revision.

        Exactly one semantic binding is expected: the root goal the Mission was
        opened for.  Zero is the missing-binding corruption §18.5 names; more than
        one means someone wrote task meanings without a plan to hold them, and
        picking one of them would be this module inventing a root.  Both are
        :class:`PlanIntegrityError`, not a legacy fallback.
        """

        snapshot, integrity = self._read_seed_network(mission_id)
        if integrity is not None:
            raise integrity
        return snapshot

    def _read_seed_network(
        self, mission_id: str
    ) -> tuple[TaskNetworkSnapshot, PlanIntegrityError | None]:
        """The seed, or an empty network plus the reason there is no seed.

        The empty snapshot is what lets the explaining caller answer at all: with
        the error carried beside it, every query over it reports corruption instead
        of raising out of a loop that is also driving other Missions.
        """

        bindings: dict[str, TaskSemanticBindingV1] = {}
        for binding in self.semantics().list_task_semantics(mission_id):
            bindings[str(binding.task_id)] = binding  # ordered by revision: newest wins
        if len(bindings) != 1:
            empty = TaskNetworkSnapshot(
                mission_id=MissionRef(mission_id),
                plan_revision=PlanRevision(0),
                occurrences=(),
                task_bindings=(),
            )
            return empty, root_not_identified(mission_id, sorted(bindings))
        root = next(iter(bindings.values()))
        return RootNetwork(mission_id=MissionRef(mission_id), root_task=root).snapshot(), None

    def plan_view(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot | None = None,
        *,
        integrity: GraphIntegrityError | None = None,
    ) -> ActivePlanView:
        """The frozen read one readiness judgement is made against.

        Both kinds of corruption are *carried* here rather than raised: an
        unorderable projection (``require_topological_order``) and an incomplete
        plan meaning (``integrity``, from :meth:`_read_network`).  §24.1 decision 11
        wants a damaged plan to stop every dispatch in the scope, which is exactly
        what ``ActivePlanView.integrity_error`` does — while the caller that actually
        materialises files gets the exception from :mod:`..artifacts.versioning`.
        """

        mission = self.mission(mission_id)
        if network is None:
            network, carried = self._read_network(mission_id)
            integrity = integrity or carried
        resolved = network
        if integrity is None:
            try:
                require_topological_order(resolved.execution_projection())
            except GraphIntegrityError as error:
                integrity = error
        return ActivePlanView(
            snapshot=resolved,
            # "Admits work" is the *selection* gate's question — is this Mission still
            # one whose plan may be acted on — so the answer is "it has not stopped",
            # not "it is already dispatching".  A Mission that is still being planned
            # admits planning work; a stopped one admits none.  Whether a primitive may
            # actually be dispatched is the dispatch transaction's decision (TG §8.1
            # layer 3), which this view deliberately does not make.
            mission_admits_work=mission.status not in TERMINAL_MISSION,
            manager_epoch=self.semantics().epoch(mission_id, "mission"),
            # TG decision 9: an active share needs a live demand, and the duty's
            # account is where that is recorded.  Read, never invented: a duty with
            # no account is "no admitted demand", which is the selection gate's
            # NOT_SELECTED and not an implicit permission.
            obligation_accounts=self.obligation_accounts(mission_id, resolved),
            # The epoch barrier the START-precondition gate compares against (§11.5 /
            # I19).  Left empty — as it was — every recorded witness answered
            # ``witness_epoch_unknown``: the view could not confirm the epoch it was
            # taken at, so the one lane that reads ``scope_epochs`` could never open,
            # whatever the deployment had observed.
            scope_epochs=self.scope_epochs(mission_id),
            integrity_error=integrity,
        )

    def scope_epochs(self, mission_id: str) -> dict[str, int]:
        """The live validity epoch of every scope this Mission's witnesses name.

        ``mission`` is always in it: it is the scope a witness takes by default and
        the one the manager epoch belongs to, and a barrier that is missing reads as
        "unknown", which refuses rather than allows.
        """

        semantics = self.semantics()
        scopes = {"mission"} | {
            str(witness.scope_id) for witness in semantics.list_validity_witnesses(mission_id)
        }
        return {scope: semantics.epoch(mission_id, scope) for scope in sorted(scopes)}

    def obligation_accounts(
        self, mission_id: str, network: TaskNetworkSnapshot
    ) -> dict[ObligationId, ObligationAccountView]:
        duties = ObligationStore(self.store)
        accounts: dict[ObligationId, ObligationAccountView] = {}
        for spec in network.occurrences:
            if spec.obligation_id in accounts:
                continue
            if duties.exists(mission_id, spec.obligation_id):
                accounts[spec.obligation_id] = duties.account(mission_id, spec.obligation_id)
        return accounts

    def read(
        self, mission_id: str, *, now_ms: int | None = None, tolerate_integrity: bool = False
    ) -> NetworkView:
        """One complete read: snapshot, views, readiness reports, outcomes.

        This is the single place the new mode answers "what is in the plan", so
        ``list_tasks``, the ready set, the concurrency count, the terminal test and
        the root review all end up reading the same thing (TG §7).

        ``tolerate_integrity`` picks which of the two answers decision 11 asks for.
        The default is the execution answer: a plan whose meaning is incomplete
        raises, because acting on the readable part of a damaged plan is exactly the
        silent half-mode §18.5 forbids.  ``True`` is the explaining answer: the same
        error is carried on the view, every occurrence reports ``GRAPH_INTEGRITY``,
        both frontiers come back empty, and an operator can still see the Mission.

        Everything the readiness gates read is fetched **once** here and passed down:
        the acceptance projection, the witnesses and the duty accounts are the same
        for every occurrence in one read, and re-deriving them per occurrence turned
        one read of a plan into a quadratic number of store round-trips.  They are also
        *carried* on the returned view (review F8), so a caller that has to build the
        same manifest again — ``admissions()`` — judges against this snapshot rather
        than taking a second one.
        """

        network, integrity = self._read_network(mission_id)
        if integrity is not None and not tolerate_integrity:
            raise integrity
        plan = self.plan_view(mission_id, network, integrity=integrity)
        outcomes = self.occurrence_outcomes(mission_id, network)
        resolved = self.resolved_occurrences(mission_id, network)
        witnesses = self.witnesses(mission_id, network)
        accepted = self.accepted_outputs(mission_id, network, outcomes=outcomes)
        # Two different questions, two different key spaces: ``witnesses()`` answers
        # "which condition was this witness taken for" (the START-precondition lane)
        # and the input index answers "which acceptance did this consumer get a
        # licence over" (the DATA lane, I19).  Feeding the precondition map to the
        # resolver meant no key ever matched and every DATA consumer refused with
        # ``WITNESS_MISSING`` whatever the deployment had accepted.
        licences = self.input_witness_index(mission_id)
        # The START-precondition lane is keyed by *consumer*, not by condition: two
        # occurrences under one method inherit the same digest, and §11.5 says one
        # consumer's permission is not another's.  The mission-wide map from the
        # frozen ``witness_ref`` stays underneath it, so a deployment that recorded
        # the link that way is still read.
        starts = self.start_witness_index(mission_id)
        moment = int(self.store.now * 1000) if now_ms is None else int(now_ms)
        views: dict[OccurrenceId, TaskView] = {}
        reports: dict[OccurrenceId, ReadinessReport] = {}
        for spec in network.occurrences:
            view = self.task_view(mission_id, network, spec)
            views[spec.occurrence_id] = view
            reports[spec.occurrence_id] = evaluate_readiness(
                view,
                plan,
                outcomes,
                EvidenceView(witnesses={**witnesses, **starts.get(str(spec.task_id), {})}),
                self.resolved_inputs(
                    mission_id,
                    network,
                    spec,
                    accepted=accepted,
                    witnesses=licences.get(str(spec.task_id), {}),
                ),
                now_ms=moment,
            )
        return NetworkView(
            network=network,
            plan=plan,
            views=views,
            reports=reports,
            outcomes=outcomes,
            resolved=resolved,
            accepted=accepted,
            witnesses=witnesses,
            licences=licences,
            starts=starts,
        )

    def task_view(
        self, mission_id: str, network: TaskNetworkSnapshot, spec: OccurrenceSpec
    ) -> TaskView:
        """The typed read of one occurrence; the legacy status is diagnostics only."""

        task = self.store.get_task(str(spec.task_id))
        try:
            binding: TaskSemanticBindingV1 | None = network.binding_for_occurrence(
                spec.occurrence_id
            )
        except KeyError:  # pragma: no cover - network() refuses this first
            binding = None
        return TaskView(
            occurrence_id=spec.occurrence_id,
            task_id=spec.task_id,
            binding=binding,
            legacy_status="" if task is None else str(task.status),
        )

    def occurrence_outcomes(
        self, mission_id: str, network: TaskNetworkSnapshot
    ) -> dict[OccurrenceId, OccurrenceOutcome]:
        """What the world says about each occurrence, for the ORDER release test.

        A reading, not a status copy: an accepted :class:`Acceptance` is what makes
        an occurrence ``ACCEPTED``, and an occurrence with no Acceptance is
        ``RUNNING`` or ``UNKNOWN`` however its legacy Task row happens to read.
        """

        semantics = self.semantics()
        # An Acceptance names a *task*, so an occurrence counts as accepted when the
        # task it instantiates has a CURRENT Acceptance under the same duty.  A
        # revoked or superseded Acceptance is not one (§11.5).
        accepted = {
            (str(item.task_id), str(item.obligation_id))
            for item in semantics.list_acceptances(mission_id)
            if str(item.validity) == "CURRENT"
        }
        outcomes: dict[OccurrenceId, OccurrenceOutcome] = {}
        for spec in network.occurrences:
            if (str(spec.task_id), str(spec.obligation_id)) in accepted:
                outcomes[spec.occurrence_id] = OccurrenceOutcome.ACCEPTED
                continue
            task = self.store.get_task(str(spec.task_id))
            if task is None:
                outcomes[spec.occurrence_id] = OccurrenceOutcome.UNKNOWN
                continue
            status = str(task.status)
            if status == "FAILED":
                outcomes[spec.occurrence_id] = OccurrenceOutcome.FAILED
            elif status == "CANCELLED":
                outcomes[spec.occurrence_id] = OccurrenceOutcome.CANCELLED
            elif status == "COMPLETED":
                # Completed *without* an Acceptance is not an acceptance: TG
                # decision 1 says an unknown ending never settles anything.
                outcomes[spec.occurrence_id] = OccurrenceOutcome.SETTLED_OTHER
            else:
                outcomes[spec.occurrence_id] = OccurrenceOutcome.RUNNING
        return outcomes

    def resolved_occurrences(
        self, mission_id: str, network: TaskNetworkSnapshot
    ) -> frozenset[OccurrenceId]:
        """Occurrences whose duty has an *adopted* GoalResolution (§7.2)."""

        semantics = self.semantics()
        adopted: set[ObligationId] = set()
        for spec in network.occurrences:
            if spec.obligation_id in adopted:
                continue
            if semantics.adopted_goal_resolution(mission_id, str(spec.obligation_id)) is not None:
                adopted.add(spec.obligation_id)
        return frozenset(
            spec.occurrence_id for spec in network.occurrences if spec.obligation_id in adopted
        )

    def witnesses(
        self, mission_id: str, network: TaskNetworkSnapshot
    ) -> dict[str, ValidityWitness]:
        """The START-precondition witnesses, keyed by condition digest.

        The link is the method instance's own
        :class:`~..contracts.htn.PreconditionWitnessRecord`, which is the only place
        that says *which condition* a stored :class:`ValidityWitness` was taken for.
        Guessing that link from the witness alone would let one witness license a
        precondition it was never computed for.
        """

        by_id = {
            str(witness.witness_id): witness
            for witness in self.semantics().list_validity_witnesses(mission_id)
        }
        found: dict[str, ValidityWitness] = {}
        for draft in network.method_instances:
            for record in draft.precondition_witnesses:
                if record.witness_ref is None:
                    continue
                witness = by_id.get(str(record.witness_ref.id))
                if witness is not None:
                    found[record.condition_digest] = witness
        return found

    def input_result(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot,
        spec: OccurrenceSpec,
        *,
        accepted: AcceptedOutputsIndex | None = None,
        witnesses: Mapping[str, ValidityWitness] | None = None,
    ) -> ResolutionResult | None:
        """The declared DATA contract of one occurrence, resolved — or None.

        None means "this occurrence declares no data requirement", which is a
        different answer from "its inputs did not resolve"; the DATA gate reads the
        difference, so the two are not collapsed here.
        """

        requirements = [
            item
            for item in network.data_requirements
            if item.consumer_occurrence == spec.occurrence_id
        ]
        if not requirements:
            return None
        return resolve_input_manifest(
            network.binding_for_occurrence(spec.occurrence_id),
            network,
            accepted if accepted is not None else self.accepted_outputs(mission_id, network),
            consumer_occurrence=spec.occurrence_id,
            # P2.3c part 2b: the witnesses this lane needs are keyed by **acceptance
            # id** and issued to *this* consumer (I19), which is a different question
            # from ``witnesses()``'s "which condition was this witness taken for".
            # Passing the precondition map here meant no key ever matched and every
            # DATA consumer refused with ``WITNESS_MISSING`` whatever the deployment
            # had recorded.
            witnesses=(
                self.input_witnesses(mission_id, str(spec.task_id))
                if witnesses is None
                else witnesses
            ),
            policy=self.resolution_policy_for(mission_id),
        )

    def resolution_policy_for(self, mission_id: str, *, now_ms: int | None = None) -> Any:
        """This deployment's resolution policy with the Mission's live epochs in it.

        I19 is a *comparison*, and the policy this class is constructed with does not
        know the scope epochs — so a policy left at its defaults would refuse every
        binding with ``WITNESS_EPOCH_UNKNOWN``.  The epochs are read here rather than
        cached on the dataclass, for the same reason nothing else on this class is
        cached: a bumped epoch has to be visible to the very next read.

        A policy the caller already filled in is left exactly as it is.
        """

        from dataclasses import replace as _replace

        policy = self.resolution_policy
        if policy.scope_epochs:
            return policy
        return _replace(
            policy,
            scope_epochs=self.scope_epochs(mission_id),
            now_ms=int(self.store.now * 1000) if now_ms is None else int(now_ms),
        )

    #: The purpose a witness must carry to license *binding an accepted output* as an
    #: input.  ``START`` because that is what the use is: starting this consumer's
    #: work on that artifact.  An ``ACCEPT`` witness licensed the producer's
    #: acceptance and is not transferable to the consumer (§11.5).
    INPUT_WITNESS_PURPOSE = WitnessPurpose.START

    def input_witness_index(self, mission_id: str) -> dict[str, dict[str, ValidityWitness]]:
        """consumer task id → acceptance id → the START witness issued for it.

        The link from a witness to the acceptance it covers is the witness's own
        ``support_refs``: a witness that does not name what it was taken over could
        be made to license anything, which is exactly what §11.5 says a witness is
        not.  Witnesses for other consumers land under *their* task id rather than
        being filtered later, because the resolver's consumer check would only be
        able to report the last one it happened to see.

        The whole index is built in one pass for the same reason ``read`` fetches
        the acceptance projection once: the readiness gates ask this question for
        every occurrence in the plan, and asking per occurrence turned one read of
        the witness library into a quadratic number of store round-trips.
        """

        index: dict[str, dict[str, ValidityWitness]] = {}
        for witness in self.semantics().list_validity_witnesses(mission_id):
            if witness.purpose is not self.INPUT_WITNESS_PURPOSE:
                continue
            if witness.consumer_ref.kind is not TypedRefKind.TASK:
                continue
            for reference in witness.support_refs:
                if reference.kind is TypedRefKind.ACCEPTANCE:
                    index.setdefault(str(witness.consumer_ref.id), {})[str(reference.id)] = witness
        return index

    def input_witnesses(self, mission_id: str, task_id: str) -> dict[str, ValidityWitness]:
        """acceptance id → the START witness issued to ``task_id`` for it."""

        return self.input_witness_index(mission_id).get(str(task_id), {})

    def issue_input_witnesses(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot,
        *,
        now_ms: int,
        scope_id: str = "mission",
    ) -> tuple[ValidityWitness, ...]:
        """Re-read each accepted output a consumer depends on and record the licence.

        This is the "recompute rather than reuse the old TRUE" half of I19 on the
        DATA lane: for every declared edge whose producer has a recorded accepted
        output, the acceptance is read *now* and a START witness is written for the
        consumer, carrying the acceptance's own support revision.  An acceptance that
        is no longer ``CURRENT`` produces an ``UNUSABLE`` witness rather than none, so
        the refusal downstream says "this was revoked" and not "nobody looked".
        """

        semantics = self.semantics()
        epoch = semantics.epoch(mission_id, scope_id)
        issued: list[ValidityWitness] = []
        for output in self._recorded_outputs(mission_id, network):
            consumers = {
                str(network.binding_for_occurrence(item.consumer_occurrence).task_id)
                for item in network.data_requirements
                if item.producer_occurrence == output.producer_occurrence
                and item.output_port == output.output_port
            }
            for consumer in sorted(consumers):
                acceptance = semantics.get_acceptance(str(output.acceptance_id))
                usable = acceptance.validity is Validity.CURRENT
                witness = ValidityWitness(
                    witness_id="wit-in-"
                    + content_hash_of({"c": consumer, "a": str(output.acceptance_id), "e": epoch})[
                        :28
                    ],
                    consumer_ref=_typed(TypedRefKind.TASK, consumer),
                    purpose=self.INPUT_WITNESS_PURPOSE,
                    truth=TruthValue.TRUE if usable else TruthValue.FALSE,
                    freshness=Validity.CURRENT if usable else acceptance.validity,
                    availability=Availability.READABLE,
                    # ``BLOCKED``, not ``UNAVAILABLE``: the acceptance was read and found
                    # revoked or superseded, which is a different fact from "the
                    # library could not be read".
                    decision=WitnessDecision.USABLE if usable else WitnessDecision.BLOCKED,
                    scope_id=scope_id,
                    scope_epoch=epoch,
                    support_revision=int(output.support_revision),
                    as_of_ms=int(now_ms),
                    support_refs=(_typed(TypedRefKind.ACCEPTANCE, str(output.acceptance_id)),),
                )
                subject = acceptance_subject(str(output.acceptance_id))
                stored = self._record_witness(mission_id, witness, subject=subject)
                if stored is None:
                    # Since migration 18 the subject is part of the key, so this is no
                    # longer the DATA lane colliding with the precondition lane: it is
                    # this consumer already holding a *different* verdict about this
                    # very acceptance at this very reading of the world.  Skipping is
                    # the honest answer — the consumer then reports WAITING_DATA,
                    # which is true — and the anomaly is written down.
                    self._witness_key_taken(mission_id, witness, subject=subject)
                    continue
                issued.append(stored)
        return tuple(issued)

    def _record_witness(
        self, mission_id: str, witness: ValidityWitness, *, subject: str
    ) -> ValidityWitness | None:
        """Store one witness, or answer what is already stored under its identity.

        Three outcomes, none of them an exception for the caller to interpret: the
        row is written; the very same row is already there (the same reading of the
        same world, re-issued) and comes back; or the *key* is held by a different
        licence and the answer is None.

        Since migration 18 the key carries ``subject`` — which object this licence was
        issued for — so the DATA lane and the precondition lane no longer contend for
        one row.  ``None`` now means what it always claimed to mean: two different
        conclusions about the *same* subject at the same reading of the world, which
        is a real contradiction and is reported rather than swallowed.

        Third-round review P2-1 closed the way that claim was still escapable.  Both
        production lanes derive ``witness_id`` from the key's own components, so a
        contention shows up as a **primary-key** conflict rather than an index one —
        and the old recovery re-read the row *by id* and returned it, whatever it
        said.  A second, opposite reading of the same world therefore inherited the
        first reading's ``USABLE`` licence in silence (visible the moment a revoke or
        supersede command exists: the same acceptance goes CURRENT → REVOKED and
        ``issue_input_witnesses`` hands back the old permission).  So the row that
        comes back is now *compared*: identical conclusion → the same licence,
        re-issued; different conclusion → ``None``, and the caller records
        :data:`WITNESS_KEY_TAKEN`.
        """

        try:
            semantics = self.semantics()
            semantics.insert_validity_witness(mission_id, witness, subject=subject)
            return witness
        except StoreError:
            for held in self.semantics().list_validity_witnesses(mission_id):
                if held.witness_id != witness.witness_id:
                    continue
                if _same_conclusion(held, witness):
                    return held
                return None
            return None

    def _witness_key_taken(
        self, mission_id: str, witness: ValidityWitness, *, subject: str
    ) -> None:
        """Record, once, that two conclusions were reached about the same subject.

        Before migration 18 this fired whenever the two licence lanes met, which was
        routine rather than exceptional.  With ``subject_digest`` in the key it fires
        only when the *same* consumer reaches a *different* verdict about the *same*
        subject at the same epoch and support revision — one reading of one world
        giving two answers.  That is a genuine anomaly and is written down.
        """

        append_hierarchical_event(
            self.store,
            WITNESS_KEY_TAKEN,
            mission_id,
            key=f"{mission_id}:{witness.consumer_ref.id}:{witness.scope_epoch}:"
            f"{witness.support_revision}:{subject}",
            task_id=str(witness.consumer_ref.id),
            payload={
                "consumer": str(witness.consumer_ref.id),
                "purpose": str(witness.purpose),
                "subject_digest": str(subject),
                "scope_id": witness.scope_id,
                "scope_epoch": int(witness.scope_epoch),
                "support_revision": int(witness.support_revision),
                "reason_codes": list(witness.reason_codes),
                "detail": (
                    "the validity_witnesses unique index holds one licence per "
                    "consumer per purpose per subject per scope epoch and support "
                    "revision, and this consumer already holds a different verdict "
                    "about that same subject at that same reading of the world; the "
                    "licence was not stored and the occurrence stays withheld rather "
                    "than running on a contested one"
                ),
            },
        )

    #: The reason code that records **which condition** a START-precondition witness
    #: was taken for.  The DATA lane answers that question with ``support_refs`` (the
    #: acceptance it covers); a condition is not a stored object and has no
    #: :class:`TypedRefKind`, so the digest travels as a reason code instead — the
    #: only alternative was guessing the link from the witness, which §11.5 calls
    #: exactly the thing a witness must never allow.  ``knowledge.validity`` owns the
    #: prefix so the storage subject and this module agree by construction.
    CONDITION_REASON_PREFIX = WITNESS_CONDITION_PREFIX

    def issue_start_witnesses(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot | None = None,
        *,
        now_ms: int | None = None,
        scope_id: str = "mission",
    ) -> tuple[ValidityWitness, ...]:
        """Re-read every occurrence's START preconditions and record the licence.

        P2.3c part 2c, found by the real-model smoke.  ``grounding.task_binding_for``
        gives every primitive child the *parent method's* ``applicable_when`` as a
        SELECT :class:`~..contracts.htn.PreconditionRef`, and TG §9 refuses to
        dispatch an occurrence whose START precondition has no ``purpose=START``
        :class:`ValidityWitness`.  Nothing issued one: ``issue_input_witnesses``
        covers the DATA lane only and ``grounding`` deliberately leaves
        ``witness_ref`` empty, so under a gated method every leaf sat in
        ``WAITING_EVIDENCE`` / ``witness_missing`` for ever — the plan committed, the
        duty was admitted, and no work was ever dispatched.

        It is the same shape as the DATA lane and for the same reason (I19): the
        conditions are **evaluated again now**, against the deployment's current
        evidence snapshot, and the verdict is written down with the support revision
        it was taken at.  A condition that no longer holds produces a ``BLOCKED``
        witness rather than none, so the refusal downstream says "the world moved"
        and not "nobody looked"; only an evaluation that
        :func:`~..planning.htn.applicability.authorization_gate` admits — TRUE with
        every leaf backed by a real observation or an authoritative denial (§6.6
        rule 2) — is ``USABLE``.

        One witness covers **all** of a consumer's START preconditions, and names
        each of them in its reason codes.  Since migration 18 the row is filed under
        ``conditions:<digest of that set>``, so "this consumer may start, now, on this
        reading of the world, over *these* conditions" is one row by construction —
        and it sits beside, rather than instead of, the DATA lane's licence for the
        same consumer.  ALL semantics make the combination honest — one UNKNOWN
        member is enough to withhold the whole licence (§6.6 rule 1).
        """

        from ..planning.htn.applicability import (
            all_truth,
            authorization_gate,
            evaluate_condition,
        )

        try:
            world = self._world()
        except ContractError:
            # No PlanningWorld: the predicates and the evidence snapshot a condition is
            # judged against do not exist here, so no licence may be issued.  Fail
            # closed and say nothing new — the occurrences stay withheld with
            # ``witness_missing``, and the deployment defect is already reported by
            # ``HierarchicalAssemblyMissing`` / ``require_planning_world``.
            return ()
        semantics = self.semantics()
        snapshot = world.snapshot()
        network = self.network(mission_id) if network is None else network
        epoch = semantics.epoch(mission_id, scope_id)
        moment = int(self.store.now * 1000) if now_ms is None else int(now_ms)
        forms = {spec.occurrence_id: spec.form for spec in network.occurrences}
        issued: list[ValidityWitness] = []
        for draft in sorted(network.method_instances, key=lambda item: str(item.instance_id)):
            contract = world.registry.definition(draft.method_ref)
            if contract is None:
                # The method left the registry.  That is a recheck problem (§6.6 rule
                # 3), not a licence this method may invent from the frozen verdict.
                continue
            by_digest = {condition_digest(item): item for item in contract.applicable_when}
            if not by_digest:
                continue
            parameters = {binding.name: binding.value for binding in draft.grounded_parameters}
            evaluations = {
                digest: evaluate_condition(
                    by_digest[digest],
                    registry=world.predicates,
                    snapshot=snapshot,
                    parameters=parameters,
                    now_ms=moment,
                )
                for digest in sorted(by_digest)
            }
            truth = all_truth(tuple(item.truth for item in evaluations.values()))
            allowed = all(authorization_gate(item).allowed for item in evaluations.values())
            support: list[TypedRef] = []
            seen: set[tuple[str, str]] = set()
            for digest in sorted(by_digest):
                for reference in self._condition_support(
                    by_digest[digest], parameters=parameters, world=world, snapshot=snapshot
                ):
                    key = (str(reference.kind), str(reference.id))
                    if key in seen:
                        continue
                    seen.add(key)
                    support.append(reference)
            reasons = tuple(
                f"{self.CONDITION_REASON_PREFIX}{digest}" for digest in sorted(by_digest)
            )
            # The whole (sorted) condition set is what this licence covers, so it is
            # what the row is filed under.  ``condition_subject`` is the same function
            # the store re-runs over ``reason_codes`` before it writes.
            subject = condition_subject(by_digest)
            for child in sorted(draft.child_bindings, key=lambda item: str(item.occurrence_id)):
                if forms.get(child.occurrence_id) is not TaskForm.PRIMITIVE:
                    continue
                consumer = str(network.binding_for_occurrence(child.occurrence_id).task_id)
                witness = ValidityWitness(
                    witness_id="wit-pre-"
                    + content_hash_of(
                        {
                            "c": consumer,
                            "e": epoch,
                            "s": int(snapshot.support_revision),
                            "p": str(scope_id),
                            # The subject is part of the row's identity since
                            # migration 18, so it is part of the row's name too: a
                            # different condition set is a different licence, not a
                            # second verdict about the same one.
                            "j": subject,
                        }
                    )[:28],
                    consumer_ref=_typed(TypedRefKind.TASK, consumer),
                    purpose=WitnessPurpose.START,
                    truth=truth,
                    freshness=Validity.CURRENT,
                    availability=Availability.READABLE,
                    decision=(WitnessDecision.USABLE if allowed else WitnessDecision.BLOCKED),
                    scope_id=scope_id,
                    scope_epoch=epoch,
                    support_revision=int(snapshot.support_revision),
                    as_of_ms=moment,
                    support_refs=tuple(support),
                    reason_codes=reasons,
                )
                stored = self._record_witness(mission_id, witness, subject=subject)
                if stored is None:
                    self._witness_key_taken(mission_id, witness, subject=subject)
                    continue
                issued.append(stored)
        return tuple(issued)

    def _condition_support(
        self,
        condition: Any,
        *,
        parameters: Mapping[str, Any],
        world: PlanningWorld,
        snapshot: Any,
    ) -> tuple[TypedRef, ...]:
        """The observations the snapshot holds for this condition's own atoms.

        A licence that named nothing could not be audited back to what was read; a
        licence that named every observation of the Mission would say nothing about
        *this* condition.  Only the atoms of this condition are followed, and only
        as far as the snapshot already went — this method never observes.
        """

        from ..knowledge.predicates import proposition_key
        from ..planning.htn.applicability import ground_value
        from ..planning.htn.registry import iter_predicates

        refs: list[TypedRef] = []
        seen: set[tuple[str, str]] = set()
        for atom in iter_predicates((condition,)):
            signature = world.predicates.resolve(atom.predicate_ref)
            if signature is None:
                continue
            errors: list[str] = []
            arguments = {
                name: ground_value(item, parameters, path="precondition", errors=errors)
                for name, item in atom.arguments.items()
            }
            if errors or not world.predicates.check_arguments(signature, arguments).ok:
                continue
            entry = snapshot.lookup(proposition_key(signature, arguments))
            if entry is None:
                continue
            for reference in entry.observation_refs:
                key = (str(reference.kind), str(reference.id))
                if key in seen:
                    continue
                seen.add(key)
                refs.append(reference)
        return tuple(refs)

    def start_witness_index(self, mission_id: str) -> dict[str, dict[str, ValidityWitness]]:
        """consumer task id → condition digest → the freshest START witness for it.

        "Freshest" is the support revision the witness was taken at, then its clock:
        re-issuing after new evidence must not leave the earlier verdict in play, and
        a witness is keyed by what it concluded, so both rows exist in the library.
        """

        index: dict[str, dict[str, ValidityWitness]] = {}
        for witness in self.semantics().list_validity_witnesses(mission_id):
            if witness.purpose is not WitnessPurpose.START:
                continue
            if witness.consumer_ref.kind is not TypedRefKind.TASK:
                continue
            digests = [
                code[len(self.CONDITION_REASON_PREFIX) :]
                for code in witness.reason_codes
                if code.startswith(self.CONDITION_REASON_PREFIX)
            ]
            for digest in digests:
                held = index.setdefault(str(witness.consumer_ref.id), {}).get(digest)
                if held is not None and (held.support_revision, held.as_of_ms) >= (
                    witness.support_revision,
                    witness.as_of_ms,
                ):
                    continue
                index.setdefault(str(witness.consumer_ref.id), {})[digest] = witness
        return index

    def start_witnesses(self, mission_id: str, task_id: str) -> dict[str, ValidityWitness]:
        """condition digest → the START witness issued to ``task_id`` for it."""

        return self.start_witness_index(mission_id).get(str(task_id), {})

    def resolved_inputs(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot,
        spec: OccurrenceSpec,
        *,
        accepted: AcceptedOutputsIndex | None = None,
        witnesses: Mapping[str, ValidityWitness] | None = None,
    ) -> ResolutionResult:
        """The DATA gate's input, never ``None`` (P2.3c part 2).

        ``input_result`` answers ``None`` for an occurrence that declares no data
        requirement, and :func:`~..graph.eligibility.evaluate_readiness` reads ``None``
        as *"no input resolution was supplied"* — which is ``WAITING_DATA``.  That is
        the right answer for a caller that forgot to resolve, and the wrong one for an
        occurrence that has nothing to resolve: before this, every input-less leaf in
        the plan sat in ``WAITING_DATA`` forever and nothing could ever be dispatched.

        So an occurrence with no declared requirement gets an **empty frozen**
        manifest rather than no manifest.  "This dispatch consumed nothing" is a
        statement an ``Acceptance`` can quote and an ``input_manifest_hash`` can
        cover; "nobody resolved the inputs" is not, and the two must not share a
        representation.
        """

        result = self.input_result(
            mission_id, network, spec, accepted=accepted, witnesses=witnesses
        )
        if result is not None:
            return result
        return ResolutionResult(manifest=InputManifest(consumer_task_ref=spec.task_id))

    def accepted_outputs(
        self,
        mission_id: str,
        network: TaskNetworkSnapshot,
        *,
        outcomes: Mapping[OccurrenceId, OccurrenceOutcome] | None = None,
    ) -> AcceptedOutputsIndex:
        """What the resolver may choose from.

        P2.3b reads the *completed producers* from the outcome projection and takes
        the accepted outputs a deployment recorded through ``bound_inputs``; a
        deployment that has not wired its acceptance index yet therefore gets
        ``WAITING_DATA`` rather than a silent all-ancestors sweep.
        """

        settled = self.occurrence_outcomes(mission_id, network) if outcomes is None else outcomes
        completed = frozenset(
            occurrence
            for occurrence, outcome in settled.items()
            if outcome is OccurrenceOutcome.ACCEPTED
        )
        return AcceptedOutputsIndex(
            outputs=tuple(self._recorded_outputs(mission_id, network)),
            completed_producers=completed,
        )

    def declared_output_ports_for(
        self, mission_id: str, task_id: str, network: TaskNetworkSnapshot | None = None
    ) -> tuple[dict[str, Any], ...]:
        """The output ports this leaf is expected to deliver on, for its own context.

        P2.3c part 2d, decision 4.  Part 2c's smoke ended here: the plan declared one
        output port, the model wrote two files under names of its own, and nothing had
        ever told it that ``repository_facts`` was the name the plan used.  The accept
        side then paired by substring (``artifacts/…`` matches a port called ``facts``)
        or by "one port, one file" — the guess TG design §10.2 forbids — or, honestly
        but uselessly, not at all.

        The ports come from :func:`~.accepted_outputs.output_ports_in_revision`, the
        same function the accept side checks against, so "which ports exist" has one
        answer rather than two.  A port is in this list because a ``DataRequirement``
        consumes it **or** because a ``criterion_link`` of the adopted method points at
        this occurrence (P2.3d / defect D3: the finalizer step's port has no downstream
        edge and the root's success criterion still reads it, so a leaf that was never
        told the port existed delivered nothing and the root review rejected the
        Mission).  An occurrence that is neither consumed nor criterion-linked feeds
        nobody and is not asked for at all.

        Deliberately **not** intersected with the binding's own ``output_ports``.  The
        memo describes the two sources as an intersection, and a port a live edge
        consumes while the producer's contract does not declare it *is* a real defect —
        but it is a defect in the **plan**, and answering it by quietly dropping the
        port here would hide it: the leaf would then be told to produce nothing, the
        consumer would wait for a port nobody was asked for, and the Mission would stall
        with no reason anybody could read.  Plan integrity is where that belongs, and
        until it refuses there, the honest thing is to ask for the port the edge needs
        and let ``OUTPUT_PORT_UNCLAIMED`` name it if it never arrives.  The binding is
        consulted for ``cardinality`` only, because that is where TG §4.3 declares it;
        a port the binding does not list is reported ``single``, the contract's own
        default.
        """

        from .accepted_outputs import output_ports_in_revision

        semantics = self.semantics()
        active = semantics.active_plan_revision(mission_id)
        if active is None:
            return ()
        revision = int(active.revision)
        occurrence = next(
            (
                spec.occurrence_id
                for spec in semantics.list_plan_memberships(mission_id, revision)
                if str(spec.task_id) == str(task_id)
            ),
            None,
        )
        if occurrence is None:
            return ()
        ports = output_ports_in_revision(
            semantics, mission_id, revision, occurrence, str(task_id)
        )
        binding = semantics.task_semantics_of(mission_id, str(task_id))
        specs = {} if binding is None else {item.port_key: item for item in binding.output_ports}
        del network  # the rows are the authority here; the network is not rebuilt
        return tuple(
            {
                "port": port,
                "required": True,
                "cardinality": (
                    str(specs[port].cardinality) if port in specs else str(PortCardinality.SINGLE)
                ),
                "schema": {"id": schema.id, "version": int(schema.version)},
            }
            for port, schema in sorted(ports.items())
        )

    def _recorded_outputs(self, mission_id: str, network: TaskNetworkSnapshot) -> Sequence[Any]:
        """The accepted outputs the resolver may choose from (P2.3c part 2).

        Read from migration 17's ``acceptance_outputs``, which is the *recorded*
        answer to "which artifact did this Acceptance accept, at which output port".
        P2.3b returned nothing here and said why: nothing in the schema held that
        fact, and taking the Attempt's artifacts and guessing the port from the path
        would be the all-ancestors sweep §24.1 decision 4 removed, wearing a typed
        name.

        The one writer is ``ResolutionCommitsMixin.accept_review`` (P2.3c part 2b),
        and it writes inside its own transaction only what
        :func:`.accepted_outputs.check_against_ports` passed — the same rule
        :func:`.accepted_outputs.check_declared` applies to a whole network, read off
        the committed ``DataRequirement`` rows instead of a rebuilt snapshot — so an
        entry exists only where the plan drew an edge.  A static guard in the suite
        keeps that the *only* writer.

        Two rows are dropped rather than offered, for the same reason in two lanes:

        * an occurrence this revision no longer contains — an accepted output of a
          retired branch is history, and the resolver choosing from it would bind a
          consumer to work the current plan does not do;
        * an entry whose ``Acceptance`` is no longer CURRENT — ``list_acceptance_outputs``
          joins ``acceptances`` and filters on validity (review F3), so a supersede or a
          revoke stops feeding consumers at the read rather than after them.
        """

        live = {str(spec.occurrence_id) for spec in network.occurrences}
        outputs: list[AcceptedOutput] = []
        for row in self.semantics().list_acceptance_outputs(mission_id):
            if str(row.get("producer_occurrence")) not in live:
                continue
            outputs.append(accepted_output_from_json(row))
        return tuple(outputs)

    # ------------------------------------------------------------------ the three reads
    def occurrences(self, mission_id: str) -> tuple[TaskView, ...]:
        """The new mode's ``list_tasks``: typed views in projection order."""

        view = self.read(mission_id)
        projection = view.network.execution_projection()
        ordered = [
            occurrence
            for occurrence in view.views
            if occurrence in projection.projected_occurrences
        ]
        unprojected = [
            occurrence
            for occurrence in view.views
            if occurrence not in projection.projected_occurrences
        ]
        order = [*sorted(ordered, key=str), *sorted(unprojected, key=str)]
        return tuple(view.views[occurrence] for occurrence in order)

    def ready_occurrences(self, mission_id: str) -> tuple[OccurrenceId, ...]:
        """The ExecutionFrontier — not a ``TaskStatus.READY`` scan (TG §8.1)."""

        return self.read(mission_id).execution_frontier.occurrences

    def running_occurrences(self, mission_id: str) -> tuple[OccurrenceId, ...]:
        """The concurrency count: adopted primitive occurrences with a live Attempt.

        The *set* comes from the projection, so a compound is never counted as
        running work and a retired branch is never counted at all; the liveness
        comes from the Attempt rows, which is where it actually lives.
        """

        view = self.read(mission_id)
        projected = view.network.execution_projection().projected_occurrences
        live: list[OccurrenceId] = []
        for spec in view.network.occurrences:
            if spec.occurrence_id not in projected or spec.form is not TaskForm.PRIMITIVE:
                continue
            attempts = self.store.list_attempts(str(spec.task_id))
            if any(str(attempt.status) == "RUNNING" for attempt in attempts):
                live.append(spec.occurrence_id)
        return tuple(sorted(live, key=str))

    def root_review_ready(self, mission_id: str) -> bool:
        """Whether the root review may run at all (§7.2, TG §6.2).

        Every gating child of every adopted root method needs an accepted outcome
        first.  The parent's own state is deliberately not consulted: a review that
        waits for the parent it is supposed to conclude does not terminate.
        """

        view = self.read(mission_id)
        if view.plan.integrity_error is not None:
            return False
        roots = view.network.root_occurrence_ids
        if not roots:
            return False
        for root in roots:
            spec = view.network.occurrence(root)
            if spec.form is not TaskForm.COMPOUND:
                if view.outcomes.get(root) is not OccurrenceOutcome.ACCEPTED:
                    return False
                continue
            children = view.network.adopted_children(root)
            if not children:
                return False
            for binding in children:
                if binding.requiredness not in GATING_REQUIREDNESS:
                    continue
                if view.outcomes.get(binding.occurrence_id) is not OccurrenceOutcome.ACCEPTED:
                    return False
        return True

    def terminal(self, mission_id: str) -> bool:
        """Whether the Mission's work is finished, read from the resolutions."""

        view = self.read(mission_id)
        required = set(view.network.required_obligations)
        if not required:
            return False
        semantics = self.semantics()
        return all(
            semantics.adopted_goal_resolution(mission_id, str(duty)) is not None
            for duty in sorted(required, key=str)
        )

    # --------------------------------------------------------- the root acceptance gate
    def root_contributions(self, mission_id: str) -> dict[str, tuple[str, ...]]:
        """Which occurrences of the adopted root method have a **CURRENT** Acceptance.

        Read from the ``acceptances`` rows and not from the outcome projection.  They
        usually agree, and where they do not the store is right: an Acceptance that was
        superseded or revoked since the projection was computed is history, and a root
        resolution quoting it would be declaring the Mission complete on the strength
        of work nobody accepts any more (§21.5 "wrongly declared complete = 0").
        """

        semantics = self.semantics()
        network = self.network(mission_id)
        by_occurrence: dict[str, list[str]] = {}
        for acceptance in semantics.list_acceptances(mission_id):
            if acceptance.validity is not Validity.CURRENT:
                continue
            for spec in network.occurrences:
                if str(spec.task_id) != str(acceptance.task_id):
                    continue
                if str(spec.obligation_id) != str(acceptance.obligation_id):
                    continue
                by_occurrence.setdefault(str(spec.occurrence_id), []).append(
                    str(acceptance.acceptance_id)
                )
        return {key: tuple(sorted(value)) for key, value in by_occurrence.items()}

    def root_resolution_inputs(self, mission_id: str) -> RootResolutionInputs:
        """Everything the root ``commit_goal_resolution`` command is built from.

        Every field is *read*, never invented.  The three that a deployment has to
        have produced beforehand — the ``MISSION_FINAL`` review package, its official
        record, and the ``purpose=ACCEPT`` witness — are reported as missing rather
        than fabricated: a Mission root resolution is formed out of the success
        formula plus the final acceptance (§6.3, §8.1), so a system that writes its
        own review anchor has decided the answer it was supposed to check.
        """

        network = self.network(mission_id)
        roots = network.root_occurrence_ids
        if len(roots) != 1:
            return RootResolutionInputs(
                reason="ROOT_NOT_SINGULAR",
                detail=(
                    f"this plan revision has {len(roots)} root occurrence(s); a Mission root "
                    "resolution is formed for one root goal and choosing among several would "
                    "be this module inventing a root"
                ),
            )
        root = roots[0]
        spec = network.occurrence(root)
        binding = network.binding_for_occurrence(root)
        semantics = self.semantics()
        existing = semantics.adopted_goal_resolution(mission_id, str(spec.obligation_id))
        if existing is not None:
            return RootResolutionInputs(
                reason="ALREADY_RESOLVED",
                detail=(
                    f"duty {spec.obligation_id!s} is already resolved by {existing.resolution_id!s}"
                ),
                occurrence_id=str(root),
            )
        package = self.live_root_review_package(
            mission_id, task_id=str(spec.task_id), obligation_id=str(spec.obligation_id)
        )
        if package is None:
            return RootResolutionInputs(
                reason="ROOT_REVIEW_PACKAGE_MISSING",
                detail=(
                    f"no MISSION_FINAL ReviewPackage is stored for task {spec.task_id!s}; the "
                    "root review has not been cut, so there is nothing to resolve from"
                ),
                occurrence_id=str(root),
            )
        # Review P1-7: the root's requirements are **the ones its review package was
        # cut against**, not whatever revision happens to be latest.
        # ``latest_requirements_revision`` used to answer this, but every leaf
        # acceptance publishes a Mission-level revision carrying *that leaf's*
        # coverage criteria — so one more leaf accepted between cutting the root
        # review and forming the resolution silently moved the Mission's criteria to
        # the last leaf's, and the resolution claimed coverage of something the
        # reviewer never saw.  The package's binding is the only revision that was
        # actually reviewed.
        try:
            requirements = semantics.get_requirements_revision(
                mission_id, int(package.binding.requirements_revision)
            )
        except StoreError:
            requirements = None
        if requirements is None:
            return RootResolutionInputs(
                reason="REQUIREMENTS_MISSING",
                detail=(
                    "the root review package names requirements revision "
                    f"{int(package.binding.requirements_revision)}, which this Mission does not "
                    "hold; there is nothing for the root resolution to claim coverage of (§6.3)"
                ),
                occurrence_id=str(root),
            )
        record = semantics.official_review_record(str(package.package_id))
        if record is None:
            return RootResolutionInputs(
                reason="ROOT_REVIEW_RECORD_MISSING",
                detail=(
                    f"review package {package.package_id!s} has no official ReviewRecord; the "
                    "final review has not concluded"
                ),
                occurrence_id=str(root),
            )
        witness = next(
            (
                item
                for item in semantics.list_validity_witnesses(mission_id)
                if item.purpose is WitnessPurpose.ACCEPT
                and item.consumer_ref.kind is TypedRefKind.TASK
                and item.consumer_ref.id == str(spec.task_id)
            ),
            None,
        )
        if witness is None:
            return RootResolutionInputs(
                reason="ROOT_WITNESS_MISSING",
                detail=(
                    f"no purpose=ACCEPT ValidityWitness is stored for task {spec.task_id!s}; a "
                    "witness is not transferable and the commit consumes one (§11.5)"
                ),
                occurrence_id=str(root),
            )
        instance = network.adopted_instance_for(root)
        contributions = self.root_contributions(mission_id)
        return RootResolutionInputs(
            reason="",
            occurrence_id=str(root),
            task_id=str(spec.task_id),
            obligation_id=str(spec.obligation_id),
            contract_revision=int(binding.contract_revision),
            method_instance_id=None if instance is None else str(instance.instance_id),
            requirements=requirements,
            package=package,
            record=record,
            witness_id=str(witness.witness_id),
            contributions=contributions,
        )

    def superseded_review_packages(self, mission_id: str) -> frozenset[str]:
        """The ``MISSION_FINAL`` packages a re-cut has retired (P2.3c part 3a).

        A :class:`~...contracts.resolution.ReviewPackage` is an immutable anchor, so
        "this review no longer counts" cannot be a column on it — it is the
        :data:`ROOT_REVIEW_SUPERSEDED` event, and this is the one reader of it.
        """

        return frozenset(
            str(event.payload.get("package_id", ""))
            for event in self.store.list_events(mission_id)
            if event.type == ROOT_REVIEW_SUPERSEDED
        )

    def live_root_review_package(
        self, mission_id: str, *, task_id: str, obligation_id: str
    ) -> ReviewPackage | None:
        """The ``MISSION_FINAL`` package this root resolves from, or ``None``.

        Three rules, in order, and each of them exists because of a way this could
        answer wrongly:

        1. only packages cut for **this** root, so a Mission with two roots in its
           history cannot have one root's review resolve the other;
        2. never a **superseded** one — the whole point of a re-cut is that the older
           anchor described a world that has moved;
        3. among what is left, the **last one this deployment cut**.  The order comes
           from the :data:`ROOT_REVIEW_CUT` events rather than from ``created_at``,
           because two packages written in the same clock tick have no order in the
           rows at all.  A package nobody recorded a cut for is not ranked against
           one that was: a recorded cut is this deployment saying "this is the anchor
           now", and an unrecorded package is one somebody stored directly — so the
           recorded ones win outright, and the old first-stored answer is kept for a
           Mission that has none.
        """

        semantics = self.semantics()
        retired = self.superseded_review_packages(mission_id)
        candidates = [
            item
            for item in semantics.list_review_packages(
                mission_id, purpose=ReviewPurpose.MISSION_FINAL
            )
            if str(item.binding.subject_ref.id) == str(task_id)
            and str(item.binding.obligation_id) == str(obligation_id)
            and str(item.package_id) not in retired
        ]
        if not candidates:
            return None
        order = {
            str(event.payload.get("package_id", "")): index
            for index, event in enumerate(
                item for item in self.store.list_events(mission_id) if item.type == ROOT_REVIEW_CUT
            )
        }
        recorded = [item for item in candidates if str(item.package_id) in order]
        if not recorded:
            return candidates[0]
        recorded.sort(key=lambda item: order[str(item.package_id)])
        return recorded[-1]

    def attempt_root_resolution(
        self,
        mission_id: str,
        *,
        principal: PlanPrincipal,
        command_id: str,
        resolution_id: str | None = None,
        input_manifest_hash: str | None = None,
        required_delivery_stage: Any = None,
        delivery_receipt_ids: Sequence[str] = (),
        source: Mapping[str, Any] | None = None,
    ) -> RootResolutionOutcome:
        """Form the Mission's root :class:`GoalResolution`, or say why not.

        This is the only place a Mission's root resolution is proposed from, and it is
        deliberately a *proposal*: every rule that decides whether it may be formed —
        the adopted method, a valid Acceptance for every required child, coverage of
        every root criterion, the delivery contract, the witness — lives in
        ``ResolutionCommitsMixin.commit_goal_resolution`` and runs inside its
        transaction.  What this method does is read the facts the command is made of
        and hand them over; if it also decided, there would be two places that could
        declare a Mission complete and §21.5's "wrongly declared complete = 0" would
        depend on both of them agreeing.
        """

        self.require_hierarchical(mission_id)
        if not self.root_review_ready(mission_id):
            return RootResolutionOutcome(
                committed=False,
                reason="ROOT_REVIEW_NOT_READY",
                detail="a gating child of the adopted root method has no accepted outcome yet",
            )
        inputs = self.root_resolution_inputs(mission_id)
        if inputs.reason:
            return RootResolutionOutcome(
                committed=inputs.reason == "ALREADY_RESOLVED",
                reason=inputs.reason,
                detail=inputs.detail,
            )
        requirements = inputs.requirements
        assert requirements is not None and inputs.package is not None
        assert inputs.record is not None
        manifest_hash = input_manifest_hash or inputs.package.binding.input_manifest_hash
        resolution = GoalResolution(
            resolution_id=GoalResolutionId(resolution_id or f"res-{inputs.occurrence_id}"),
            mission_id=mission_id,
            obligation_id=inputs.obligation_id,
            goal_task_id=inputs.task_id,
            requirements_version=int(requirements.revision),
            contract_revision=int(inputs.contract_revision),
            method_instance_id=inputs.method_instance_id,
            input_manifest_hash=manifest_hash,
            artifact_refs=(),
            child_resolution_ids=(),
            # Review F4: every verdict is **read from the official review record**, and
            # a criterion the record did not judge is written ``UNKNOWN``.  Writing
            # ``PASS`` for all of them — what this used to do — put an unevidenced
            # assertion into a permanent record: the accept side only refuses a verdict
            # that *contradicts* the record and a required criterion that is *missing*,
            # so a criterion nobody reviewed and no rule quotes would have been stored
            # as passed forever.  It is the same rule this command already follows for
            # ``composition_obligation_passed``: restate the review, never overrule or
            # extend it.  A required criterion the record left unjudged now shows up as
            # UNKNOWN and the AER §6.2 formula answers for it, instead of the trigger
            # answering on the reviewer's behalf.
            criteria=_root_criteria(requirements, inputs.record),
            review_receipt_id=str(inputs.record.record_id),
            verdict=ReviewVerdict.ACCEPT,
            validity=Validity.CURRENT,
        )
        command = CommitGoalResolutionCommand(
            command_id=command_id,
            mission_id=mission_id,
            resolution=resolution,
            package=inputs.package,
            record=inputs.record,
            requirements=requirements,
            witness_id=inputs.witness_id,
            # No defaults anywhere, and nothing asserted: the facts are **read off
            # the package**, which is the frozen anchor that recorded who produced
            # the candidate at the time it was cut (AER §5.3).  Part 3a's review
            # coordinator fills those in, so a root review whose reviewer is one of
            # the producers now refuses (``INDEPENDENT_REVIEW_MISSING``) instead of
            # passing vacuously on an empty set.  ``reviewer_can_write_candidate`` is
            # derived from the package's own workspace access rather than stated:
            # ``WRITE`` is refused at construction, so this reads False for every
            # package that exists — and it reads it from the anchor instead of from
            # a caller who could say otherwise.
            independence=IndependenceFacts(
                producer_agent_ids=tuple(inputs.package.producer_agent_ids),
                reviewer_can_write_candidate=(
                    inputs.package.reviewer_workspace_access is WorkspaceAccess.WRITE
                ),
            ),
            posture=ExecutionPosture(),
            read_set=self.read_set_for_root(mission_id, inputs),
            decided_at_ms=int(self.store.now * 1000),
            purpose=ReviewPurpose.MISSION_FINAL,
            # ``CompoundFacts`` is "the two things only the caller can know", and both
            # are *read* here rather than asserted.  ``composition_obligation_passed``
            # in particular: hard-coding ``True`` would be this trigger claiming, on
            # the reviewer's behalf, that the composition held — which is precisely the
            # shape "wrongly declared complete = 0" exists to forbid.  It is the
            # official review record's own verdict.  (The *contributions* are never
            # taken from the command either: ``commit_goal_resolution`` re-derives them
            # from the store and refuses a mismatch with
            # ``COMPOUND_FACTS_CONTRADICT_STORE``.)
            compound=CompoundFacts(
                selected_method_legal=inputs.method_instance_id is not None,
                contributing_occurrence_ids=tuple(sorted(inputs.contributions)),
                composition_obligation_passed=inputs.record.verdict is ReviewVerdict.ACCEPT,
            ),
            is_mission_root=True,
            required_delivery_stage=required_delivery_stage,
            delivery_receipts=tuple(str(item) for item in delivery_receipt_ids),
            issued_by=principal.principal_id,
            scope_id=principal.scope_id,
            source=dict(source or {}),
        )
        # The accept side authenticates against its *own* principal type.  Two types
        # rather than one shared "principal" because the two sides authorise different
        # things: ``PlanPrincipal`` carries the manager epoch a plan commit is checked
        # against, and passing it here would be refused as ``BAD_PRINCIPAL`` — which is
        # exactly what it should be, since the caller would not have said whose accept
        # authority it is claiming.
        accepting = ResolutionPrincipal(
            principal_id=principal.principal_id, scope_id=principal.scope_id
        )
        try:
            receipt = self.commit.commit_goal_resolution(command, accepting)
        except ResolutionCommitRejected as error:
            self._append(
                ROOT_RESOLUTION_REFUSED,
                mission_id,
                key=f"{mission_id}:{command_id}:{error.reason}",
                task_id=inputs.task_id,
                payload={
                    "reason": error.reason,
                    "detail": error.detail,
                    "command_id": command_id,
                    "occurrence_id": inputs.occurrence_id,
                    "obligation_id": inputs.obligation_id,
                },
            )
            return RootResolutionOutcome(committed=False, reason=error.reason, detail=error.detail)
        return RootResolutionOutcome(
            committed=True,
            reason="",
            resolution_id=str(receipt.resolution_id),
            detail="",
        )

    def read_set_for_root(self, mission_id: str, inputs: RootResolutionInputs) -> SemanticReadSet:
        """The semantic read-set the root commit is checked against.

        It names what this proposal actually read: the root task's contract revision,
        the requirements revision, the manager epoch and the mission scope epoch.  The
        commit re-reads every one of them, so an epoch that moved between this read and
        the transaction refuses the command — which is the point of recording it rather
        than letting the commit assume nothing changed.
        """

        semantics = self.semantics()
        binding = semantics.task_semantics_of(mission_id, inputs.task_id)
        goal_reads: tuple[ReadItem, ...] = ()
        if binding is not None:
            goal_reads = (
                ReadItem(
                    kind=ReadItemKind.TASK,
                    id=str(binding.task_id),
                    semantic_revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
            )
        assert inputs.requirements is not None
        return SemanticReadSet(
            requirements_revision=int(inputs.requirements.revision),
            goal_revisions=goal_reads,
            manager_epoch=semantics.epoch(mission_id, "mission"),
            scope_epochs=(
                ScopeEpochRead(
                    scope_id="mission", validity_epoch=semantics.epoch(mission_id, "mission")
                ),
            ),
        )

    # ------------------------------------------------------- the admission transaction
    def admissions(self, mission_id: str, *, now_ms: int | None = None) -> DispatchAdmissions:
        """Run the readiness gate over the whole plan and admit what passed (TG §8.3).

        This is the input :func:`~..scheduling.allocator.allocate_v2` takes, and it is
        the answer to P2.3b's blocker (b): before P2.3c part 2 the event handler only
        asked the *form* gate before creating an Attempt, so a DATA consumer whose
        producer had not been accepted was dispatched with no inputs and ran anyway.
        Now every occurrence goes through ``evaluate_readiness`` first and only a
        ``READY_CANDIDATE`` reaches ``admit_for_dispatch``.

        Three properties worth stating, because each is a way this could have been
        written wrongly:

        * **One read.**  ``self.read()`` fetches the acceptance projection, the
          witnesses and the duty accounts once and every occurrence is judged against
          that same snapshot, so the plan cannot move between two occurrences'
          verdicts — and ``admit_for_dispatch``'s ``_same_origin`` check would refuse
          the admission if it had.
        * **A refusal is never an admission with a flag.**  An occurrence that is not
          ready simply has no :class:`EligiblePrimitiveTask`; the allocator takes only
          admissions, so there is no field a caller could misread as a permission.
        * **``NotEligible`` at this point is fail-closed, not a retry.**  A report that
          said ``READY_CANDIDATE`` and an admission that refuses it means the report
          and the view disagree — a race or a caller bug — and the honest answer is to
          withhold this occurrence with the refusal text, not to admit it anyway.
        """

        view = self.read(mission_id, now_ms=now_ms)
        moment = int(self.store.now * 1000) if now_ms is None else int(now_ms)
        network = view.network
        # Review F8: taken from the view, not read again.  The acceptance projection and
        # the DATA lane's licences (keyed by acceptance id per consumer — see ``read``
        # for why the precondition map cannot answer that question) are what the
        # readiness reports on this view were computed against, and hashing a manifest
        # built from a *second* read would mean the report and the admission describe
        # two different moments.
        accepted = view.accepted
        licences = view.licences
        bindings: dict[str, TaskSemanticBindingV1] = {}
        readiness: dict[str, EligiblePrimitiveTask] = {}
        refusals: list[DispatchRefusal] = []
        for spec in network.occurrences:
            task_id = str(spec.task_id)
            task_view = view.views[spec.occurrence_id]
            if task_view.binding is not None:
                bindings[task_id] = task_view.binding
            report = view.reports[spec.occurrence_id]
            if not report.ready:
                refusals.append(
                    DispatchRefusal(
                        task_id=task_id,
                        occurrence_id=str(spec.occurrence_id),
                        reason=report.reason,
                        detail_codes=report.detail_codes,
                        detail="; ".join(detail.message for detail in report.details),
                    )
                )
                continue
            result = self.resolved_inputs(
                mission_id,
                network,
                spec,
                accepted=accepted,
                witnesses=licences.get(task_id, {}),
            )
            manifest = (
                result.manifest
                if result.manifest is not None
                else InputManifest(consumer_task_ref=spec.task_id)
            )
            try:
                readiness[task_id] = admit_for_dispatch(
                    report, task_view, view.plan, manifest, now_ms=moment
                )
            except (NotEligible, ManifestNotFrozen) as error:
                refusals.append(
                    DispatchRefusal(
                        task_id=task_id,
                        occurrence_id=str(spec.occurrence_id),
                        reason=ReadinessReason.STALE_BINDING,
                        detail_codes=("admission_refused",),
                        detail=str(error),
                    )
                )
        return DispatchAdmissions(
            plan_revision=int(network.plan_revision),
            bindings=bindings,
            readiness=readiness,
            refusals=tuple(refusals),
        )

    def record_withheld(self, mission_id: str, admissions: DispatchAdmissions) -> tuple[Event, ...]:
        """Record every structured refusal once per (occurrence, revision, reason).

        The idempotency key carries the plan revision *and* the reason, and
        ``Store.append_event`` returns the stored row for a key it already holds — so
        a Mission that waits ten cycles for a producer leaves one event rather than
        ten, and a Mission whose reason *changes* leaves the new one beside it.  That
        is the difference between a log an operator can read and a log that drowns the
        one line that mattered.
        """

        return tuple(
            self._append(
                DISPATCH_WITHHELD,
                mission_id,
                key=(
                    f"{mission_id}:{refusal.task_id}:{admissions.plan_revision}:{refusal.reason!s}"
                ),
                task_id=refusal.task_id,
                payload={"plan_revision": int(admissions.plan_revision), **refusal.to_json()},
            )
            for refusal in admissions.refusals
        )

    # -------------------------------------------------------------- the dispatch gate
    def intercept_worker_dispatch(
        self, mission_id: str, task_id: str
    ) -> DispatchInterception | None:
        """Refuse a compound before the old READY entry can dispatch it.

        The gate is ``form`` from the semantic binding (§18.5 rule 4), asked of
        :func:`legacy_ready_is_not_eligibility` so the allocator wiring in P2.3c and
        this one cannot disagree about the answer.  A primitive returns None here —
        which is not a permission, only "this gate has nothing to say".
        """

        binding = self.semantics().task_semantics_of(mission_id, task_id)
        if binding is None:
            raise missing_bindings(mission_id, [task_id])
        task = self.store.get_task(task_id)
        verdict = legacy_ready_is_not_eligibility(
            "" if task is None else str(task.status), form=binding.form
        )
        if verdict.gate_reason is None:
            return None
        occurrence = self._occurrence_of(mission_id, task_id)
        interception = DispatchInterception(
            task_id=task_id,
            occurrence_id=None if occurrence is None else str(occurrence),
            reason=str(verdict.gate_reason),
            explanation=verdict.explanation,
        )
        self._append(
            DISPATCH_INTERCEPTED,
            mission_id,
            key=f"{mission_id}:{task_id}:{interception.reason}",
            task_id=task_id,
            payload={
                "task_id": task_id,
                "occurrence_id": interception.occurrence_id,
                "form": str(binding.form),
                "reason": interception.reason,
                "legacy_status": "" if task is None else str(task.status),
                "explanation": interception.explanation,
            },
        )
        return interception

    def record_integrity_failure(self, mission_id: str, error: GraphIntegrityError) -> Event:
        """Record that one Mission's plan is damaged, with the diagnosis (TG §14.3).

        The durable record is the point: the caller is about to stop *this* Mission
        and leave every other Mission in the run alone, so what went wrong has to be
        readable afterwards without re-deriving it from a traceback that no longer
        exists.  Deliberately no topological order in the payload — a damaged
        projection's healthy prefix is a partial order, not a plan.
        """

        code = getattr(error, "code", "projection_not_orderable")
        return self._append(
            PLAN_INTEGRITY_FAILED,
            mission_id,
            key=f"{mission_id}:{code}:{content_hash_of(sorted(error.remaining))[:16]}",
            payload={
                "code": str(code),
                "subjects": sorted(str(item) for item in error.remaining),
                "cycle": [str(item) for item in error.cycle],
                "diagnose": error.diagnose(),
            },
        )

    def _occurrence_of(self, mission_id: str, task_id: str) -> OccurrenceId | None:
        try:
            network = self.network(mission_id)
        except GraphIntegrityError:
            return None
        for spec in network.occurrences:
            if str(spec.task_id) == task_id:
                return spec.occurrence_id
        return None

    # ----------------------------------------------------------- compound phase driver
    def advance_compound_phases(self, mission_id: str) -> dict[OccurrenceId, CompoundPhase]:
        """Run the typed reducer over every compound and record what changed.

        No Attempt is created and no legacy Task row is written: the phase is a
        *projection* of typed state, so the only durable trace is the event.
        """

        view = self.read(mission_id)
        phases: dict[OccurrenceId, CompoundPhase] = {}
        for spec in view.network.occurrences:
            if spec.form is not TaskForm.COMPOUND:
                continue
            children = {
                binding.occurrence_id: view.outcomes.get(
                    binding.occurrence_id, OccurrenceOutcome.UNKNOWN
                )
                for binding in view.network.adopted_children(spec.occurrence_id)
            }
            phase = next_compound_phase(
                spec,
                view.network,
                view.reports[spec.occurrence_id],
                child_outcomes=children,
                resolved=spec.occurrence_id in view.resolved,
            )
            phases[spec.occurrence_id] = phase
            self._append(
                COMPOUND_PHASE_CHANGED,
                mission_id,
                key=f"{mission_id}:{spec.occurrence_id!s}:{int(view.network.plan_revision)}:{phase!s}",
                task_id=str(spec.task_id),
                payload={
                    "occurrence_id": str(spec.occurrence_id),
                    "task_id": str(spec.task_id),
                    "plan_revision": int(view.network.plan_revision),
                    "phase": str(phase),
                    "display_status": str(COMPOUND_DISPLAY_STATUS[phase]),
                    "readiness_reason": str(view.reports[spec.occurrence_id].reason),
                },
            )
        return phases

    # ------------------------------------------------------------------ one plan round
    def admit_method_proposal(self, text: str, **kwargs: Any) -> AdmissionReceipt:
        """``<method_proposal>`` → the registry's admission protocol (§7.3).

        **Not called from ``src`` yet — P2.3c wires it.**  The MethodSynthesizer is
        its own dispatch intent with its own budget account (§18.5: a new role may
        not wear the TaskCritic's), and opening that intent belongs with the
        allocator work.  The parse and the admission call are here because they are
        assembly, and assembling them once is what stops the two call sites P2.3c
        will add from disagreeing about whether a declared ``registry_status`` is
        dropped (it is not: the protocol refuses it and records the refusal).
        """

        world = self._world()
        proposal = parse_method_proposal(text)
        return world.registry.admit(proposal, **kwargs)

    # ------------------------------------------------------- the MethodSynthesizer
    def synthesis_request(
        self, mission_id: str, goal_task_id: str, *, domain: str | None = None
    ) -> Any:
        """The typed context one synthesis round is given (§7.3 source 4, §18.5 C8).

        Built from the deployment's own declarations — the operators it really
        registered, the capability table, and the four-axis report explaining why
        each existing method for this goal type does not apply.  Nothing about the
        Mission, the principal or any budget account is in it: which Mission this is
        belongs to the *dispatch* that carries the request, never to the payload the
        model reads.
        """

        from ..planning.htn.applicability import assess_method as _assess
        from ..planning.htn.synthesis import build_request

        world = self._world()
        network = self.network(mission_id)
        goal = network.binding_for_task(TaskRef(str(goal_task_id)))
        reports: dict[str, Any] = {}
        signature = goal.goal_signature
        for reference in world.registry.method_refs():
            contract = world.registry.definition(reference)
            if contract is None or contract.goal_type_ref.id != signature.signature_id:
                continue
            reports[f"{contract.method_id}@{int(contract.method_version)}"] = _assess(
                goal,
                contract,
                world.snapshot(),
                world.capabilities(),
                registry=world.predicates,
            )
        return build_request(
            goal,
            world.capabilities(),
            world.registry,
            catalog=world.catalog,
            reports=reports,
            mission_id=mission_id,
            domain=domain,
        )

    def method_applicability(self, mission_id: str) -> tuple[Any, ...]:
        """Why each registered method does not apply to each still-open goal.

        Review F16: the hierarchical Planner package always passed ``reports=()``, so
        the ``applicability`` section the package's own docstring calls load-bearing
        was empty in every deployment — the Planner was told "no method fits" with no
        axis and no reason, which is the exact state the section exists to replace.

        Only *refused* verdicts are reported: an applicable method is already in
        ``method_library`` and repeating it here as "nothing was wrong with this one"
        would push the refusals out of the model's attention.
        """

        from ..planning.htn.planner_package import MethodApplicability

        world = self._world()
        network = self.network(mission_id)
        snapshot = world.snapshot()
        capabilities = world.capabilities()
        entries: list[Any] = []
        for spec in sorted(network.occurrences, key=lambda item: str(item.occurrence_id)):
            if spec.form is not TaskForm.COMPOUND:
                continue
            if network.adopted_instance_for(spec.occurrence_id) is not None:
                continue
            goal = network.binding_for_occurrence(spec.occurrence_id)
            signature = goal.goal_signature.signature_id
            for reference in world.registry.method_refs():
                contract = world.registry.definition(reference)
                if contract is None or str(contract.goal_type_ref.id) != str(signature):
                    continue
                report = assess_method(
                    goal, contract, snapshot, capabilities, registry=world.predicates
                )
                if report.applicable:
                    continue
                entries.append(
                    MethodApplicability(
                        goal_occurrence_id=str(spec.occurrence_id),
                        goal_signature_id=str(signature),
                        method_ref=contract.method_ref(),
                        report=report,
                    )
                )
        return tuple(entries)

    def record_method_applicability(
        self, mission_id: str, *, reports: Sequence[Any] | None = None
    ) -> Event | None:
        """Persist why each method was refused, at the revision it was assessed against.

        G1.  :meth:`method_applicability` fed the Planner prompt and nothing else, so
        the four axes — unknown precondition, conflict, parameter mismatch, missing
        capability, missing authority — existed only inside one rendered message.  A
        reader after the fact (the Host's acceptance runner, an operator, this suite)
        had no way to ask "why was ``code.fix-by-patch`` not chosen here", which is the
        question the axes were separated for in the first place.

        ``reports`` is passed in by the caller that also renders the prompt, so the
        record and the prompt are the *same* assessment rather than two runs of it
        against a world that may have moved between them.

        Returns ``None`` when nothing was refused: an event saying "no method was
        refused" and the absence of an event are the same fact, and writing the first
        one per round would bury the rounds that have something to say.
        """

        from ..planning.htn.planner_package import applicability_reports

        entries = tuple(reports) if reports is not None else self.method_applicability(mission_id)
        if not entries:
            return None
        network = self.network(mission_id)
        semantics = self.semantics()
        rendered = applicability_reports(entries, limit=MAX_RECORDED_REFUSALS)
        refused: list[dict[str, Any]] = []
        for item in rendered:
            cited = tuple(item.get("unknown_preconditions", ())) + tuple(
                item.get("conflicting_preconditions", ())
            )
            seen: list[str] = []
            for key in cited:
                for record in semantics.list_observations(mission_id, proposition_key=str(key)):
                    observation = str(getattr(record, "observation_id", ""))
                    if observation and observation not in seen:
                        seen.append(observation)
            # The observations that *bear on* the cited propositions, which is not the
            # same as observations that settle them: a proposition is unknown here
            # precisely because what was observed did not settle it.  Naming them is
            # what lets a reader tell "nobody looked" from "somebody looked and the
            # answer did not decide it".
            refused.append({**item, "observation_ids": seen})
        return append_hierarchical_event(
            self.store,
            METHOD_APPLICABILITY_ASSESSED,
            mission_id,
            key=f"{mission_id}:{int(network.plan_revision)}",
            payload={
                "plan_revision": int(network.plan_revision),
                "refused_methods": refused,
                "refusal_count": len(entries),
                "truncated": len(entries) > len(rendered),
            },
        )

    def plan_revision_committed_at(self, mission_id: str, plan_revision: int) -> int | None:
        """When this plan revision was committed, in observation milliseconds.

        ``None`` when no such revision has been committed — third-round review P2-7.
        It used to answer ``0``, which the evidence cap reads as "look at everything
        since the beginning of time": the degradation back to a Mission-lifetime cap
        was silent, and silent is the one thing a cap must not be.  The caller decides
        what to do with "there is no such revision"; this method only says so.

        Read with a typed query rather than by scanning every event of the Mission.
        This runs once per evidence round per Mission, so the old ``list_events``
        sweep was an O(events) read on a hot path that lengthens with the run.
        """

        rows = self.store.connection.execute(
            "SELECT created_at, payload_json FROM events WHERE mission_id = ? AND type = ?"
            " ORDER BY seq",
            (str(mission_id), PLAN_REVISION_COMMITTED),
        ).fetchall()
        stamps = [
            int(float(row[0]) * 1000)
            for row in rows
            if int((json.loads(row[1]) or {}).get("plan_revision", -1)) == int(plan_revision)
        ]
        return max(stamps) if stamps else None

    def propositions_looked_at(self, mission_id: str, *, plan_revision: int) -> frozenset[str]:
        """The propositions this Mission has already read **under this revision**.

        An observation does not always settle the atom that motivated it — a
        non-authoritative negative on an OPEN predicate is still UNKNOWN (§6.6) — so
        ``pending_asks`` keeps offering it, and the first real-model evidence round
        recorded the same proposition several hundred times in a few seconds until
        ``EvidenceEntry`` refused the snapshot.  A second identical read of an
        unchanged world produces the same answer at the cost of one more row, so
        within one revision each proposition is read once.

        Review P1-4: part 2c capped it over the Mission's whole **lifetime**, which
        made evidence unrefreshable — a precondition repaired while the plan ran (the
        smoke's own ``code.test-is-failing``) was never looked at again, and
        ``issue_start_witnesses``' promise of I19's "recompute, do not reuse the old
        TRUE" was recomputing an expression over a frozen snapshot.  The rule, written
        down: **committing a plan revision re-opens the look.**  A revision is the
        moment the world has demonstrably changed — work retired, a method adopted, a
        duty opened — and it is bounded, because a revision costs a Planner round of
        its own.  So an observation recorded *before* the current revision was
        committed no longer counts as having looked.
        """

        since = self.plan_revision_committed_at(mission_id, plan_revision)
        if since is None:
            # Review P2-7: a revision this Mission never committed is no barrier, and
            # saying so out loud is the difference between "no barrier" and a silent
            # fall back to the Mission-lifetime cap this rule replaced.  Every read
            # counts, which is the conservative answer: nothing is looked at twice on
            # the strength of a revision that does not exist.
            return frozenset(
                str(record.proposition_key)
                for record in self.semantics().list_observations(mission_id)
            )
        return frozenset(
            str(record.proposition_key)
            for record in self.semantics().list_observations(mission_id)
            if int(record.recorded_at_ms) >= since
        )

    def run_evidence_round(self, mission_id: str, *, now_ms: int | None = None) -> Any:
        """Look at the UNKNOWN preconditions of every still-open goal, once.

        P2.3c part 2c.  Part 2b built the round (``planning.htn.evidence_round``) and
        left it for a deployment to call by hand, so in the real loop an UNKNOWN
        precondition stayed UNKNOWN forever: the Planner was handed a package whose
        methods all read ``NEEDS_EVIDENCE`` and no evidence was ever gathered.  ADR-07
        is "look, do not guess", and nothing was looking.

        What it does **not** do is dispatch an agent.  Every read goes through the
        deployment's :class:`~..planning.htn.observation_pipeline.ObserverIndex`,
        which is the one path that enforces §6.6 C28 (only a listed observer, only a
        read-only type) on the way in; an observer that cannot answer produces
        ``OBSERVER_UNAVAILABLE`` and **no record**, so a proposition never becomes
        FALSE because nobody looked.

        Which propositions are asked is the catalogue's decision, not this method's:
        an atom is asked only when ``evidence_requests`` found a registered read-only
        task type that declares it observes that predicate.  A deployment that
        installed no observer index gets an empty round rather than an error — it has
        simply not wired the evidence lane, which the readiness reports already say.
        """

        from ..planning.htn.evidence_round import (
            EvidenceAsk,
            EvidenceRoundResult,
            asks_for_requests,
            pending_asks,
            run_round,
        )
        from ..planning.htn.refinement import evidence_requests, unknown_predicates

        world = self._world()
        index = getattr(world, "observer_index", None)
        if index is None:
            return EvidenceRoundResult()
        network = self.network(mission_id)
        snapshot = world.snapshot()
        looked_at = self.propositions_looked_at(
            mission_id, plan_revision=int(network.plan_revision)
        )
        moment = int(self.store.now * 1000) if now_ms is None else int(now_ms)
        asks: dict[str, EvidenceAsk] = {}
        for spec in sorted(network.occurrences, key=lambda item: str(item.occurrence_id)):
            if spec.form is not TaskForm.COMPOUND:
                continue
            if network.adopted_instance_for(spec.occurrence_id) is not None:
                continue
            goal = network.binding_for_occurrence(spec.occurrence_id)
            parameters = dict(goal.typed_parameters)
            for reference in world.registry.method_refs():
                contract = world.registry.definition(reference)
                if contract is None:
                    continue
                if str(contract.goal_type_ref.id) != str(goal.goal_signature.signature_id):
                    continue
                conditions = contract.applicable_when
                unknowns = unknown_predicates(
                    conditions,
                    parameters=parameters,
                    predicates=world.predicates,
                    snapshot=snapshot,
                    now_ms=moment,
                )
                if not unknowns:
                    continue
                requests = evidence_requests(
                    unknowns,
                    for_occurrence=spec.occurrence_id,
                    obligation_id=spec.obligation_id,
                    catalog=world.catalog,
                    semantic_scope=goal.semantic_scope,
                    contract_revision=int(goal.contract_revision),
                )
                candidates = pending_asks(
                    conditions,
                    parameters=parameters,
                    registry=world.predicates,
                    snapshot=snapshot,
                    now_ms=moment,
                )
                for ask in asks_for_requests(requests, candidates):
                    if ask.proposition_key in looked_at:
                        continue
                    asks.setdefault(ask.proposition_key, ask)
        if not asks:
            return EvidenceRoundResult()
        return run_round(
            index,
            self.semantics(),
            mission_id,
            tuple(asks[key] for key in sorted(asks)),
            now_ms=moment,
        )

    def goals_needing_method(self, mission_id: str) -> tuple[str, ...]:
        """The open goals for which no registered method can *ever* apply as things are.

        P2.3c part 2c: the judgment part 2b left open — "when is a compound missing a
        method".  It is deliberately narrow, because a MethodSynthesizer round costs a
        model call and admitting a synthesised method is the most consequential thing
        a model does in this system (§7.3):

        * a goal with **no** registered method for its signature qualifies;
        * a goal whose every candidate is refused for something a *different method*
          could route around — a capability this deployment does not have, arguments
          that do not type-check, a precondition that is simply false here — qualifies;
        * a goal that has at least one applicable method does **not**, and neither
          does one whose candidates are ``NEEDS_EVIDENCE`` or ``CONFLICT``: the repair
          there is to look (:meth:`run_evidence_round`) or to settle the contradiction,
          and synthesising a method around an unanswered question would be inventing a
          way past the very check that is unanswered.

        Returned as *goal task ids* because that is what ``synthesis_request`` takes.
        """

        world = self._world()
        network = self.network(mission_id)
        refused: dict[str, list[Any]] = {}
        for entry in self.method_applicability(mission_id):
            refused.setdefault(str(entry.goal_occurrence_id), []).append(entry.report)
        by_signature: dict[str, int] = {}
        for reference in world.registry.method_refs():
            definition = world.registry.definition(reference)
            if definition is None:
                continue
            key = str(definition.goal_type_ref.id)
            by_signature[key] = by_signature.get(key, 0) + 1
        needing: list[str] = []
        for spec in sorted(network.occurrences, key=lambda item: str(item.occurrence_id)):
            if spec.form is not TaskForm.COMPOUND:
                continue
            if network.adopted_instance_for(spec.occurrence_id) is not None:
                continue
            goal = network.binding_for_occurrence(spec.occurrence_id)
            candidates = by_signature.get(str(goal.goal_signature.signature_id), 0)
            seen = refused.get(str(spec.occurrence_id), [])
            if candidates and len(seen) < candidates:
                continue  # at least one method applies; nothing to synthesise
            if any(
                report.status not in SYNTHESIS_WORTHY_REFUSALS
                and not self._evidence_is_saturated(mission_id, report)
                for report in seen
            ):
                continue  # the answer is "look" or "settle", not "invent"
            needing.append(str(spec.task_id))
        return tuple(needing)

    def _evidence_is_saturated(self, mission_id: str, report: Any) -> bool:
        """Whether "look again" has stopped being an answer for this refusal (D2b).

        ``NEEDS_EVIDENCE`` is excluded from :data:`SYNTHESIS_WORTHY_REFUSALS` because
        its repair is to look, not to invent — and that is right until looking cannot
        change anything.  The Grok acceptance run found the case where it cannot: for an
        **OPEN** predicate a non-authoritative negative observation contributes
        ``NO_SUPPORT``, so ``atom_truth`` answers UNKNOWN however many times the observer
        reads the world and answers "no".  All six L3 episodes sat in that live-lock —
        ``_gather_evidence`` reported progress every cycle because it had *recorded* an
        observation, ``goals_needing_method`` returned nothing every cycle because the
        refusal was NEEDS_EVIDENCE, and planning never had a way forward.

        So: a precondition one observer has already read ``evidence_saturation_rounds``
        times, while the truth is still UNKNOWN, is treated as a refusal a *different
        method* could route around — which is all ``SYNTHESIS_WORTHY_REFUSALS`` means.
        I18 is untouched: nothing here turns UNKNOWN into TRUE or opens a safety gate;
        it only decides whether proposing a new method is a sensible next question.
        """

        if report.status is not ApplicabilityStatus.NEEDS_EVIDENCE:
            return False
        keys = tuple(report.needs_evidence)
        if not keys:
            return False
        bound = int(self.evidence_saturation_rounds)
        semantics = self.semantics()
        for key in keys:
            by_observer: dict[str, int] = {}
            for record in semantics.list_observations(mission_id, proposition_key=str(key)):
                observer = str(record.observer_id or "")
                by_observer[observer] = by_observer.get(observer, 0) + 1
            if not by_observer or max(by_observer.values()) < bound:
                return False
        return True

    def record_synthesis_outcome(
        self,
        mission_id: str,
        *,
        goal_task_id: str,
        admitted: bool,
        problems: Sequence[str] = (),
        method_id: str = "",
        verdict: str = "",
        author: str = "",
    ) -> Event:
        """What one MethodSynthesizer round produced, recorded where it can be read.

        A refused proposal leaves nothing in the registry and nothing on the plan, so
        without this the only trace of a synthesis round would be its token cost.
        Keyed by ``(mission, goal)``: one round per goal is what
        :meth:`goals_needing_method` asks for, and a second event under the same key
        would mean the bound was not held.
        """

        return self._append(
            SYNTHESIS_ROUND_RECORDED,
            mission_id,
            key=f"{mission_id}:{goal_task_id}",
            task_id=goal_task_id or None,
            payload={
                "goal_task_id": str(goal_task_id),
                "admitted": bool(admitted),
                "verdict": str(verdict),
                "method_id": str(method_id),
                "author": str(author),
                "problems": [str(item) for item in problems][:12],
            },
        )

    def synthesis_round_recorded(self, mission_id: str, goal_task_id: str) -> bool:
        """Whether a MethodSynthesizer round for this goal has already been concluded.

        Read from the event this assembly writes, not from a field on the plan: the
        round produces nothing on the plan when it is refused, so the event *is* the
        record that it happened.
        """

        key = f"{SYNTHESIS_ROUND_RECORDED}:{mission_id}:{goal_task_id}"
        return any(event.idempotency_key == key for event in self.store.list_events(mission_id))

    def apply_synthesizer_reply(
        self, mission_id: str, text: str, *, policy: Any = None
    ) -> AdmissionReceipt:
        """``<method_proposal>`` from the synthesiser → §7.3, author fixed at MODEL.

        The author is **not** a parameter here.  ``text`` reached this method from a
        model, and presenting it as anything else would hand a model-authored
        definition the registration rights of the registry service — which
        ``MethodRegistration`` allows only at DRAFT precisely to stop that (§6.3,
        §7.3).  ``admit_method_proposal`` stays available for a caller that genuinely
        has a human- or tool-authored definition and says so.
        """

        from ..planning.htn.synthesis import MethodSynthesizer

        world = self._world()
        self.require_hierarchical(mission_id)
        synthesizer = MethodSynthesizer(world.registry, world.catalog)
        resolved = policy if policy is not None else self._admission_policy(mission_id)
        receipt = synthesizer.accept_response(text, policy=resolved)
        if receipt.admitted:
            self._publish_admitted_method(world, receipt.method_ref)
        return receipt

    def _publish_admitted_method(self, world: Any, reference: Any) -> None:
        """A just-admitted method goes into the **library**, not only into memory.

        P2.3d review P1-1.  Admission decides against the in-memory
        :class:`MethodRegistry`, but ``compile_proposal`` reads the chosen method back
        out of ``htn_store`` — the definition a plan revision was compiled from has to
        be durable and re-readable at exactly the version the commit recorded.
        ``build_planning_world`` publishes the *seed* methods for that reason
        (:func:`~..planning.htn.world.publish_methods`); nothing published a method the
        Mission synthesised for itself, so the Planner's next round died with
        "method … is not stored" and the synthesis round bought nothing at all.

        Same re-registration rule as the assembly path: identical bytes are a no-op.
        """

        semantics = getattr(world, "semantics", None)
        if semantics is None or reference is None:
            return
        contract = world.registry.definition(reference)
        registration = world.registry.registration(reference)
        if contract is None or registration is None:
            return
        semantics.register_method(contract, registration)

    def _admission_policy(self, mission_id: str) -> Any:
        """The policy a synthesised method is decided against on this deployment.

        A world that knows how to build its own policy is asked for it; anything else
        gets one assembled from the four declaration stores plus the live capability
        table, so a deployment cannot end up admitting a method against capabilities
        nobody has.
        """

        world = self._world()
        builder = getattr(world, "policy", None)
        if callable(builder):
            return builder(mission_id=mission_id)
        from ..contracts.htn import MissionRef
        from ..planning.htn.registry import AdmissionPolicy

        return AdmissionPolicy(
            policy_ref="deployment-policy",
            policy_version=1,
            mission_id=MissionRef(mission_id),
            predicates=world.predicates,
            task_types=world.catalog,
            schemas=world.schemas,
            capabilities=world.capabilities(),
        )

    def apply_planner_reply(
        self,
        mission_id: str,
        text: str,
        *,
        principal: PlanPrincipal,
        command_id: str,
        source: Mapping[str, Any] | None = None,
    ) -> PlanRoundOutcome:
        """One Planner reply → at most one committed plan revision (§18.3, §9.4).

        The loop is the whole point: compile against the snapshot that is current
        *now*, offer it, and if the commit was refused for a reason a newer snapshot
        could fix, compile again against that newer snapshot.  ``allow_rebase`` is
        never reachable from here (C19) and the number of attempts is bounded, so a
        Mission moving underneath the proposer ends with a named refusal rather than
        a busy loop.
        """

        mission = self.require_hierarchical(mission_id)
        del mission
        proposal = parse_plan_proposal(text, mission_id=mission_id)
        refusals: list[PlanRefusal] = []
        limit = int(self.compile_attempts)
        for attempt in range(1, limit + 1):
            network = self.network(mission_id)
            compilation = self.compile_proposal(mission_id, proposal, network)
            command = self.build_command(
                mission_id,
                proposal,
                compilation,
                principal=principal,
                command_id=f"{command_id}:{attempt}",
                source={**dict(source or {}), "compile_attempt": attempt},
            )
            try:
                receipt = self.commit.commit_plan_revision(command, principal)
            except PlanCommitRejected as refused:
                recompilable = refused.reason in RECOMPILABLE_REFUSALS
                refusals.append(
                    PlanRefusal(
                        attempt=attempt,
                        reason=refused.reason,
                        detail=refused.detail,
                        recompilable=recompilable,
                    )
                )
                if recompilable and attempt < limit:
                    continue
                self._record_refusal(mission_id, proposal, refusals)
                return PlanRoundOutcome(proposal_id=proposal.proposal_id, refusals=tuple(refusals))
            return PlanRoundOutcome(
                proposal_id=proposal.proposal_id, receipt=receipt, refusals=tuple(refusals)
            )
        raise AssertionError("unreachable: the loop returns on every path")  # pragma: no cover

    def compile_proposal(
        self, mission_id: str, proposal: PlanProposal, network: TaskNetworkSnapshot
    ) -> RefinementCompilation:
        """The model's proposal, compiled into a checked increment (§18.3).

        One ``refine`` operation per round.  A proposal carrying several is refused
        rather than partly applied: §24.1 decision 8 requires the *merged* result to
        be fully re-validated on the current transaction state, and that belongs to
        P3.1 — silently taking the first operation would report a commit the Planner
        did not ask for.
        """

        world = self._world()
        operations = [item for item in proposal.operations if _is_refine(item)]
        if len(operations) != len(proposal.operations) or len(operations) != 1:
            raise ContractError(
                f"proposal {proposal.proposal_id!r} carries "
                f"{len(proposal.operations)} operation(s) of which {len(operations)} refine; "
                "this slice assembles exactly one refinement per round (a merged delta is "
                "re-validated as a whole, §24.1 decision 8)"
            )
        operation: RefineOperation = operations[0]
        try:
            parent = network.binding_for_task(TaskRef(str(operation.goal_id)))
        except KeyError as error:
            raise missing_bindings(mission_id, [str(operation.goal_id)]) from error
        # P2.3c part 2c: *which occurrence* of that goal is being refined.  A
        # ``refine`` operation names a goal and a duty, and for the Mission root the
        # occurrence id happens to equal the task id — so the draft's default
        # (``goal_occurrence_id = goal_id``) was right by coincidence and every
        # *child* compound was refused by the compiler with "the draft refines
        # occurrence <task id>, which this network does not contain".  A plan deeper
        # than one level could therefore never be committed at all, which is also why
        # no test in this suite had ever run a second refinement round.
        occurrence = _refined_occurrence(mission_id, network, operation)
        contract = (
            self.semantics()
            .get_method(operation.method_ref.id, int(operation.method_ref.version))
            .contract
        )
        report = assess_method(
            parent,
            contract,
            world.snapshot(),
            world.capabilities(),
            registry=world.predicates,
        )
        # G2: what this network already holds that a slot of the new method may bind
        # instead of re-doing.  Without it ``sharing`` was always ``None`` here, so a
        # read-only sub-goal two consumers both need — TG §12's shared goal, and
        # §21.5's "a shared sub-goal is reused at least once" — could not happen in a
        # running Mission at all, whatever the method library said.
        sharing = shared_goal_index(network, catalog=world.catalog)
        draft = ground_method(
            parent,
            contract,
            dict(operation.bindings),
            report,
            catalog=world.catalog,
            schemas=world.schemas,
            sharing=sharing,
            plan_revision=network.plan_revision,
            goal_occurrence_id=occurrence,
        )
        return compile_refinement_bundle(
            draft,
            network,
            method=contract,
            catalog=world.catalog,
            schemas=world.schemas,
            registry=world.registry,
            sharing=sharing,
            # The delta records which proposal it was compiled from, so the commit's
            # own read-set row and event name the model's proposal and not a derived
            # delta id (§18.3's naming convention: the two are different objects).
            compiled_from_proposal_id=proposal.proposal_id,
        )

    def build_command(
        self,
        mission_id: str,
        proposal: PlanProposal,
        compilation: RefinementCompilation,
        *,
        principal: PlanPrincipal,
        command_id: str,
        source: Mapping[str, Any] | None = None,
    ) -> CommitPlanCommand:
        """The command the Commit service checks.  Authority is bound here, not read."""

        mission = self.mission(mission_id)
        return CommitPlanCommand(
            command_id=command_id,
            mission_id=mission_id,
            delta=compilation.delta,
            network=compilation.network,
            task_bindings=compilation.task_bindings,
            # P2.3a: in the hierarchical mode the serialisation point is the *plan
            # revision*, and committing one does not advance ``graph_version``.  The
            # integer is passed because ADR-13 keeps it as the coarse gate every
            # Mission agrees on; nothing in this module treats it as a concurrency
            # token or expects it to move.
            base_graph_version=int((mission.final_report or {}).get("graph_version") or 1),
            issued_by=principal.principal_id,
            scope_id=principal.scope_id,
            budget_requirement=compilation.budget_requirement,
            source={
                **dict(source or {}),
                "proposal_id": proposal.proposal_id,
                "rationale": proposal.rationale,
            },
        )

    def attempt_inputs(self, mission_id: str, task_id: str) -> list[UpstreamInput]:
        """What one dispatch of ``task_id`` starts from, in the new mode.

        Only the resolved :class:`InputManifest` — §24.1 decision 4's "no longer
        collect every ancestor's files".  An ORDER-only predecessor contributes
        nothing however many artifacts it accepted, which is the property T015 /
        T066 exist to hold.
        """

        network = self.network(mission_id)
        spec = next((item for item in network.occurrences if str(item.task_id) == task_id), None)
        if spec is None:
            raise missing_bindings(mission_id, [task_id])
        result = self.input_result(mission_id, network, spec)
        if result is None or result.manifest is None:
            return []
        if not result.manifest.is_frozen:
            # A producer that has not finished leaves a *symbolic* binding.  That is
            # "not resolved yet", which is the DATA gate's business (WAITING_DATA) and
            # not a materialisation failure — so nothing is placed and nothing raises.
            return []
        rules = self.target_rules or TargetRules(namespace=f"workspace:{task_id}")
        return manifest_upstream_inputs(result.manifest, rules, network=network)

    # ------------------------------------------------------------------------ plumbing
    def require_planning_world(self) -> PlanningWorld:
        """The deployment's declarations, or a visible refusal.

        Public because the event handler needs the same answer when it builds the
        hierarchical Planner's package: a deployment without a ``PlanningWorld`` must
        fail here rather than quietly hand the model the legacy DAG package (§18.5).
        """

        return self._world()

    def _world(self) -> PlanningWorld:
        if self.planning is None:
            raise ContractError(
                "the hierarchical assembly needs a PlanningWorld (task types, schemas, method "
                "registry, predicates); a deployment without one cannot compile a refinement "
                "and must not fall back to the legacy planner (§18.5)"
            )
        return self.planning

    def _record_refusal(
        self, mission_id: str, proposal: PlanProposal, refusals: Sequence[PlanRefusal]
    ) -> Event:
        last = refusals[-1]
        return self._append(
            PLAN_COMMIT_REFUSED,
            mission_id,
            key=f"{mission_id}:{proposal.proposal_id}:{len(refusals)}",
            payload={
                "proposal_id": proposal.proposal_id,
                "attempts": len(refusals),
                "compile_attempts_allowed": int(self.compile_attempts),
                "reason": last.reason,
                "detail": last.detail,
                "refusals": [item.to_json() for item in refusals],
                # P2.3c part 2c: what the proposal actually named.  A
                # ``READ_SET_UNRESOLVED`` that says "this store cannot re-check
                # observation X" leaves the reader unable to tell an id the proposer
                # invented from one the store lost — and that is the only question
                # worth asking about that refusal.  Kind and id only: the revisions and
                # hashes are the proposer's claims and belong to the proposal, not to
                # the diagnosis.
                "read_set_named": [
                    {"kind": str(item.kind), "id": str(item.id)} for item in proposal.read_set
                ],
                # C19, stated in the record: nothing was replayed on the proposer's
                # behalf, so a reader knows the proposal has to be re-authored.
                "rebased": False,
            },
        )

    def _append(
        self,
        event_type: str,
        mission_id: str,
        *,
        key: str,
        payload: Mapping[str, Any],
        task_id: str | None = None,
    ) -> Event:
        return append_hierarchical_event(
            self.store, event_type, mission_id, key=key, payload=payload, task_id=task_id
        )


def _refined_occurrence(
    mission_id: str, network: TaskNetworkSnapshot, operation: RefineOperation
) -> OccurrenceId:
    """Which occurrence a ``refine`` operation is about (P2.3c part 2c).

    The proposal contract names a *goal* and a *duty*, not an occurrence — the model
    is shown ``goal_id`` / ``obligation_id`` in the package's ``open_compound_goals``
    and must quote them back.  The occurrence is therefore resolved here, from the
    plan, and two situations are refusals rather than guesses:

    * nothing in the plan matches that (goal, duty) pair — the proposal is about a
      goal this revision does not carry;
    * more than one still-open occurrence matches — TG §12 lets two slots share a
      goal, and choosing one of them would be this module deciding which of the
      Planner's two open goals it meant.

    An occurrence that is already refined is skipped rather than matched, so a replay
    of the same proposal is refused for the honest reason ("no open occurrence") and
    not by silently re-refining the one that is adopted.
    """

    named = [
        spec
        for spec in network.occurrences
        if str(spec.task_id) == str(operation.goal_id)
        and str(spec.obligation_id) == str(operation.obligation_id)
    ]
    if not named:
        raise missing_bindings(mission_id, [str(operation.goal_id)])
    matches = [
        spec
        for spec in named
        if spec.form is TaskForm.COMPOUND
        and network.adopted_instance_for(spec.occurrence_id) is None
    ]
    if len(matches) == 1:
        return matches[0].occurrence_id
    if not matches:
        # The goal is in the plan but every occurrence of it is already refined (or is
        # primitive).  The occurrence is still handed over so the *compiler* refuses in
        # its own vocabulary — "this occurrence is already refined", "this occurrence is
        # primitive" — rather than this resolver inventing a second way to say no.
        return named[0].occurrence_id
    raise ContractError(
        f"goal {operation.goal_id!r} on duty {operation.obligation_id!r} has "
        f"{len(matches)} open occurrences in this plan revision; a refinement names one "
        "of them and choosing here would be this module picking which goal the Planner "
        "meant (TG §12)"
    )


def _root_criteria(requirements: Any, record: Any) -> tuple[ResolutionCriterion, ...]:
    """The root resolution's criteria, restated from the review record (review F4).

    The *set* of criteria is the requirements revision's — that is what the goal owes
    — and each verdict is the record's own.  ``UNKNOWN`` where the reviewer said
    nothing: it is the enum's word for "not judged", and it is the only honest thing a
    trigger that decides nothing can write.
    """

    reviewed = {str(item.criterion_id): item.verdict for item in record.criteria}
    return tuple(
        ResolutionCriterion(
            criterion_id=item.criterion_id,
            verdict=reviewed.get(str(item.criterion_id), CriterionVerdict.UNKNOWN),
        )
        for item in requirements.criteria
    )


def _same_conclusion(held: ValidityWitness, offered: ValidityWitness) -> bool:
    """Whether two licences over one key say the same thing (review P2-1).

    Only the *verdict* axes, deliberately: the id already pins consumer, purpose,
    scope, epoch, support revision and subject, so what is left to differ is what the
    licence concluded.  ``as_of_ms`` is not on the list — re-reading the same world a
    second later is the same conclusion, and treating it as a contradiction would make
    every honest re-issue an anomaly.
    """

    return (
        held.truth is offered.truth
        and held.decision is offered.decision
        and held.freshness is offered.freshness
        and held.availability is offered.availability
    )


def append_hierarchical_event(
    store: Store,
    event_type: str,
    mission_id: str,
    *,
    key: str,
    payload: Mapping[str, Any],
    task_id: str | None = None,
) -> Event:
    """Append one of this module's event types, keyed for idempotency.

    A module function and not only a method because :data:`ASSEMBLY_MISSING` has to be
    recordable by a caller that has *no* assembly — that is what the event says.
    """

    idempotency_key = f"{event_type}:{key}"
    return store.append_event(
        Event(
            id=ids.event_id(idempotency_key),
            type=event_type,
            trace_id=ids.trace_id(mission_id),
            mission_id=mission_id,
            task_id=task_id,
            attempt_id=None,
            actor_type="system",
            actor_id="orchestrator",
            payload=dict(payload),
            idempotency_key=idempotency_key,
            created_at=store.now,
        )
    )


def record_assembly_missing(store: Store, mission: Mission, *, at: str) -> Event:
    """Refuse-and-record: this Mission runs hierarchically and the gates are not installed.

    Review finding F1.  ``commit_plan_revision`` materialises occurrence rows through
    the Commit Service while every dispatch gate — ``allocate_v2``'s admissions, the
    TG §8.3 re-check, the root ``GoalResolution`` trigger — lives on this assembly and
    is reached only through ``Orchestrator._hierarchical``.  Nothing couples the two,
    so a deployment that writes hierarchical plans without calling
    ``install_hierarchical`` used to hand those rows to the legacy ``allocate()``:
    dispatch on the ``TaskStatus.READY`` string, a DATA consumer running before its
    producer was accepted, and "every live Task is COMPLETED" standing in for a root
    resolution.  That is the silent half-mode §18.5 rule 1 forbids — half the Mission
    committed under the new rules and half of it scheduled under the old ones.

    The answer is the same one ``require_planning_world`` already gives on the planning
    side: refuse, and say so.  The Mission stays where it is, visibly not moving, with
    one event naming the deployment defect — which is a Mission an operator can fix,
    rather than one that quietly ran under rules nobody chose for it.
    """

    return append_hierarchical_event(
        store,
        ASSEMBLY_MISSING,
        mission.id,
        key=mission.id,
        payload={
            "semantics": semantics_of(mission),
            "at": at,
            "detail": (
                "this Mission runs under hierarchical semantics and no HierarchicalDispatch "
                "is installed on this Orchestrator; the readiness gate, the TG §8.3 dispatch "
                "re-check and the root GoalResolution trigger all live on that assembly, so "
                "nothing is dispatched and nothing is judged (§18.5 rule 1: a Mission does "
                "not run half in each mode).  Call Orchestrator.install_hierarchical()."
            ),
        },
    )


def shared_goal_index(network: TaskNetworkSnapshot, *, catalog: Any) -> SharedGoalIndex:
    """The occurrences of this network a later slot may bind instead of re-doing.

    G2.  Every occurrence that has a semantic binding is offered; nothing here decides
    that two goals *are* one.  :func:`may_share` still has to agree on the whole
    sharing signature, on the consumer slot's reuse policy, and on the task type being
    read-only or carrying an effect identity — so an index entry is a candidate, never
    a merge.  An occurrence whose task type this deployment cannot resolve is skipped
    rather than indexed under a guess.
    """

    by_signature = {
        (str(spec.goal_signature.signature_id), int(spec.goal_signature.version)): spec
        for spec in catalog.task_types()
    }
    entries: list[SharedGoalEntry] = []
    for occurrence in network.occurrences:
        try:
            binding = network.binding_for_occurrence(occurrence.occurrence_id)
        except (KeyError, ContractError):
            continue
        spec = by_signature.get(
            (str(binding.goal_signature.signature_id), int(binding.goal_signature.version))
        )
        if spec is None:
            continue
        entries.append(
            SharedGoalEntry(
                occurrence_id=occurrence.occurrence_id,
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                signature=SharingSignature.of(
                    spec,
                    dict(binding.typed_parameters),
                    authority_scope=binding.semantic_scope,
                    semantic_scope=binding.semantic_scope,
                ),
                reuse_policy=spec.reuse_policy,
            )
        )
    return SharedGoalIndex(entries)


def _is_refine(operation: object) -> bool:
    """Whether one parsed plan operation is a refinement.

    ``isinstance`` and not a duck-typed field probe: ``ProposeSuccessorOperation``
    also carries a versioned reference and a goal-shaped id, so a structural test
    would silently accept it as a refinement and compile the wrong thing.
    """

    return isinstance(operation, RefineOperation)


__all__ = (
    "ASSEMBLY_MISSING",
    "MISSION_STALLED",
    "ROOT_REVIEW_CUT",
    "ROOT_REVIEW_SUPERSEDED",
    "WITNESS_KEY_TAKEN",
    "COMPOUND_DISPLAY_STATUS",
    "SYNTHESIS_ROUND_RECORDED",
    "COMPOUND_PHASE_CHANGED",
    "DEFAULT_COMPILE_ATTEMPTS",
    "DISPATCH_INTERCEPTED",
    "PLAN_COMMIT_REFUSED",
    "PLAN_INTEGRITY_FAILED",
    "RECOMPILABLE_REFUSALS",
    "CompoundPhase",
    "DispatchInterception",
    "HierarchicalDispatch",
    "NetworkView",
    "PlanIntegrityError",
    "PlanRefusal",
    "PlanRoundOutcome",
    "PlanningWorld",
    "SYNTHESIS_WORTHY_REFUSALS",
    "METHOD_APPLICABILITY_ASSESSED",
    "append_hierarchical_event",
    "shared_goal_index",
    "is_hierarchical",
    "missing_bindings",
    "next_compound_phase",
    "record_assembly_missing",
    "root_not_identified",
)
