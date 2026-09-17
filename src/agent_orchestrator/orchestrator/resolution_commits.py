# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c: ``accept_review`` and ``commit_goal_resolution`` (AER §6–§7, §25.1 decision 4).

AER §6.1 keeps four facts apart and §25.1 decision 4 keeps two *actions* apart:
accepting one contribution and declaring a goal satisfied are not the same write,
do not share a receipt, and do not imply one another.  This module is therefore
two entry points rather than one with a flag:

``accept_review``
    a Commit accepted **one contribution** under stated requirements, inputs,
    artefacts, review identity and support.  It writes an
    :class:`~..contracts.resolution.Acceptance` and touches no duty lifecycle:
    accepting a report is not declaring the goal done, and it is not authorising
    an action either (invariants I01 / I03).
``commit_goal_resolution``
    a Task / Obligation is satisfied **as a whole**.  For a compound that means a
    legal adopted method, a valid ``Acceptance`` for every *required* child
    occurrence, the composition obligation, and coverage of every required root
    criterion; for a Mission root it additionally means the delivery contract was
    actually reached.  Only then is a
    :class:`~..contracts.resolution.GoalResolution` written and the duty moved to
    ``SATISFIED`` with its ``resolution_ref``.

Three refusals this module exists to make impossible to bypass:

* **"wrongly declared complete" = 0.**  A Mission root ``GoalResolution`` is formed
  here and nowhere else, and it is formed from the *success formula plus the final
  acceptance* — never from "the last topological leaf finished" and never from the
  string ``Mission.status == COMPLETED`` (§6.3, §8.1).  A root whose requirements
  declare a delivery contract is refused until a :class:`DeliveryReceipt` says the
  output actually travelled that far (AER §6.1, §6.3).
* **No implicit permissiveness.**  ``IndependenceFacts()`` reads as "nobody
  produced this and the reviewer holds no rights" and ``ExecutionPosture()`` as
  "nothing is in flight" — the two most permissive possible worlds — so the
  commands carry them with **no defaults**.  Forgetting to look may not become
  acceptable (``verification.acceptance_rules.AcceptanceSubject`` says the same).
* **A Commit re-checks current applicability.**  §25.1 decision 4: an ``ACCEPT``
  verdict recorded earlier is not a standing permission.  The scope epoch, the
  support-set revision (not only the individual evidence rows — AER §7) and the
  ``purpose=ACCEPT`` ``ValidityWitness`` are all re-read inside the transaction,
  and a stale one refuses the command with nothing written.

Boundaries kept from ``plan_commits.py``: a legacy Mission stops at the door
before any migration-16 table is read; there is no raw SQL here; nothing slow runs
inside the transaction; and every refusal carries a stable machine ``reason``
because a stale read-set, a missing child acceptance and an unreached delivery
stage are three different repairs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..contracts import TERMINAL_MISSION, ContractError
from ..contracts.evidence_state import Validity, ValidityWitness, WitnessPurpose
from ..contracts.htn import ObligationId, SemanticReadSet
from ..contracts.obligations import ObligationAccountView, ObligationLifecycle
from ..contracts.resolution import (
    Acceptance,
    AcceptanceId,
    DeliveryReceipt,
    DeliveryStage,
    GoalResolution,
    RequirementsRevision,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewVerdict,
    account_for_purpose,
)
from ..contracts.semantic_base import EvidenceRef, TypedRefKind, content_hash_of
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.store import StoreError
from ..verification.acceptance_rules import (
    AcceptanceSubject,
    AcceptDecision,
    CompoundFacts,
    ExecutionPosture,
    IndependenceFacts,
    acceptable,
)
from ._read_set import ReadSetChannelUnknown, SemanticReadSetChecker
from .accepted_outputs import accepted_output_json, output_ports_in_revision
from .plan_commits import HIERARCHICAL_SEMANTICS, semantics_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..contracts import Event, Mission
    from ..contracts.htn import ChildBinding
    from ..storage.store import Store

#: The two event types this module appends.  Both are *new*: §18.5 rule 3 lets the
#: hierarchical mode add event types and forbids it to rewrite an existing one's
#: bytes.
ACCEPTANCE_COMMITTED = "AcceptanceCommitted"
GOAL_RESOLUTION_COMMITTED = "GoalResolutionCommitted"
#: P2.3c part 2: the library recorded how far one accepted output travelled (AER §6.1).
DELIVERY_RECEIPT_RECORDED = "DeliveryReceiptRecorded"

#: AER §6.1: "only the delivery stage the goal asked for completes the goal".  The
#: order is how far the output actually travelled; ``FAILED`` is not a lesser stage
#: of the same journey, so it reaches nothing.
DELIVERY_STAGE_ORDER: Mapping[DeliveryStage, int] = {
    DeliveryStage.FAILED: 0,
    DeliveryStage.PERSISTED: 1,
    DeliveryStage.ENQUEUED: 2,
    DeliveryStage.SENT: 3,
    DeliveryStage.CONFIRMED: 4,
}

#: An Acceptance that may still be quoted.  A superseded or revoked one is history
#: and never a current contribution (AER §6.1).
USABLE_ACCEPTANCE_VALIDITY: frozenset[Validity] = frozenset({Validity.CURRENT})

#: A duty in one of these states is no longer the duty a method asked for, so an
#: acceptance on it is not a live contribution however valid the acceptance itself is
#: (§6.1: cancelled, or replaced by an authorised substitute).
CLOSED_DUTY_LIFECYCLES: frozenset[ObligationLifecycle] = frozenset(
    {ObligationLifecycle.CANCELLED, ObligationLifecycle.SUPERSEDED}
)

#: How many events one paged mission read takes at a time.  The replay lookup itself
#: is a keyed read into migration 17's ``acceptance_commit_receipts`` since P2.3c part
#: 2 (see :meth:`ResolutionCommitsMixin._replayed`); this stays as the page size for
#: any caller that still has to walk a Mission's log.
REPLAY_PAGE = 512


def command_event_key(mission_id: str, command_id: str, event_type: str) -> str:
    """The ``_emit`` key one accept-side command appends under (§17.4).

    Derived from the **command id** rather than from the subject, so
    ``Store.append_event``'s unique-key path *is* the "one command, one commit, one
    receipt" guarantee: a second delivery of the same command cannot append a second
    event even if every check were to run twice.
    """

    return f"{mission_id}:{event_type}:{command_id}"


def command_idempotency_key(mission_id: str, command_id: str, event_type: str) -> str:
    """The key as the ``events`` row actually stores it.

    ``CommitService._emit`` prefixes the event type onto the key it is handed, so the
    stored value is ``"<type>:<key>"``.  Spelling that out here rather than
    reconstructing it at the lookup keeps the two ends of the idempotency guarantee
    in one place.
    """

    return f"{event_type}:{command_event_key(mission_id, command_id, event_type)}"


def delivery_reached(receipt: DeliveryReceipt, required: DeliveryStage) -> bool:
    """Whether one receipt says the output travelled at least as far as ``required``."""

    if receipt.stage is DeliveryStage.FAILED:
        return False
    return DELIVERY_STAGE_ORDER[receipt.stage] >= DELIVERY_STAGE_ORDER[required]


class ResolutionCommitRejected(StoreError):
    """The command was refused; nothing was written.

    ``reason`` is a stable machine name and part of the contract: the caller decides
    what to do next from it, so it is not a message to be reworded freely.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ResolutionPrincipal:
    """Who is issuing the command, and inside which manager scope (AER §7 step 1)."""

    principal_id: str
    scope_id: str = "mission"
    manager_epoch: int = 0

    def __post_init__(self) -> None:
        if not str(self.principal_id).strip():
            raise ContractError("a resolution commit needs an identified principal")


def _facts_json(facts: IndependenceFacts) -> dict[str, Any]:
    return {
        "producer_agent_ids": sorted(facts.producer_agent_ids),
        "reviewer_can_write_candidate": facts.reviewer_can_write_candidate,
        "reviewed_revision_authored_by_reviewer": facts.reviewed_revision_authored_by_reviewer,
        "reviewer_model_id": facts.reviewer_model_id,
        "producer_model_id": facts.producer_model_id,
    }


def _posture_json(posture: ExecutionPosture) -> dict[str, Any]:
    return {
        "unowned_critical_operation_ids": sorted(posture.unowned_critical_operation_ids),
        "cancellation_requested": posture.cancellation_requested,
        "method_adoption_current": posture.method_adoption_current,
    }


def _declared_ports(
    semantics: HtnStore, mission_id: str, revision: int, producer: Any, task_id: str
) -> Mapping[str, Any]:
    """The producer's declared output ports, read from the plan revision's own rows.

    P2.3d / defect D3: "declared" is "consumed by a ``DataRequirement`` **or** pointed
    at by a ``criterion_link``".  The finalizer step is the case that matters — its
    artifact is what the root's success criterion reads, and while this side counted
    only edges, ``OUTPUT_PORT_UNCLAIMED`` could never fire for it and the gap surfaced
    two steps later as a root review rejection nobody could act on.
    """

    return output_ports_in_revision(semantics, mission_id, revision, producer, task_id)


@dataclass(frozen=True, slots=True)
class AcceptReviewCommand:
    """One request to accept **one contribution** (AER §6.1, §7).

    ``independence`` and ``posture`` carry no defaults on purpose — see the module
    docstring.  ``read_set`` is what the proposer read while deciding, re-checked
    item by item inside the transaction (AER §7: the *support set* version, not only
    the individual evidence rows).
    """

    command_id: str
    mission_id: str
    task_id: str
    obligation_id: str
    acceptance_id: str
    package: ReviewPackage
    record: ReviewRecord
    requirements: RequirementsRevision
    witness_id: str
    independence: IndependenceFacts
    posture: ExecutionPosture
    read_set: SemanticReadSet
    accepted_at_ms: int
    artifact_refs: tuple[EvidenceRef, ...] = ()
    #: P2.3c part 2b: which artifact this acceptance accepted at which **declared**
    #: output port.  It is stated rather than derived because deriving it is the
    #: all-ancestors sweep §24.1 decision 4 removed; it is checked against the plan's
    #: own ``DataRequirement`` edges inside the transaction
    #: (:func:`~.accepted_outputs.check_declared`), so a port nobody declared is a
    #: refusal and not a row.  Empty is the ordinary case: a leaf whose output no
    #: consumer reads feeds nothing, and an index entry for it would be a claim about
    #: an edge the plan never drew.
    outputs: tuple[Any, ...] = ()
    purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT
    semantic_review_required: bool = True
    policy_ref: str | None = None
    issued_by: str = ""
    scope_id: str = "mission"
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.command_id).strip():
            raise ContractError("an accept_review command needs a command_id (§17.4)")
        for name, kind in (
            ("package", ReviewPackage),
            ("record", ReviewRecord),
            ("requirements", RequirementsRevision),
        ):
            if not isinstance(getattr(self, name), kind):
                raise ContractError(f"command.{name} must be a {kind.__name__}")
        if not isinstance(self.independence, IndependenceFacts):
            raise ContractError(
                "command.independence must be stated as IndependenceFacts; an implied "
                "'nobody produced this' is the most permissive world there is (AER §5.3)"
            )
        if not isinstance(self.posture, ExecutionPosture):
            raise ContractError(
                "command.posture must be stated as an ExecutionPosture; an implied "
                "'nothing is in flight' is the most permissive world there is (AER §6.2)"
            )
        if not isinstance(self.read_set, SemanticReadSet):
            raise ContractError("command.read_set must be a SemanticReadSet")
        object.__setattr__(self, "purpose", ReviewPurpose(str(self.purpose)))

    @property
    def account(self) -> str:
        """Which budget account this review's cost lands on (§13 v1.4)."""

        return str(account_for_purpose(self.purpose))

    def intent_hash(self) -> str:
        return content_hash_of(
            {
                "command_id": self.command_id,
                "mission_id": self.mission_id,
                "task_id": self.task_id,
                "obligation_id": self.obligation_id,
                "acceptance_id": self.acceptance_id,
                "package": self.package.to_json(),
                "record": self.record.to_json(),
                "requirements": self.requirements.to_json(),
                "witness_id": self.witness_id,
                "independence": _facts_json(self.independence),
                "posture": _posture_json(self.posture),
                "read_set": self.read_set.to_json(),
                "artifact_refs": [ref.to_json() for ref in self.artifact_refs],
                # The outputs are part of the intent: two commands that accept the
                # same review but index different artifacts are two different asks,
                # and a replay must not hand back the first one's receipt for the
                # second one's index (§17.4).
                "outputs": [accepted_output_json(item) for item in self.outputs],
                "purpose": str(self.purpose),
                "semantic_review_required": self.semantic_review_required,
                "policy_ref": self.policy_ref,
                "issued_by": self.issued_by,
                "scope_id": self.scope_id,
                "accepted_at_ms": int(self.accepted_at_ms),
            }
        )


@dataclass(frozen=True, slots=True)
class CommitGoalResolutionCommand:
    """One request to declare a Task / Obligation satisfied **as a whole** (AER §6.1).

    ``compound`` states the two facts only the caller can know — the selected method
    is legal and the composition obligation passed.  The *contributions* are never
    taken from it: which required occurrences actually have a valid ``Acceptance`` is
    read from the store inside the transaction and cross-checked against what the
    command claims, so a command cannot talk a missing child into existence.
    """

    command_id: str
    mission_id: str
    resolution: GoalResolution
    package: ReviewPackage
    record: ReviewRecord
    requirements: RequirementsRevision
    witness_id: str
    independence: IndependenceFacts
    posture: ExecutionPosture
    read_set: SemanticReadSet
    #: When this decision is being made.  The witness's epoch barrier, deadline and
    #: freshness are all checked against it, so a witness that was fresh when it was
    #: taken is not fresh for a commit that happens after its deadline (§11.5).
    decided_at_ms: int
    purpose: ReviewPurpose = ReviewPurpose.COMPOSITION
    compound: CompoundFacts | None = None
    semantic_review_required: bool = True
    #: A Mission root resolution.  It is the only kind that has to satisfy the
    #: delivery contract as well as the success formula (§6.3, AER §6.1).
    is_mission_root: bool = False
    required_delivery_stage: DeliveryStage | None = None
    #: The ids of the :class:`DeliveryReceipt` rows this root relies on.  P2.3c part 2
    #: moved the receipts themselves out of the command and into the library
    #: (migration 17's ``delivery_receipts``), because a receipt handed in on a command
    #: is a *claim* about the world and "wrongly declared complete = 0" is precisely
    #: the invariant that a claim may not stand in for a record.  The command names
    #: which records it relies on; the content is re-read from the store.
    delivery_receipts: tuple[str, ...] = ()
    issued_by: str = ""
    scope_id: str = "mission"
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.command_id).strip():
            raise ContractError("a commit_goal_resolution command needs a command_id (§17.4)")
        if not isinstance(self.resolution, GoalResolution):
            raise ContractError("command.resolution must be a GoalResolution")
        for name, kind in (
            ("package", ReviewPackage),
            ("record", ReviewRecord),
            ("requirements", RequirementsRevision),
        ):
            if not isinstance(getattr(self, name), kind):
                raise ContractError(f"command.{name} must be a {kind.__name__}")
        if not isinstance(self.independence, IndependenceFacts):
            raise ContractError("command.independence must be stated as IndependenceFacts")
        if not isinstance(self.posture, ExecutionPosture):
            raise ContractError("command.posture must be stated as an ExecutionPosture")
        if not isinstance(self.read_set, SemanticReadSet):
            raise ContractError("command.read_set must be a SemanticReadSet")
        if self.compound is not None and not isinstance(self.compound, CompoundFacts):
            raise ContractError("command.compound must be CompoundFacts or None")
        object.__setattr__(self, "purpose", ReviewPurpose(str(self.purpose)))
        if self.required_delivery_stage is not None:
            object.__setattr__(
                self, "required_delivery_stage", DeliveryStage(str(self.required_delivery_stage))
            )
        receipts: list[str] = []
        for item in self.delivery_receipts:
            if isinstance(item, DeliveryReceipt):
                raise ContractError(
                    "command.delivery_receipts carries receipt *ids*, not DeliveryReceipt "
                    "objects: record the receipt with CommitService.record_delivery_receipt "
                    "first, then name it here (AER §6.1 — a receipt is a claim until the "
                    "library holds it)"
                )
            receipts.append(str(item))
        object.__setattr__(self, "delivery_receipts", tuple(receipts))

    @property
    def account(self) -> str:
        return str(account_for_purpose(self.purpose))

    def intent_hash(self) -> str:
        return content_hash_of(
            {
                "command_id": self.command_id,
                "mission_id": self.mission_id,
                "resolution": self.resolution.to_json(),
                "package": self.package.to_json(),
                "record": self.record.to_json(),
                "requirements": self.requirements.to_json(),
                "witness_id": self.witness_id,
                "independence": _facts_json(self.independence),
                "posture": _posture_json(self.posture),
                "read_set": self.read_set.to_json(),
                # ``decided_at_ms`` is deliberately **not** hashed (review F12).  It is
                # the moment the formula was evaluated at — the freshness clock handed
                # to ``acceptable()`` and to the witness check — and unlike
                # ``AcceptReviewCommand.accepted_at_ms`` it is not written into
                # anything this command produces.  Hashing it made every re-offer of an
                # unchanged root resolution a *different* intent under the same command
                # id, so a single anomaly (a receipt recorded whose resolution a later
                # read could not find) turned into a permanent
                # ``COMMAND_PAYLOAD_CONFLICT`` and a Mission that could never complete.
                # The intent is what the command would *do*; the clock is when it was
                # asked.
                "purpose": str(self.purpose),
                "compound": (
                    None
                    if self.compound is None
                    else {
                        "selected_method_legal": self.compound.selected_method_legal,
                        "composition_obligation_passed": (
                            self.compound.composition_obligation_passed
                        ),
                        "contributing_occurrence_ids": sorted(
                            self.compound.contributing_occurrence_ids
                        ),
                    }
                ),
                "semantic_review_required": self.semantic_review_required,
                "is_mission_root": self.is_mission_root,
                "required_delivery_stage": (
                    None
                    if self.required_delivery_stage is None
                    else str(self.required_delivery_stage)
                ),
                "delivery_receipts": list(self.delivery_receipts),
                "issued_by": self.issued_by,
                "scope_id": self.scope_id,
            }
        )


#: What ``output_identity["kind"]`` says one command produced.  Two kinds, because
#: §25.1 decision 4 keeps the two actions apart all the way into the receipt: a
#: caller replaying a command id must be told it accepted a contribution, not that
#: it resolved a goal.
ACCEPTANCE_KIND = "acceptance"
GOAL_RESOLUTION_KIND = "goal_resolution"


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """§17.4: what one accept-side command did, durable and replay-addressable.

    It is deliberately **not** a ``PlanCommitReceipt``: that table is plan-shaped —
    its own CHECK constraint requires ``new_plan_revision > base_plan_revision`` —
    and an acceptance creates no plan revision.  Rather than write a misleading
    revision pair, the receipt is projected from the durable event this command
    appended, whose ``idempotency_key`` is what makes a second delivery of the same
    command one commit and one receipt.  A dedicated table is a storage change and
    belongs to the slice that owns ``storage/``.
    """

    command_id: str
    mission_id: str
    kind: str
    subject_id: str
    intent_hash: str
    read_set_hash: str
    event_id: str
    output_identity: Mapping[str, Any] = field(default_factory=dict)
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "mission_id": self.mission_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "intent_hash": self.intent_hash,
            "read_set_hash": self.read_set_hash,
            "event_id": self.event_id,
            "output_identity": dict(self.output_identity),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReceipt:
    """What one ``accept_review`` did.  A replay returns the same receipt (§17.4)."""

    acceptance: Acceptance
    commit: CommandReceipt
    decision: AcceptDecision | None = None
    replayed: bool = False

    @property
    def acceptance_id(self) -> str:
        return str(self.acceptance.acceptance_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "acceptance": self.acceptance.to_json(),
            "commit_receipt": self.commit.to_json(),
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class GoalResolutionReceipt:
    """What one ``commit_goal_resolution`` did, including the duty it satisfied."""

    resolution: GoalResolution
    commit: CommandReceipt
    account: ObligationAccountView | None = None
    decision: AcceptDecision | None = None
    replayed: bool = False

    @property
    def resolution_id(self) -> str:
        return str(self.resolution.resolution_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.to_json(),
            "commit_receipt": self.commit.to_json(),
            "obligation_account": None if self.account is None else self.account.to_json(),
            "replayed": self.replayed,
        }


class ResolutionCommitsMixin:
    """The two accept-side Commit Service entry points of the hierarchical mode.

    It is a mixin for the same reason ``PlanCommitsMixin`` is: the *writing*
    authority of this deployment is one class, and adding a second one would put two
    transaction owners on one database.  Every check below is a pure function of
    values read inside the transaction; no model, no tool and no solver runs here.
    """

    if TYPE_CHECKING:  # pragma: no cover - provided by CommitService
        _store: Store

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

        # Provided by ``ObligationCommitsMixin`` (P2.3c part 2d, decision 3): the one
        # audited entry point for ending a consumer's interest in a duty.
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
        ) -> Any: ...

    # =============================================================== accept_review
    def accept_review(
        self, command: AcceptReviewCommand, principal: ResolutionPrincipal
    ) -> AcceptanceReceipt:
        """Accept one contribution atomically, or write nothing (AER §7)."""

        if not isinstance(command, AcceptReviewCommand):
            raise ResolutionCommitRejected(
                "BAD_COMMAND", "accept_review expects an AcceptReviewCommand"
            )
        if not isinstance(principal, ResolutionPrincipal):
            raise ResolutionCommitRejected(
                "BAD_PRINCIPAL", "accept_review expects a ResolutionPrincipal"
            )
        # AER §7 step 1: identity first — before the receipt lookup, so a forged
        # replay of someone else's command_id cannot read back their receipt.
        _authorize(command.issued_by, command.scope_id, principal)
        intent = command.intent_hash()
        with self._store.transaction():
            mission = self._open_mission(command.mission_id)
            semantics = HtnStore(self._store)
            replayed = self._replayed(semantics, command.mission_id, command.command_id, intent)
            if replayed is not None:
                self._require_kind(replayed, ACCEPTANCE_KIND)
                return AcceptanceReceipt(
                    acceptance=semantics.get_acceptance(replayed.subject_id),
                    commit=replayed,
                    replayed=True,
                )
            binding = self._require_binding(semantics, command.mission_id, command.task_id)
            if str(binding.obligation_id) != command.obligation_id:
                raise ResolutionCommitRejected(
                    "BINDING_MISMATCH",
                    f"task {command.task_id} serves duty {binding.obligation_id!s}, and the "
                    f"command names {command.obligation_id!r}",
                )
            self._check_review_bindings(
                semantics,
                mission_id=command.mission_id,
                task_id=command.task_id,
                obligation_id=command.obligation_id,
                package=command.package,
                record=command.record,
                requirements=command.requirements,
                purpose=command.purpose,
            )
            if int(command.package.binding.requirements_revision) != int(
                command.requirements.revision
            ):
                raise ResolutionCommitRejected(
                    "REQUIREMENTS_MISMATCH",
                    "the review package was cut from requirements revision "
                    f"{int(command.package.binding.requirements_revision)} and the command "
                    f"presents revision {int(command.requirements.revision)}",
                )
            self._check_reads(semantics, command.mission_id, command.read_set, principal)
            witness = self._require_accept_witness(
                semantics,
                command.mission_id,
                command.witness_id,
                subject=command.task_id,
                now_ms=int(command.accepted_at_ms),
            )
            self._check_posture(command.posture)
            subject = AcceptanceSubject(
                revision=command.requirements,
                package=command.package,
                record=command.record,
                independence=command.independence,
                posture=command.posture,
                semantic_review_required=command.semantic_review_required,
            )
            decision = acceptable(
                subject,
                now_ms=int(command.accepted_at_ms),
                purpose=command.purpose,
                witness=witness,
                current_scope_epoch=semantics.epoch(command.mission_id, witness.scope_id),
            )
            if not decision.acceptable:
                raise ResolutionCommitRejected(
                    "NOT_ACCEPTABLE",
                    "the AER §6.2 formula refused: "
                    + ", ".join(str(reason) for reason in decision.reasons),
                )
            acceptance = Acceptance(
                acceptance_id=AcceptanceId(command.acceptance_id),
                mission_id=command.mission_id,
                task_id=binding.task_id,
                obligation_id=binding.obligation_id,
                requirements_revision=int(command.requirements.revision),
                contract_revision=int(binding.contract_revision),
                input_manifest_hash=command.package.binding.input_manifest_hash,
                review_record_id=command.record.record_id,
                accepted_at_ms=int(command.accepted_at_ms),
                artifact_refs=tuple(command.artifact_refs),
                policy_ref=command.policy_ref,
                validity=Validity.CURRENT,
            )
            try:
                semantics.insert_acceptance(acceptance)
            except StoreError as error:
                raise ResolutionCommitRejected("ACCEPTANCE_CONFLICT", str(error)) from error
            indexed = self._record_accepted_outputs(semantics, command, acceptance)
            payload = {
                "command_id": command.command_id,
                "acceptance_id": str(acceptance.acceptance_id),
                "task_id": str(acceptance.task_id),
                "obligation_id": str(acceptance.obligation_id),
                "review_record_id": str(acceptance.review_record_id),
                "review_package_id": str(command.package.package_id),
                "purpose": str(command.purpose),
                "review_account": command.account,
                "requirements_revision": int(command.requirements.revision),
                "contract_revision": int(binding.contract_revision),
                "input_manifest_hash": acceptance.input_manifest_hash,
                "witness_id": witness.witness_id,
                "intent_hash": intent,
                "read_set_hash": content_hash_of(command.read_set.to_json()),
                "artifact_refs": [ref.to_json() for ref in acceptance.artifact_refs],
                "accepted_outputs": [
                    {"port": item.output_port, "artifact_id": item.artifact_id} for item in indexed
                ],
                "source": dict(command.source),
            }
            payload["output_identity"] = {
                "kind": ACCEPTANCE_KIND,
                "subject_id": str(acceptance.acceptance_id),
                "content_hash": content_hash_of(acceptance.to_json()),
                "task_id": str(acceptance.task_id),
                "obligation_id": str(acceptance.obligation_id),
            }
            payload["detail"] = {
                "mission_version": mission.version,
                "scope_id": command.scope_id,
                "principal_scope_epoch": semantics.epoch(command.mission_id, command.scope_id),
            }
            event = self._emit(
                ACCEPTANCE_COMMITTED,
                command.mission_id,
                key=command_event_key(command.mission_id, command.command_id, ACCEPTANCE_COMMITTED),
                task_id=str(acceptance.task_id),
                payload=payload,
            )
            commit = self._record_receipt(semantics, command.mission_id, event.id, payload)
            return AcceptanceReceipt(acceptance=acceptance, commit=commit, decision=decision)

    def _record_accepted_outputs(
        self,
        semantics: HtnStore,
        command: AcceptReviewCommand,
        acceptance: Acceptance,
    ) -> tuple[Any, ...]:
        """Write the "which artifact, at which declared port" index (§24.1 decision 4).

        Three things happen here and each is a refusal rather than a repair:

        * the **producer occurrence is read**, never taken from the command.  It is
          the occurrence of the accepted task in the *active* plan revision, so an
          output cannot be filed under an occurrence the acceptance does not belong
          to even if the caller spells one.
        * the **port table is read from the plan's own data requirements**, and
          :func:`~.accepted_outputs.check_against_ports` refuses a port the plan never
          declared or a schema the edge does not carry.  An index entry exists only
          where the plan drew an edge; anything else is a consumer binding to a
          contract that does not exist.
        * a stated output with **no active plan revision** to declare it is refused.
          The alternative — writing it anyway — is an index row whose meaning depends
          on a plan that will be committed later, which is the stale read ADR-13 is
          about.

        A leaf whose result no occurrence consumes has no declared port, so a command
        with no outputs and no unclaimed port writes nothing and asks nothing;
        inventing an entry for it is the ancestor sweep under a typed name.

        P2.3c part 2d, decision 4 adds the fourth refusal, and it is the one that runs
        when the command states *nothing*: a port this occurrence declares, that a
        ``DataRequirement`` actually consumes, and that no output covers, is
        ``OUTPUT_PORT_UNCLAIMED``.  TG implementation design §4.3 says an unprovable
        binding produces an explicit conversion task **or a refusal** — never ``Any``,
        and never a quiet gap — and leaving the port empty here is what made part 2c's
        smoke sit in ``WAITING_DATA`` for ever.  A refusal is recorded and does **not**
        undo the verification (part 2b's rule): it is §9.1's "content defect → a new
        content attempt against the same duty", and the retry path is the ordinary one.
        """

        from .accepted_outputs import check_against_ports

        outputs = tuple(command.outputs)
        active = semantics.active_plan_revision(command.mission_id)
        if active is None:
            if not outputs:
                return ()
            raise ResolutionCommitRejected(
                "NO_PLAN_REVISION",
                "the command states accepted outputs, and this Mission has no active plan "
                "revision to declare a port in (§24.1 decision 4)",
            )
        revision = int(active.revision)
        members = semantics.list_plan_memberships(command.mission_id, revision)
        producer = next(
            (spec.occurrence_id for spec in members if str(spec.task_id) == command.task_id), None
        )
        if producer is None:
            if not outputs:
                return ()
            raise ResolutionCommitRejected(
                "OCCURRENCE_UNKNOWN",
                f"task {command.task_id!r} is not a member of plan revision {revision}; an "
                "accepted output is filed under the occurrence the plan holds, not under one "
                "the command names",
            )
        consumed = _declared_ports(
            semantics, command.mission_id, revision, producer, command.task_id
        )
        # Order matters: "you named a port that does not exist" is answered before
        # "you left a declared port empty".  A relabelled output is both, and the
        # first is the actionable one — the second would send the producer looking
        # for a file it already wrote.
        try:
            checked = check_against_ports(consumed, producer, outputs)
        except ContractError as error:
            raise ResolutionCommitRejected("OUTPUT_NOT_DECLARED", str(error)) from error
        unclaimed = sorted(set(consumed) - {output.output_port for output in outputs})
        if unclaimed:
            raise ResolutionCommitRejected(
                "OUTPUT_PORT_UNCLAIMED",
                f"occurrence {producer!s} declares output port(s) {unclaimed} that a data "
                f"requirement of plan revision {revision} consumes, and this acceptance "
                f"claims none of them; it offers {sorted(output.output_port for output in outputs)}"
                ". Name the file you produced at each port rather than leaving the consumer "
                "waiting on a port nobody filled (TG §4.3: refuse, do not leave it empty)",
            )
        if not checked:
            return ()
        for output in checked:
            try:
                semantics.insert_acceptance_output(
                    command.mission_id,
                    acceptance_id=str(acceptance.acceptance_id),
                    output_port=output.output_port,
                    artifact_id=output.artifact_id,
                    producer_occurrence=str(output.producer_occurrence),
                    producer_task_ref=str(output.producer_task_ref),
                    producer_result_id=str(output.producer_result_id),
                    support_revision=int(output.support_revision),
                    content_hash=str(output.content_hash),
                    source_revision=str(output.source_revision),
                    document=accepted_output_json(output),
                )
            except StoreError as error:
                raise ResolutionCommitRejected("OUTPUT_CONFLICT", str(error)) from error
        return checked

    # ==================================================== commit_goal_resolution
    def commit_goal_resolution(
        self, command: CommitGoalResolutionCommand, principal: ResolutionPrincipal
    ) -> GoalResolutionReceipt:
        """Declare one goal satisfied as a whole, atomically, or write nothing.

        A Mission root resolution is formed **here and only here**, out of the
        success formula plus the final acceptance plus the delivery contract — never
        out of "the last leaf in topological order finished" and never out of the
        Mission's own status string (§6.3, §8.1, AER §6.1).
        """

        if not isinstance(command, CommitGoalResolutionCommand):
            raise ResolutionCommitRejected(
                "BAD_COMMAND", "commit_goal_resolution expects a CommitGoalResolutionCommand"
            )
        if not isinstance(principal, ResolutionPrincipal):
            raise ResolutionCommitRejected(
                "BAD_PRINCIPAL", "commit_goal_resolution expects a ResolutionPrincipal"
            )
        _authorize(command.issued_by, command.scope_id, principal)
        intent = command.intent_hash()
        resolution = command.resolution
        with self._store.transaction():
            mission = self._open_mission(command.mission_id)
            semantics = HtnStore(self._store)
            replayed = self._replayed(semantics, command.mission_id, command.command_id, intent)
            if replayed is not None:
                self._require_kind(replayed, GOAL_RESOLUTION_KIND)
                return GoalResolutionReceipt(
                    resolution=semantics.get_goal_resolution(replayed.subject_id),
                    commit=replayed,
                    replayed=True,
                )
            if resolution.mission_id != command.mission_id:
                raise ResolutionCommitRejected(
                    "BINDING_MISMATCH",
                    f"the resolution belongs to mission {resolution.mission_id!r}",
                )
            binding = self._require_binding(semantics, command.mission_id, resolution.goal_task_id)
            if str(binding.obligation_id) != resolution.obligation_id:
                raise ResolutionCommitRejected(
                    "BINDING_MISMATCH",
                    f"task {resolution.goal_task_id} serves duty {binding.obligation_id!s}, "
                    f"and the resolution names {resolution.obligation_id!r}",
                )
            if int(resolution.contract_revision) != int(binding.contract_revision):
                raise ResolutionCommitRejected(
                    "CONTRACT_REVISION_MISMATCH",
                    f"the resolution was built against contract revision "
                    f"{int(resolution.contract_revision)}; the task is at "
                    f"{int(binding.contract_revision)}",
                )
            if resolution.verdict is not ReviewVerdict.ACCEPT:
                raise ResolutionCommitRejected(
                    "RESOLUTION_VERDICT_NOT_ACCEPT",
                    f"a resolution with verdict {resolution.verdict!s} is not a satisfaction; "
                    "a review that ended is not a review that passed (invariant I01)",
                )
            self._check_review_bindings(
                semantics,
                mission_id=command.mission_id,
                task_id=resolution.goal_task_id,
                obligation_id=resolution.obligation_id,
                package=command.package,
                record=command.record,
                requirements=command.requirements,
                purpose=command.purpose,
            )
            self._check_resolution_identity(semantics, command, binding)
            self._check_reads(semantics, command.mission_id, command.read_set, principal)
            witness = self._require_accept_witness(
                semantics,
                command.mission_id,
                command.witness_id,
                subject=resolution.goal_task_id,
                now_ms=int(command.decided_at_ms),
            )
            duties = ObligationStore(self._store)
            account = self._require_open_duty(duties, command.mission_id, resolution.obligation_id)
            compound, contributions = self._compound_facts(semantics, duties, command)
            self._check_posture(command.posture)
            subject = AcceptanceSubject(
                revision=command.requirements,
                package=command.package,
                record=command.record,
                independence=command.independence,
                posture=command.posture,
                semantic_review_required=command.semantic_review_required,
                compound=compound,
            )
            decision = acceptable(
                subject,
                now_ms=int(command.decided_at_ms),
                purpose=command.purpose,
                witness=witness,
                current_scope_epoch=semantics.epoch(command.mission_id, witness.scope_id),
            )
            if not decision.acceptable:
                raise ResolutionCommitRejected(
                    "NOT_ACCEPTABLE",
                    "the AER §6.2 formula refused: "
                    + ", ".join(str(reason) for reason in decision.reasons),
                )
            delivery = self._check_delivery(semantics, command)
            withdrawn = bool(account.has_admitted_demand)
            shared = (not command.is_mission_root) and _duty_has_other_occurrences(
                semantics, command
            )
            try:
                # P2.3l / N7: an inner compound that ``refines_parent`` shares the
                # parent's duty.  Adopting and SATISFYing that duty here would close
                # the root (and every sibling) the moment the nested compound
                # resolved.  The resolution is still stored — ORDER keys on
                # ``goal_task_id`` — but it is not the duty's adopted conclusion.
                semantics.insert_goal_resolution(resolution, adopted=not shared)
            except StoreError as error:
                raise ResolutionCommitRejected("RESOLUTION_CONFLICT", str(error)) from error
            if withdrawn and not shared:
                # TG decision 9: withdrawing the demand ends a *share*, not the duty.
                # The open-duty CHECK refuses SATISFIED while a demand still hangs on
                # it, so releasing the share is part of resolving the duty.  P2.3c
                # part 2d routes it through the audited entry point, so the release is
                # a record with a name on it and not a silent bit flip.
                self.withdraw_obligation_demand(
                    command.mission_id,
                    ObligationId(resolution.obligation_id),
                    principal=str(principal.principal_id),
                    requester=(
                        {"kind": "mission_root"}
                        if command.is_mission_root
                        else {
                            "kind": "method_slot",
                            "method_instance_id": str(resolution.method_instance_id or ""),
                        }
                    ),
                    evidence={
                        "resolution_id": str(resolution.resolution_id),
                        "obligation_id": str(resolution.obligation_id),
                        "purpose": str(command.purpose),
                    },
                )
            if shared:
                satisfied = account
            else:
                satisfied = duties.set_lifecycle(
                    command.mission_id,
                    ObligationId(resolution.obligation_id),
                    ObligationLifecycle.SATISFIED,
                    resolution_ref=str(resolution.resolution_id),
                )
            payload = {
                "command_id": command.command_id,
                "resolution_id": str(resolution.resolution_id),
                "goal_task_id": resolution.goal_task_id,
                "obligation_id": resolution.obligation_id,
                "method_instance_id": resolution.method_instance_id,
                "purpose": str(command.purpose),
                "review_account": command.account,
                "requirements_version": int(resolution.requirements_version),
                "contract_revision": int(resolution.contract_revision),
                "child_resolution_ids": list(resolution.child_resolution_ids),
                "contributing_occurrences": (
                    [] if compound is None else sorted(compound.contributing_occurrence_ids)
                ),
                "contributing_acceptances": {
                    occurrence: list(ids) for occurrence, ids in sorted(contributions.items())
                },
                "is_mission_root": command.is_mission_root,
                "required_delivery_stage": (
                    None
                    if command.required_delivery_stage is None
                    else str(command.required_delivery_stage)
                ),
                "delivery_receipt_id": delivery,
                "witness_id": witness.witness_id,
                "demand_withdrawn": bool(withdrawn and not shared),
                "intent_hash": intent,
                "read_set_hash": content_hash_of(command.read_set.to_json()),
                "source": dict(command.source),
            }
            payload["output_identity"] = {
                "kind": GOAL_RESOLUTION_KIND,
                "subject_id": str(resolution.resolution_id),
                "content_hash": content_hash_of(resolution.to_json()),
                "obligation_id": resolution.obligation_id,
                "goal_task_id": resolution.goal_task_id,
                "is_mission_root": command.is_mission_root,
            }
            payload["detail"] = {
                "mission_version": mission.version,
                "scope_id": command.scope_id,
                "obligation_lifecycle": str(satisfied.lifecycle),
                "resolution_ref": satisfied.resolution_ref,
            }
            event = self._emit(
                GOAL_RESOLUTION_COMMITTED,
                command.mission_id,
                key=command_event_key(
                    command.mission_id, command.command_id, GOAL_RESOLUTION_COMMITTED
                ),
                task_id=resolution.goal_task_id,
                payload=payload,
            )
            commit = self._record_receipt(semantics, command.mission_id, event.id, payload)
            return GoalResolutionReceipt(
                resolution=resolution, commit=commit, account=satisfied, decision=decision
            )

    # =============================================== record_delivery_receipt (AER §6.1)
    def record_delivery_receipt(
        self,
        mission_id: str,
        receipt: DeliveryReceipt,
        *,
        command_id: str,
        source: Mapping[str, Any] | None = None,
    ) -> DeliveryReceipt:
        """Record how far one accepted output actually travelled.

        This is the *only* way a :class:`DeliveryReceipt` becomes something a root
        resolution may quote.  Before P2.3c part 2 the receipt rode in on the
        ``commit_goal_resolution`` command, which meant the evidence that a Mission
        really delivered was supplied by the same caller that wanted the Mission
        declared complete.  AER §6.1 wants a record; migration 17 gives it a table;
        this writes it.

        The receipt is checked against the store before it is held: it names this
        Mission, and the ``Acceptance`` it quotes has to exist (the foreign key says
        so too) and belong to this Mission.  Whether that acceptance is still
        **CURRENT** is deliberately *not* checked here and is re-checked where the
        receipt is quoted (:meth:`_check_delivery` → :func:`require_valid_receipt`):
        an acceptance that is current when the delivery happens can be superseded
        afterwards, so the question that decides a Mission is "is it current *now,* at
        the moment this root resolution is being formed", not "was it current when
        somebody filed the paperwork".

        Idempotent per ``command_id`` under the same §17.4 rule as the commit
        receipts; the same id with a different receipt is a conflict, not a second
        write.
        """

        if not isinstance(receipt, DeliveryReceipt):
            raise ResolutionCommitRejected(
                "BAD_COMMAND", "record_delivery_receipt expects a DeliveryReceipt"
            )
        intent = content_hash_of(receipt.to_json())
        with self._store.transaction():
            self._open_mission(mission_id)
            semantics = HtnStore(self._store)
            if receipt.mission_id != mission_id:
                raise ResolutionCommitRejected(
                    "DELIVERY_RECEIPT_INVALID",
                    f"delivery receipt {receipt.receipt_id!r} belongs to mission "
                    f"{receipt.mission_id!r}, not {mission_id!r}",
                )
            try:
                acceptance = semantics.get_acceptance(str(receipt.acceptance_id))
            except StoreError as error:
                raise ResolutionCommitRejected(
                    "DELIVERY_RECEIPT_INVALID",
                    f"delivery receipt {receipt.receipt_id!r} quotes acceptance "
                    f"{receipt.acceptance_id!s}, which is not stored ({error})",
                ) from error
            if acceptance.mission_id != mission_id:
                raise ResolutionCommitRejected(
                    "DELIVERY_RECEIPT_INVALID",
                    f"delivery receipt {receipt.receipt_id!r} quotes an acceptance of mission "
                    f"{acceptance.mission_id!r}",
                )
            try:
                stored = semantics.record_delivery_receipt(
                    mission_id, receipt, command_id=command_id, intent_hash=intent
                )
            except StoreError as error:
                raise ResolutionCommitRejected(
                    "COMMAND_PAYLOAD_CONFLICT",
                    f"delivery receipt {receipt.receipt_id!r} conflicts with a stored "
                    f"record ({error})",
                ) from error
            self._emit(
                DELIVERY_RECEIPT_RECORDED,
                mission_id,
                key=f"{mission_id}:{DELIVERY_RECEIPT_RECORDED}:{command_id}",
                payload={
                    "command_id": command_id,
                    "receipt_id": stored.receipt_id,
                    "acceptance_id": str(stored.acceptance_id),
                    "stage": str(stored.stage),
                    "observed_at_ms": int(stored.observed_at_ms),
                    "operation_id": stored.operation_id,
                    "intent_hash": intent,
                    "source": dict(source or {}),
                },
            )
            return stored

    # ---------------------------------------------------------------- shared gates
    def _open_mission(self, mission_id: str) -> Mission:
        """The door: a legacy Mission is refused before any new table is read."""

        mission = self._require_mission(mission_id)
        mode = semantics_of(mission)
        if mode != HIERARCHICAL_SEMANTICS:
            raise ResolutionCommitRejected(
                "SEMANTICS_NOT_HIERARCHICAL",
                f"mission {mission.id} runs under {mode!r}; an Acceptance / GoalResolution is "
                "committed through this path only for a Mission that asked for the "
                "hierarchical semantics (§18.5 rule 1)",
            )
        if mission.status in TERMINAL_MISSION:
            raise ResolutionCommitRejected(
                "MISSION_NOT_WRITABLE",
                f"mission {mission.id} is {mission.status!s} and accepts no new acceptance",
            )
        return mission

    def _replayed(
        self, semantics: HtnStore, mission_id: str, command_id: str, intent: str
    ) -> CommandReceipt | None:
        """§17.4: the same command twice is one commit and one receipt.

        **One keyed read** into ``acceptance_commit_receipts`` (migration 17).  P2.3c
        part 1 had to project the receipt out of the Mission's event log and page
        through it, because ``plan_commit_receipts`` is plan-shaped — its CHECK
        requires ``new_plan_revision > base_plan_revision`` and an acceptance produces
        no plan revision — and ``storage/`` was not that slice's to change.  Part 2
        owns storage, so the receipt is a row with ``(mission_id, command_id)`` as its
        primary key and this lookup is an index hit.

        A *different* intent under the same id is not a replay: it is two commands
        wearing one name, and answering it with the stored receipt would report work
        that was never done.  The store raises on that conflict at write time; this
        read raises the same refusal before any check runs.

        The event's ``idempotency_key`` (see :func:`command_event_key`) is still
        derived from the command id, so ``Store.append_event`` remains the durable
        backstop: even if every check ran twice, the second run could not append a
        second event.  The table is the *addressable* record, not a replacement for it.
        """

        stored = semantics.find_acceptance_receipt(mission_id, command_id)
        if stored is None:
            return None
        if stored.intent_hash != intent:
            raise ResolutionCommitRejected(
                "COMMAND_PAYLOAD_CONFLICT",
                f"command {command_id!r} was applied with intent {stored.intent_hash}, "
                f"and this delivery asks for {intent}",
            )
        return CommandReceipt(
            command_id=stored.command_id,
            mission_id=stored.mission_id,
            kind=stored.kind,
            subject_id=stored.subject_id,
            intent_hash=stored.intent_hash,
            read_set_hash=stored.read_set_hash,
            event_id=stored.event_id,
            output_identity=dict(stored.output_identity),
            detail=dict(stored.detail),
        )

    @staticmethod
    def _record_receipt(
        semantics: HtnStore, mission_id: str, event_id: str, payload: Mapping[str, Any]
    ) -> CommandReceipt:
        """Project the receipt out of what was just emitted and store it (§17.4).

        The projection is the same one part 1 used, so the receipt a caller gets back
        is byte-identical to the one a replay reads; what changed is that it is now
        also a row, which is what makes the replay lookup a keyed read.
        """

        receipt = _receipt_from_event(event_id, mission_id, payload)
        semantics.record_acceptance_receipt(
            mission_id,
            command_id=receipt.command_id,
            kind=receipt.kind,
            subject_id=receipt.subject_id,
            intent_hash=receipt.intent_hash,
            read_set_hash=receipt.read_set_hash,
            event_id=receipt.event_id,
            output_identity=receipt.output_identity,
            detail=receipt.detail,
        )
        return receipt

    @staticmethod
    def _require_kind(receipt: CommandReceipt, kind: str) -> None:
        if receipt.kind != kind:
            raise ResolutionCommitRejected(
                "COMMAND_PAYLOAD_CONFLICT",
                f"command {receipt.command_id!r} was applied as {receipt.kind!r}, "
                f"not as {kind!r}; accepting a contribution and resolving a goal are two "
                "actions and do not share a receipt (§25.1 decision 4)",
            )

    @staticmethod
    def _require_binding(semantics: HtnStore, mission_id: str, task_id: str) -> Any:
        binding = semantics.task_semantics_of(mission_id, task_id)
        if binding is None:
            raise ResolutionCommitRejected(
                "MISSING_SEMANTIC_BINDING",
                f"task {task_id} has no TaskSemanticBindingV1; in the hierarchical mode a "
                "missing binding is corruption, not a legacy fallback (§18.5)",
            )
        return binding

    def _check_review_bindings(
        self,
        semantics: HtnStore,
        *,
        mission_id: str,
        task_id: str,
        obligation_id: str,
        package: ReviewPackage,
        record: ReviewRecord,
        requirements: RequirementsRevision,
        purpose: ReviewPurpose,
    ) -> None:
        """AER §7: contract / input / artefact / review identity, before the formula.

        The package and the record are re-read from the store and compared by content
        hash rather than trusted as presented: the whole point of an *immutable review
        anchor* is that the thing the formula is evaluated against is the thing that
        was frozen at packaging time, not a copy the caller edited on the way in.
        """

        binding = package.binding
        if binding.mission_id != mission_id or binding.obligation_id != obligation_id:
            raise ResolutionCommitRejected(
                "BINDING_MISMATCH",
                f"the review package is bound to {binding.mission_id!r}/"
                f"{binding.obligation_id!r}, and the command names {mission_id!r}/"
                f"{obligation_id!r}",
            )
        subject = binding.subject_ref
        if subject.kind is TypedRefKind.TASK and subject.id != task_id:
            raise ResolutionCommitRejected(
                "BINDING_MISMATCH",
                f"the review package reviewed task {subject.id!r}, not {task_id!r}",
            )
        if record.package_id != package.package_id or record.binding != package.binding:
            raise ResolutionCommitRejected(
                "REVIEW_IDENTITY_MISMATCH",
                "the review record does not describe the package it is presented with",
            )
        if record.purpose is not package.purpose or record.purpose is not purpose:
            raise ResolutionCommitRejected(
                "REVIEW_PURPOSE_MISMATCH",
                f"the package is a {package.purpose!s} review, the record a "
                f"{record.purpose!s} one, and the command asks for {purpose!s}; a purpose "
                "is also a budget account and is never re-labelled at delivery (§13 v1.4)",
            )
        try:
            stored_package = semantics.get_review_package(str(package.package_id))
        except StoreError as error:
            raise ResolutionCommitRejected(
                "REVIEW_NOT_STORED",
                f"review package {package.package_id!s} is not stored; an acceptance quotes a "
                f"frozen anchor, not one supplied with the command ({error})",
            ) from error
        if stored_package.content_hash() != package.content_hash():
            raise ResolutionCommitRejected(
                "REVIEW_CONTENT_MISMATCH",
                f"review package {package.package_id!s} is stored with different content",
            )
        official = semantics.official_review_record(str(package.package_id))
        if official is None:
            raise ResolutionCommitRejected(
                "REVIEW_NOT_OFFICIAL",
                f"no official ReviewRecord is stored for package {package.package_id!s}",
            )
        if official.to_json() != record.to_json():
            raise ResolutionCommitRejected(
                "REVIEW_NOT_OFFICIAL",
                f"the presented record {record.record_id!s} is not the official record "
                f"{official.record_id!s} of package {package.package_id!s}",
            )
        try:
            stored_revision = semantics.get_requirements_revision(
                mission_id, int(requirements.revision)
            )
        except StoreError as error:
            raise ResolutionCommitRejected(
                "REQUIREMENTS_NOT_STORED",
                f"requirements revision {int(requirements.revision)} is not stored for "
                f"mission {mission_id} ({error})",
            ) from error
        if stored_revision.content_hash() != requirements.content_hash():
            raise ResolutionCommitRejected(
                "REQUIREMENTS_MISMATCH",
                f"requirements revision {int(requirements.revision)} is stored with different "
                "content; requirements are never quietly relaxed (invariant I01)",
            )
        try:
            semantics.get_input_manifest(binding.input_manifest_hash)
        except StoreError as error:
            raise ResolutionCommitRejected(
                "INPUT_MANIFEST_UNKNOWN",
                f"input manifest {binding.input_manifest_hash[:12]} is not stored; an "
                f"acceptance names the inputs it was granted ({error})",
            ) from error

    def _check_resolution_identity(
        self, semantics: HtnStore, command: CommitGoalResolutionCommand, binding: Any
    ) -> None:
        """The resolution must describe the review it points at, and cover the root."""

        resolution = command.resolution
        if int(resolution.requirements_version) != int(command.requirements.revision):
            raise ResolutionCommitRejected(
                "REQUIREMENTS_MISMATCH",
                f"the resolution claims requirements version "
                f"{int(resolution.requirements_version)} and the command presents "
                f"{int(command.requirements.revision)}",
            )
        if resolution.input_manifest_hash != command.package.binding.input_manifest_hash:
            raise ResolutionCommitRejected(
                "BINDING_MISMATCH",
                "the resolution names another input manifest than the review package",
            )
        if resolution.review_receipt_id != str(command.record.record_id):
            raise ResolutionCommitRejected(
                "REVIEW_IDENTITY_MISMATCH",
                f"the resolution points at review receipt {resolution.review_receipt_id!r} "
                f"and the command presents record {command.record.record_id!s}",
            )
        if resolution.method_instance_id is not None:
            state = semantics.method_instance_state(
                command.mission_id, resolution.method_instance_id
            )
            if state != "ADOPTED":
                raise ResolutionCommitRejected(
                    "METHOD_INSTANCE_NOT_ADOPTED",
                    f"method instance {resolution.method_instance_id} is {state}; a goal is "
                    "satisfied through a method that is currently adopted (§8.1)",
                )
        elif binding.form is not None and str(binding.form) == "compound":
            raise ResolutionCommitRejected(
                "METHOD_INSTANCE_NOT_ADOPTED",
                "a compound goal is satisfied through a method instance; the resolution "
                "names none (§8.1)",
            )
        for child in resolution.child_resolution_ids:
            try:
                semantics.get_goal_resolution(str(child))
            except StoreError as error:
                raise ResolutionCommitRejected(
                    "CHILD_RESOLUTION_UNKNOWN",
                    f"the resolution binds child resolution {child!r}, which is not stored "
                    f"({error})",
                ) from error
        reported = {item.criterion_id: item.verdict for item in resolution.criteria}
        reviewed = {item.criterion_id: item.verdict for item in command.record.criteria}
        contradicted = sorted(
            criterion_id
            for criterion_id, verdict in reported.items()
            if criterion_id in reviewed and reviewed[criterion_id] is not verdict
        )
        if contradicted:
            raise ResolutionCommitRejected(
                "RESOLUTION_CONTRADICTS_REVIEW",
                f"the resolution reports {contradicted} differently from the review record it "
                "binds; a resolution restates a review, it does not overrule one",
            )
        missing = tuple(
            criterion_id
            for criterion_id in command.requirements.required_criterion_ids()
            if criterion_id not in reported
        )
        if missing:
            raise ResolutionCommitRejected(
                "ROOT_CRITERION_MISSING",
                f"the resolution does not carry required root criteria {sorted(missing)}; a "
                "goal is not satisfied as a whole while a stated requirement is unaddressed "
                "(AER §4.2, scenario AER-V01)",
            )

    @staticmethod
    def _require_open_duty(
        duties: ObligationStore, mission_id: str, obligation_id: str
    ) -> ObligationAccountView:
        duty = ObligationId(obligation_id)
        if not duties.exists(mission_id, duty):
            raise ResolutionCommitRejected(
                "OBLIGATION_UNKNOWN",
                f"mission {mission_id} carries no duty {obligation_id!r}",
            )
        account = duties.account(mission_id, duty)
        if account.lifecycle is not ObligationLifecycle.UNSATISFIED:
            raise ResolutionCommitRejected(
                "OBLIGATION_NOT_OPEN",
                f"duty {obligation_id!r} is already {account.lifecycle!s}; a duty is "
                "satisfied once and its resolution is not overwritten",
            )
        return account

    def _compound_facts(
        self,
        semantics: HtnStore,
        duties: ObligationStore,
        command: CommitGoalResolutionCommand,
    ) -> tuple[CompoundFacts | None, dict[str, tuple[str, ...]]]:
        """Which required child occurrences actually have a valid ``Acceptance``.

        Read from the store, never from the command.  The caller's own
        ``CompoundFacts`` may state the two things only it knows — the selected
        method is legal and the composition obligation passed — and its
        ``contributing_occurrence_ids`` are *cross-checked*: a command that claims a
        contribution the store does not have is a finding, not something to resolve
        in the command's favour.
        """

        stated = command.compound
        if stated is None:
            return None, {}
        instance_id = command.resolution.method_instance_id
        if instance_id is None:
            raise ResolutionCommitRejected(
                "METHOD_INSTANCE_NOT_ADOPTED",
                "a compound resolution names no method instance",
            )
        children: Sequence[ChildBinding] = semantics.list_child_occurrences(
            command.mission_id, str(instance_id)
        )
        accepted = self._accepted_occurrences(semantics, duties, command.mission_id, children)
        if set(stated.contributing_occurrence_ids) != set(accepted):
            raise ResolutionCommitRejected(
                "COMPOUND_FACTS_CONTRADICT_STORE",
                "the command claims contributions "
                f"{sorted(stated.contributing_occurrence_ids)} and the store holds valid "
                f"acceptances on live duties for {sorted(accepted)}",
            )
        return (
            replace(
                stated,
                child_bindings=tuple(children),
                contributing_occurrence_ids=tuple(sorted(accepted)),
            ),
            dict(accepted),
        )

    @staticmethod
    def _accepted_occurrences(
        semantics: HtnStore,
        duties: ObligationStore,
        mission_id: str,
        children: Sequence[ChildBinding],
    ) -> dict[str, tuple[str, ...]]:
        """Occurrence → the currently valid acceptances that carry it.

        Keyed by **occurrence**, not by duty: TG §12 lets two slots adopt one shared
        goal, so a duty can stand behind more than one occurrence and collapsing them
        would let one contribution answer for two.  The duty's own lifecycle is read
        too — the P2.3c review found it missing — because an acceptance on a duty that
        has since been CANCELLED or SUPERSEDED is history: the work it accepted is no
        longer the work this method asked for (§6.1).
        """

        found: dict[str, tuple[str, ...]] = {}
        active = semantics.active_plan_revision(mission_id)
        members = (
            {
                str(spec.occurrence_id): str(spec.task_id)
                for spec in semantics.list_plan_memberships(mission_id, int(active.revision))
            }
            if active is not None
            else {}
        )
        for child in children:
            duty = ObligationId(str(child.obligation_id))
            if not duties.exists(mission_id, duty):
                continue
            account = duties.account(mission_id, duty)
            if account.lifecycle in CLOSED_DUTY_LIFECYCLES:
                continue
            task_id = members.get(str(child.occurrence_id))
            usable = tuple(
                str(item.acceptance_id)
                for item in semantics.list_acceptances(mission_id, obligation_id=str(duty))
                if item.validity in USABLE_ACCEPTANCE_VALIDITY
                and (task_id is None or str(item.task_id) == task_id)
            )
            if usable:
                found[str(child.occurrence_id)] = usable
                continue
            if any(
                str(item.goal_task_id) == task_id
                and str(item.verdict) == "ACCEPT"
                and str(item.validity) == "CURRENT"
                for item in semantics.list_goal_resolutions(mission_id)
            ):
                # A nested compound contributes its GoalResolution, not an Acceptance.
                found[str(child.occurrence_id)] = ()
        return found

    def _check_reads(
        self,
        semantics: HtnStore,
        mission_id: str,
        read_set: SemanticReadSet,
        principal: ResolutionPrincipal,
    ) -> None:
        """AER §7: the manager epoch, then every channel of the read-set.

        The channel-by-channel work is :class:`~._read_set.SemanticReadSetChecker` —
        the same implementation ``plan_commits`` uses.  The P2.3c review found this
        method with a hand-written five-channel copy, which let three real changes
        through: a *refuted* observation (the FACT channel), a **revoked** authority
        (AUTHORITY) and a re-planned duty (OBLIGATION) all left the rows the copy
        happened to look at byte-identical.  There is now one implementation and
        eleven channels, and ``allow_task_control_channels`` is on here because an
        accept command's read-set is the one
        :func:`~..graph.eligibility.build_read_set` built, whose TASK lane namespaces
        the two dispatch-control values.

        The support-set entry is the reason this is not simply "re-read each evidence
        row": adding a counter-observation leaves every positive support byte-identical
        and only moves the set's member digest, so a commit that compared rows alone
        would accept a review that never saw the refutation (AER scenario I02).
        """

        current_epoch = semantics.epoch(mission_id, principal.scope_id)
        if int(read_set.manager_epoch) != current_epoch:
            raise ResolutionCommitRejected(
                "MANAGER_EPOCH_STALE",
                f"scope {principal.scope_id!r} is at epoch {current_epoch}; the command was "
                f"built at epoch {int(read_set.manager_epoch)}",
            )
        if int(principal.manager_epoch) != current_epoch:
            raise ResolutionCommitRejected(
                "MANAGER_EPOCH_STALE",
                f"scope {principal.scope_id!r} is at epoch {current_epoch}; the principal "
                f"holds epoch {int(principal.manager_epoch)}",
            )
        checker = SemanticReadSetChecker(
            self._store,
            semantics,
            mission_id=mission_id,
            allow_task_control_channels=True,
        )
        try:
            verdict = checker.verify(read_set)
        except ReadSetChannelUnknown as unknown:
            raise ResolutionCommitRejected("READ_SET_UNRESOLVED", unknown.detail) from unknown
        if verdict.unresolved:
            raise ResolutionCommitRejected("READ_SET_UNRESOLVED", verdict.unresolved_detail())
        if verdict.stale:
            raise ResolutionCommitRejected("READ_SET_STALE", verdict.stale_detail())

    @staticmethod
    def _require_accept_witness(
        semantics: HtnStore, mission_id: str, witness_id: str, *, subject: str, now_ms: int
    ) -> ValidityWitness:
        """§11.5 / AER §8.1: a ``purpose=ACCEPT`` witness, for this subject, right now.

        A witness is not a transferable token, so a ``START`` witness that licensed
        the dispatch does not license the acceptance, and one issued to another
        consumer is that consumer's permission.  Freshness is the contract's own
        ``is_fresh_for`` — epoch barrier, deadline and freshness together.
        """

        try:
            witness = semantics.get_validity_witness(witness_id)
        except StoreError as error:
            raise ResolutionCommitRejected(
                "WITNESS_UNKNOWN",
                f"no ValidityWitness {witness_id!r} is stored ({error})",
            ) from error
        if witness.purpose is not WitnessPurpose.ACCEPT:
            raise ResolutionCommitRejected(
                "WITNESS_PURPOSE_NOT_ACCEPT",
                f"witness {witness_id!r} was issued for {witness.purpose!s}; an acceptance "
                "consumes a purpose=ACCEPT witness and a witness is not transferable (§11.5)",
            )
        if witness.consumer_ref.kind is not TypedRefKind.TASK or witness.consumer_ref.id != subject:
            raise ResolutionCommitRejected(
                "WITNESS_CONSUMER_MISMATCH",
                f"witness {witness_id!r} was issued to {witness.consumer_ref.kind!s} "
                f"{witness.consumer_ref.id!r}, not to task {subject!r}",
            )
        epoch = semantics.epoch(mission_id, witness.scope_id)
        if not witness.is_fresh_for(now_ms=int(now_ms), current_scope_epoch=epoch):
            raise ResolutionCommitRejected(
                "WITNESS_STALE",
                f"witness {witness_id!r} was taken at scope epoch {witness.scope_epoch} "
                f"(now {epoch}), expires at {witness.not_after_ms} and is "
                f"{witness.freshness!s}; recompute rather than reuse the old TRUE (§11.5)",
            )
        return witness

    @staticmethod
    def _check_posture(posture: ExecutionPosture) -> None:
        """AER §7: no unowned critical operation, and no pending cancellation.

        Stated as its own refusal rather than folded into the formula's reason list,
        because "somebody has to reconcile operation X first" and "the success
        expression did not pass" send the caller to two different places.
        """

        if posture.unowned_critical_operation_ids:
            raise ResolutionCommitRejected(
                "CRITICAL_OPERATION_UNOWNED",
                "critical operations "
                f"{sorted(posture.unowned_critical_operation_ids)} are not owned by anyone; "
                "a known pending real action may not be counted as finished (AER §6.2)",
            )
        if posture.cancellation_requested:
            raise ResolutionCommitRejected(
                "CANCELLATION_PENDING",
                "a cancellation is requested for this subject; cancelling is not a result "
                "and an acceptance is not how it is settled (AER §16.1)",
            )

    @staticmethod
    def _check_delivery(semantics: HtnStore, command: CommitGoalResolutionCommand) -> str | None:
        """The Mission-root delivery gate (§6.3, AER §6.1) — "declared done" = 0.

        A root resolution that the requirements give a delivery contract is refused
        until a :class:`DeliveryReceipt` says the output actually travelled that far.
        The Mission's own ``status`` string is deliberately never read: "the Mission
        row says COMPLETED" is a projection of this decision, not evidence for it.
        """

        if not command.is_mission_root:
            return None
        if command.purpose is not ReviewPurpose.MISSION_FINAL:
            raise ResolutionCommitRejected(
                "ROOT_PURPOSE_NOT_MISSION_FINAL",
                f"a Mission root resolution is decided by a MISSION_FINAL review, not a "
                f"{command.purpose!s} one (§13 v1.4: the purpose is also the account)",
            )
        declared = command.requirements.delivery_contract_ref
        if declared is None:
            if command.required_delivery_stage is not None:
                raise ResolutionCommitRejected(
                    "DELIVERY_CONTRACT_UNDECLARED",
                    "the command demands delivery stage "
                    f"{command.required_delivery_stage!s} while the requirements revision "
                    "declares no delivery contract; a stage nobody asked for is not a "
                    "requirement this commit may invent",
                )
            return None
        required = command.required_delivery_stage
        if required is None:
            raise ResolutionCommitRejected(
                "DELIVERY_CONTRACT_UNDECLARED",
                f"requirements revision {int(command.requirements.revision)} declares "
                f"delivery contract {declared!r}, and the command names no required stage; "
                "only the stage the goal asked for completes the goal (AER §6.1)",
            )
        # Every named receipt is **read from the library** and checked — not taken
        # from the command, and not scanned until one happens to fit.
        #
        # Two review findings live in these few lines.  P2.3c part 1's mutant R9
        # survived because an invalid receipt used to be *skipped*: a receipt quoting
        # a STALE or REVOKED Acceptance, or one from another Mission, simply did not
        # count and the command went on to the next one — so an invalid receipt is now
        # a refusal of the whole command.  Part 2 closes the other half: the command
        # carries ids, and a receipt this library never recorded is a claim about the
        # world rather than a record of it.  Both halves serve one invariant, "wrongly
        # declared complete = 0" (§21.5).
        allowed = root_duty_closure(
            semantics,
            command.mission_id,
            obligation_id=command.resolution.obligation_id,
            method_instance_id=command.resolution.method_instance_id,
        )
        reached: str | None = None
        stages: set[str] = set()
        for receipt_id in command.delivery_receipts:
            receipt = semantics.find_delivery_receipt(command.mission_id, receipt_id)
            if receipt is None:
                raise ResolutionCommitRejected(
                    "DELIVERY_RECEIPT_INVALID",
                    f"delivery receipt {receipt_id!r}: mission {command.mission_id} holds no "
                    "such record; a receipt is a claim until the library recorded it "
                    "(AER §6.1)",
                )
            require_valid_receipt(semantics, command.mission_id, receipt, allowed)
            stages.add(str(receipt.stage))
            if reached is None and delivery_reached(receipt, required):
                reached = receipt.receipt_id
        if reached is not None:
            return reached
        raise ResolutionCommitRejected(
            "DELIVERY_STAGE_NOT_REACHED",
            f"delivery contract {declared!r} needs stage {required!s}; the named receipts "
            f"reached {sorted(stages)}. A Mission is not complete because a leaf finished "
            "(§6.3, §8.1)",
        )


def _duty_has_other_occurrences(
    semantics: HtnStore, command: CommitGoalResolutionCommand
) -> bool:
    """Whether this duty is still owed by another live occurrence (P2.3l / N7).

    A ``refines_parent`` inner compound shares the parent's obligation.  Concluding
    *that occurrence* must not SATISFY the shared duty while siblings (or the
    parent) still have work on it.
    """

    active = semantics.active_plan_revision(command.mission_id)
    if active is None:
        return False
    duty = str(command.resolution.obligation_id)
    task_id = str(command.resolution.goal_task_id)
    return any(
        str(spec.obligation_id) == duty and str(spec.task_id) != task_id
        for spec in semantics.list_plan_memberships(command.mission_id, int(active.revision))
    )


def root_duty_closure(
    semantics: HtnStore,
    mission_id: str,
    *,
    obligation_id: str,
    method_instance_id: str | None,
) -> frozenset[str]:
    """The duties a Mission-root delivery receipt may legitimately quote.

    AER §6.3 is explicit that sending needs *the report* accepted, not the whole
    Mission finished — so the ``Acceptance`` a receipt quotes is usually a child
    contribution rather than the root goal itself.  The closure is therefore the
    root duty plus the duties of the adopted method instance's child slots, and a
    receipt quoting anything else is describing some other work.

    Public since P2.3c part 2c (review F5): the *trigger* has to choose which
    receipts to name, and it chooses with this function rather than with a second
    copy of the rule.
    """

    duties = {str(obligation_id)}
    if method_instance_id is not None:
        for child in semantics.list_child_occurrences(mission_id, str(method_instance_id)):
            duties.add(str(child.obligation_id))
    return frozenset(duties)


def eligible_root_receipts(
    semantics: HtnStore,
    mission_id: str,
    *,
    obligation_id: str,
    method_instance_id: str | None,
    required_stage: DeliveryStage | None,
) -> tuple[str, ...]:
    """The recorded receipts a root resolution command may name (review F5).

    ``_check_delivery`` refuses the *whole command* when any named receipt fails
    :func:`require_valid_receipt` — the right rule, and the reason a trigger must not
    simply hand over every receipt the Mission ever recorded.  One receipt written for
    a branch the plan later retired, or for an ``Acceptance`` that was afterwards
    superseded, would then make the root resolution unformable for good, with the same
    refusal replayed every cycle.  That is a liveness defect the trigger creates for
    itself.

    So the trigger asks here, and the answer is produced by running the accept side's
    own predicate over what the store holds.  Nothing is relaxed: this is a *filter*,
    and every receipt it returns is checked again inside the commit transaction, where
    the plan may have moved since.
    """

    allowed = root_duty_closure(
        semantics,
        mission_id,
        obligation_id=obligation_id,
        method_instance_id=method_instance_id,
    )
    chosen: list[str] = []
    for receipt in semantics.list_delivery_receipts(mission_id):
        try:
            require_valid_receipt(semantics, mission_id, receipt, allowed)
        except ResolutionCommitRejected:
            continue
        if required_stage is not None and not delivery_reached(receipt, required_stage):
            continue
        chosen.append(str(receipt.receipt_id))
    return tuple(chosen)


def require_valid_receipt(
    semantics: HtnStore,
    mission_id: str,
    receipt: DeliveryReceipt,
    allowed_duties: frozenset[str],
) -> Acceptance:
    """Re-read one receipt's ``Acceptance`` and refuse anything it cannot show.

    Four things a receipt has to survive, each with the same reason code because each
    means "this receipt is not evidence of this Mission's delivery": it names this
    Mission, its ``Acceptance`` is stored, that Acceptance belongs to this Mission and
    to this root's duty closure, and it is still CURRENT.  A receipt is a *claim*
    until the store agrees with it.
    """

    def refuse(detail: str) -> ResolutionCommitRejected:
        return ResolutionCommitRejected(
            "DELIVERY_RECEIPT_INVALID",
            f"delivery receipt {receipt.receipt_id!r}: {detail}",
        )

    if receipt.mission_id != mission_id:
        raise refuse(f"it belongs to mission {receipt.mission_id!r}, not {mission_id!r}")
    try:
        acceptance = semantics.get_acceptance(str(receipt.acceptance_id))
    except StoreError as error:
        raise refuse(
            f"it quotes acceptance {receipt.acceptance_id!s}, which is not stored ({error})"
        ) from error
    if acceptance.mission_id != mission_id:
        raise refuse(
            f"acceptance {acceptance.acceptance_id!s} belongs to mission "
            f"{acceptance.mission_id!r}, not {mission_id!r}"
        )
    if str(acceptance.obligation_id) not in allowed_duties:
        raise refuse(
            f"acceptance {acceptance.acceptance_id!s} serves duty "
            f"{acceptance.obligation_id!s}, which is outside this root's duty closure "
            f"{sorted(allowed_duties)}; a receipt for other work does not deliver this goal"
        )
    if acceptance.validity not in USABLE_ACCEPTANCE_VALIDITY:
        raise refuse(
            f"acceptance {acceptance.acceptance_id!s} is {acceptance.validity!s}; a superseded "
            "or revoked acceptance is history and delivers nothing now (AER §6.1)"
        )
    return acceptance


def _receipt_from_event(
    event_id: str, mission_id: str, payload: Mapping[str, Any]
) -> CommandReceipt:
    """Project one :class:`CommandReceipt` out of the event that recorded the command."""

    identity = dict(payload.get("output_identity") or {})
    return CommandReceipt(
        command_id=str(payload.get("command_id")),
        mission_id=mission_id,
        kind=str(identity.get("kind")),
        subject_id=str(identity.get("subject_id")),
        intent_hash=str(payload.get("intent_hash")),
        read_set_hash=str(payload.get("read_set_hash")),
        event_id=event_id,
        output_identity=identity,
        detail=dict(payload.get("detail") or {}),
    )


def _authorize(issued_by: str, scope_id: str, principal: ResolutionPrincipal) -> None:
    """Authorship is stated by the command and re-checked, never inferred.

    An *unsigned* command is refused rather than attributed to whoever presents it:
    treating a blank ``issued_by`` as "no claim, so no mismatch" would let any
    principal holding the scope accept a contribution it did not judge, and the
    receipt would then name a reviewer that never issued the command (AER §7).
    """

    if not str(issued_by).strip():
        raise ResolutionCommitRejected(
            "PRINCIPAL_MISMATCH",
            "the command names no issuer; an acceptance is committed on behalf of an "
            f"identified principal, and an unsigned command is not attributed to "
            f"{principal.principal_id!r} by default (AER §7)",
        )
    if issued_by != principal.principal_id:
        raise ResolutionCommitRejected(
            "PRINCIPAL_MISMATCH",
            f"the command was issued by {issued_by!r} and presented by "
            f"{principal.principal_id!r}; authorship is not re-assignable at delivery",
        )
    if scope_id != principal.scope_id:
        raise ResolutionCommitRejected(
            "SCOPE_NOT_AUTHORIZED",
            f"{principal.principal_id!r} holds scope {principal.scope_id!r} and may not "
            f"commit into {scope_id!r}",
        )


__all__ = (
    "ACCEPTANCE_COMMITTED",
    "ACCEPTANCE_KIND",
    "CLOSED_DUTY_LIFECYCLES",
    "DELIVERY_STAGE_ORDER",
    "GOAL_RESOLUTION_COMMITTED",
    "GOAL_RESOLUTION_KIND",
    "REPLAY_PAGE",
    "USABLE_ACCEPTANCE_VALIDITY",
    "AcceptReviewCommand",
    "AcceptanceReceipt",
    "DELIVERY_RECEIPT_RECORDED",
    "CommandReceipt",
    "CommitGoalResolutionCommand",
    "GoalResolutionReceipt",
    "ResolutionCommitRejected",
    "ResolutionCommitsMixin",
    "ResolutionPrincipal",
    "command_event_key",
    "command_idempotency_key",
    "delivery_reached",
    "eligible_root_receipts",
    "require_valid_receipt",
    "root_duty_closure",
)
