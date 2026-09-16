# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 3a: cutting the root ``MISSION_FINAL`` review, and cutting it again.

Part 2d's real-model smoke ended on one blocker and one blocker only::

    root resolution not formed: ROOT_REVIEW_PACKAGE_MISSING (no MISSION_FINAL
    ReviewPackage is stored for task task-root; the root review has not been cut,
    so there is nothing to resolve from)

``HierarchicalDispatch.root_resolution_inputs`` *reads* the three anchors a root
``GoalResolution`` is formed from — the ``MISSION_FINAL`` ``ReviewPackage``, its
official ``ReviewRecord`` and the ``purpose=ACCEPT`` witness — and reports each as
missing rather than inventing it.  Nothing in ``src`` produced them, so a
hierarchical Mission could never reach ``COMPLETED``.  This module is the deployment
side that produces them, and the whole of its design is in what it refuses to do:

* **It never writes a verdict.**  :meth:`RootReviewCoordinator.cut` freezes the
  anchor; the *conclusion* only ever arrives through
  :meth:`RootReviewCoordinator.record_review`, from a Critic reply the caller
  parsed.  AER I05 — "a review that ended is not a review that passed" — is not a
  rule this module checks, it is a shape it has: there is no code path that
  produces ``ReviewVerdict.ACCEPT`` without a reviewer having said ``PASS``.
* **It never resolves anything.**  The root resolution stays where it was
  (``attempt_root_resolution`` → ``commit_goal_resolution``), so the rule set that
  decides "is this Mission complete" is still in one place (§21.5).
* **It never re-cuts silently, and never re-cuts for ever.**  A cut is a promise
  that a reviewer saw *this* requirements revision over *these* contributions; when
  either moves the old package is superseded **with a record** and a new one is cut,
  and the number of cuts a single requirements revision may carry is bounded
  (:data:`DEFAULT_MAX_CUTS_PER_REVISION`).  Past the bound nothing is cut, a
  structured event is written, and the Mission takes part 2d's idle-stall path — a
  visible stop rather than a loop that spends a model call every cycle.

Why a re-cut is needed at all (part 2d, review P1-7).  Every leaf acceptance
publishes a Mission-level ``RequirementsRevision`` carrying *that leaf's* coverage
criteria, and the root resolution is checked against the revision its review package
was bound to.  So a leaf accepted (or revoked) after the root review was cut moves
the Mission's requirements underneath a review that never saw them, and the read-set
channel refuses the resolution with ``READ_SET_STALE``.  That refusal is correct;
the repair is to review again, which is what this module does.

The account (§13 v1.4, §21.5).  A ``MISSION_FINAL`` review's cost belongs to
``ReviewAccount.MISSION`` — :func:`~...contracts.resolution.account_for_purpose` is
the only place that mapping lives and this module quotes it rather than repeating
it.  It is deliberately **not** any Task's budget: the root review judges the
composition of every child, and charging it to whichever leaf happened to finish
last is exactly the mis-accounting §18.5 warns a new role purpose about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from ..contracts.htn import TaskForm, TaskSemanticBindingV1
from ..contracts.models import ContractError
from ..contracts.resolution import (
    AllExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequiredEvidencePolicy,
    RequirementClass,
    RequirementsRevision,
    RequirementsRevisionId,
    ReviewAccount,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
    account_for_purpose,
)
from ..contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from ..knowledge.validity import NO_SUBJECT, witness_subject
from ..storage.htn_store import HtnStore
from ..storage.store import StoreError
from .hierarchical_dispatch import (
    ROOT_REVIEW_CUT,
    ROOT_REVIEW_SUPERSEDED,
    append_hierarchical_event,
)

#: The review policy this deployment cuts a root ``MISSION_FINAL`` package under.
#: One named constant rather than a literal at three call sites, so the package, the
#: record and the event cannot drift into quoting two policies.
ROOT_REVIEW_POLICY = "hierarchical-root-review-v1"

#: How many times one ``requirements_revision`` may be re-cut before the coordinator
#: stops cutting.  Three, not one: the first re-cut is the ordinary answer to a leaf
#: accepted late, and a second is the ordinary answer to a leaf revoked while the
#: first re-cut was in flight.  A third that still does not settle is no longer a
#: race — it is a world that keeps moving, and spending another model call on it is
#: how a loop that never terminates looks from the inside.
DEFAULT_MAX_CUTS_PER_REVISION = 3

#: How many times one package may be *put to* the reviewer.  Two, and the second one
#: only ever happens when the first reply could not be read: an unreadable answer is
#: not an answer, so asking again is not asking twice, and it is exactly the second
#: chance the Task Critic already gets through ``critic_schema_retry_feedback``.  A
#: reply that *was* read — PASS or FAIL — is never re-asked, whatever it said.
MAX_ROOT_REVIEW_ASKS = 2

#: :data:`~.hierarchical_dispatch.ROOT_REVIEW_CUT` and
#: :data:`~.hierarchical_dispatch.ROOT_REVIEW_SUPERSEDED` are declared beside the
#: reader that has to honour them — ``root_resolution_inputs`` picks the live package
#: and a picker that could not see a supersede record would resolve from an anchor
#: the world has moved past.  They are re-exported here because this module is the
#: one that writes them.

#: The reviewer concluded something other than ACCEPT.  §9.1's decision table, not a
#: silent retry: nothing is re-cut and nothing is resolved on the strength of it.
ROOT_REVIEW_REJECTED = "HierarchicalRootReviewRejected"

#: The reviewer's reply could not be read as a verdict.  Also **not** a retry: an
#: unreadable answer is not a conclusion, and asking again with the same package
#: would spend the Mission account on the same question (AER-V04).
ROOT_REVIEW_UNREADABLE = "HierarchicalRootReviewUnreadable"

#: One requirements revision has used up its cuts.  The Mission is left with no
#: live package, which is how it reaches part 2d's idle-stall path visibly.
ROOT_REVIEW_CUT_BUDGET_SPENT = "HierarchicalRootReviewCutBudgetSpent"


class RootReviewStatus(StrEnum):
    """Where the root review stands.  Exactly one of these is true at a time."""

    #: A gating child of the adopted root method has no accepted outcome yet.
    NOT_READY = "NOT_READY"
    #: The root duty is already resolved; there is nothing left to review.
    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    #: Ready, and no package has ever been cut.
    CUT_REQUIRED = "CUT_REQUIRED"
    #: A package was cut and the world moved under it.
    RECUT_REQUIRED = "RECUT_REQUIRED"
    #: A live package is waiting for its reviewer.
    AWAITING_REVIEW = "AWAITING_REVIEW"
    #: The reviewer concluded, and did not conclude ACCEPT.
    REVIEW_REJECTED = "REVIEW_REJECTED"
    #: A live package carries an official ACCEPT record: the resolution may be offered.
    READY = "READY"
    #: A re-cut is needed and this revision has no cuts left.
    CUT_BUDGET_SPENT = "CUT_BUDGET_SPENT"
    #: The plan could not be read.  Diagnosed elsewhere; never a reason to cut.
    UNREADABLE_PLAN = "UNREADABLE_PLAN"


@dataclass(frozen=True, slots=True)
class RootReviewState:
    """What :meth:`RootReviewCoordinator.state` read, and nothing it decided."""

    status: RootReviewStatus
    detail: str = ""
    task_id: str = ""
    obligation_id: str = ""
    package: ReviewPackage | None = None
    record: ReviewRecord | None = None
    #: The Mission's current requirements revision, which a fresh cut binds.
    requirements_revision: int = 0
    #: The acceptance ids that currently contribute, sorted.
    contributions: tuple[str, ...] = ()
    #: How many cuts the package's requirements revision has already spent.
    cuts_used: int = 0
    #: Why a re-cut is needed, in machine-readable form.
    stale_reasons: tuple[str, ...] = ()

    @property
    def needs_cut(self) -> bool:
        return self.status in {RootReviewStatus.CUT_REQUIRED, RootReviewStatus.RECUT_REQUIRED}

    def to_json(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "detail": self.detail,
            "task_id": self.task_id,
            "obligation_id": self.obligation_id,
            "package_id": None if self.package is None else str(self.package.package_id),
            "record_id": None if self.record is None else str(self.record.record_id),
            "requirements_revision": int(self.requirements_revision),
            "contributions": list(self.contributions),
            "cuts_used": int(self.cuts_used),
            "stale_reasons": list(self.stale_reasons),
        }


@dataclass(frozen=True, slots=True)
class RootReviewRequest:
    """The typed context the root reviewer is given.  No Mission-side authority in it.

    It carries what a reviewer needs to judge a composition — the goal, the criteria
    it owes, and the child acceptances offered as the candidate — and nothing that
    would let a reply steer the system: no principal, no budget field, no ids the
    commit path takes its permissions from.
    """

    package_id: str
    goal_task_id: str
    goal_statement: str
    criteria: tuple[Mapping[str, Any], ...]
    contributions: tuple[Mapping[str, Any], ...]
    requirements_revision: int
    #: What was wrong with the previous reply about *this* package, when there was
    #: one.  It is the reply's **shape** and never its conclusion: a reviewer that
    #: judged and was misread is asked to say the same thing readably, and one that
    #: judged FAIL is never asked again at all (§9.1 owns that, not this module).
    schema_feedback: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "review_package_id": self.package_id,
            "purpose": str(ReviewPurpose.MISSION_FINAL),
            "budget_account": str(ReviewAccount.MISSION),
            "goal_task_id": self.goal_task_id,
            "goal_statement": self.goal_statement,
            "requirements_revision": int(self.requirements_revision),
            "criteria": [dict(item) for item in self.criteria],
            "contributions": [dict(item) for item in self.contributions],
        }
        if self.schema_feedback:
            payload["schema_feedback"] = self.schema_feedback
        return payload

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(str(item["criterion_id"]) for item in self.criteria)


def root_criteria(binding: TaskSemanticBindingV1) -> tuple[Criterion, ...]:
    """The root goal's coverage criteria, as the final review's criteria.

    Two deliberate choices, because each is a way this could have been written
    wrongly:

    * ``EvaluationKind.SEMANTIC`` with **no** ``required_check_ids``.  A root
      criterion is not re-executed here — the checks that could run already ran on
      the leaves and their receipts live on the leaf acceptances — so naming a check
      id would either be a check nobody runs (and the criterion could never be
      satisfied) or a receipt this side forged.  What remains is a judgement, and
      the enum has a word for that.
    * ``independence_required=True``.  The one thing a composition review must not
      be is the producers' own say-so, so the acceptance formula is *asked* to
      enforce it rather than this module hoping for it: a reviewer who appears in
      the package's ``producer_agent_ids`` makes the whole resolution refuse.
    """

    names = tuple(binding.goal_signature.coverage_criteria) or tuple(binding.requirement_refs)
    if not names:
        raise ContractError(
            f"root task {binding.task_id!s} declares no coverage criterion and no requirement "
            "ref; a Mission whose root owes nothing has nothing for a final review to judge "
            "(AER §6.2)"
        )
    return tuple(
        Criterion(
            criterion_id=str(name),
            revision=1,
            origin=CriterionOrigin.DERIVED,
            statement=(
                f"{name} is covered by the accepted contributions of {binding.task_id!s}, "
                "judged as a whole"
            ),
            requirement_class=RequirementClass.REQUIRED_OUTCOME,
            evaluation_kind=EvaluationKind.SEMANTIC,
            required_evidence_policy=RequiredEvidencePolicy(
                independence_required=True,
                coverage_statement=(
                    "judged by the MISSION_FINAL review over the children's acceptances"
                ),
            ),
        )
        for name in dict.fromkeys(names)
    )


def root_requirements(
    mission_id: str, binding: TaskSemanticBindingV1, *, revision: int
) -> RequirementsRevision:
    """The requirements revision a root review is cut against."""

    criteria = root_criteria(binding)
    expression: Any = CriterionExpr(criteria[0].criterion_id)
    if len(criteria) > 1:
        expression = AllExpr(children=tuple(CriterionExpr(item.criterion_id) for item in criteria))
    return RequirementsRevision(
        revision_id=RequirementsRevisionId(f"req-{mission_id}-{int(revision)}"),
        mission_id=mission_id,
        revision=int(revision),
        criteria=criteria,
        success_expression=expression,
    )


def acceptance_ref(acceptance_id: str) -> TypedRef:
    """One contributing acceptance, referenced as the candidate it is.

    ``Provenance.TOOL``: an ``Acceptance`` row is written by the Commit Service, so
    the attribution this side writes is the one ``receipt_source`` may trust.  A
    model never gets to attribute a candidate ref to the system.
    """

    return TypedRef(
        kind=TypedRefKind.ACCEPTANCE,
        id=str(acceptance_id),
        revision=1,
        content_hash=content_hash_of(str(acceptance_id)),
        produced_by=Provenance.TOOL,
    )


@dataclass(slots=True)
class RootReviewCoordinator:
    """The deployment side of the root ``MISSION_FINAL`` review.

    Holds the store, the one Commit Service and the hierarchical assembly; holds no
    state about a Mission.  Everything it needs to know about where a Mission stands
    is read back from the library and from its own events, so a restarted process
    reaches the same conclusion as the one that cut the package.
    """

    store: Any
    commit: Any
    dispatch: Any
    scope_id: str = "mission"
    #: Who signs the package and the witness.  The **reviewer** is named on the
    #: record and comes from the reply, not from here.
    issued_by: str = "root-review-coordinator"
    max_cuts_per_revision: int = DEFAULT_MAX_CUTS_PER_REVISION

    @property
    def semantics(self) -> HtnStore:
        """A thin view over the same connection, never a cached one.

        ``HtnStore`` is a wrapper and not a session, and a cached one would keep the
        *previous* handle alive across a restart of the store — which is the shape
        that made part 2d's fixtures read a closed connection.
        """

        return HtnStore(self.store)

    @property
    def account(self) -> ReviewAccount:
        """§13 v1.4: which budget account a ``MISSION_FINAL`` review lands on."""

        return account_for_purpose(ReviewPurpose.MISSION_FINAL)

    # ------------------------------------------------------------------ reading
    def _root_binding(self, mission_id: str) -> TaskSemanticBindingV1 | None:
        network = self.dispatch.network(mission_id)
        roots = network.root_occurrence_ids
        if len(roots) != 1:
            return None
        spec = network.occurrence(roots[0])
        return self.semantics.task_semantics_of(mission_id, str(spec.task_id))

    def contributions(self, mission_id: str) -> tuple[str, ...]:
        """Every acceptance id that currently contributes, sorted.

        Read through ``HierarchicalDispatch.root_contributions``, which reads the
        ``acceptances`` rows rather than the outcome projection — an acceptance that
        was superseded or revoked since is history, and a review cut over it would
        be a review of work nobody accepts any more.
        """

        found = self.dispatch.root_contributions(mission_id)
        return tuple(sorted({str(item) for ids in found.values() for item in ids}))

    def producer_agent_ids(self, mission_id: str, acceptances: Sequence[str]) -> tuple[str, ...]:
        """Who produced the contributions, read off their own review packages.

        Not re-derived from Attempt rows and not taken from a caller: the leaf's
        ``ReviewPackage`` is the frozen anchor that recorded authorship at the time
        it was reviewed, and the independence rule is only worth anything if both
        sides read the same frozen fact (AER §5.3).
        """

        semantics = self.semantics
        found: list[str] = []
        for acceptance_id in acceptances:
            try:
                acceptance = semantics.get_acceptance(str(acceptance_id))
                stored = semantics.get_review_record(str(acceptance.review_record_id))
                package = semantics.get_review_package(str(stored.record.package_id))
            except (StoreError, AttributeError):
                # A contribution whose anchors cannot be re-read contributes no
                # authorship claim.  It does **not** contribute an empty one: the
                # package records what was readable, and the resolution's own
                # re-reads refuse a contribution the store cannot produce.
                continue
            found.extend(str(item) for item in package.producer_agent_ids)
        return tuple(sorted(set(found)))

    def _cut_events(self, mission_id: str) -> tuple[Any, ...]:
        return tuple(
            event for event in self.store.list_events(mission_id) if event.type == ROOT_REVIEW_CUT
        )

    def superseded_package_ids(self, mission_id: str) -> frozenset[str]:
        """Package ids this coordinator has retired.  A row is never rewritten."""

        return self.dispatch.superseded_review_packages(mission_id)

    def cuts_for_revision(self, mission_id: str, revision: int) -> int:
        """How many packages have been cut against one requirements revision."""

        return sum(
            1
            for event in self._cut_events(mission_id)
            if int(event.payload.get("requirements_revision", -1)) == int(revision)
        )

    def live_package(self, mission_id: str) -> ReviewPackage | None:
        """The ``MISSION_FINAL`` package to resolve from, or ``None``.

        Delegated to :meth:`HierarchicalDispatch.live_root_review_package` rather than
        re-derived: the coordinator and the root resolution have to agree on *which*
        review is the live one, and two implementations of "which" is how they would
        stop agreeing.
        """

        binding = self._root_binding(mission_id)
        if binding is None:
            return None
        return self.dispatch.live_root_review_package(
            mission_id,
            task_id=str(binding.task_id),
            obligation_id=str(binding.obligation_id),
        )

    def stale_reasons(self, mission_id: str, package: ReviewPackage) -> tuple[str, ...]:
        """Why this package no longer describes the world, in machine-readable codes.

        Each code is a real refusal the root commit would otherwise produce:

        ``REQUIREMENTS_MOVED``
            the Mission's requirements revision is no longer the one the package was
            bound to, which the read-set channel refuses as ``READ_SET_STALE``
            (part 2d, review P1-7).
        ``CONTRIBUTIONS_MOVED``
            a child was accepted or revoked since the cut, so the candidate set the
            reviewer was shown is not the one the resolution would quote — and
            ``commit_goal_resolution`` re-derives the contributions and refuses a
            mismatch with ``COMPOUND_FACTS_CONTRADICT_STORE``.
        ``SCOPE_EPOCH_MOVED``
            the scope was re-opened under the review, which spends every witness
            taken in the old epoch (§11.5, I19).
        """

        semantics = self.semantics
        reasons: list[str] = []
        latest = semantics.latest_requirements_revision(mission_id)
        current = 0 if latest is None else int(latest.revision)
        if current != int(package.binding.requirements_revision):
            reasons.append("REQUIREMENTS_MOVED")
        cut_over = tuple(sorted(str(item.id) for item in package.child_acceptance_refs))
        if cut_over != self.contributions(mission_id):
            reasons.append("CONTRIBUTIONS_MOVED")
        recorded = next(
            (
                event
                for event in self._cut_events(mission_id)
                if str(event.payload.get("package_id", "")) == str(package.package_id)
            ),
            None,
        )
        if recorded is not None:
            was = int(recorded.payload.get("scope_epoch", 0))
            if was != int(semantics.epoch(mission_id, self.scope_id)):
                reasons.append("SCOPE_EPOCH_MOVED")
        return tuple(reasons)

    def state(self, mission_id: str) -> RootReviewState:
        """Where the root review stands.  Reads only; decides nothing and writes nothing."""

        from ..graph.projection_validation import GraphIntegrityError

        try:
            binding = self._root_binding(mission_id)
        except (GraphIntegrityError, ContractError, StoreError) as error:
            return RootReviewState(
                status=RootReviewStatus.UNREADABLE_PLAN,
                detail=f"the plan could not be read: {error}",
            )
        if binding is None:
            return RootReviewState(
                status=RootReviewStatus.UNREADABLE_PLAN,
                detail=(
                    "this plan revision has no single root occurrence with a semantic binding; "
                    "choosing among several would be this module inventing a root"
                ),
            )
        if binding.form is not TaskForm.COMPOUND and not self.dispatch.root_review_ready(
            mission_id
        ):
            return RootReviewState(
                status=RootReviewStatus.NOT_READY,
                detail="the root occurrence has no accepted outcome yet",
                task_id=str(binding.task_id),
                obligation_id=str(binding.obligation_id),
            )
        semantics = self.semantics
        resolved = semantics.adopted_goal_resolution(mission_id, str(binding.obligation_id))
        if resolved is not None:
            return RootReviewState(
                status=RootReviewStatus.ALREADY_RESOLVED,
                detail=f"duty {binding.obligation_id!s} is resolved by {resolved.resolution_id!s}",
                task_id=str(binding.task_id),
                obligation_id=str(binding.obligation_id),
            )
        if not self.dispatch.root_review_ready(mission_id):
            return RootReviewState(
                status=RootReviewStatus.NOT_READY,
                detail="a gating child of the adopted root method has no accepted outcome yet",
                task_id=str(binding.task_id),
                obligation_id=str(binding.obligation_id),
            )
        latest = semantics.latest_requirements_revision(mission_id)
        current = 0 if latest is None else int(latest.revision)
        contributions = self.contributions(mission_id)
        package = self.live_package(mission_id)
        if package is None:
            return RootReviewState(
                status=RootReviewStatus.CUT_REQUIRED,
                detail="no MISSION_FINAL package has been cut for this root",
                task_id=str(binding.task_id),
                obligation_id=str(binding.obligation_id),
                requirements_revision=current,
                contributions=contributions,
                cuts_used=self.cuts_for_revision(mission_id, current),
            )
        bound = int(package.binding.requirements_revision)
        spent = self.cuts_for_revision(mission_id, bound)
        stale = self.stale_reasons(mission_id, package)
        base = RootReviewState(
            status=RootReviewStatus.AWAITING_REVIEW,
            task_id=str(binding.task_id),
            obligation_id=str(binding.obligation_id),
            package=package,
            requirements_revision=bound,
            contributions=contributions,
            cuts_used=spent,
            stale_reasons=stale,
        )
        if stale:
            # The bound this refuses against is the *next* cut's, and the next cut
            # binds whatever revision is current — so the budget question is asked
            # about that revision and not about the one going stale.
            if self.cuts_for_revision(mission_id, current) >= int(self.max_cuts_per_revision):
                return RootReviewState(
                    status=RootReviewStatus.CUT_BUDGET_SPENT,
                    detail=(
                        f"requirements revision {current} has already been cut "
                        f"{self.cuts_for_revision(mission_id, current)} time(s) and the bound is "
                        f"{int(self.max_cuts_per_revision)}; the world keeps moving under the "
                        "review and another cut would be another model call on the same question"
                    ),
                    task_id=base.task_id,
                    obligation_id=base.obligation_id,
                    package=package,
                    requirements_revision=current,
                    contributions=contributions,
                    cuts_used=self.cuts_for_revision(mission_id, current),
                    stale_reasons=stale,
                )
            return RootReviewState(
                status=RootReviewStatus.RECUT_REQUIRED,
                detail=("the package no longer describes the world: " + ", ".join(stale)),
                task_id=base.task_id,
                obligation_id=base.obligation_id,
                package=package,
                requirements_revision=current,
                contributions=contributions,
                cuts_used=self.cuts_for_revision(mission_id, current),
                stale_reasons=stale,
            )
        record = semantics.official_review_record(str(package.package_id))
        if record is None:
            return base
        if record.verdict is not ReviewVerdict.ACCEPT:
            return RootReviewState(
                status=RootReviewStatus.REVIEW_REJECTED,
                detail=(
                    f"the final review of {package.package_id!s} concluded {record.verdict!s}; "
                    "a review that ended is not a review that passed (AER I05)"
                ),
                task_id=base.task_id,
                obligation_id=base.obligation_id,
                package=package,
                record=record,
                requirements_revision=bound,
                contributions=contributions,
                cuts_used=spent,
            )
        return RootReviewState(
            status=RootReviewStatus.READY,
            detail="",
            task_id=base.task_id,
            obligation_id=base.obligation_id,
            package=package,
            record=record,
            requirements_revision=bound,
            contributions=contributions,
            cuts_used=spent,
        )

    # ------------------------------------------------------------------ cutting
    def cut(self, mission_id: str, *, now_ms: int) -> ReviewPackage:
        """Freeze the root ``MISSION_FINAL`` anchor, and the licence that spends it.

        Refuses unless :meth:`state` says a cut is required, so "cut it anyway" is
        not an available move: a package cut while one is live and fresh would give
        ``root_resolution_inputs`` two anchors for one root, and which one wins
        would be a matter of ordering rather than of judgement.

        What it writes: a ``RequirementsRevision`` for the root's own criteria (re-used
        rather than re-published when the content is unchanged, so the revision number
        moves exactly when the content does), the ``ReviewPackage``, the
        ``purpose=ACCEPT`` witness the root commit consumes, and one
        :data:`ROOT_REVIEW_CUT` event recording everything the cut was made over.

        What it does **not** write: a ``ReviewRecord``.  There is no conclusion yet.
        """

        state = self.state(mission_id)
        if not state.needs_cut:
            raise ContractError(
                f"the root review of mission {mission_id!r} is {state.status!s}, not a cut this "
                f"coordinator may make ({state.detail}); a second live package would leave "
                "which review the root resolves from a matter of row order"
            )
        binding = self._root_binding(mission_id)
        assert binding is not None  # state() answered UNREADABLE_PLAN otherwise
        semantics = self.semantics
        previous = state.package
        if previous is not None:
            self._supersede(mission_id, previous, reasons=state.stale_reasons)
        requirements = self._requirements(mission_id, binding)
        contributions = self.contributions(mission_id)
        producers = self.producer_agent_ids(mission_id, contributions)
        manifest = self._manifest_hash(mission_id, str(binding.task_id))
        package = ReviewPackage(
            package_id=ReviewPackageId(
                "pkg-root-"
                + content_hash_of(
                    {
                        "mission": str(mission_id),
                        "task": str(binding.task_id),
                        "requirements": int(requirements.revision),
                        "contributions": list(contributions),
                        "cut": self.cuts_for_revision(mission_id, int(requirements.revision)) + 1,
                    }
                )[:32]
            ),
            purpose=ReviewPurpose.MISSION_FINAL,
            binding=ReviewBinding(
                mission_id=mission_id,
                obligation_id=str(binding.obligation_id),
                subject_ref=TypedRef(
                    kind=TypedRefKind.TASK,
                    id=str(binding.task_id),
                    revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
                requirements_revision=int(requirements.revision),
                input_manifest_hash=manifest,
                policy_ref=TypedRef(
                    kind=TypedRefKind.SOURCE,
                    id=ROOT_REVIEW_POLICY,
                    revision=1,
                    content_hash=content_hash_of(ROOT_REVIEW_POLICY),
                ),
            ),
            criteria=requirements.criteria,
            success_expression=requirements.success_expression,
            # The candidate of a composition review is the children's accepted work,
            # and it is named in both places the annex has for it: ``candidate_refs``
            # is what the reviewer is shown, ``child_acceptance_refs`` is what the
            # cut was made over and what :meth:`stale_reasons` compares against.
            candidate_refs=tuple(acceptance_ref(item) for item in contributions),
            child_acceptance_refs=tuple(acceptance_ref(item) for item in contributions),
            producer_agent_ids=producers,
            reviewer_workspace_access=WorkspaceAccess.READ_ONLY,
            requirements_content_hash=requirements.content_hash(),
        )
        try:
            semantics.get_review_package(str(package.package_id))
        except StoreError:
            semantics.insert_review_package(package)
        self._witness(mission_id, str(binding.task_id), now_ms=now_ms)
        append_hierarchical_event(
            self.store,
            ROOT_REVIEW_CUT,
            mission_id,
            key=f"{mission_id}:{package.package_id}",
            task_id=str(binding.task_id),
            payload={
                "package_id": str(package.package_id),
                "purpose": str(ReviewPurpose.MISSION_FINAL),
                "review_account": str(self.account),
                "requirements_revision": int(requirements.revision),
                "requirements_content_hash": requirements.content_hash(),
                "contributions": list(contributions),
                "producer_agent_ids": list(producers),
                "input_manifest_hash": manifest,
                "scope_epoch": int(semantics.epoch(mission_id, self.scope_id)),
                "manager_epoch": int(semantics.epoch(mission_id, self.scope_id)),
                "superseded": None if previous is None else str(previous.package_id),
                "recut_reasons": list(state.stale_reasons),
                "policy_ref": ROOT_REVIEW_POLICY,
            },
        )
        return package

    def _supersede(
        self, mission_id: str, package: ReviewPackage, *, reasons: Sequence[str]
    ) -> None:
        """Retire a package with a record.  The row itself is never touched.

        An anchor is immutable, so "this review no longer counts" cannot be a field
        on it; it is this event, and :meth:`live_package` is the one reader of it.
        Writing it *before* the new package is cut matters: a crash between the two
        leaves a Mission with no live package, which :meth:`state` answers as
        ``CUT_REQUIRED`` — one review too few, which is recoverable.  The other order
        would leave two live packages, which is one review too many.
        """

        append_hierarchical_event(
            self.store,
            ROOT_REVIEW_SUPERSEDED,
            mission_id,
            key=f"{mission_id}:{package.package_id}",
            task_id=str(package.binding.subject_ref.id),
            payload={
                "package_id": str(package.package_id),
                "requirements_revision": int(package.binding.requirements_revision),
                "reasons": list(reasons),
            },
        )

    def record_cut_budget_spent(self, mission_id: str, state: RootReviewState) -> None:
        """Say, once per revision, that this revision has no cuts left (§9.1)."""

        append_hierarchical_event(
            self.store,
            ROOT_REVIEW_CUT_BUDGET_SPENT,
            mission_id,
            key=f"{mission_id}:{state.requirements_revision}",
            task_id=state.task_id or None,
            payload={
                "code": "root_review_cut_budget_spent",
                "requirements_revision": int(state.requirements_revision),
                "cuts_used": int(state.cuts_used),
                "bound": int(self.max_cuts_per_revision),
                "stale_reasons": list(state.stale_reasons),
                "package_id": None if state.package is None else str(state.package.package_id),
                "detail": state.detail,
            },
        )

    def _requirements(
        self, mission_id: str, binding: TaskSemanticBindingV1
    ) -> RequirementsRevision:
        """The root's own requirements revision, published once and re-read after that.

        The same rule the leaf side follows: a revision is immutable, so a *new*
        number is published only when the content differs.  That is what makes the
        read-set channel's "which revision was this decided at" mean anything — and
        it is also what bounds the re-cut loop, because a re-cut that changes nothing
        binds the same revision and therefore spends that revision's cut budget.
        """

        semantics = self.semantics
        latest = semantics.latest_requirements_revision(mission_id)
        candidate = root_requirements(
            mission_id, binding, revision=1 if latest is None else int(latest.revision)
        )
        if latest is not None and latest.content_hash() == candidate.content_hash():
            return latest
        published = (
            candidate
            if latest is None
            else root_requirements(mission_id, binding, revision=int(latest.revision) + 1)
        )
        semantics.insert_requirements_revision(published)
        return published

    def _manifest_hash(self, mission_id: str, task_id: str) -> str:
        """Freeze what the root consumed, and return the library's own digest.

        A compound root consumes nothing directly — its children do — so the honest
        manifest is the empty one, and it is *stored* rather than hashed in place
        because ``commit_goal_resolution`` re-reads it (``INPUT_MANIFEST_UNKNOWN``
        otherwise).  A deployment whose root does bind inputs gets them from the
        assembly, the same way the leaf side does.
        """

        document: dict[str, Any] = {"consumer_task_ref": str(task_id), "bindings": []}
        network = self.dispatch.network(mission_id)
        spec = next(
            (item for item in network.occurrences if str(item.task_id) == str(task_id)), None
        )
        if spec is not None:
            resolved = self.dispatch.resolved_inputs(mission_id, network, spec)
            if resolved.manifest is not None and resolved.manifest.is_frozen:
                document = dict(resolved.manifest.to_json())
        return self.semantics.insert_input_manifest(mission_id, str(task_id), document)

    def _witness(self, mission_id: str, task_id: str, *, now_ms: int) -> ValidityWitness:
        """The ``purpose=ACCEPT`` licence the root commit consumes (§11.5, AER §8.1).

        Its id is derived from exactly what the row's unique key is made of —
        consumer, purpose, scope, epoch, support revision and subject — which is the
        shape part 2d's review P0-2 settled on for the leaf side: "is this licence
        already stored" is then one keyed read instead of an insert that raises.

        The support revision is the **number of acceptances**, not the number of
        observations the leaf lane counts.  A root review rests on its children's
        acceptances, so that is what "how much support exists" means here — and it
        makes a re-cut after a child was accepted or revoked a *new* licence rather
        than a reuse of the one taken over the old support (I19: recompute, never
        reuse the old TRUE).

        The subject is :data:`~...knowledge.validity.NO_SUBJECT`: this licence is
        taken over a *set* of acceptances, and ``witness_subject`` deliberately gives
        no single subject to a witness that names several.  Naming them in
        ``support_refs`` would file this licence under a subject that is one of them,
        which is the collision migration 18 exists to prevent.
        """

        semantics = self.semantics
        epoch = int(semantics.epoch(mission_id, self.scope_id))
        support_revision = len(semantics.list_acceptances(mission_id))
        witness = ValidityWitness(
            witness_id="wit-"
            + content_hash_of(
                {
                    "consumer": str(task_id),
                    "purpose": str(WitnessPurpose.ACCEPT),
                    "scope": self.scope_id,
                    "epoch": epoch,
                    "support_revision": int(support_revision),
                    "subject": NO_SUBJECT,
                }
            )[:32],
            consumer_ref=TypedRef(
                kind=TypedRefKind.TASK,
                id=str(task_id),
                revision=1,
                content_hash=content_hash_of(str(task_id)),
            ),
            purpose=WitnessPurpose.ACCEPT,
            truth=TruthValue.TRUE,
            freshness=Validity.CURRENT,
            availability=Availability.READABLE,
            decision=WitnessDecision.USABLE,
            scope_id=self.scope_id,
            scope_epoch=epoch,
            support_revision=int(support_revision),
            as_of_ms=int(now_ms),
        )
        subject = witness_subject(witness)
        try:
            return semantics.get_validity_witness(witness.witness_id)
        except StoreError:
            semantics.insert_validity_witness(mission_id, witness, subject=subject)
            return witness

    # ------------------------------------------------------------------ reviewing
    def request(
        self, mission_id: str, package: ReviewPackage, *, schema_feedback: str = ""
    ) -> RootReviewRequest:
        """What the reviewer is shown.  Every field is read off a stored anchor."""

        semantics = self.semantics
        binding = semantics.task_semantics_of(mission_id, str(package.binding.subject_ref.id))
        statement = ""
        if binding is not None and binding.goal_signature.statement:
            statement = str(binding.goal_signature.statement)
        # What each Acceptance *delivered*, read from migration 17's
        # ``acceptance_outputs``.  Part 3a's round-3 smoke ended here: the request
        # showed only ``acceptance.artifact_refs``, which the accept path does not
        # populate — it records the artifact against the **output port** the plan
        # declared — so every contribution reached the reviewer as an empty list and a
        # correct reviewer answered FAIL on "no evidence at all".  The port is part of
        # the evidence, not decoration: it is what says the artifact is the thing the
        # plan asked that slot for.
        delivered: dict[str, list[Mapping[str, Any]]] = {}
        for row in semantics.list_acceptance_outputs(mission_id):
            delivered.setdefault(str(row.get("acceptance_id", "")), []).append(
                {
                    "port": str(row.get("output_port", "")),
                    "artifact_id": str(row.get("artifact_id", "")),
                }
            )
        contributions: list[Mapping[str, Any]] = []
        for reference in package.child_acceptance_refs:
            try:
                acceptance = semantics.get_acceptance(str(reference.id))
            except StoreError:  # pragma: no cover - the cut read them a moment ago
                continue
            child = semantics.task_semantics_of(mission_id, str(acceptance.task_id))
            contributions.append(
                {
                    "acceptance_id": str(acceptance.acceptance_id),
                    "task_id": str(acceptance.task_id),
                    "obligation_id": str(acceptance.obligation_id),
                    "requirements_revision": int(acceptance.requirements_revision),
                    "goal_statement": (
                        "" if child is None else str(child.goal_signature.statement)
                    ),
                    "accepted_outputs": delivered.get(str(acceptance.acceptance_id), []),
                    "review": self._child_review(str(acceptance.review_record_id)),
                    "artifacts": [str(item.id) for item in acceptance.artifact_refs],
                }
            )
        return RootReviewRequest(
            package_id=str(package.package_id),
            goal_task_id=str(package.binding.subject_ref.id),
            goal_statement=statement,
            criteria=tuple(
                {
                    "criterion_id": str(item.criterion_id),
                    "statement": str(item.statement),
                    "requirement_class": str(item.requirement_class),
                }
                for item in package.criteria
            ),
            contributions=tuple(contributions),
            requirements_revision=int(package.binding.requirements_revision),
            schema_feedback=str(schema_feedback),
        )

    def _child_review(self, record_id: str) -> Mapping[str, Any]:
        """The judgement this contribution was accepted under, quoted not summarised.

        A composition review is asked whether *these accepted parts* add up to the
        root goal, and "accepted under what" is half of that question.  Read-only and
        best effort: a record the library cannot produce leaves the field empty rather
        than making the whole request unbuildable.
        """

        if not record_id:
            return {}
        try:
            stored = self.semantics.get_review_record(record_id)
        except (StoreError, ContractError):  # pragma: no cover - a library that lost it
            return {}
        return {
            "review_record_id": str(stored.record.record_id),
            "verdict": str(stored.record.verdict),
            "criteria": {
                str(item.criterion_id): str(item.verdict) for item in stored.record.criteria
            },
        }

    def record_review(
        self,
        mission_id: str,
        package: ReviewPackage,
        *,
        verdict: ReviewVerdict,
        criterion_verdicts: Mapping[str, CriterionVerdict],
        reviewer_agent_id: str,
        reviewer_turn_id: str,
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> ReviewRecord:
        """Freeze the reviewer's conclusion.  This module never supplies one.

        ``verdict`` and ``criterion_verdicts`` both come from the reply the caller
        parsed.  There is no default and no fallback: a criterion the reviewer did
        not judge is written ``UNKNOWN`` — the enum's word for "not judged" — and the
        acceptance formula answers for it, rather than this side answering on the
        reviewer's behalf.  AER I05 is a property of that shape: nothing here can
        turn "the review finished" into "the review passed".

        A non-ACCEPT conclusion is recorded *and* announced
        (:data:`ROOT_REVIEW_REJECTED`), because §9.1 sends it to a decision table and
        a silent retry is the one response that table does not have.
        """

        resolved = ReviewVerdict(verdict)
        limitations = tuple(
            f"{one.get('severity', 'minor')}: {str(one.get('detail', ''))[:200]}"
            for one in findings
        )
        outcomes = tuple(
            self._outcome(
                item.criterion_id,
                criterion_verdicts.get(str(item.criterion_id), CriterionVerdict.UNKNOWN),
                limitations,
            )
            for item in package.criteria
        )
        record = ReviewRecord(
            record_id=ReviewRecordId(
                "rec-root-"
                + content_hash_of(
                    {"package": str(package.package_id), "turn": str(reviewer_turn_id)}
                )[:32]
            ),
            package_id=package.package_id,
            purpose=package.purpose,
            binding=package.binding,
            reviewer_agent_id=str(reviewer_agent_id),
            reviewer_turn_id=str(reviewer_turn_id),
            evidence_manifest_hash=content_hash_of(
                {
                    "contributions": [str(item.id) for item in package.child_acceptance_refs],
                    "findings": [dict(item) for item in findings],
                }
            ),
            criteria=outcomes,
            verdict=resolved,
        )
        official = self.semantics.official_review_record(str(package.package_id))
        if official is None:
            self.semantics.insert_review_record(record, official=True)
        elif official.to_json() != record.to_json():
            raise ContractError(
                f"review package {package.package_id!s} already carries official record "
                f"{official.record_id!s}; a second conclusion for one anchor is a new review "
                "and needs a new package (AER §5.2)"
            )
        if resolved is not ReviewVerdict.ACCEPT:
            append_hierarchical_event(
                self.store,
                ROOT_REVIEW_REJECTED,
                mission_id,
                key=f"{mission_id}:{package.package_id}",
                task_id=str(package.binding.subject_ref.id),
                payload={
                    "code": "root_review_not_accepted",
                    "package_id": str(package.package_id),
                    "record_id": str(record.record_id),
                    "verdict": str(resolved),
                    "requirements_revision": int(package.binding.requirements_revision),
                    "reviewer_agent_id": str(reviewer_agent_id),
                    "criteria": {str(item.criterion_id): str(item.verdict) for item in outcomes},
                    "findings": [dict(item) for item in findings][:16],
                },
            )
        return record

    @staticmethod
    def _outcome(
        criterion_id: str, verdict: CriterionVerdict, limitations: Sequence[str]
    ) -> CriterionOutcome:
        """One criterion's outcome, with the execution axis kept honest (I07).

        A composition review is a judgement rather than a check run, but "the
        judgement reached a conclusion" is still an execution fact and the contract
        refuses a PASS whose execution says otherwise.  So a criterion the reviewer
        *judged* — either way — carries ``SUCCEEDED``, and a criterion it left alone
        carries ``NOT_RUN`` beside its ``UNKNOWN``.  There is no shape here that
        turns a criterion nobody looked at into a passed one.
        """

        resolved = CriterionVerdict(verdict)
        return CriterionOutcome(
            criterion_id=str(criterion_id),
            verdict=resolved,
            check_execution=(
                CheckExecution.NOT_RUN
                if resolved is CriterionVerdict.UNKNOWN
                else CheckExecution.SUCCEEDED
            ),
            limitations=tuple(limitations),
        )

    def record_unreadable(
        self, mission_id: str, package: ReviewPackage, *, detail: str, reviewer_turn_id: str = ""
    ) -> None:
        """A reply that is not a verdict.  No record, no conclusion.

        Writing an ``INCONCLUSIVE`` record here would burn the package's one official
        record on an answer nobody gave, so the event is the whole response.  The
        loop may put the *same* question once more with this event's ``detail``
        attached (:data:`MAX_ROOT_REVIEW_ASKS`) — an unreadable reply is not an
        answer, and the part-3a smoke found a model that produced the block on its
        second try.  A reply that *was* read is never re-asked, whatever it said: a
        FAIL goes to §9.1's decision table and stays there.
        """

        append_hierarchical_event(
            self.store,
            ROOT_REVIEW_UNREADABLE,
            mission_id,
            key=f"{mission_id}:{package.package_id}:{reviewer_turn_id}",
            task_id=str(package.binding.subject_ref.id),
            payload={
                "code": "root_review_unreadable",
                "package_id": str(package.package_id),
                "requirements_revision": int(package.binding.requirements_revision),
                "reviewer_turn_id": str(reviewer_turn_id),
                "detail": str(detail)[:500],
            },
        )


__all__ = (
    "DEFAULT_MAX_CUTS_PER_REVISION",
    "MAX_ROOT_REVIEW_ASKS",
    "ROOT_REVIEW_CUT",
    "ROOT_REVIEW_CUT_BUDGET_SPENT",
    "ROOT_REVIEW_POLICY",
    "ROOT_REVIEW_REJECTED",
    "ROOT_REVIEW_SUPERSEDED",
    "ROOT_REVIEW_UNREADABLE",
    "RootReviewCoordinator",
    "RootReviewRequest",
    "RootReviewState",
    "RootReviewStatus",
    "acceptance_ref",
    "root_criteria",
    "root_requirements",
)
