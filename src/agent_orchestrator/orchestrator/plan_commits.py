# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3a: one plan revision is committed atomically, or not at all (§9.4, TG §7.4).

This is the ninth Commit Service mixin and the single writing entry point of the
hierarchical mode.  Everything it does is arranged around one sentence of ADR-13:

    the integer ``base_graph_version`` stays the coarse concurrency gate for every
    Mission; a hierarchical proposal carries a *semantic* read-set beside it, which
    is checked item by item afterwards, and any stale item refuses the commit — with
    no automatic rebase.

So the order below is not cosmetic.  The integer gate runs first because it is the
cheap answer that every Mission already agrees on; the read-set runs second because
it is the expensive one and only hierarchical Missions have it; and the automatic
``allow_rebase`` replay of :meth:`CommitService.commit_graph_change` is deliberately
*not* reachable from here (C19): a proposal whose read went stale is handed back to
its author to recompile against the new snapshot, because replaying it would mean
deciding, on the author's behalf, that a fact it read did not matter.

Three further boundaries the plan states and this module keeps:

* **Legacy Missions stop at the door.**  A Mission whose
  ``orchestration_semantics_version`` is not ``hierarchical`` is refused after the
  ``missions`` row is read and before any migration-16 table is touched — no read,
  no write, no event (§18.5 rule 1).
* **No raw SQL, ever.**  Every row goes through :class:`HtnStore` /
  :class:`ObligationStore`, which stay the single writing authority for the new
  tables.  This module decides *whether*; storage decides *how*.
* **Nothing slow runs inside the transaction.**  No model, no solver, no tool.  The
  structural re-validation is pure computation over the snapshot the caller
  compiled plus the delta, which is the "check certificate, re-confirmed under the
  structural version" TG §7.4 asks for.

The command is refused with a named reason, never with a boolean: a stale read-set,
a cycle, an exhausted allowance and unreconciled running work are four different
repairs, and collapsing them would tell the proposer nothing about what to do next.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import canonical_json

from ..contracts import TERMINAL_MISSION, Budget, ContractError, MissionStatus, TaskStatus
from ..contracts.htn import (
    ContractRevision,
    DispatchGeneration,
    GraphStructureBudget,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    ProposedPlanDelta,
    ReadItem,
    ReadItemKind,
    RunningWorkPolicy,
    SemanticReadSet,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    require_commit_ready,
)
from ..contracts.obligations import ObligationAccountView, ObligationLedger
from ..contracts.semantic_base import content_hash_of
from ..governance.budgets import BudgetError
from ..graph.projection_validation import (
    validate_execution_projection,
    validate_refinement_acyclic,
)
from ..graph.task_network import DEFAULT_PROJECTION_BUDGET, TaskNetworkSnapshot
from ..planning.htn.compiler import (
    BudgetRequirement,
    DemandAdmission,
    DemandNotAdmissible,
    apply_obligation_openings,
)
from ..planning.manager import inherit_limits
from ..storage.htn_store import HtnStore, PlanCommitReceipt
from ..storage.obligation_store import ObligationStore
from ..storage.store import StoreError
from ._read_set import ReadSetChannelUnknown, SemanticReadSetChecker
from .occurrence_tasks import (
    COMPOUND_TOKENS,
    MIN_TOKEN_SHARE,
    Materialisation,
    OccurrenceTask,
    occurrence_task,
    share_tokens,
    task_pool_tokens,
)
from .state_machine import next_mission

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..contracts import Event, Mission
    from ..governance.budgets import BudgetLedger
    from ..storage.store import Store

#: The one event type this module appends.  It is *new*, which is the whole point:
#: §18.5 rule 3 allows the hierarchical mode to add event types and forbids it to
#: rewrite the bytes of an existing one.
PLAN_REVISION_COMMITTED = "PlanRevisionCommitted"

#: P2.3c part 2.  Appended once per commit that put occurrences on the board, so
#: "which Task row is which occurrence" and the budget equation are durable facts
#: rather than something a reader has to re-derive from three tables.
OCCURRENCES_MATERIALISED = "PlanOccurrencesMaterialised"

#: The server-side default, hard-coded (§18.5 rule 1).  A Mission is legacy unless
#: its creator asked for the other one in so many words.
LEGACY_SEMANTICS = "legacy"
HIERARCHICAL_SEMANTICS = "hierarchical"
#: §18.5 spells the opt-in ``full-target-v1``; the code's own name for the same mode
#: is ``hierarchical``.  Both are accepted at the boundary and normalise to one value,
#: so the two documents cannot drift into two modes.
SEMANTICS_ALIASES: Mapping[str, str] = {
    LEGACY_SEMANTICS: LEGACY_SEMANTICS,
    HIERARCHICAL_SEMANTICS: HIERARCHICAL_SEMANTICS,
    "full-target-v1": HIERARCHICAL_SEMANTICS,
}
#: Where the mode is recorded on the Mission.  It is written into the Mission's
#: context bag *only* for a hierarchical Mission, so a legacy Mission's stored JSON
#: keeps the bytes it had before this slice existed.
SEMANTICS_KEY = "orchestration_semantics_version"


def normalise_semantics(value: object) -> str:
    """The canonical mode name, or raise for one this deployment does not have."""

    text = LEGACY_SEMANTICS if value is None else str(value)
    resolved = SEMANTICS_ALIASES.get(text)
    if resolved is None:
        raise ContractError(
            f"{SEMANTICS_KEY} {text!r} is not a known orchestration semantics version; "
            f"it is one of {sorted(set(SEMANTICS_ALIASES))}"
        )
    return resolved


def semantics_of(mission: Mission) -> str:
    """The mode one Mission runs under.  Absent means ``legacy`` (§18.5 rule 1)."""

    return normalise_semantics((mission.final_report or {}).get(SEMANTICS_KEY))


class PlanCommitRejected(StoreError):
    """The plan commit was refused; nothing was written.

    ``reason`` is a stable machine name — the proposer decides what to do next from
    it, so it is part of the contract and not a message to be reworded freely.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PlanPrincipal:
    """Who is issuing the command, and inside which manager scope (§7.4 step 1)."""

    principal_id: str
    scope_id: str = "mission"
    manager_epoch: int = 0

    def __post_init__(self) -> None:
        if not str(self.principal_id).strip():
            raise ContractError("a plan commit needs an identified principal")


@dataclass(frozen=True, slots=True)
class CommitPlanCommand:
    """One request to make ``delta`` the Mission's next plan revision.

    The command carries the *checked* delta, the task bindings the increment
    creates (TG §3.2 keeps those beside the delta, not inside it) and the resulting
    network the proposer compiled — the check certificate this commit re-confirms
    under the current structural state rather than trusts.
    """

    command_id: str
    mission_id: str
    delta: ProposedPlanDelta
    network: TaskNetworkSnapshot
    task_bindings: tuple[TaskSemanticBindingV1, ...] = ()
    base_graph_version: int = 1
    issued_by: str = ""
    scope_id: str = "mission"
    running_work_policy: RunningWorkPolicy = RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED
    superseded_occurrences: tuple[OccurrenceId, ...] = ()
    budget_requirement: BudgetRequirement | None = None
    structure_budget: GraphStructureBudget = DEFAULT_PROJECTION_BUDGET
    granted_fuel: Mapping[str, int] = field(default_factory=dict)
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.command_id).strip():
            raise ContractError("a plan commit command needs a command_id (§17.4 idempotency)")
        require_commit_ready(self.delta)
        if not isinstance(self.network, TaskNetworkSnapshot):
            raise ContractError("command.network must be the resulting TaskNetworkSnapshot")
        object.__setattr__(
            self,
            "running_work_policy",
            RunningWorkPolicy(str(self.running_work_policy)),
        )

    @property
    def read_set(self) -> SemanticReadSet:
        return self.delta.read_set

    def intent_hash(self) -> str:
        """What this command *asks for*, independent of when it was delivered.

        Two deliveries of the same intent under one ``command_id`` are the same
        command (§17.4); a different intent under the same id is a conflict, which
        is why the delta, the bindings and the concurrency basis are all in here.
        """

        return content_hash_of(
            {
                "command_id": self.command_id,
                "mission_id": self.mission_id,
                "delta": self.delta.to_json(),
                "task_bindings": [binding.to_json() for binding in self.task_bindings],
                "base_graph_version": self.base_graph_version,
                "scope_id": self.scope_id,
                "issued_by": self.issued_by,
                "running_work_policy": str(self.running_work_policy),
                "superseded_occurrences": sorted(str(item) for item in self.superseded_occurrences),
                "structure_budget": self.structure_budget.to_json(),
            }
        )


class PlanCommitsMixin:
    """Commit one PlanRevision: check everything, then write it in one transaction."""

    if TYPE_CHECKING:  # pragma: no cover - provided by CommitService
        _store: Store
        _ledger: BudgetLedger
        _deployed_layers: frozenset[str]

        def _emit(
            self,
            event_type: str,
            mission_id: str,
            *,
            key: str,
            task_id: str | None = None,
            attempt_id: str | None = None,
            payload: Mapping[str, Any] | None = None,
        ) -> Event: ...

        def _require_mission(self, mission_id: str) -> Mission: ...

        # Provided by ``ObligationCommitsMixin``, which ``CommitService`` inherits
        # alongside this one: admitting or withdrawing a demand is an authorisation
        # act with one audited entry point (P2.3c part 2d, decision 3).
        def admit_obligation_demand(
            self,
            mission_id: str,
            obligation_id: Any,
            *,
            principal: str,
            requester: Mapping[str, Any],
            evidence: Mapping[str, Any],
            parent_obligation_id: Any = None,
            relation: str | None = None,
            ledger: Any = None,
            plan_revision: int | None = None,
        ) -> ObligationAccountView: ...

        def withdraw_obligation_demand(
            self,
            mission_id: str,
            obligation_id: Any,
            *,
            principal: str,
            requester: Mapping[str, Any],
            evidence: Mapping[str, Any],
            parent_obligation_id: Any = None,
            relation: str | None = None,
            ledger: Any = None,
            plan_revision: int | None = None,
        ) -> ObligationAccountView: ...

    # ------------------------------------------------------------------ public entry
    def commit_plan_revision(
        self, command: CommitPlanCommand, principal: PlanPrincipal
    ) -> PlanCommitReceipt:
        """Apply one plan revision atomically; return the receipt (§9.4, TG §7.4)."""

        if not isinstance(command, CommitPlanCommand):
            raise PlanCommitRejected(
                "bad_command", "commit_plan_revision expects a CommitPlanCommand"
            )
        if not isinstance(principal, PlanPrincipal):
            raise PlanCommitRejected(
                "bad_principal", "commit_plan_revision expects a PlanPrincipal"
            )
        # §7.4: identity first — before the receipt lookup, so a forged replay of
        # someone else's command_id cannot read back their receipt.
        self._authorize(command, principal)
        intent = command.intent_hash()
        with self._store.transaction():
            mission = self._require_mission(command.mission_id)
            mode = semantics_of(mission)
            if mode != HIERARCHICAL_SEMANTICS:
                # The door.  Only the ``missions`` row was read; no migration-16
                # table is touched on this path, and no event is appended.
                raise PlanCommitRejected(
                    "SEMANTICS_NOT_HIERARCHICAL",
                    f"mission {mission.id} runs under {mode!r}; a plan revision is committed "
                    "only for a Mission that asked for the hierarchical semantics (§18.5 rule 1)",
                )
            semantics = HtnStore(self._store)
            replayed = self._replayed_receipt(semantics, command, intent)
            if replayed is not None:
                return replayed
            if mission.status in TERMINAL_MISSION:
                raise PlanCommitRejected(
                    "MISSION_NOT_WRITABLE",
                    f"mission {mission.id} is {mission.status!s} and accepts no new plan",
                )
            read_set = command.read_set
            self._check_manager_epoch(semantics, command, principal, read_set)
            self._check_integer_gate(mission, command)
            self._check_read_set(semantics, command, read_set)
            base, new_revision = self._check_plan_revision(semantics, command)
            self._check_structure(semantics, command)
            obligations = ObligationStore(self._store)
            self._check_commit_ready(obligations, command)
            self._check_budget(semantics, obligations, command)
            revoked = self._revoke_running_work(semantics, command)
            return self._write(
                semantics,
                obligations,
                command,
                mission=mission,
                base=base,
                new_revision=new_revision,
                intent=intent,
                revoked=revoked,
            )

    # ------------------------------------------------------- the proposer's own side
    def read_item_for(self, mission_id: str, kind: ReadItemKind, subject_id: str) -> ReadItem:
        """The read-set entry a proposer should record for one subject, right now.

        The proposing side and the checking side must agree on *what* a subject's
        semantic revision is; writing that formula twice is how they stop agreeing.
        So the compiler-side entry is built by the same resolvers the commit uses,
        and a subject whose current state cannot be expressed as a revision plus a
        content hash is refused here rather than recorded as an unre-checkable read.
        """

        checker = self._read_set_checker(mission_id)
        try:
            return checker.read_item(kind, subject_id)
        except ReadSetChannelUnknown as unknown:
            raise PlanCommitRejected("READ_SET_UNRESOLVED", unknown.detail) from unknown

    def _read_set_checker(self, mission_id: str) -> SemanticReadSetChecker:
        """The one re-validation implementation, configured for the *plan* path.

        ``allow_task_control_channels`` stays off: a plan proposal's TASK ids are
        bare task ids, and leaving the namespaced dispatch-control ids unresolved is
        exactly what this path did before ``_read_set`` existed (§18.5 constraint 3 —
        the old behaviour is not redefined by sharing an implementation).
        """

        return SemanticReadSetChecker(
            self._store,
            HtnStore(self._store),
            mission_id=mission_id,
            resolvers={
                "goal": self._goal_state,
                "method": self._method_state,
                "observation": self._observation_state,
                "acceptance": self._acceptance_state,
                "obligation": self._obligation_state,
                "authority": self._authority_state,
            },
        )

    # -- one seam per channel: this path's own name for the shared resolver.  They
    # delegate rather than re-implement — ``_read_set`` is the single implementation
    # (§ADR-13 clause 2) — and they exist because the mutation suite weakens one
    # channel at a time through *this* class, which is also how a deployment would
    # override one without forking the check.
    def _goal_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).goal_state(semantics, mission_id, item)

    def _method_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).method_state(semantics, mission_id, item)

    def _observation_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).observation_state(
            semantics, mission_id, item
        )

    def _witness_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).witness_state(
            semantics, mission_id, item
        )

    def _acceptance_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).acceptance_state(
            semantics, mission_id, item
        )

    def _obligation_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).obligation_state(
            semantics, mission_id, item
        )

    def _authority_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        return _shared(self._store, semantics, mission_id).authority_state(
            semantics, mission_id, item
        )

    # ------------------------------------------------------------------ gate 1: identity
    @staticmethod
    def _authorize(command: CommitPlanCommand, principal: PlanPrincipal) -> None:
        """Authorship is stated by the command and re-checked here, never inferred.

        An *unsigned* command is refused rather than attributed to whoever presents
        it: treating a blank ``issued_by`` as "no claim, so no mismatch" would let
        any principal holding the scope commit a proposal it did not author, and the
        receipt would then name a manager that never issued the command (§7.4).
        """

        if not str(command.issued_by).strip():
            raise PlanCommitRejected(
                "PRINCIPAL_MISMATCH",
                "the command names no issuer; a plan revision is committed on behalf of "
                "an identified manager, and an unsigned command is not attributed to "
                f"{principal.principal_id!r} by default (§7.4)",
            )
        if command.issued_by != principal.principal_id:
            raise PlanCommitRejected(
                "PRINCIPAL_MISMATCH",
                f"the command was issued by {command.issued_by!r} and presented by "
                f"{principal.principal_id!r}; authorship is not re-assignable at delivery",
            )
        if command.scope_id != principal.scope_id:
            raise PlanCommitRejected(
                "SCOPE_NOT_AUTHORIZED",
                f"{principal.principal_id!r} holds scope {principal.scope_id!r} and may not "
                f"commit into {command.scope_id!r}",
            )

    # --------------------------------------------------------------- gate 2: idempotency
    @staticmethod
    def _replayed_receipt(
        semantics: HtnStore, command: CommitPlanCommand, intent: str
    ) -> PlanCommitReceipt | None:
        """The receipt of an earlier delivery of this very command, if there is one.

        §17.4: the same command twice is one commit and one receipt.  A *different*
        intent under the same id is not a replay — it is two commands wearing one
        name, and answering it with the stored receipt would report work that was
        never done.
        """

        try:
            stored = semantics.get_commit_receipt(command.command_id)
        except StoreError:
            return None
        if stored.intent_hash != intent:
            raise PlanCommitRejected(
                "COMMAND_PAYLOAD_CONFLICT",
                f"command {command.command_id!r} was applied with intent {stored.intent_hash}, "
                f"and this delivery asks for {intent}",
            )
        return stored

    # ------------------------------------------------------------- gate 3: manager epoch
    @staticmethod
    def _check_manager_epoch(
        semantics: HtnStore,
        command: CommitPlanCommand,
        principal: PlanPrincipal,
        read_set: SemanticReadSet,
    ) -> None:
        current = semantics.epoch(command.mission_id, command.scope_id)
        if int(read_set.manager_epoch) != current:
            raise PlanCommitRejected(
                "MANAGER_EPOCH_STALE",
                f"scope {command.scope_id!r} is at epoch {current}; the proposal was built "
                f"at epoch {int(read_set.manager_epoch)}",
            )
        if int(principal.manager_epoch) != current:
            raise PlanCommitRejected(
                "MANAGER_EPOCH_STALE",
                f"scope {command.scope_id!r} is at epoch {current}; the principal holds "
                f"epoch {int(principal.manager_epoch)}",
            )

    # -------------------------------------------------------- gate 4: the integer gate
    @staticmethod
    def _check_integer_gate(mission: Mission, command: CommitPlanCommand) -> None:
        """ADR-13 clause 1, unchanged for both modes — and clause C19 on top of it.

        There is deliberately no ``allow_rebase`` here.  The legacy
        ``_commit_graph_change`` may replay a stale proposal whose operations touch
        nothing that changed since; a hierarchical proposal may not, because the
        thing that went stale might be a *fact it read*, which no overlap test on
        task ids can see.
        """

        current = int((mission.final_report or {}).get("graph_version") or 1)
        if int(command.base_graph_version) != current:
            raise PlanCommitRejected(
                "GRAPH_VERSION_STALE",
                f"the proposal is based on graph version {int(command.base_graph_version)}, "
                f"current is {current}; recompile against the new snapshot "
                "(a hierarchical proposal is never rebased automatically, ADR-13/C19)",
            )

    # ----------------------------------------------------------- gate 5: the read-set
    def _check_read_set(
        self, semantics: HtnStore, command: CommitPlanCommand, read_set: SemanticReadSet
    ) -> None:
        """ADR-13 clause 2: every channel, item by item, fail closed.

        The channel-by-channel work lives in :mod:`._read_set` so this path and the
        accept-side one cannot disagree about what a subject's semantic revision is
        (the P2.3c review found a second, five-channel copy).  What stays here is
        this path's own vocabulary: a channel that cannot be re-checked is refused
        separately from one that is out of date — "I could not tell" and "it changed"
        call for different work by the proposer, and reporting the first as the second
        would send them off to recompile something that was never the problem.
        """

        del semantics
        checker = self._read_set_checker(command.mission_id)
        try:
            verdict = checker.verify(read_set)
        except ReadSetChannelUnknown as unknown:
            raise PlanCommitRejected("READ_SET_UNRESOLVED", unknown.detail) from unknown
        if verdict.unresolved:
            raise PlanCommitRejected("READ_SET_UNRESOLVED", verdict.unresolved_detail())
        if verdict.stale:
            raise PlanCommitRejected("READ_SET_STALE", verdict.stale_detail())

    # ------------------------------------------------------- gate 6: the active plan
    @staticmethod
    def _check_plan_revision(semantics: HtnStore, command: CommitPlanCommand) -> tuple[int, int]:
        active = semantics.active_plan_revision(command.mission_id)
        current = 0 if active is None else active.revision
        base = int(command.delta.base_plan_revision)
        if base != current:
            raise PlanCommitRejected(
                "PLAN_REVISION_STALE",
                f"the delta is based on plan revision {base}; mission "
                f"{command.mission_id} is at {current}",
            )
        if int(command.network.plan_revision) != base + 1:
            # The network the proposer compiled is the *resulting* one, so it carries
            # the revision this commit is about to create.  Checking it here is what
            # stops a certificate for some other revision being presented instead.
            raise PlanCommitRejected(
                "PLAN_REVISION_STALE",
                f"the supplied network is at plan revision {int(command.network.plan_revision)}; "
                f"a delta based on {base} must compile to {base + 1}",
            )
        return current, current + 1

    # ---------------------------------------------------- gate 7: structure, again
    def _check_structure(self, semantics: HtnStore, command: CommitPlanCommand) -> None:
        """Re-validate the whole resulting network, not only the delta (TG §7.3).

        Two proposals that each add one edge can merge into a cycle, so a check that
        only looked at the increment would pass both.  The network the proposer
        compiled is re-checked here, under the current transaction, and its contents
        are pinned to the delta so a certificate for some *other* network cannot be
        presented instead.

        Three things are checked that a projection validator alone cannot see, because
        each of them is about the relation between the *supplied* network and what the
        Mission already holds:

        * the network must **preserve** the plan in force — an occurrence that simply
          vanishes, with no operation retiring or superseding it, is work silently
          dropped, and the projection of the smaller network is perfectly valid;
        * an alternative must *be* a method instance, and the delta may not adopt one
          the certificate does not hold — otherwise an adopted row is written past the
          network the snapshot invariant checked;
        * the delta's own bindings must agree with the network's (``assert_consistent_with``).
        """

        network = command.network
        delta = command.delta
        if str(network.mission_id) != command.mission_id:
            raise PlanCommitRejected(
                "STRUCTURE_INVALID",
                f"the supplied network belongs to mission {network.mission_id!s}",
            )
        present = {spec.occurrence_id for spec in network.occurrences}
        missing = sorted(
            str(spec.occurrence_id)
            for spec in delta.occurrences
            if spec.occurrence_id not in present
        )
        if missing:
            raise PlanCommitRejected(
                "STRUCTURE_INVALID",
                f"the supplied network does not contain the delta's occurrences: {missing}",
            )
        dangling = sorted(str(item) for item in delta.referenced_occurrences if item not in present)
        if dangling:
            raise PlanCommitRejected(
                "STRUCTURE_INVALID",
                f"the delta references occurrences the network does not hold: {dangling}",
            )
        self._check_preserves_plan(semantics, command, present)
        self._check_alternatives_are_method_instances(semantics, command)
        bindings = {binding.task_id: binding for binding in network.task_bindings}
        try:
            delta.assert_consistent_with(bindings)
        except ContractError as error:
            raise PlanCommitRejected("STRUCTURE_INVALID", str(error)) from error
        projection = validate_execution_projection(
            network.execution_projection(), command.structure_budget
        )
        refinement = validate_refinement_acyclic(network)
        problems = [*projection.problems, *refinement.problems]
        if problems:
            raise PlanCommitRejected(
                "STRUCTURE_INVALID",
                "; ".join(f"{problem.kind!s}: {problem.detail}" for problem in problems),
            )

    def _check_preserves_plan(
        self, semantics: HtnStore, command: CommitPlanCommand, present: set[Any]
    ) -> None:
        """A revision may replace work; it may not quietly lose it (§9.4).

        The supplied network is the *resulting* plan, so anything the plan in force
        held and it does not is a removal.  A removal is legitimate only when the
        delta says so — the occurrence is named as superseded, or its parent method
        instance is being retired.  Otherwise the commit is refused: the projection
        of the smaller network is entirely valid, so no structural validator would
        notice, and the duty behind the dropped occurrence would be left with nobody
        working on it and no record that anyone decided that.
        """

        active = semantics.active_plan_revision(command.mission_id)
        if active is None:
            return
        previous = {
            spec.occurrence_id
            for spec in semantics.list_plan_memberships(command.mission_id, active.revision)
        }
        dropped = previous - set(present)
        if not dropped:
            return
        accounted = {str(item) for item in command.superseded_occurrences} | self._retired_children(
            semantics, command
        )
        orphaned = sorted(str(item) for item in dropped if str(item) not in accounted)
        if orphaned:
            raise PlanCommitRejected(
                "PLAN_NOT_PRESERVED",
                f"the supplied network drops occurrences {orphaned} that plan revision "
                f"{active.revision} holds, and the delta neither supersedes them nor retires "
                "the method instance that opened them; work is replaced by decision, "
                "never by omission (§9.4)",
            )

    @staticmethod
    def _check_alternatives_are_method_instances(
        semantics: HtnStore, command: CommitPlanCommand
    ) -> None:
        """§18.5: a choice lives in the method-instance layer, resolved, and only there.

        The graph layer has exactly one reading — several dependencies mean *all of
        them* — so an alternative can only be a second MethodInstance for one goal
        occurrence, of which at most one may be adopted.

        :class:`TaskNetworkSnapshot` already refuses to *exist* with two adopted
        instances over one occurrence ("alternatives are OR, not AND"), so the
        proposer cannot compile that network at all.  What it does not cover is the
        write path: :meth:`_write` inserts ``delta.method_instances`` as ``ADOPTED``
        without the network having to contain them, so a delta carrying an instance
        the certificate never included would land an adopted row the snapshot
        invariant never saw — and two of those over one occurrence is precisely the
        unresolved OR, assembled in the database instead of in the snapshot.

        Both halves are closed here: every instance this delta adopts must be one the
        supplied network holds *and* adopts, and it may not join an instance the store
        already has adopted over the same occurrence unless the delta retires it.
        """

        network = command.network
        adopted_in_network = {
            str(draft.instance_id)
            for draft in network.method_instances
            if draft.instance_id in network.adopted_instance_ids
        }
        smuggled = sorted(
            str(draft.instance_id)
            for draft in command.delta.method_instances
            if str(draft.instance_id) not in adopted_in_network
        )
        if smuggled:
            raise PlanCommitRejected(
                "OR_NOT_RESOLVED",
                f"the delta adopts method instances {smuggled} that the supplied network does "
                "not hold as adopted; an adopted row written past the certificate is how two "
                "alternatives end up adopted over one occurrence (§18.5)",
            )
        retiring = {str(item) for item in command.delta.retired_instance_ids}
        by_occurrence: dict[str, set[str]] = {}
        for draft in semantics.list_method_instances(command.mission_id, state="ADOPTED"):
            if str(draft.instance_id) in retiring:
                continue
            by_occurrence.setdefault(str(draft.effective_goal_occurrence_id), set()).add(
                str(draft.instance_id)
            )
        for draft in command.delta.method_instances:
            by_occurrence.setdefault(str(draft.effective_goal_occurrence_id), set()).add(
                str(draft.instance_id)
            )
        contested = {
            occurrence: sorted(instances)
            for occurrence, instances in by_occurrence.items()
            if len(instances) > 1
        }
        if contested:
            raise PlanCommitRejected(
                "OR_NOT_RESOLVED",
                "; ".join(
                    f"occurrence {occurrence!r} would have {len(instances)} adopted method "
                    f"instances {instances}; an alternative is a method instance and at most "
                    "one of them is adopted — the graph layer reads several dependencies as "
                    "AND, never as a choice (§18.5)"
                    for occurrence, instances in sorted(contested.items())
                ),
            )

    # --------------------------------------------- gate 8: the delta is commit-ready
    @staticmethod
    def _check_commit_ready(obligations: ObligationStore, command: CommitPlanCommand) -> None:
        """§18.3 / §6.1: every duty an occurrence names exists, and none is re-opened.

        Run as a *gate*, against the duties registered **before** this delta's own
        openings are applied.  It used to run in the write half, after the openings
        had been persisted — at which point a newly opened duty is indistinguishable
        from one that already existed, so a perfectly legitimate opening was reported
        as a re-opening and a bare ``ContractError`` escaped with no reason name on
        it.  Both halves of that are fixed here: the set is the pre-opening one, and
        the refusal carries a name the proposer can act on.
        """

        registered = frozenset(obligations.obligation_ids(command.mission_id))
        try:
            require_commit_ready(command.delta, registered_obligations=registered)
        except ContractError as error:
            raise PlanCommitRejected("DELTA_NOT_COMMIT_READY", str(error)) from error

    # ------------------------------------------------------------- gate 9: the budget
    @staticmethod
    def _check_budget(
        semantics: HtnStore, obligations: ObligationStore, command: CommitPlanCommand
    ) -> None:
        """Only a check.  The legacy token ledger is not consulted and not moved.

        Two allowances are in play and they are not the same thing: the *structural*
        budget (ADR-08) bounds how big a plan may get, and a duty's *recursion fuel*
        (§6.1) bounds how much re-planning one responsibility may buy.  Overrunning
        either is ``BOUND_REACHED`` / ``BUDGET_INSUFFICIENT``, never a silently
        trimmed plan.
        """

        budget = command.structure_budget
        network = command.network
        live = len(network.occurrences)
        if live > budget.max_live_tasks:
            raise PlanCommitRejected(
                "BOUND_REACHED",
                f"the plan would hold {live} occurrences, above max_live_tasks="
                f"{budget.max_live_tasks} (structure budget v{budget.budget_version})",
            )
        expanded = len(semantics.list_method_instances(command.mission_id)) + len(
            command.delta.method_instances
        )
        if expanded > budget.max_expanded_nodes:
            raise PlanCommitRejected(
                "BOUND_REACHED",
                f"the mission would have expanded {expanded} method instances, above "
                f"max_expanded_nodes={budget.max_expanded_nodes}",
            )
        declared = command.budget_requirement
        if declared is not None:
            actual = (
                len(command.delta.occurrences),
                len(command.delta.order_constraints),
                len(command.delta.data_requirements),
            )
            asked = (
                declared.new_occurrences,
                declared.new_order_constraints,
                declared.new_data_requirements,
            )
            if actual != asked:
                raise PlanCommitRejected(
                    "BUDGET_REQUIREMENT_MISMATCH",
                    f"the declared cost {asked} does not describe the delta {actual}",
                )
        # The fuel side is decided by the ledger itself: ``open_from`` refuses a share
        # the parent no longer has.  It runs here, on a throwaway ledger, so an
        # exhausted allowance refuses the command before anything is written.
        if command.delta.obligation_openings:
            ledger = obligations.load_ledger(command.mission_id)
            try:
                apply_obligation_openings(
                    ledger, command.delta, granted_fuel=dict(command.granted_fuel)
                )
            except DemandNotAdmissible as error:
                # P2.3c part 2d, decision 3.  "Nobody asked for this work" is not a
                # budget problem and does not get the budget's name (§7.4): the repair
                # is to adopt the slot that wants it, or not to open the duty.
                raise PlanCommitRejected("DEMAND_NOT_ADMITTED", str(error)) from error
            except ContractError as error:
                raise PlanCommitRejected("BUDGET_INSUFFICIENT", str(error)) from error

    # ------------------------------------------------ gate 10: work already in flight
    def _revoke_running_work(
        self, semantics: HtnStore, command: CommitPlanCommand
    ) -> dict[str, int]:
        """Take away the execution right of everything this delta replaces (§9.4).

        It withdraws eligibility; it does not approve the new version and it does not
        cancel an Attempt.  The dispatch generation is bumped so a dispatch that was
        already handed out no longer matches, and a durable recheck entry is written
        so the reconciliation is somebody's job rather than a hope.
        """

        retired = self._retired_children(semantics, command)
        listed = {str(item) for item in command.superseded_occurrences}
        policy = command.running_work_policy
        if policy is RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED and retired - listed:
            raise PlanCommitRejected(
                "RUNNING_WORK_NOT_RECONCILED",
                f"retiring a method instance changes the bindings of {sorted(retired - listed)}; "
                "'retain if bindings unchanged' cannot decide that case",
            )
        if policy is RunningWorkPolicy.EXPLICIT_PER_SUBJECT_IN_COMMIT and retired - listed:
            raise PlanCommitRejected(
                "RUNNING_WORK_NOT_RECONCILED",
                "this policy decides every replaced subject explicitly; "
                f"{sorted(retired - listed)} "
                "were replaced and not named in the command",
            )
        targets = sorted(listed | retired)
        if not targets:
            return {}
        by_occurrence = self._occurrence_tasks(semantics, command)
        epoch = semantics.epoch(command.mission_id, command.scope_id)
        revoked: dict[str, int] = {}
        for occurrence in targets:
            task_id = by_occurrence.get(occurrence)
            if task_id is None:
                raise PlanCommitRejected(
                    "RUNNING_WORK_NOT_RECONCILED",
                    f"occurrence {occurrence!r} is named as replaced but belongs to no task "
                    "this mission holds",
                )
            binding = semantics.task_semantics_of(command.mission_id, task_id)
            if binding is None:
                raise PlanCommitRejected(
                    "MISSING_SEMANTIC_BINDING",
                    f"task {task_id} has no semantic binding; in the hierarchical mode a "
                    "missing binding is corruption, not a legacy fallback (§18.5)",
                )
            superseded = _superseded(binding)
            semantics.put_task_semantics(command.mission_id, superseded)
            semantics.mark_dirty(
                command.mission_id,
                subject_kind="occurrence",
                subject_id=occurrence,
                scope_id=command.scope_id,
                epoch=epoch,
                reason="dispatch_generation_revoked",
            )
            revoked[task_id] = int(superseded.dispatch_generation)
        return revoked

    def _held_by_retired_row(self, task: Any) -> int:
        """What a Task row the active plan no longer names still holds (P2.3j).

        Its settled usage plus whatever is still reserved on it — never more than its
        ceiling, and the ceiling itself when the account cannot be read (a row opened
        before accounts existed, or one this ledger does not know): counting *more*
        is the conservative failure, counting less would grant the same tokens twice.
        """

        from .commit_service import task_account

        ceiling = int(task.budget.max_tokens or 0)
        try:
            snapshot = self._ledger.account(task_account(task.id))
        except BudgetError:
            return ceiling
        held = int(snapshot.reserved_tokens) + int(snapshot.settled_tokens)
        return max(0, min(ceiling, held))

    @staticmethod
    def _retired_children(semantics: HtnStore, command: CommitPlanCommand) -> set[str]:
        found: set[str] = set()
        for instance_id in command.delta.retired_instance_ids:
            try:
                bindings = semantics.list_child_occurrences(command.mission_id, str(instance_id))
            except StoreError:  # pragma: no cover - list never raises today
                continue
            found.update(str(child.occurrence_id) for child in bindings)
        return found

    def _withdraw_retired_demands(
        self,
        semantics: HtnStore,
        obligations: ObligationStore,
        command: CommitPlanCommand,
        *,
        plan_revision: int,
    ) -> list[str]:
        """Retiring a slot ends **its own** interest, and nobody else's (§24.1 dec. 9).

        A duty that another adopted slot still binds keeps its demand: that is the
        shared-sub-goal case, where two consumers each hold a ``DemandRef`` and one
        leaving must not take the other's work away.  A duty whose last adopting slot
        just went is released, so it stops looking dispatchable to a plan that no
        longer contains it.
        """

        if not command.delta.retired_instance_ids:
            return []
        retiring = {str(item) for item in command.delta.retired_instance_ids}
        drafts = {
            str(item.instance_id): item
            for item in semantics.list_method_instances(command.mission_id)
        }
        orphaned: set[str] = set()
        for instance_id in sorted(retiring):
            try:
                children = semantics.list_child_occurrences(command.mission_id, instance_id)
            except StoreError:  # pragma: no cover - list never raises today
                continue
            held = drafts.get(instance_id)
            # A refinement slot carries the *parent's* duty and opened nothing (§6.1),
            # so retiring it cannot release an interest it never created — least of all
            # the Mission's own root duty, which the requirements ask for and no slot
            # above it holds.
            parent_duty = None if held is None else str(held.obligation_id)
            orphaned.update(
                str(child.obligation_id)
                for child in children
                if str(child.obligation_id) != parent_duty
            )
        for draft in semantics.list_method_instances(command.mission_id, state="ADOPTED"):
            if str(draft.instance_id) in retiring:
                continue
            orphaned -= {str(binding.obligation_id) for binding in draft.child_bindings}
        for draft in command.delta.method_instances:
            orphaned -= {str(binding.obligation_id) for binding in draft.child_bindings}
        released: list[str] = []
        for duty in sorted(orphaned):
            account = obligations.account(command.mission_id, ObligationId(duty))
            if not account.has_admitted_demand:
                continue
            self.withdraw_obligation_demand(
                command.mission_id,
                ObligationId(duty),
                principal=command.issued_by or "commit-service",
                requester={"kind": "method_slot"},
                evidence={
                    "retired_method_instances": sorted(retiring),
                    "plan_revision": plan_revision,
                    "delta_id": command.delta.delta_id,
                },
                plan_revision=plan_revision,
            )
            released.append(duty)
        return released

    @staticmethod
    def _occurrence_tasks(semantics: HtnStore, command: CommitPlanCommand) -> dict[str, str]:
        """Occurrence → task, from the plan in force plus the one being proposed."""

        mapping: dict[str, str] = {}
        active = semantics.active_plan_revision(command.mission_id)
        if active is not None:
            for spec in semantics.list_plan_memberships(command.mission_id, active.revision):
                mapping[str(spec.occurrence_id)] = str(spec.task_id)
        for spec in command.network.occurrences:
            mapping.setdefault(str(spec.occurrence_id), str(spec.task_id))
        return mapping

    # ----------------------------------------------------------------- the write half
    def _write(
        self,
        semantics: HtnStore,
        obligations: ObligationStore,
        command: CommitPlanCommand,
        *,
        mission: Mission,
        base: int,
        new_revision: int,
        intent: str,
        revoked: Mapping[str, int],
    ) -> PlanCommitReceipt:
        delta = command.delta
        network = command.network
        proposal_id = delta.compiled_from_proposal_id or delta.delta_id

        # The duty set as it stands *before* this delta opens anything: that is the
        # set the "names a duty nobody opened / re-opens an existing duty" invariant
        # is about (§18.3).  Reading it after the openings were persisted is what made
        # every legitimate opening look like a re-opening.
        registered = frozenset(obligations.obligation_ids(command.mission_id))
        require_commit_ready(delta, registered_obligations=registered)
        # Duties next: an occurrence may not name a duty nobody opened, so the
        # openings have to exist before the membership that references them.
        admitted: list[str] = []
        if delta.obligation_openings:
            ledger = obligations.load_ledger(command.mission_id)
            opened_by_duty = {
                str(opening.obligation_id): opening for opening in delta.obligation_openings
            }

            def _admit(admission: DemandAdmission, held: ObligationLedger) -> ObligationAccountView:
                # P2.3c part 2d, decision 3: the admission is a *recorded* act, taken by
                # the principal this command was authorised under, inside the same
                # transaction as the revision that adopted the slot asking for it
                # (TG implementation design §7.3).
                admitted.append(str(admission.obligation_id))
                return self.admit_obligation_demand(
                    command.mission_id,
                    admission.obligation_id,
                    principal=command.issued_by or "commit-service",
                    requester=admission.requester(),
                    evidence={
                        "parent_obligation_id": str(admission.parent_obligation_id),
                        "plan_revision": new_revision,
                        "delta_id": delta.delta_id,
                        "requirement_refs": list(
                            opened_by_duty[str(admission.obligation_id)].requirement_refs
                        ),
                    },
                    parent_obligation_id=admission.parent_obligation_id,
                    relation=str(admission.relation),
                    ledger=held,
                    plan_revision=new_revision,
                )

            apply_obligation_openings(
                ledger, delta, granted_fuel=dict(command.granted_fuel), admit=_admit
            )
            obligations.persist(ledger)
        released = self._withdraw_retired_demands(
            semantics, obligations, command, plan_revision=new_revision
        )

        for binding in command.task_bindings:
            if semantics.task_semantics_of(command.mission_id, str(binding.task_id)) is None:
                semantics.put_task_semantics(command.mission_id, binding)

        semantics.insert_plan_revision(
            command.mission_id,
            new_revision,
            snapshot_hash=content_hash_of(_network_identity(network)),
            read_set=delta.read_set,
            state="PREPARED",
            base_revision=base or None,
            delta_id=delta.delta_id,
        )

        instance_of = _occurrence_instances(network)
        for draft in delta.method_instances:
            semantics.insert_method_instance(command.mission_id, draft, state="ADOPTED")
        for instance_id in delta.retired_instance_ids:
            semantics.set_method_instance_state(command.mission_id, str(instance_id), "RETIRED")
        for spec in network.occurrences:
            semantics.insert_plan_membership(
                command.mission_id,
                new_revision,
                spec,
                instance_id=instance_of.get(str(spec.occurrence_id)),
                adopted=True,
            )
        for constraint in network.order_constraints:
            semantics.insert_order_constraint(command.mission_id, new_revision, constraint)
        for requirement in network.data_requirements:
            semantics.insert_data_requirement(command.mission_id, new_revision, requirement)
        semantics.record_read_set(command.mission_id, proposal_id, delta.read_set)

        # PREPARED → ACTIVE, in this same transaction: §9.4 forbids half an old plan
        # and half a new one being dispatchable, so the switch is never a second step.
        semantics.activate_plan_revision(command.mission_id, new_revision)

        # P2.3c part 2: the occurrence → work bridge.  Same transaction as the
        # revision it belongs to, because a plan whose occurrences are only half on
        # the board is exactly the "half an old plan and half a new one" §9.4
        # forbids.  Materialising is idempotent by construction — a row that exists
        # is reused, never rewritten — so a crash between the revision and the next
        # cycle cannot double-materialise or double-charge.
        materialised = self._materialise_occurrences(
            semantics, command, mission=mission, plan_revision=new_revision
        )
        activated = self._activate_for_work(mission, materialised)

        pending = _pending_dispatch(network, delta)
        payload = {
            "command_id": command.command_id,
            "delta_id": delta.delta_id,
            "proposal_id": proposal_id,
            "base_plan_revision": base,
            "plan_revision": new_revision,
            "base_graph_version": int(command.base_graph_version),
            "scope_id": command.scope_id,
            "intent_hash": intent,
            "read_set_hash": content_hash_of(delta.read_set.to_json()),
            "running_work_policy": str(command.running_work_policy),
            "adopted_method_instances": [str(item.instance_id) for item in delta.method_instances],
            "retired_method_instances": [str(item) for item in delta.retired_instance_ids],
            "added_occurrences": [str(spec.occurrence_id) for spec in delta.occurrences],
            "opened_obligations": [
                str(opening.obligation_id) for opening in delta.obligation_openings
            ],
            # P2.3c part 2d, decision 3: "opened" and "somebody asked for it" are two
            # different facts and the receipt carries both.
            "admitted_demands": sorted(admitted),
            "withdrawn_demands": sorted(released),
            "order_constraints": len(network.order_constraints),
            "data_requirements": len(network.data_requirements),
            "revoked_dispatch_generations": dict(sorted(revoked.items())),
            # Registered, not dispatched: the scheduler decides when, and P2.3c is
            # where it learns to read this.  Writing it here keeps the event and the
            # projection one call (§18.5) without this module dispatching anything.
            "pending_dispatch": pending,
            # P2.3c part 2: what the scheduler may now see, and under which equation.
            "materialised_tasks": list(materialised.created),
            "reused_tasks": list(materialised.reused),
            "budget_conservation": materialised.conservation(),
            "mission_status": str((activated or mission).status),
            "source": dict(command.source),
        }
        self._emit(
            PLAN_REVISION_COMMITTED,
            command.mission_id,
            key=f"{command.mission_id}:plan-{new_revision}",
            payload=payload,
        )
        return semantics.record_commit_receipt(
            command.mission_id,
            command_id=command.command_id,
            delta_id=delta.delta_id,
            base_plan_revision=base,
            new_plan_revision=new_revision,
            intent_hash=intent,
            read_set=delta.read_set,
            output_identity={
                "plan_revision": new_revision,
                "snapshot_hash": content_hash_of(_network_identity(network)),
                "occurrences": sorted(str(spec.occurrence_id) for spec in network.occurrences),
                "adopted_method_instances": sorted(
                    str(item.instance_id) for item in delta.method_instances
                ),
                "pending_dispatch": pending,
            },
            detail={
                "base_graph_version": int(command.base_graph_version),
                "mission_version": mission.version,
                "scope_id": command.scope_id,
                "principal_scope_epoch": semantics.epoch(command.mission_id, command.scope_id),
                "revoked_dispatch_generations": dict(sorted(revoked.items())),
                "running_work_policy": str(command.running_work_policy),
                "materialised_tasks": list(materialised.created),
                "budget_conservation": materialised.conservation(),
                "source": dict(command.source),
            },
        )

    # --------------------------------------------- P2.3c: occurrences become work
    def _materialise_occurrences(
        self,
        semantics: HtnStore,
        command: CommitPlanCommand,
        *,
        mission: Mission,
        plan_revision: int,
    ) -> Materialisation:
        """Give every occurrence of the new plan a ``Task`` row and an account.

        The rules, in the order they matter:

        1. **A row that already exists is reused, never rewritten.**  An occurrence
           keeps its Task across revisions (the occurrence id *is* the task id in the
           compiled network), and rewriting the row would throw away its attempts,
           its accepted artefacts and its budget history — the very facts §9.4's
           running-work reconciliation is about.  ``_revoke_running_work`` has
           already withdrawn the execution right of anything this delta replaces.
        2. **Every form gets a row; only a primitive gets a dispatchable one.**  See
           :mod:`.occurrence_tasks` — a compound row is BLOCKED and is refused by the
           form gate at three independent places.
        3. **The budget is a share of what the Mission's task pool still has.**
           ``open_account`` refuses limits that do not fit the parent, so a share is
           computed rather than guessed, and every other dimension is inherited from
           the Mission.  The dividend is the pool *minus what the existing rows
           already hold* and the divisor counts the compounds nobody has refined yet,
           so a later refinement round is still payable — see
           :func:`.occurrence_tasks.share_tokens`.  A share that does not fit, or a
           pool that cannot pay for the new primitives at all, is a refusal
           (``BUDGET_INSUFFICIENT``), never a row with an account nobody can draw on.
        """

        network = command.network
        pool = task_pool_tokens(mission)
        requirements = semantics.latest_requirements_revision(command.mission_id)
        existing: dict[str, int] = {}
        reused: list[str] = []
        pending: list[tuple[OccurrenceSpec, TaskSemanticBindingV1]] = []
        stored = self._store.list_tasks(command.mission_id)
        # What the Mission has already handed out — every row, not only the ones this
        # network names: an occurrence a previous revision retired still holds the
        # account it was opened with, and leaving it out of the equation would let the
        # same tokens be granted twice.
        #
        # P2.3j: *holds* is read from the account for a row this network no longer
        # contains.  A retired occurrence's row keeps its ceiling as history, but
        # what it can never give back is only what it used or still has reserved;
        # counting the whole ceiling meant a Mission whose first plan had shared the
        # pool out over its leaves could not fund any replacement at all — the
        # rejected branch held every token it had never spent, and the repair round's
        # replacement was refused ``BUDGET_INSUFFICIENT`` (H-L3-C1-r1's shape: six
        # accepted leaves holding the entire pool).  A row the network still names is
        # counted at its ceiling as before, because it can still draw on it.
        named = {str(spec.task_id) for spec in network.occurrences}
        committed = sum(
            int(task.budget.max_tokens or 0)
            if task.id in named
            else self._held_by_retired_row(task)
            for task in stored
        )
        for spec in sorted(network.occurrences, key=lambda item: str(item.occurrence_id)):
            task_id = str(spec.task_id)
            current = self._store.get_task(task_id)
            if current is not None:
                reused.append(task_id)
                if current.budget.max_tokens is not None:
                    existing[task_id] = int(current.budget.max_tokens)
                continue
            binding = semantics.task_semantics_of(command.mission_id, task_id)
            if binding is None:
                # Unreachable through ``commit_plan_revision`` — ``_check_structure``
                # refuses a membership with no binding first — but a materialiser that
                # invented a meaning here would be the silent half-mode §18.5 forbids.
                raise PlanCommitRejected(
                    "MISSING_SEMANTIC_BINDING",
                    f"occurrence {spec.occurrence_id!s} names task {task_id} which carries no "
                    "TaskSemanticBindingV1; an occurrence is not materialised without its "
                    "meaning (§18.5)",
                )
            pending.append((spec, binding))
        funded_now = sum(1 for spec, _binding in pending if spec.form is TaskForm.PRIMITIVE)
        # One share held back per compound that still owes a refinement — the ones
        # already refined have their children on the board (or in ``pending``) and do
        # not need a second reservation.
        reserved = sum(
            1
            for spec in network.occurrences
            if spec.form is TaskForm.COMPOUND
            and network.adopted_instance_for(spec.occurrence_id) is None
        )
        available = None if pool is None else max(0, pool - committed)
        share = share_tokens(available, funded_now, reserved)
        if share is not None and funded_now and share < MIN_TOKEN_SHARE:
            raise PlanCommitRejected(
                "BUDGET_INSUFFICIENT",
                f"the Mission's task pool has {available} tokens left and this revision needs "
                f"to fund {funded_now} new primitive occurrence(s) while holding a share for "
                f"{reserved} unrefined compound(s); a plan whose work cannot pay for anything "
                "is refused, not opened with an account nobody can draw on (§21.5)",
            )
        built: list[OccurrenceTask] = []
        ordinal = len(stored)
        for spec, binding in pending:
            ordinal += 1
            tokens = COMPOUND_TOKENS if spec.form is TaskForm.COMPOUND else share
            built.append(
                occurrence_task(
                    mission,
                    spec,
                    binding,
                    plan_revision=plan_revision,
                    budget=inherit_limits(
                        Budget(max_tokens=tokens, max_attempts=mission.budget.max_attempts),
                        mission.budget,
                    ),
                    ordinal=ordinal,
                    deployed=frozenset(self._deployed_layers),
                    requirements=requirements,
                    now=self._store.now,
                )
            )
        # The account *names* come from the host service, never spelled again here: a
        # second copy of the ``budget:`` prefix is a silent way to open a Task account
        # under a parent that does not exist, which ``open_account`` can only report as
        # "unknown budget account".  ``selection_commits`` reaches for them the same way.
        from .commit_service import mission_account, task_account

        result = Materialisation(
            tasks=tuple(built),
            reused=tuple(reused),
            pool_tokens=pool,
            share_tokens=share,
            committed_tokens=committed,
            funded_now=funded_now,
            reserved_subtrees=reserved,
            existing=existing,
        )
        equation = result.conservation()
        if not equation["holds"]:
            # Checked before a single row is written: the refusal is about the plan,
            # so it must not depend on the transaction being rolled back to be true.
            raise PlanCommitRejected(
                "BUDGET_INSUFFICIENT",
                f"materialising the plan would grant {result.granted_tokens} tokens on top of "
                f"the {committed} its rows already hold, out of a task pool of {pool}; a plan "
                "is not committed by overdrawing the Mission (§21.5 budget conservation)",
            )
        for item in built:
            self._store.insert_task(item.task, ordinal=item.ordinal)
            try:
                self._ledger.open_account(
                    account_id=task_account(item.task.id),
                    scope="task",
                    parent_id=mission_account(command.mission_id),
                    mission_id=command.mission_id,
                    limits=item.task.budget,
                )
            except BudgetError as error:
                raise PlanCommitRejected(
                    "BUDGET_INSUFFICIENT",
                    f"occurrence {item.occurrence_id} cannot be opened with "
                    f"{item.task.budget.to_json()}: {error}",
                ) from error
            self._emit(
                "TaskCommitted",
                command.mission_id,
                key=item.task.id,
                task_id=item.task.id,
                payload={
                    "commit_id": command.command_id,
                    "key": item.occurrence_id,
                    "dependencies": [],
                    "proposal": item.to_json(),
                    "source": {
                        **dict(command.source),
                        "materialised_by": "plan_revision",
                        "plan_revision": int(plan_revision),
                    },
                },
            )
        if built or reused:
            self._emit(
                OCCURRENCES_MATERIALISED,
                command.mission_id,
                key=f"{command.mission_id}:plan-{plan_revision}",
                payload={
                    "plan_revision": int(plan_revision),
                    "command_id": command.command_id,
                    **result.to_json(),
                },
            )
        return result

    def _activate_for_work(self, mission: Mission, materialised: Materialisation) -> Mission | None:
        """PLANNING → ACTIVE, by the formal rule rather than by a test helper.

        P2.3b's two guard tests had to call a ``_force_active`` helper because nothing
        moved a hierarchical Mission out of PLANNING — ``commit_task_graph`` does that
        for the legacy mode and it is never called here.  The rule is the same one:
        a Mission becomes ACTIVE when there is committed work to do.  "Committed work"
        is read from what this revision materialised, so a revision that put nothing
        dispatchable on the board leaves the Mission in PLANNING, which is the true
        statement about it.
        """

        if mission.status is not MissionStatus.PLANNING:
            return None
        if not any(item.dispatchable for item in materialised.tasks):
            if not any(
                (found := self._store.get_task(task_id)) is not None
                and found.status is not TaskStatus.CANCELLED
                and str(found.context.get("form", "")) == str(TaskForm.PRIMITIVE)
                for task_id in materialised.reused
            ):
                return None
        activated = next_mission(mission, MissionStatus.ACTIVE)
        self._store.update_mission(activated, expected_version=mission.version)
        return activated

    # ------------------------------------------------- the hierarchical graph gate
    def _require_semantic_bindings(
        self,
        mission: Mission,
        task_ids: Sequence[str],
        bindings: Mapping[str, TaskSemanticBindingV1] | None,
        key_to_id: Mapping[str, str],
    ) -> None:
        """§18.5: in the hierarchical mode every Task has a meaning, or nothing is written.

        A legacy Mission never reaches this method — the caller checks the mode
        first — so the old commit path keeps its exact behaviour and its bytes.
        """

        semantics = HtnStore(self._store)
        supplied = dict(bindings or {})
        for key, task_id in key_to_id.items():
            binding = supplied.get(key) or supplied.get(task_id)
            if binding is None:
                continue
            if str(binding.task_id) != task_id:
                binding = _rebound(binding, task_id)
            if semantics.task_semantics_of(mission.id, task_id) is None:
                semantics.put_task_semantics(mission.id, binding)
        missing = [
            task_id
            for task_id in task_ids
            if semantics.task_semantics_of(mission.id, task_id) is None
        ]
        if missing:
            raise PlanCommitRejected(
                "MISSING_SEMANTIC_BINDING",
                f"mission {mission.id} runs under the hierarchical semantics, where every Task "
                f"carries a TaskSemanticBindingV1; {sorted(missing)} carry none (§18.5)",
            )


def _shared(store: Store, semantics: HtnStore, mission_id: str) -> SemanticReadSetChecker:
    """The shared resolver set, with no overrides — the implementation itself."""

    return SemanticReadSetChecker(store, semantics, mission_id=mission_id)


def _rebound(binding: TaskSemanticBindingV1, task_id: str) -> TaskSemanticBindingV1:
    """The same meaning on the id the Commit Service minted for it.

    The Planner names its nodes ``A``/``B``; the formal ids are assigned inside the
    commit.  Re-pointing the binding is therefore normal, and the contract hash is
    recomputed from the old one so the lineage stays readable.
    """

    return replace(
        binding,
        task_id=TaskRef(task_id),
        contract_hash=content_hash_of(
            {
                "task_id": task_id,
                "bound_from_task_id": str(binding.task_id),
                "bound_from_contract_hash": binding.contract_hash,
                "contract_revision": int(binding.contract_revision),
            }
        ),
    )


def _superseded(binding: TaskSemanticBindingV1) -> TaskSemanticBindingV1:
    """The same Task with its execution right withdrawn (§6.4).

    The dispatch generation is what an in-flight dispatch carries, so raising it is
    the revocation; the contract revision moves with it because the store keys a
    binding by ``(task_id, contract_revision)`` and the previous row is history, not
    something to overwrite.
    """

    return replace(
        binding,
        contract_revision=ContractRevision(int(binding.contract_revision) + 1),
        contract_hash=content_hash_of(
            {
                "superseded_contract_hash": binding.contract_hash,
                "task_id": str(binding.task_id),
                "contract_revision": int(binding.contract_revision) + 1,
                "dispatch_generation": int(binding.dispatch_generation) + 1,
            }
        ),
        dispatch_generation=DispatchGeneration(int(binding.dispatch_generation) + 1),
    )


def _occurrence_instances(network: TaskNetworkSnapshot) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for draft in network.method_instances:
        for child in draft.child_bindings:
            mapping[str(child.occurrence_id)] = str(draft.instance_id)
    return mapping


def _network_identity(network: TaskNetworkSnapshot) -> dict[str, Any]:
    """The bytes that identify one plan revision's shape.

    Deliberately the *structure*, not the whole snapshot: two snapshots that place
    the same occurrences under the same edges are the same plan, and a hash that
    moved with an unrelated field would make every receipt incomparable.
    """

    return {
        "mission_id": str(network.mission_id),
        "occurrences": sorted(str(spec.occurrence_id) for spec in network.occurrences),
        "adopted_instances": sorted(str(item) for item in network.adopted_instance_ids),
        "order": sorted(
            [str(item.before), str(item.after), str(item.release_condition)]
            for item in network.order_constraints
        ),
        "data": sorted(item.requirement_id for item in network.data_requirements),
    }


def _pending_dispatch(network: TaskNetworkSnapshot, delta: ProposedPlanDelta) -> list[str]:
    """The primitive occurrences this revision newly put on the board.

    Registered only.  Whether one of them may actually run is the scheduler's
    question (readiness, inputs, budget, form) and is answered in P2.3c; answering
    it here would be exactly the "approve the new version while committing it" the
    plan forbids.
    """

    added = {spec.occurrence_id for spec in delta.occurrences}
    return sorted(
        str(spec.occurrence_id)
        for spec in network.occurrences
        if spec.occurrence_id in added and spec.form is TaskForm.PRIMITIVE
    )


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """The canonical bytes of a ``PlanRevisionCommitted`` payload (test helper)."""

    return canonical_json(dict(payload))


__all__ = (
    "HIERARCHICAL_SEMANTICS",
    "OCCURRENCES_MATERIALISED",
    "LEGACY_SEMANTICS",
    "PLAN_REVISION_COMMITTED",
    "SEMANTICS_ALIASES",
    "SEMANTICS_KEY",
    "CommitPlanCommand",
    "PlanCommitRejected",
    "PlanCommitsMixin",
    "PlanPrincipal",
    "canonical_payload",
    "normalise_semantics",
    "semantics_of",
)
