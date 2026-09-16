# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One semantic read-set re-validation, shared by every Commit path (ADR-13 clause 2).

ADR-13's second clause is a single sentence — *a hierarchical proposal carries a
semantic read-set beside it, which is checked item by item afterwards, and any stale
item refuses the commit* — and the only way to keep it true is for there to be **one**
implementation of "item by item".  Two of them drift: the P2.3c review found exactly
that, a second copy in ``resolution_commits`` that re-checked five of the eleven
channels and silently let a refuted observation, a revoked authority and a re-planned
duty through.

So the eleven channels live here, once:

===========================  =================================================
``requirements_revision``    the Mission's latest requirements revision
``goal_revisions``           TASK — a task's contract revision and hash
``method_revisions``         METHOD — a definition's version and hash, or its
                             *status* when it is suspended / retired / rejected
``observation_revisions``    FACT — an observation record, or a ``ValidityWitness``
                             by its scope epoch; a *superseded* observation is stale
``acceptance_revisions``     ACCEPTANCE — contract revision and hash, or its
                             ``validity`` when it is no longer CURRENT
``obligation_revisions``     OBLIGATION — shape-change count plus lifecycle and
                             ``resolution_ref``
``authority_revisions``      AUTHORITY — the approval record's version and hash
``support_sets``             the *set* of supports: member revision **and** digest
``scope_epochs``             the §11.5 epoch barrier
``absences``                 "there is no such thing" is a read, and it goes stale
                             by becoming false
``budget_grant_revision``    the allowance a proposal was built against
===========================  =================================================

``manager_epoch`` is the twelfth value a :class:`SemanticReadSet` carries and is
deliberately *not* here: both callers check it in a gate of their own, before this
one, because a stale manager epoch is a scope-authority answer
(``MANAGER_EPOCH_STALE``) rather than one of a list of stale items.

Three design points worth stating, because each is a property the suites rely on:

* **Nothing here raises the caller's rejection.**  :meth:`SemanticReadSetChecker.verify`
  returns a :class:`ReadSetVerdict` and each Commit path turns it into its own
  exception with its own reason code.  That is what lets ``plan_commits`` keep its
  message bytes exactly as they were while ``resolution_commits`` gains the channels
  it was missing.
* **"I could not tell" and "it changed" stay apart.**  A channel this deployment
  cannot re-check is ``unresolved``; one that moved is ``stale``.  Reporting the
  first as the second sends a proposer off to recompile something that was never the
  problem.
* **Fail closed.**  A value with no store-side authority — ``budget_grant_revision``
  when no resolver is injected — is *unresolved* when it is claimed and ignored when
  it is zero.  A claim nobody can re-check is never quietly accepted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.evidence_state import Validity
from ..contracts.htn import (
    AbsenceRead,
    MethodRegistryStatus,
    ObligationId,
    ReadItem,
    ReadItemKind,
    SemanticReadSet,
)
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.store import Store, StoreError

#: The absence predicates this deployment knows how to re-check (TG §11.2).  An
#: absence claim outside this set is unresolvable, and saying so is the point: an
#: unknown "there is nothing there" is not evidence that there is nothing there.
KNOWN_ABSENCE_PREDICATES: frozenset[str] = frozenset(
    {"no_adopted_method_instance", "no_obligation", "no_order_constraint_into"}
)

#: A method in one of these states is not "missing"; it is a definition that may no
#: longer be built on, and reporting the status is more useful than a hash mismatch.
UNUSABLE_METHOD_STATUSES: frozenset[MethodRegistryStatus] = frozenset(
    {
        MethodRegistryStatus.SUSPENDED,
        MethodRegistryStatus.RETIRED,
        MethodRegistryStatus.REJECTED,
    }
)

#: The separator that namespaces the two dispatch-control values
#: :func:`~..graph.eligibility.build_read_set` records in the TASK lane
#: (``<task>#dispatch_generation`` / ``<task>#input_binding_revision``).  A
#: :class:`~..contracts.htn.ReadItem` holds one revision, and a task has three, so
#: the id spelling is how they stay apart.  Only a caller that opts in resolves them.
TASK_CHANNEL_SEPARATOR = "#"


@dataclass(frozen=True, slots=True)
class StaleRead:
    """One read-set item that no longer describes the world."""

    channel: str
    subject: str
    expected: str
    found: str

    def detail(self) -> str:
        return (
            f"{self.channel} {self.subject!r} was read at {self.expected}, "
            f"the current state is {self.found}"
        )


class ReadSetChannelUnknown(RuntimeError):
    """A claim whose *channel* this deployment does not re-check at all.

    Raised rather than collected because it is a different kind of answer from a
    subject that is merely absent: the caller cannot repair it by recompiling, and
    every Commit path maps it to ``READ_SET_UNRESOLVED`` with this message.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ReadSetVerdict:
    """What the re-validation found: nothing, or stale items, or unresolvable ones.

    The two ``*_detail`` helpers assemble the messages both Commit paths use, so the
    wording is part of this contract rather than of either caller.
    """

    stale: tuple[StaleRead, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.stale or self.unresolved)

    def unresolved_detail(self) -> str:
        return "the read-set names subjects this store cannot re-check: " + "; ".join(
            sorted(self.unresolved)
        )

    def stale_detail(self) -> str:
        return "; ".join(
            item.detail() for item in sorted(self.stale, key=lambda s: (s.channel, s.subject))
        )


#: ``(semantics, mission_id, item) -> (revision, digest) | None``.
Resolver = Callable[[HtnStore, str, ReadItem], "tuple[int, str] | None"]


class SemanticReadSetChecker:
    """Re-validate one :class:`SemanticReadSet` against the store, channel by channel.

    ``allow_task_control_channels`` exists so this module can be shared without
    changing what ``plan_commits`` does.  A plan proposal's TASK ids are bare task
    ids; an *accept* command's read-set is the one
    :func:`~..graph.eligibility.build_read_set` built, whose TASK lane also carries
    ``<task>#dispatch_generation`` and ``<task>#input_binding_revision``.  With the
    flag off those ids resolve to nothing and are reported unresolved — exactly the
    behaviour ``plan_commits`` had before this module existed.

    ``budget_grant_resolver`` is injected for the same reason: P2 has no store-side
    budget-grant authority, so a claimed non-zero allowance revision is *unresolved*
    until one exists.  Passing a resolver is how a later slice turns it into a real
    check without touching either caller.
    """

    def __init__(
        self,
        store: Store,
        semantics: HtnStore,
        *,
        mission_id: str,
        allow_task_control_channels: bool = False,
        budget_grant_resolver: Callable[[str], int] | None = None,
        resolvers: Mapping[str, Resolver] | None = None,
    ) -> None:
        self._store = store
        self._semantics = semantics
        self._mission_id = mission_id
        self._allow_task_channels = bool(allow_task_control_channels)
        self._budget_grant = budget_grant_resolver
        #: A caller may substitute one channel's resolver — which is how a Commit
        #: path keeps a seam its own mutation tests can reach, and how a test proves
        #: that weakening a single channel is caught.  Everything not overridden is
        #: this class's own method, so there is still one implementation.
        self._overrides = dict(resolvers or {})

    def _resolver(self, channel: str, default: Resolver) -> Resolver:
        return self._overrides.get(channel, default)

    # ------------------------------------------------------------------ the one entry
    def verify(self, read_set: SemanticReadSet) -> ReadSetVerdict:
        """Every channel, item by item, fail closed.  Raises only for an unknown channel."""

        stale: list[StaleRead] = []
        unresolved: list[str] = []
        self._check_requirements(read_set, stale)
        self._check_budget_grant(read_set, stale, unresolved)
        for channel, items, default in (
            ("goal", read_set.goal_revisions, self.goal_state),
            ("method", read_set.method_revisions, self.method_state),
            ("observation", read_set.observation_revisions, self.observation_state),
            ("acceptance", read_set.acceptance_revisions, self.acceptance_state),
            ("obligation", read_set.obligation_revisions, self.obligation_state),
            ("authority", read_set.authority_revisions, self.authority_state),
        ):
            resolve = self._resolver(channel, default)
            for item in items:
                self._probe(item, channel, resolve, stale, unresolved)
        self._check_support_sets(read_set, stale, unresolved)
        self._check_scope_epochs(read_set, stale)
        self._check_absences(read_set, stale)
        return ReadSetVerdict(stale=tuple(stale), unresolved=tuple(unresolved))

    # ------------------------------------------------------- the proposer's own side
    def read_item(self, kind: ReadItemKind, subject_id: str) -> ReadItem:
        """The entry a proposer should record for one subject, right now.

        The proposing side and the checking side must agree on *what* a subject's
        semantic revision is; writing that formula twice is how they stop agreeing.
        A subject whose current state cannot be expressed as a revision plus a
        content hash raises here rather than being recorded as an unre-checkable read.
        """

        channels: Mapping[ReadItemKind, tuple[str, Resolver]] = {
            ReadItemKind.TASK: ("goal", self.goal_state),
            ReadItemKind.METHOD: ("method", self.method_state),
            ReadItemKind.FACT: ("observation", self.observation_state),
            ReadItemKind.ACCEPTANCE: ("acceptance", self.acceptance_state),
            ReadItemKind.OBLIGATION: ("obligation", self.obligation_state),
            ReadItemKind.AUTHORITY: ("authority", self.authority_state),
        }
        chosen = channels.get(ReadItemKind(str(kind)))
        resolve = None if chosen is None else self._resolver(chosen[0], chosen[1])
        if resolve is None:
            raise ReadSetChannelUnknown(f"{kind!s} is not a channel this deployment re-checks")
        probe = ReadItem(kind=kind, id=subject_id, semantic_revision=0, content_hash="0" * 64)
        found = resolve(self._semantics, self._mission_id, probe)
        if found is None:
            raise ReadSetChannelUnknown(
                f"{kind!s} {subject_id!r} is not something this store holds"
            )
        revision, digest = found
        try:
            return ReadItem(
                kind=kind, id=subject_id, semantic_revision=revision, content_hash=digest
            )
        except ContractError as error:
            # e.g. a suspended method, whose "state" is a status and not a hash: there
            # is nothing here a proposal may legitimately claim to have read.
            raise ReadSetChannelUnknown(
                f"{kind!s} {subject_id!r} is currently {digest}, which cannot be read as a "
                f"revision ({error})"
            ) from error

    # ------------------------------------------------------------------ scalar channels
    def _check_requirements(self, read_set: SemanticReadSet, stale: list[StaleRead]) -> None:
        latest = self._semantics.latest_requirements_revision(self._mission_id)
        current = 0 if latest is None else int(latest.revision)
        if int(read_set.requirements_revision) != current:
            stale.append(
                StaleRead(
                    "requirements",
                    self._mission_id,
                    str(int(read_set.requirements_revision)),
                    str(current),
                )
            )

    def _check_budget_grant(
        self, read_set: SemanticReadSet, stale: list[StaleRead], unresolved: list[str]
    ) -> None:
        """The allowance revision a proposal was built against (§21.5, ADR-13).

        P2 has no store-side authority for it.  Zero is "nothing claimed"; a claimed
        revision with nobody to confirm it is ``unresolved`` rather than accepted —
        a budget the commit cannot re-read is not a budget it may spend against.
        """

        claimed = int(read_set.budget_grant_revision)
        if self._budget_grant is None:
            if claimed:
                unresolved.append(f"budget_grant_revision {claimed!r}")
            return
        current = int(self._budget_grant(self._mission_id))
        if claimed != current:
            stale.append(StaleRead("budget_grant", self._mission_id, str(claimed), str(current)))

    # ------------------------------------------------------------------ item channels
    def _probe(
        self,
        item: ReadItem,
        channel: str,
        resolve: Resolver,
        stale: list[StaleRead],
        unresolved: list[str],
    ) -> None:
        found = resolve(self._semantics, self._mission_id, item)
        if found is None:
            unresolved.append(f"{channel} {item.id!r}")
            return
        revision, digest = found
        if (revision, digest) != (int(item.semantic_revision), item.content_hash):
            stale.append(
                StaleRead(
                    channel,
                    item.id,
                    f"revision {int(item.semantic_revision)} hash {item.content_hash[:12]}",
                    f"revision {revision} hash {digest[:12]}",
                )
            )

    def goal_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        """A task's contract revision and hash — or one of its control channels.

        The control channels are only resolved for a caller that opted in; see the
        class docstring for why.
        """

        subject = item.id
        channel = ""
        if TASK_CHANNEL_SEPARATOR in subject:
            if not self._allow_task_channels:
                return None
            subject, channel = subject.split(TASK_CHANNEL_SEPARATOR, 1)
        binding = semantics.task_semantics_of(mission_id, subject)
        if binding is None:
            return None
        if not channel:
            return int(binding.contract_revision), binding.contract_hash
        value = _task_channel_value(binding, channel)
        if value is None:
            return None
        return value, content_hash_of({"task": subject, "channel": channel, "value": value})

    def method_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        del mission_id
        try:
            stored = semantics.get_method(item.id, int(item.semantic_revision))
        except StoreError:
            return None
        if stored.registration.status in UNUSABLE_METHOD_STATUSES:
            return int(item.semantic_revision), f"status:{stored.registration.status!s}"
        return stored.contract.method_version, stored.contract.method_ref().content_hash

    def observation_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        try:
            record = semantics.get_observation(item.id)
        except StoreError:
            return self.witness_state(semantics, mission_id, item)
        # An observation record is immutable, so it goes stale by being *superseded*:
        # a later record for the same proposition is precisely the counter-evidence
        # a plan built on the earlier one must not ignore.
        newest = semantics.list_observations(mission_id, proposition_key=record.proposition_key)
        if newest and newest[-1].observation_id != record.observation_id:
            return int(item.semantic_revision), f"superseded_by:{newest[-1].observation_id}"
        return int(item.semantic_revision), content_hash_of(record.to_json())

    def witness_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        """A fact read that names a :class:`ValidityWitness` rather than a record.

        C29: a witness is valid only while its scope epoch still stands, so the
        epoch is the witness's semantic revision.  A fact read that names neither an
        observation nor a witness is left unresolved on purpose — it cannot be
        re-checked, and passing it would be a gate that answers "yes" by default.
        """

        try:
            witness = semantics.get_validity_witness(item.id)
        except StoreError:
            return None
        current = semantics.epoch(mission_id, witness.scope_id)
        if current != witness.scope_epoch:
            return current, f"scope_epoch:{current}"
        return witness.scope_epoch, content_hash_of(witness.to_json())

    def acceptance_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        del mission_id
        try:
            acceptance = semantics.get_acceptance(item.id)
        except StoreError:
            return None
        if acceptance.validity is not Validity.CURRENT:
            return int(item.semantic_revision), f"validity:{acceptance.validity!s}"
        return int(acceptance.contract_revision), content_hash_of(acceptance.to_json())

    def obligation_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        del semantics
        obligations = ObligationStore(self._store)
        duty_id = ObligationId(item.id)
        if not obligations.exists(mission_id, duty_id):
            return None
        duty = obligations.obligation(mission_id, duty_id)
        account = obligations.account(mission_id, duty_id)
        # A duty's *shape* history is its semantic revision: re-planning it is exactly
        # what a plan that read it needs to hear about (§8.4).
        return account.shape_changes, content_hash_of(
            {
                "obligation": duty.to_json(),
                "lifecycle": str(account.lifecycle),
                "resolution_ref": account.resolution_ref,
            }
        )

    def authority_state(
        self, semantics: HtnStore, mission_id: str, item: ReadItem
    ) -> tuple[int, str] | None:
        del mission_id, semantics
        record = self._store.get_approval(item.id)
        if record is None:
            return None
        return int(record.get("version", 0)), content_hash_of(dict(record))

    # ------------------------------------------------------------------ set channels
    def _check_support_sets(
        self, read_set: SemanticReadSet, stale: list[StaleRead], unresolved: list[str]
    ) -> None:
        # C29: the *set* of supports, not only its members.  Adding a
        # counter-observation leaves every positive support untouched, so without
        # the member digest the read would still look current (AER scenario I02).
        for support in read_set.support_sets:
            try:
                stored = self._semantics.get_justification_set(support.support_set_id)
            except StoreError:
                unresolved.append(f"support_set {support.support_set_id!r}")
                continue
            if (stored.member_revision, stored.member_digest) != (
                int(support.revision),
                support.member_digest,
            ):
                stale.append(
                    StaleRead(
                        "support_set",
                        support.support_set_id,
                        f"revision {int(support.revision)} digest {support.member_digest[:12]}",
                        f"revision {stored.member_revision} digest {stored.member_digest[:12]}",
                    )
                )

    def _check_scope_epochs(self, read_set: SemanticReadSet, stale: list[StaleRead]) -> None:
        # C29: the epoch barrier.  Evidence is invalidated by raising the scope's
        # epoch, which never edits the record the proposal read.
        for scope in read_set.scope_epochs:
            current = self._semantics.epoch(self._mission_id, scope.scope_id)
            if int(scope.validity_epoch) != current:
                stale.append(
                    StaleRead(
                        "validity_epoch",
                        scope.scope_id,
                        str(int(scope.validity_epoch)),
                        str(current),
                    )
                )

    def _check_absences(self, read_set: SemanticReadSet, stale: list[StaleRead]) -> None:
        # TG §11.2: "there is no such thing" is a read too, and it goes stale by
        # becoming false — which is exactly the A→B / B→A merge the plan warns about.
        for absence in read_set.absences:
            present = self.absence_broken(absence)
            if present is not None:
                stale.append(
                    StaleRead(
                        "absence",
                        f"{absence.predicate}/{absence.scope_id}",
                        "nothing",
                        present,
                    )
                )

    def absence_broken(self, absence: AbsenceRead) -> str | None:
        """What now exists where the proposal read nothing, or ``None``.

        The three predicates are the three "nothing is there" facts a plan can rest
        on: no method instance adopted at an occurrence, no duty under that id, and
        no order edge into a node.  A predicate this deployment does not know is not
        silently treated as absent — an unknown absence claim is unresolvable.
        """

        predicate = absence.predicate
        scope_id = absence.scope_id
        semantics = self._semantics
        mission_id = self._mission_id
        if predicate == "no_adopted_method_instance":
            for draft in semantics.list_method_instances(mission_id, state="ADOPTED"):
                if str(draft.effective_goal_occurrence_id) == scope_id:
                    return f"method instance {draft.instance_id!s}"
            return None
        if predicate == "no_obligation":
            obligations = ObligationStore(self._store)
            return (
                f"obligation {scope_id}"
                if obligations.exists(mission_id, ObligationId(scope_id))
                else None
            )
        if predicate == "no_order_constraint_into":
            active = semantics.active_plan_revision(mission_id)
            if active is None:
                return None
            found = semantics.list_order_constraints(mission_id, active.revision, after=scope_id)
            return f"order constraint from {found[0].before!s}" if found else None
        raise ReadSetChannelUnknown(
            f"absence predicate {predicate!r} is not one this deployment can re-check"
        )


def _task_channel_value(binding: Any, channel: str) -> int | None:
    """The value of one namespaced TASK control channel, or ``None`` if unknown."""

    if channel == "dispatch_generation":
        return int(binding.dispatch_generation)
    if channel == "input_binding_revision":
        return int(binding.input_binding_revision)
    return None


def channel_names(read_set: SemanticReadSet) -> tuple[str, ...]:
    """Which channels of ``read_set`` actually carry a claim (diagnostics / tests)."""

    named: list[str] = ["requirements"]
    groups: Sequence[tuple[str, Sequence[Any]]] = (
        ("goal", read_set.goal_revisions),
        ("method", read_set.method_revisions),
        ("observation", read_set.observation_revisions),
        ("acceptance", read_set.acceptance_revisions),
        ("obligation", read_set.obligation_revisions),
        ("authority", read_set.authority_revisions),
        ("support_set", read_set.support_sets),
        ("validity_epoch", read_set.scope_epochs),
        ("absence", read_set.absences),
    )
    named.extend(name for name, items in groups if items)
    if int(read_set.budget_grant_revision):
        named.append("budget_grant")
    return tuple(named)


__all__ = (
    "KNOWN_ABSENCE_PREDICATES",
    "TASK_CHANNEL_SEPARATOR",
    "UNUSABLE_METHOD_STATUSES",
    "ReadSetChannelUnknown",
    "ReadSetVerdict",
    "Resolver",
    "SemanticReadSetChecker",
    "StaleRead",
    "channel_names",
)
