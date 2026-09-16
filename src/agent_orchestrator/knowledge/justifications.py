# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Justification maintenance: multi-group support and a grounded least fixpoint.

Plan §11.2/§11.5 and AER §9.1–9.3 pick one specific algorithm and this module is
it: *polarised finite positive rules plus a least fixpoint*.  Four properties are
load-bearing and are pinned by tests rather than left to the implementer:

* **Nothing derives itself.**  Every round starts from the anchors admitted for
  *this* evaluation; a conclusion that was TRUE last time is not an anchor this
  time (AER §9.2 step 2).  ``A ← B`` with ``B ← A`` and no anchor therefore has
  no support, and ``A ← (A AND E)`` cannot bootstrap itself even when ``E`` is
  observed.
* **No negation as failure.**  A premise that has not been derived is *not*
  false, and a closed-world denial needs an authoritative, scoped, watermarked
  negative observation — never "we looked and found nothing" (§6.6 C28).
* **Running out of budget is not an answer about the world.**  Exceeding the
  deployment limit yields :attr:`ClosureStatus.EVALUATION_INCOMPLETE`, which
  blocks release; it is never reported as an UNKNOWN world fact.
* **History is not current reason.**  :class:`LineageRecord` keeps ``was_used``
  (what the artifact really read) and ``supports_for_use`` (why it may be used
  now) in two compartments that cannot rewrite each other (AER §9.3).

Out of scope here (P3.5 / P3.6): epoch barriers, durable dirty queues and
persistence.  This module is pure, in-memory and imports no store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn

from ..contracts.evidence_state import (
    Availability,
    ObservationRecord,
    RecheckOutcome,
    SupportCount,
    TemporalUse,
    TruthValue,
    Validity,
    WitnessPurpose,
)
from ..contracts.models import ContractError
from ..contracts.semantic_base import (
    MAX_LIST,
    EvidenceRef,
    TypedRef,
    content_hash_of,
    enum_of,
    flag,
    identifier,
    index,
    optional_identifier,
    optional_index,
    reject_executable,
    sequence_of,
)
from .predicates import PredicateSignature, WorldAssumption

#: Default ceiling on admitted support nodes for one closure run.
DEFAULT_NODE_BUDGET = 10_000

#: Release-blocking reason codes (never a claim about the world).
EVALUATION_INCOMPLETE = "EVALUATION_INCOMPLETE"
UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
REFUTED = "REFUTED"
UNVERIFIED_ASSUMPTION = "UNVERIFIED_ASSUMPTION"


class Polarity(StrEnum):
    """Which side of a single proposition a support speaks for (AER §8.3)."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"

    @property
    def opposite(self) -> Polarity:
        return Polarity.NEGATIVE if self is Polarity.POSITIVE else Polarity.POSITIVE


_POLARITY_ALIASES = {
    "+": Polarity.POSITIVE,
    "-": Polarity.NEGATIVE,
    "POSITIVE": Polarity.POSITIVE,
    "NEGATIVE": Polarity.NEGATIVE,
}


def parse_polarity(value: object, name: str = "polarity") -> Polarity:
    """Accept ``+`` / ``-`` / ``True`` / ``False`` / the enum spelling."""

    if isinstance(value, Polarity):
        return value
    if isinstance(value, bool):
        return Polarity.POSITIVE if value else Polarity.NEGATIVE
    if isinstance(value, str) and value in _POLARITY_ALIASES:
        return _POLARITY_ALIASES[value]
    return enum_of(Polarity, value, name)


@dataclass(frozen=True, slots=True)
class Atom:
    """One proposition taken with one polarity — the unit the fixpoint reaches.

    ``(key, POSITIVE)`` and ``(key, NEGATIVE)`` are two separate atoms; their
    closures are computed independently and only combined into a four-valued
    answer at the end, which is what keeps a counter-observation from being
    outvoted by however many positive supports exist.
    """

    key: str
    polarity: Polarity = Polarity.POSITIVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", identifier(self.key, "atom.key"))
        object.__setattr__(self, "polarity", parse_polarity(self.polarity, "atom.polarity"))

    @property
    def negated(self) -> Atom:
        return Atom(key=self.key, polarity=self.polarity.opposite)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "polarity": str(self.polarity)}

    @classmethod
    def from_json(cls, value: object, name: str = "atom") -> Atom:
        if isinstance(value, Atom):
            return value
        if isinstance(value, str):
            return cls(key=value)
        if not isinstance(value, Mapping) or "key" not in value:
            raise ContractError(f"{name} must be an object with a key")
        return cls(key=value["key"], polarity=value.get("polarity", Polarity.POSITIVE))


@dataclass(frozen=True, slots=True)
class PropositionPremise:
    """A premise satisfied only when that atom has grounded support this round."""

    atom: Atom

    def __post_init__(self) -> None:
        if not isinstance(self.atom, Atom):
            raise ContractError("premise.atom must be an Atom")

    def to_json(self) -> dict[str, Any]:
        return {"kind": "proposition", "atom": self.atom.to_json()}


@dataclass(frozen=True, slots=True)
class EvidencePremise:
    """A premise satisfied only while that exact evidence revision is admitted.

    Byte identity matters: a premise naming ``E1@rev3`` is not satisfied by a
    different revision of the same source, which is what makes "retract E1" a
    real retraction rather than a relabelling.
    """

    ref: EvidenceRef

    def __post_init__(self) -> None:
        if not isinstance(self.ref, EvidenceRef):
            raise ContractError("premise.ref must be an EvidenceRef")

    def to_json(self) -> dict[str, Any]:
        return {"kind": "evidence", "ref": self.ref.to_json()}


Premise = PropositionPremise | EvidencePremise


def parse_premise(value: object, name: str = "premise") -> Premise:
    """Normalise a ``PropositionKey | Atom | EvidenceRef`` into a premise node."""

    if isinstance(value, (PropositionPremise, EvidencePremise)):
        return value
    if isinstance(value, EvidenceRef):
        return EvidencePremise(ref=value)
    if isinstance(value, (Atom, str)):
        return PropositionPremise(atom=Atom.from_json(value, name))
    if isinstance(value, Mapping):
        kind = value.get("kind")
        if kind == "evidence":
            return EvidencePremise(ref=EvidenceRef.from_json(value["ref"], f"{name}.ref"))
        if kind == "proposition":
            return PropositionPremise(atom=Atom.from_json(value["atom"], f"{name}.atom"))
    raise ContractError(f"{name} must be a proposition key, an Atom or an EvidenceRef")


@dataclass(frozen=True, slots=True)
class RuleCondition:
    """A named side condition of a rule.

    A condition is never true by default: it holds only while the caller asserts
    it, which is the *explicitly revocable assumption* AER §9.2 allows in place of
    a real negative observation.  Asserting one always taints the support that
    passes through it, so a high-risk gate can refuse the lot.

    ``defeasible`` says whether this rule is *allowed* to rest on an assertion at
    all; it does not decide whether the resulting support counts as assumed.  A
    non-defeasible condition that is only satisfied by an assertion is still
    assumed support — otherwise ``defeasible=False`` would be a way to launder an
    assumption into a verified fact.
    """

    name: str
    defeasible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name, "condition.name"))
        object.__setattr__(self, "defeasible", flag(self.defeasible, "condition.defeasible"))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "defeasible": self.defeasible}

    @classmethod
    def from_json(cls, value: object, name: str = "condition") -> RuleCondition:
        if isinstance(value, RuleCondition):
            return value
        if isinstance(value, str):
            return cls(name=value)
        if not isinstance(value, Mapping) or "name" not in value:
            raise ContractError(f"{name} must be an object with a name")
        return cls(name=value["name"], defeasible=bool(value.get("defeasible", False)))


@dataclass(frozen=True, slots=True)
class JustificationSet:
    """One jointly sufficient support group for one polarised conclusion (§9.1).

    Groups are ANDed inside and ORed between: every premise of *this* group has
    to hold, and any one group that holds gives the conclusion support.  That is
    a representation choice, not a requirement to expand arbitrary formulas into
    DNF — the witness records which group actually fired.

    A group with no premises is refused: observations are the explicit anchors of
    this system, and a premise-free "rule" would be an unfalsifiable fact smuggled
    in as inference (AER §9.1).
    """

    conclusion: str
    polarity: Polarity = Polarity.POSITIVE
    premises: tuple[Premise, ...] = ()
    conditions: tuple[RuleCondition, ...] = ()
    rule_version: str = ""
    receipt_ref: TypedRef | None = None
    source_group: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "conclusion", identifier(self.conclusion, "justification.conclusion")
        )
        object.__setattr__(
            self, "polarity", parse_polarity(self.polarity, "justification.polarity")
        )
        premises = sequence_of(
            self.premises,
            "justification.premises",
            parse_premise,
            limit=MAX_LIST,
            minimum=1,
        )
        if len(set(premises)) != len(premises):
            raise ContractError("justification.premises must not repeat a premise")
        object.__setattr__(self, "premises", premises)
        conditions = sequence_of(
            self.conditions,
            "justification.conditions",
            RuleCondition.from_json,
        )
        names = [condition.name for condition in conditions]
        if len(set(names)) != len(names):
            raise ContractError("justification.conditions must not repeat a name")
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(
            self, "rule_version", identifier(self.rule_version, "justification.rule_version")
        )
        if self.receipt_ref is not None and not isinstance(self.receipt_ref, TypedRef):
            raise ContractError("justification.receipt_ref must be a TypedRef or null")
        object.__setattr__(
            self,
            "source_group",
            optional_identifier(self.source_group, "justification.source_group"),
        )

    @property
    def atom(self) -> Atom:
        return Atom(key=self.conclusion, polarity=self.polarity)

    @property
    def signature(self) -> str:
        """The identity of this support group; it may enter a closure only once."""

        return content_hash_of(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "polarity": str(self.polarity),
            "premises": [premise.to_json() for premise in self.premises],
            "conditions": [condition.to_json() for condition in self.conditions],
            "rule_version": self.rule_version,
            "receipt_ref": None if self.receipt_ref is None else self.receipt_ref.to_json(),
            "source_group": self.source_group,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "justification_set") -> JustificationSet:
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} must be an object")
        raw_receipt = value.get("receipt_ref")
        return cls(
            conclusion=value["conclusion"],
            polarity=value.get("polarity", Polarity.POSITIVE),
            premises=tuple(value.get("premises", ())),
            conditions=tuple(value.get("conditions", ())),
            rule_version=value.get("rule_version", ""),
            receipt_ref=(
                None if raw_receipt is None else TypedRef.from_json(raw_receipt, f"{name}.receipt")
            ),
            source_group=value.get("source_group"),
        )


class SupportGraph:
    """An immutable conclusion → support-groups index ("AND inside, OR between").

    Registration is the single admission point, so a group that carries an
    ``eval``-shaped string or a callable is refused here rather than at firing
    time: by the time the fixpoint walks a group, it is known to be plain data.
    """

    __slots__ = ("_by_conclusion", "_by_signature", "_frozen")

    def __init__(self, justifications: Iterable[JustificationSet] = ()) -> None:
        object.__setattr__(self, "_frozen", False)
        by_conclusion: dict[Atom, list[JustificationSet]] = {}
        by_signature: dict[str, JustificationSet] = {}
        for entry in justifications:
            if not isinstance(entry, JustificationSet):
                raise ContractError("SupportGraph accepts JustificationSet entries only")
            reject_executable(entry.to_json(), "justification_set")
            signature = entry.signature
            if signature in by_signature:
                # The same group offered twice is still one group, not two
                # independent reasons; deduplicate so counting cannot be gamed.
                continue
            by_signature[signature] = entry
            by_conclusion.setdefault(entry.atom, []).append(entry)
        self._by_conclusion = {atom: tuple(items) for atom, items in by_conclusion.items()}
        self._by_signature = by_signature
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise ContractError("SupportGraph is immutable; build a new graph instead")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise ContractError("SupportGraph is immutable; build a new graph instead")

    def __len__(self) -> int:
        return len(self._by_signature)

    def groups_for(self, atom: Atom) -> tuple[JustificationSet, ...]:
        return self._by_conclusion.get(atom, ())

    def justifications(self) -> tuple[JustificationSet, ...]:
        """Every group, in a stable order so a closure is reproducible."""

        return tuple(
            sorted(
                self._by_signature.values(),
                key=lambda entry: (entry.conclusion, str(entry.polarity), entry.signature),
            )
        )

    def signatures(self) -> frozenset[str]:
        return frozenset(self._by_signature)

    def conclusions(self) -> frozenset[Atom]:
        return frozenset(self._by_conclusion)

    def with_justifications(self, *more: JustificationSet) -> SupportGraph:
        return SupportGraph((*self.justifications(), *more))

    def without_signature(self, signature: str) -> SupportGraph:
        return SupportGraph(
            entry for entry in self.justifications() if entry.signature != signature
        )


class AnchorOrigin(StrEnum):
    """Where an anchor comes from.  Both are external; neither is a derivation."""

    OBSERVATION = "OBSERVATION"
    CHECK = "CHECK"


class AnchorRejection(StrEnum):
    """Why a candidate is not a legal anchor for this scope / purpose / as_of."""

    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    NOT_CURRENT = "NOT_CURRENT"
    UNREADABLE = "UNREADABLE"
    NOT_TRACEABLE = "NOT_TRACEABLE"
    AFTER_AS_OF = "AFTER_AS_OF"
    EXPIRED = "EXPIRED"
    NOT_AUTHORITATIVE_NEGATIVE = "NOT_AUTHORITATIVE_NEGATIVE"
    OBSERVER_NOT_AUTHORITATIVE = "OBSERVER_NOT_AUTHORITATIVE"
    COVERAGE_SCOPE_MISMATCH = "COVERAGE_SCOPE_MISMATCH"
    PREDICTED_NOT_OBSERVED = "PREDICTED_NOT_OBSERVED"
    SUPERSEDED_BY_RECEIPT = "SUPERSEDED_BY_RECEIPT"
    NO_CONTINUOUS_GUARANTEE = "NO_CONTINUOUS_GUARANTEE"
    UNREGISTERED_PREDICATE = "UNREGISTERED_PREDICATE"


@dataclass(frozen=True, slots=True)
class AnchorCandidate:
    """An observation offered as an anchor, with the bookkeeping the selector needs.

    ``predicted`` exists so a method's ``expected_effects`` can be represented
    without ever being mistaken for an observation (AER §9.1), and
    ``inapplicable_receipt`` exists so retracting a counter-observation requires
    a receipt rather than an assertion (AER §9.3).
    """

    observation: ObservationRecord
    scope_id: str
    source_group: str
    origin: AnchorOrigin = AnchorOrigin.OBSERVATION
    evidence_ref: EvidenceRef | None = None
    observer_id: str | None = None
    availability: Availability = Availability.READABLE
    validity: Validity = Validity.CURRENT
    temporal_use: TemporalUse = TemporalUse.CURRENT_AT_USE
    monitor_interval_ms: int | None = None
    traceable: bool = True
    predicted: bool = False
    inapplicable: bool = False
    inapplicable_receipt: TypedRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation, ObservationRecord):
            raise ContractError("anchor.observation must be an ObservationRecord")
        object.__setattr__(self, "scope_id", identifier(self.scope_id, "anchor.scope_id"))
        object.__setattr__(
            self, "source_group", identifier(self.source_group, "anchor.source_group")
        )
        object.__setattr__(self, "origin", enum_of(AnchorOrigin, self.origin, "anchor.origin"))
        if self.evidence_ref is not None and not isinstance(self.evidence_ref, EvidenceRef):
            raise ContractError("anchor.evidence_ref must be an EvidenceRef or null")
        object.__setattr__(
            self, "observer_id", optional_identifier(self.observer_id, "anchor.observer_id")
        )
        object.__setattr__(
            self, "availability", enum_of(Availability, self.availability, "anchor.availability")
        )
        object.__setattr__(self, "validity", enum_of(Validity, self.validity, "anchor.validity"))
        object.__setattr__(
            self, "temporal_use", enum_of(TemporalUse, self.temporal_use, "anchor.temporal_use")
        )
        object.__setattr__(
            self,
            "monitor_interval_ms",
            optional_index(self.monitor_interval_ms, "anchor.monitor_interval_ms", minimum=1),
        )
        object.__setattr__(self, "traceable", flag(self.traceable, "anchor.traceable"))
        object.__setattr__(self, "predicted", flag(self.predicted, "anchor.predicted"))
        object.__setattr__(self, "inapplicable", flag(self.inapplicable, "anchor.inapplicable"))
        if self.inapplicable_receipt is not None and not isinstance(
            self.inapplicable_receipt, TypedRef
        ):
            raise ContractError("anchor.inapplicable_receipt must be a TypedRef or null")
        if self.inapplicable and self.inapplicable_receipt is None:
            raise ContractError(
                "declaring an observation inapplicable needs an explicit receipt; "
                "a still-valid counter-observation is not dismissed by assertion (AER §9.3)"
            )

    @property
    def atom(self) -> Atom:
        return Atom(
            key=self.observation.proposition_key,
            polarity=Polarity.POSITIVE if self.observation.polarity else Polarity.NEGATIVE,
        )


#: Proof that an :class:`Anchor` came out of :meth:`AnchorSelector.select`.
_SELECTOR_TOKEN = object()


@dataclass(frozen=True, slots=True)
class Anchor:
    """A candidate the selector admitted.

    Only :meth:`AnchorSelector.select` can build one — the constructor demands a
    module-private token — and :func:`grounded_closure` accepts nothing else.  So
    "do not reuse last round's TRUE as an anchor" cannot be worked around by
    assembling the dataclass by hand within this process.

    That is a guard against mistakes, not a security boundary: anything running
    in-process can read ``_SELECTOR_TOKEN``.  Anchor authenticity across a
    restart is the store's job (P3.5 / P3.6), which re-selects from persisted
    observations rather than trusting a handed-in object.
    """

    atom: Atom
    anchor_id: str
    source_group: str
    origin: AnchorOrigin
    evidence_ref: EvidenceRef | None = None
    historical: bool = False
    token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.token is not _SELECTOR_TOKEN:
            raise ContractError(
                "an Anchor is produced by AnchorSelector.select only; building one "
                "directly would bypass every admissibility rule (AER §9.2 step 1)"
            )
        if not isinstance(self.atom, Atom):
            raise ContractError("anchor.atom must be an Atom")
        object.__setattr__(self, "anchor_id", identifier(self.anchor_id, "anchor.anchor_id"))
        object.__setattr__(
            self, "source_group", identifier(self.source_group, "anchor.source_group")
        )
        object.__setattr__(self, "origin", enum_of(AnchorOrigin, self.origin, "anchor.origin"))
        object.__setattr__(self, "historical", flag(self.historical, "anchor.historical"))

    @property
    def signature(self) -> str:
        return f"anchor:{self.anchor_id}"


@dataclass(frozen=True, slots=True)
class RejectedAnchor:
    candidate: AnchorCandidate
    reason: AnchorRejection


@dataclass(frozen=True, slots=True)
class AnchorSelection:
    anchors: tuple[Anchor, ...] = ()
    rejected: tuple[RejectedAnchor, ...] = ()

    def reason_for(self, observation_id: str) -> AnchorRejection | None:
        for entry in self.rejected:
            if entry.candidate.observation.observation_id == observation_id:
                return entry.reason
        return None

    def admitted_ids(self) -> frozenset[str]:
        return frozenset(anchor.anchor_id for anchor in self.anchors)

    def evidence_refs(self) -> frozenset[EvidenceRef]:
        return frozenset(
            anchor.evidence_ref for anchor in self.anchors if anchor.evidence_ref is not None
        )


#: Purposes that describe the past rather than act now (AER §10.4).
HISTORICAL_PURPOSES = frozenset({WitnessPurpose.CONTEXT})


class AnchorSelector:
    """Step 1 of AER §9.2: pick the legal, traceable anchors for this evaluation.

    Selection is per ``(scope, purpose, as_of)``; the same observation can be a
    legal anchor for a historical context read and an illegal one for an ACCEPT
    gate.  Permission is *not* part of that variation — a redacted source stays
    unreadable for every purpose, because historical analysis does not exempt
    authorization (AER §10.4).
    """

    __slots__ = ("_as_of_ms", "_purpose", "_scope_id", "_signatures")

    def __init__(
        self,
        *,
        scope_id: str,
        purpose: WitnessPurpose,
        as_of_ms: int,
        signatures: Mapping[str, PredicateSignature],
    ) -> None:
        self._scope_id = identifier(scope_id, "selector.scope_id")
        self._purpose = enum_of(WitnessPurpose, purpose, "selector.purpose")
        self._as_of_ms = index(as_of_ms, "selector.as_of_ms")
        if not isinstance(signatures, Mapping):
            raise ContractError(
                "selector.signatures is required: without the registry binding a "
                "CLOSED-world denial could not be checked against its authoritative "
                "observers, so an unbound selector would silently admit it (§6.6 C28)"
            )
        self._signatures = dict(signatures)

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def purpose(self) -> WitnessPurpose:
        return self._purpose

    @property
    def as_of_ms(self) -> int:
        return self._as_of_ms

    def select(self, candidates: Iterable[AnchorCandidate]) -> AnchorSelection:
        admitted: list[Anchor] = []
        rejected: list[RejectedAnchor] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, AnchorCandidate):
                raise ContractError("AnchorSelector.select accepts AnchorCandidate entries only")
            reason, historical = self._verdict(candidate)
            if reason is not None:
                rejected.append(RejectedAnchor(candidate=candidate, reason=reason))
                continue
            anchor_id = candidate.observation.observation_id
            if anchor_id in seen:
                continue
            seen.add(anchor_id)
            admitted.append(
                Anchor(
                    atom=candidate.atom,
                    anchor_id=anchor_id,
                    source_group=candidate.source_group,
                    origin=candidate.origin,
                    evidence_ref=candidate.evidence_ref,
                    historical=historical,
                    token=_SELECTOR_TOKEN,
                )
            )
        return AnchorSelection(anchors=tuple(admitted), rejected=tuple(rejected))

    def _verdict(self, candidate: AnchorCandidate) -> tuple[AnchorRejection | None, bool]:
        """The fixed check order.  Structure first, then policy, then evidence."""

        observation = candidate.observation
        if candidate.predicted:
            return AnchorRejection.PREDICTED_NOT_OBSERVED, False
        if candidate.inapplicable:
            return AnchorRejection.SUPERSEDED_BY_RECEIPT, False
        if candidate.scope_id != self._scope_id:
            return AnchorRejection.SCOPE_MISMATCH, False
        if candidate.availability is not Availability.READABLE:
            return AnchorRejection.UNREADABLE, False
        if candidate.validity is not Validity.CURRENT:
            return AnchorRejection.NOT_CURRENT, False
        if not candidate.traceable:
            return AnchorRejection.NOT_TRACEABLE, False
        signature = self._signature_for(observation.proposition_key)
        if signature is None:
            return AnchorRejection.UNREGISTERED_PREDICATE, False
        if observation.observed_at_ms > self._as_of_ms:
            return AnchorRejection.AFTER_AS_OF, False
        expired = (
            observation.valid_until_ms is not None and self._as_of_ms > observation.valid_until_ms
        ) or (observation.valid_from_ms is not None and self._as_of_ms < observation.valid_from_ms)
        historical = False
        if expired:
            if (
                candidate.temporal_use is TemporalUse.HISTORICAL_AS_OF
                and self._purpose in HISTORICAL_PURPOSES
            ):
                historical = True
            else:
                return AnchorRejection.EXPIRED, False
        if (
            candidate.temporal_use is TemporalUse.CONTINUOUS
            and self._purpose is WitnessPurpose.MAINTAIN
            and candidate.monitor_interval_ms is None
        ):
            return AnchorRejection.NO_CONTINUOUS_GUARANTEE, False
        if not observation.polarity:
            reason = self._negative_verdict(candidate, signature)
            if reason is not None:
                return reason, False
        return None, historical

    def _negative_verdict(
        self, candidate: AnchorCandidate, signature: PredicateSignature
    ) -> AnchorRejection | None:
        """§6.6 C28: a denial needs a complete, scoped, watermarked query.

        ``signature`` is non-optional: an unresolved predicate is rejected before
        this point, which is why ``AnchorSelector`` demands a registry binding.
        Without one there would be no ``observer_ids`` to check a closed-world
        denial against, and it would be admitted on the observation's own say-so.
        """

        observation = candidate.observation
        if not observation.is_authoritative_negative:
            return AnchorRejection.NOT_AUTHORITATIVE_NEGATIVE
        if observation.coverage_scope != self._scope_id:
            return AnchorRejection.COVERAGE_SCOPE_MISMATCH
        if signature.world_assumption is WorldAssumption.CLOSED:
            if candidate.observer_id not in signature.observer_ids:
                return AnchorRejection.OBSERVER_NOT_AUTHORITATIVE
        return None

    def _signature_for(self, proposition_key: str) -> PredicateSignature | None:
        return self._signatures.get(proposition_key)

    def registered(self, proposition_key: str) -> bool:
        """Whether this selector can resolve the predicate behind a proposition."""

        return proposition_key in self._signatures


class ClosureStatus(StrEnum):
    COMPLETE = "COMPLETE"
    EVALUATION_INCOMPLETE = "EVALUATION_INCOMPLETE"


class WitnessKind(StrEnum):
    ANCHOR = "ANCHOR"
    DERIVED = "DERIVED"


@dataclass(frozen=True, slots=True)
class SupportWitness:
    """The path actually taken to a support — not a reconstruction after the fact."""

    atom: Atom
    kind: WitnessKind
    signature: str
    rule_version: str | None = None
    premises: tuple[Premise, ...] = ()
    source_group: str | None = None
    depth: int = 0
    assumptions: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "atom": self.atom.to_json(),
            "kind": str(self.kind),
            "signature": self.signature,
            "rule_version": self.rule_version,
            "premises": [premise.to_json() for premise in self.premises],
            "source_group": self.source_group,
            "depth": self.depth,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ClosureResult:
    """The outcome of one grounded evaluation.

    ``status`` is deliberately separate from the truth values: an evaluation that
    ran out of budget carries whatever partial support it found, but
    :meth:`may_release` refuses everything, because "we could not finish" is a
    statement about the evaluator and not about the world.
    """

    status: ClosureStatus
    witnesses: Mapping[Atom, tuple[SupportWitness, ...]] = field(default_factory=dict)
    source_groups: Mapping[Atom, frozenset[str]] = field(default_factory=dict)
    path_groups: Mapping[Atom, tuple[frozenset[str], ...]] = field(default_factory=dict)
    assumption_backed: frozenset[Atom] = frozenset()
    admitted_signatures: frozenset[str] = frozenset()
    node_count: int = 0
    node_budget: int = DEFAULT_NODE_BUDGET

    @property
    def is_complete(self) -> bool:
        return self.status is ClosureStatus.COMPLETE

    def reached(self, atom: Atom) -> bool:
        return bool(self.witnesses.get(atom))

    def witnesses_for(self, atom: Atom) -> tuple[SupportWitness, ...]:
        return self.witnesses.get(atom, ())

    def support_for(self, key: str) -> SupportCount:
        """Independent witnesses per polarity, folded into the AER §8.3 pair."""

        positive = len(self.witnesses_for(Atom(key=key, polarity=Polarity.POSITIVE)))
        negative = len(self.witnesses_for(Atom(key=key, polarity=Polarity.NEGATIVE)))
        return SupportCount(t=positive, f=negative)

    def truth_for(self, key: str) -> TruthValue:
        return self.support_for(key).truth

    def independent_source_groups(
        self, key: str, polarity: Polarity = Polarity.POSITIVE
    ) -> frozenset[str]:
        """Every originating source behind any support of this atom, transitively.

        This is the *union* over all support paths.  It says which sources are
        involved; it does not say the conclusion has that many independent
        reasons — use :meth:`satisfies_independence` for the policy question.
        """

        return self.source_groups.get(Atom(key=key, polarity=polarity), frozenset())

    def support_paths(
        self, key: str, polarity: Polarity = Polarity.POSITIVE
    ) -> tuple[frozenset[str], ...]:
        """One entry per witness: the sources that path had to rely on."""

        return self.path_groups.get(Atom(key=key, polarity=polarity), ())

    def satisfies_independence(
        self, key: str, *, required: int, polarity: Polarity = Polarity.POSITIVE
    ) -> bool:
        """Are there ``required`` support paths that share no source at all?

        Counting distinct *sources* is the wrong question and gives the wrong
        answer twice over.  Ten restatements of one wire report are ten witnesses
        and one source, so counting witnesses over-credits; and a single
        conjunctive path ``C ← (A AND B)`` touches two sources while still being
        *one* reason, so counting sources over-credits too (AER §9.1 asks for two
        independent sufficient supports, not two names in one chain).  What is
        counted here is pairwise source-disjoint paths.
        """

        wanted = index(required, "required", minimum=1)
        paths = [groups for groups in self.support_paths(key, polarity) if groups]
        return _max_disjoint_paths(paths, wanted) >= wanted

    def rests_on_assumption(self, key: str, polarity: Polarity = Polarity.POSITIVE) -> bool:
        return Atom(key=key, polarity=polarity) in self.assumption_backed

    def require_complete(self) -> None:
        if not self.is_complete:
            raise ContractError(
                "EVALUATION_INCOMPLETE: the support closure hit its node budget; "
                "this blocks release and must not be read as an UNKNOWN world fact"
            )

    def release_block_reason(
        self,
        key: str,
        *,
        high_risk: bool,
        polarity: Polarity = Polarity.POSITIVE,
    ) -> str | None:
        """Why this conclusion may not be released, or ``None`` when it may.

        ``high_risk`` has no default on purpose: a caller that has not said which
        kind of gate it is must not silently get the permissive one.
        """

        flag(high_risk, "high_risk")
        if not self.is_complete:
            return EVALUATION_INCOMPLETE
        support = self.support_for(key)
        truth = support.truth
        if truth is TruthValue.CONFLICT:
            return CONFLICTING_EVIDENCE
        wanted = support.t if polarity is Polarity.POSITIVE else support.f
        if wanted == 0:
            return UNKNOWN_EVIDENCE if truth is TruthValue.UNKNOWN else REFUTED
        if high_risk and self.rests_on_assumption(key, polarity):
            return UNVERIFIED_ASSUMPTION
        return None

    def may_release(
        self,
        key: str,
        *,
        high_risk: bool,
        polarity: Polarity = Polarity.POSITIVE,
    ) -> bool:
        return self.release_block_reason(key, high_risk=high_risk, polarity=polarity) is None


def grounded_closure(
    graph: SupportGraph,
    anchors: Iterable[Anchor],
    *,
    node_budget: int = DEFAULT_NODE_BUDGET,
    assumptions: Mapping[str, bool] | None = None,
) -> ClosureResult:
    """The least fixpoint of polarised finite positive rules over this round's anchors.

    Every run starts from nothing derived: the only seeds are ``anchors``, each
    group fires at most once, and the loop stops when a pass adds nothing.  A
    strongly connected component with no anchor therefore never lights up, and
    removing the anchor that lit one up extinguishes it on the next run — there is
    no state that could keep it alive.
    """

    if not isinstance(graph, SupportGraph):
        raise ContractError("grounded_closure expects a SupportGraph")
    budget = index(node_budget, "node_budget", minimum=1)
    state = _assumption_state(assumptions)

    witnesses: dict[Atom, list[SupportWitness]] = {}
    admitted: set[str] = set()
    evidence_refs: set[EvidenceRef] = set()
    ref_groups: dict[EvidenceRef, set[str]] = {}
    nodes = 0
    incomplete = False

    for anchor in anchors:
        if isinstance(anchor, SupportWitness):
            raise ContractError(
                "a derived support is not an anchor; re-select anchors from observations "
                "instead of feeding back the previous closure (AER §9.2 step 2)"
            )
        if not isinstance(anchor, Anchor):
            raise ContractError("grounded_closure seeds on AnchorSelector output only")
        if anchor.signature in admitted:
            continue
        if nodes >= budget:
            incomplete = True
            break
        admitted.add(anchor.signature)
        nodes += 1
        witnesses.setdefault(anchor.atom, []).append(
            SupportWitness(
                atom=anchor.atom,
                kind=WitnessKind.ANCHOR,
                signature=anchor.signature,
                source_group=anchor.source_group,
            )
        )
        if anchor.evidence_ref is not None:
            evidence_refs.add(anchor.evidence_ref)
            ref_groups.setdefault(anchor.evidence_ref, set()).add(anchor.source_group)

    if not incomplete:
        pending = list(graph.justifications())
        while pending:
            fired: list[tuple[JustificationSet, int, tuple[str, ...]]] = []
            remaining: list[JustificationSet] = []
            for entry in pending:
                satisfied = _premises_satisfied(entry, witnesses, evidence_refs, state)
                if satisfied is None:
                    remaining.append(entry)
                    continue
                fired.append((entry, satisfied[0], satisfied[1]))
            if not fired:
                break
            for entry, depth, used in fired:
                if nodes >= budget:
                    incomplete = True
                    break
                admitted.add(entry.signature)
                nodes += 1
                witnesses.setdefault(entry.atom, []).append(
                    SupportWitness(
                        atom=entry.atom,
                        kind=WitnessKind.DERIVED,
                        signature=entry.signature,
                        rule_version=entry.rule_version,
                        premises=entry.premises,
                        source_group=entry.source_group,
                        depth=depth + 1,
                        assumptions=used,
                    )
                )
            if incomplete:
                break
            pending = remaining

    frozen = {atom: tuple(items) for atom, items in witnesses.items()}
    atom_groups = _source_groups(frozen, ref_groups)
    return ClosureResult(
        status=(ClosureStatus.EVALUATION_INCOMPLETE if incomplete else ClosureStatus.COMPLETE),
        witnesses=frozen,
        source_groups=atom_groups,
        path_groups=_path_groups(frozen, atom_groups, ref_groups),
        assumption_backed=_assumption_backed(frozen),
        admitted_signatures=frozenset(admitted),
        node_count=nodes,
        node_budget=budget,
    )


def _assumption_state(assumptions: Mapping[str, bool] | None) -> dict[str, bool]:
    if assumptions is None:
        return {}
    state: dict[str, bool] = {}
    for name, value in assumptions.items():
        state[identifier(name, "assumptions.key")] = flag(value, f"assumptions.{name}")
    return state


def _premises_satisfied(
    entry: JustificationSet,
    witnesses: Mapping[Atom, list[SupportWitness]],
    evidence_refs: set[EvidenceRef],
    state: Mapping[str, bool],
) -> tuple[int, tuple[str, ...]] | None:
    """``(depth, asserted conditions used)`` when the group fires, else ``None``.

    Every condition satisfied by the caller's assertions is reported, whatever its
    ``defeasible`` flag: the taint tracks *how* the support was obtained, not how
    the rule was declared.
    """

    depth = 0
    for premise in entry.premises:
        if isinstance(premise, EvidencePremise):
            if premise.ref not in evidence_refs:
                return None
            continue
        supports = witnesses.get(premise.atom)
        if not supports:
            return None
        depth = max(depth, min(support.depth for support in supports))
    used: list[str] = []
    for condition in entry.conditions:
        # An unasserted condition blocks the rule.  Nothing is true merely because
        # its negation has not been derived (§6.6 C28 / AER §9.2).
        if state.get(condition.name) is not True:
            return None
        used.append(condition.name)
    return depth, tuple(used)


def _source_groups(
    witnesses: Mapping[Atom, tuple[SupportWitness, ...]],
    ref_groups: Mapping[EvidenceRef, set[str]],
) -> dict[Atom, frozenset[str]]:
    groups: dict[Atom, set[str]] = {atom: set() for atom in witnesses}
    changed = True
    while changed:
        changed = False
        for atom, supports in witnesses.items():
            collected: set[str] = set()
            for support in supports:
                if support.source_group is not None:
                    collected.add(support.source_group)
                for premise in support.premises:
                    if isinstance(premise, EvidencePremise):
                        collected |= ref_groups.get(premise.ref, set())
                    else:
                        collected |= groups.get(premise.atom, set())
            if collected - groups[atom]:
                groups[atom] |= collected
                changed = True
    return {atom: frozenset(names) for atom, names in groups.items()}


#: Cap on the search for pairwise source-disjoint support paths.
MAX_INDEPENDENCE_PATHS = 64


def _max_disjoint_paths(paths: Sequence[frozenset[str]], wanted: int) -> int:
    """How many of these paths can be chosen with no source shared between them.

    Exact, with a hard cap: the answer is only ever compared against a small
    policy number, and an unbounded set-packing search is not something a gate
    should be able to trigger.
    """

    if wanted <= 0:
        return 0
    candidates = sorted(paths[:MAX_INDEPENDENCE_PATHS], key=len)
    best = 0

    def walk(start: int, used: frozenset[str], chosen: int) -> None:
        nonlocal best
        best = max(best, chosen)
        if best >= wanted:
            return
        for position in range(start, len(candidates)):
            groups = candidates[position]
            if groups & used:
                continue
            walk(position + 1, used | groups, chosen + 1)
            if best >= wanted:
                return

    walk(0, frozenset(), 0)
    return best


def _path_groups(
    witnesses: Mapping[Atom, tuple[SupportWitness, ...]],
    atom_groups: Mapping[Atom, frozenset[str]],
    ref_groups: Mapping[EvidenceRef, set[str]],
) -> dict[Atom, tuple[frozenset[str], ...]]:
    """Per-witness source sets.

    A derived witness inherits the *union* of every source behind each of its
    premises rather than the sources of one chosen sub-path.  That deliberately
    over-approximates: over-approximating makes two paths look more entangled, so
    the independence test errs towards refusing, never towards granting.
    """

    out: dict[Atom, tuple[frozenset[str], ...]] = {}
    for atom, supports in witnesses.items():
        paths: list[frozenset[str]] = []
        for support in supports:
            collected: set[str] = set()
            if support.source_group is not None:
                collected.add(support.source_group)
            for premise in support.premises:
                if isinstance(premise, EvidencePremise):
                    collected |= ref_groups.get(premise.ref, set())
                else:
                    collected |= atom_groups.get(premise.atom, frozenset())
            paths.append(frozenset(collected))
        out[atom] = tuple(paths)
    return out


def _assumption_backed(
    witnesses: Mapping[Atom, tuple[SupportWitness, ...]],
) -> frozenset[Atom]:
    """Atoms whose *every* witness passes through an asserted assumption."""

    clean: set[Atom] = set()
    changed = True
    while changed:
        changed = False
        for atom, supports in witnesses.items():
            if atom in clean:
                continue
            for support in supports:
                if support.assumptions:
                    continue
                premises = [p for p in support.premises if isinstance(p, PropositionPremise)]
                if all(premise.atom in clean for premise in premises):
                    clean.add(atom)
                    changed = True
                    break
    return frozenset(atom for atom in witnesses if atom not in clean)


@dataclass(frozen=True, slots=True)
class ConsumerUse:
    """One consumer's use of one conclusion, before and after a re-evaluation.

    ``literal_citations`` are byte-bound references the artifact really makes;
    ``support_signatures`` are the reasons currently offered for using it.  The
    two move independently, which is exactly why swapping a support does not
    repair a report that quotes a withdrawn source (AER §9.3).
    """

    consumer_ref: TypedRef
    purpose: WitnessPurpose
    conclusion: str
    polarity: Polarity = Polarity.POSITIVE
    truth: TruthValue = TruthValue.UNKNOWN
    validity: Validity = Validity.CURRENT
    availability: Availability = Availability.READABLE
    support_signatures: tuple[str, ...] = ()
    literal_citations: tuple[EvidenceRef, ...] = ()
    withdrawn_citations: tuple[EvidenceRef, ...] = ()
    citation_binding_required: bool = True
    rebinding_allowed: bool = True
    requirements_digest: str | None = None
    inputs_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.consumer_ref, TypedRef):
            raise ContractError("use.consumer_ref must be a TypedRef")
        object.__setattr__(self, "purpose", enum_of(WitnessPurpose, self.purpose, "use.purpose"))
        object.__setattr__(self, "conclusion", identifier(self.conclusion, "use.conclusion"))
        object.__setattr__(self, "polarity", parse_polarity(self.polarity, "use.polarity"))
        object.__setattr__(self, "truth", enum_of(TruthValue, self.truth, "use.truth"))
        object.__setattr__(self, "validity", enum_of(Validity, self.validity, "use.validity"))
        object.__setattr__(
            self, "availability", enum_of(Availability, self.availability, "use.availability")
        )
        object.__setattr__(
            self,
            "support_signatures",
            sequence_of(
                self.support_signatures,
                "use.support_signatures",
                lambda item, where: identifier(item, where),
            ),
        )
        for label in ("literal_citations", "withdrawn_citations"):
            for item in getattr(self, label):
                if not isinstance(item, EvidenceRef):
                    raise ContractError(f"use.{label} entries must be EvidenceRef")
        object.__setattr__(
            self,
            "citation_binding_required",
            flag(self.citation_binding_required, "use.citation_binding_required"),
        )
        object.__setattr__(
            self, "rebinding_allowed", flag(self.rebinding_allowed, "use.rebinding_allowed")
        )
        object.__setattr__(
            self,
            "requirements_digest",
            optional_identifier(self.requirements_digest, "use.requirements_digest"),
        )
        object.__setattr__(
            self, "inputs_digest", optional_identifier(self.inputs_digest, "use.inputs_digest")
        )

    @property
    def effective_truth(self) -> TruthValue:
        """Support that is no longer CURRENT stops supporting; it does not flip."""

        if self.validity is not Validity.CURRENT:
            return TruthValue.UNKNOWN
        return self.truth


def reevaluate_consumer(before: ConsumerUse, after: ConsumerUse) -> RecheckOutcome:
    """§11.5 / AER §10.3: classify a consumer's re-evaluation into five outcomes.

    The order of the checks is the substance.  Unreadability is tested first so a
    permission loss never reports as "the content is wrong"; a contradicted or
    changed requirement is INVALID; missing current grounds is NEEDS_REVIEW, not
    INVALID; and REBOUND_SUPPORT is reachable only when nothing the artifact
    actually contains has changed.
    """

    if not isinstance(before, ConsumerUse) or not isinstance(after, ConsumerUse):
        raise ContractError("reevaluate_consumer compares two ConsumerUse snapshots")
    if before.conclusion != after.conclusion or before.polarity is not after.polarity:
        raise ContractError("reevaluate_consumer compares one conclusion with itself")
    if before.consumer_ref.id != after.consumer_ref.id or before.purpose is not after.purpose:
        raise ContractError("reevaluate_consumer compares one consumer and purpose with itself")

    if after.availability is not Availability.READABLE:
        # Not being allowed to read the material says nothing about its content.
        return RecheckOutcome.UNAVAILABLE
    if before.requirements_digest != after.requirements_digest:
        return RecheckOutcome.INVALID

    truth = after.effective_truth
    if truth in (TruthValue.FALSE, TruthValue.CONFLICT):
        return RecheckOutcome.INVALID
    if truth is not TruthValue.TRUE:
        return RecheckOutcome.NEEDS_REVIEW
    if before.inputs_digest != after.inputs_digest:
        return RecheckOutcome.NEEDS_REVIEW
    if after.withdrawn_citations and after.citation_binding_required:
        # The artifact quotes a retracted source.  Rebinding the reason does not
        # change what the bytes say: revise, re-hash, re-review.
        return RecheckOutcome.NEEDS_REVIEW
    if set(before.support_signatures) == set(after.support_signatures):
        return RecheckOutcome.UNCHANGED
    if not after.rebinding_allowed:
        return RecheckOutcome.NEEDS_REVIEW
    return RecheckOutcome.REBOUND_SUPPORT


class LineageRecord:
    """Two compartments that may not rewrite each other (AER §9.3).

    ``was_used`` is append-only history: it records that the artifact really read
    those bytes.  ``supports_for_use`` is the current reason for using the
    conclusion, and it may be re-chosen.  Every method here moves exactly one of
    the two; the crossing operations exist only to raise, so that an attempt to
    launder history into a reason (or a reason into history) fails loudly instead
    of silently.
    """

    __slots__ = ("_artifact_ref", "_frozen", "_supports_for_use", "_was_used")

    def __init__(
        self,
        artifact_ref: TypedRef,
        was_used: Sequence[EvidenceRef] = (),
        supports_for_use: Sequence[str] = (),
    ) -> None:
        object.__setattr__(self, "_frozen", False)
        if not isinstance(artifact_ref, TypedRef):
            raise ContractError("lineage.artifact_ref must be a TypedRef")
        self._artifact_ref = artifact_ref
        history = sequence_of(
            tuple(was_used),
            "lineage.was_used",
            lambda item, where: _evidence_ref(item, where),
        )
        if len(set(history)) != len(history):
            raise ContractError("lineage.was_used must not repeat a reference")
        self._was_used = history
        self._supports_for_use = sequence_of(
            tuple(supports_for_use),
            "lineage.supports_for_use",
            lambda item, where: identifier(item, where),
        )
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise ContractError("LineageRecord is immutable; use with_recorded_use / with_supports")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise ContractError("LineageRecord is immutable")

    @property
    def artifact_ref(self) -> TypedRef:
        return self._artifact_ref

    @property
    def was_used(self) -> tuple[EvidenceRef, ...]:
        return self._was_used

    @property
    def supports_for_use(self) -> tuple[str, ...]:
        return self._supports_for_use

    def with_recorded_use(self, ref: EvidenceRef) -> LineageRecord:
        """Append one more thing the artifact really read.  Reasons are untouched."""

        entry = _evidence_ref(ref, "lineage.was_used[]")
        if entry in self._was_used:
            return self
        return LineageRecord(self._artifact_ref, (*self._was_used, entry), self._supports_for_use)

    def with_supports(self, signatures: Sequence[str]) -> LineageRecord:
        """Re-choose the current reasons.  History is untouched."""

        return LineageRecord(self._artifact_ref, self._was_used, tuple(signatures))

    def cites(self, ref: EvidenceRef) -> bool:
        return ref in self._was_used

    def withdrawn_citations(self, admitted: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
        """Which historical citations are no longer admitted evidence."""

        live = frozenset(admitted)
        return tuple(ref for ref in self._was_used if ref not in live)

    def adopt_history_as_support(self) -> NoReturn:
        raise ContractError(
            "was_used is historical lineage, not a current reason; "
            "produce supports_for_use from a fresh validity check (AER §9.3)"
        )

    def rewrite_history_from_supports(self) -> NoReturn:
        raise ContractError(
            "supports_for_use must not rewrite was_used; the artifact really read "
            "what it read — revise the artifact instead (AER §9.3)"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_ref": self._artifact_ref.to_json(),
            "was_used": [ref.to_json() for ref in self._was_used],
            "supports_for_use": list(self._supports_for_use),
        }


def _evidence_ref(value: object, name: str) -> EvidenceRef:
    if isinstance(value, EvidenceRef):
        return value
    return EvidenceRef.from_json(value, name)


__all__ = (
    "CONFLICTING_EVIDENCE",
    "DEFAULT_NODE_BUDGET",
    "EVALUATION_INCOMPLETE",
    "HISTORICAL_PURPOSES",
    "MAX_INDEPENDENCE_PATHS",
    "REFUTED",
    "UNKNOWN_EVIDENCE",
    "UNVERIFIED_ASSUMPTION",
    "Anchor",
    "AnchorCandidate",
    "AnchorOrigin",
    "AnchorRejection",
    "AnchorSelection",
    "AnchorSelector",
    "Atom",
    "ClosureResult",
    "ClosureStatus",
    "ConsumerUse",
    "EvidencePremise",
    "JustificationSet",
    "LineageRecord",
    "Polarity",
    "Premise",
    "PropositionPremise",
    "RejectedAnchor",
    "RuleCondition",
    "SupportGraph",
    "SupportWitness",
    "WitnessKind",
    "grounded_closure",
    "parse_polarity",
    "parse_premise",
    "reevaluate_consumer",
)
