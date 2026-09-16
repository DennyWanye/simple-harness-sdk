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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from ..artifacts.input_bindings import (
    AcceptedOutputsIndex,
    ResolutionPolicy,
    ResolutionResult,
    TargetRules,
)
from ..artifacts.versioning import UpstreamInput, manifest_upstream_inputs, resolve_input_manifest
from ..contracts import TERMINAL_MISSION, ContractError, Event, TaskStatus, ids
from ..contracts.evidence_state import ValidityWitness
from ..contracts.htn import (
    MissionRef,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    PlanRevision,
    RefineOperation,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from ..contracts.obligations import ObligationAccountView
from ..contracts.semantic_base import content_hash_of
from ..graph.eligibility import (
    ActivePlanView,
    EvidenceView,
    ExecutionFrontier,
    OccurrenceOutcome,
    PlanningFrontier,
    ReadinessReason,
    ReadinessReport,
    TaskView,
    evaluate_readiness,
    legacy_ready_is_not_eligibility,
)
from ..graph.projection_validation import GraphIntegrityError, require_topological_order
from ..graph.task_network import GATING_REQUIREDNESS, TaskNetworkSnapshot
from ..planning.htn.applicability import assess_method
from ..planning.htn.compiler import (
    RefinementCompilation,
    RootNetwork,
    compile_refinement_bundle,
)
from ..planning.htn.grounding import ground_method
from ..planning.planner import parse_method_proposal, parse_plan_proposal
from ..storage.htn_store import HtnStore, PlanCommitReceipt
from ..storage.obligation_store import ObligationStore
from .plan_commits import (
    HIERARCHICAL_SEMANTICS,
    CommitPlanCommand,
    PlanCommitRejected,
    PlanPrincipal,
    semantics_of,
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
class NetworkView:
    """One read of a Mission's plan: the snapshot plus what readiness said about it."""

    network: TaskNetworkSnapshot
    plan: ActivePlanView
    views: Mapping[OccurrenceId, TaskView]
    reports: Mapping[OccurrenceId, ReadinessReport]
    outcomes: Mapping[OccurrenceId, OccurrenceOutcome]
    resolved: frozenset[OccurrenceId] = frozenset()

    @property
    def planning_frontier(self) -> PlanningFrontier:
        return PlanningFrontier.compute(self.plan, self.reports)

    @property
    def execution_frontier(self) -> ExecutionFrontier:
        return ExecutionFrontier.compute(self.plan, self.reports)

    def report_for(self, occurrence: OccurrenceId) -> ReadinessReport:
        return self.reports[occurrence]


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

    def __post_init__(self) -> None:
        if int(self.compile_attempts) < 1:
            raise ContractError("compile_attempts must be at least 1")

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
            integrity_error=integrity,
        )

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
        one read of a plan into a quadratic number of store round-trips.
        """

        network, integrity = self._read_network(mission_id)
        if integrity is not None and not tolerate_integrity:
            raise integrity
        plan = self.plan_view(mission_id, network, integrity=integrity)
        outcomes = self.occurrence_outcomes(mission_id, network)
        resolved = self.resolved_occurrences(mission_id, network)
        witnesses = self.witnesses(mission_id, network)
        accepted = self.accepted_outputs(mission_id, network, outcomes=outcomes)
        evidence = EvidenceView(witnesses=witnesses)
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
                evidence,
                self.input_result(
                    mission_id, network, spec, accepted=accepted, witnesses=witnesses
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
            witnesses=self.witnesses(mission_id, network) if witnesses is None else witnesses,
            policy=self.resolution_policy,
        )

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

    def _recorded_outputs(self, mission_id: str, network: TaskNetworkSnapshot) -> Sequence[Any]:
        """The accepted outputs the resolver may choose from — **P2.3c wires this**.

        Empty here, and deliberately so.  Building an
        :class:`~..artifacts.input_bindings.AcceptedOutput` needs one fact nothing in
        the store records yet: which artifact an Acceptance accepted *at which output
        port*.  That index is written by the root-review / result path, which is
        P2.3c's, and inventing it here (say, by taking the Attempt's artifacts and
        guessing the port from the path) would be the ancestor sweep again wearing a
        typed name.

        The consequence while it is empty is visible and safe rather than silent: a
        consumer that declares a DATA port resolves to ``WAITING_DATA`` and is never
        dispatched with no inputs.
        """

        del mission_id, network
        return ()

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
        draft = ground_method(
            parent,
            contract,
            dict(operation.bindings),
            report,
            catalog=world.catalog,
            schemas=world.schemas,
        )
        return compile_refinement_bundle(
            draft,
            network,
            method=contract,
            catalog=world.catalog,
            schemas=world.schemas,
            registry=world.registry,
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
        idempotency_key = f"{event_type}:{key}"
        return self.store.append_event(
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
                created_at=self.store.now,
            )
        )


def _is_refine(operation: object) -> bool:
    """Whether one parsed plan operation is a refinement.

    ``isinstance`` and not a duck-typed field probe: ``ProposeSuccessorOperation``
    also carries a versioned reference and a goal-shaped id, so a structural test
    would silently accept it as a refinement and compile the wrong thing.
    """

    return isinstance(operation, RefineOperation)


__all__ = (
    "COMPOUND_DISPLAY_STATUS",
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
    "is_hierarchical",
    "missing_bindings",
    "next_compound_phase",
    "root_not_identified",
)
