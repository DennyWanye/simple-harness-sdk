# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Four-valued evidence state, validity and precondition phases (§6.6, §11.5, AER §8).

ADR-07: an unknown premise is neither false nor true.  A proposition carries a
positive support count ``t`` and a negative support count ``f`` (AER §8.3); the
pair maps to TRUE / FALSE / UNKNOWN / CONFLICT.  Two properties of that mapping
are load-bearing and are pinned by tests:

* a counter-observation is never diluted by however many positive supports exist
  (``(3, 1)`` is CONFLICT, not TRUE);
* "no record found" produces no ``f`` at all — missing stays UNKNOWN.

``(t, f)`` is the merge representation for a *single* proposition only.  The
ALL / ANY / NOT combination of propositions is decided by the priority tables in
``planning.htn.applicability`` and never by ``(t, f)`` arithmetic (§6.6 v1.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import ContractError
from .semantic_base import (
    MAX_LIST,
    MAX_REASON,
    TypedRef,
    enum_of,
    fields_of,
    flag,
    identifier,
    index,
    optional_index,
    schema_version,
    sequence_of,
    text,
)

VALIDITY_WITNESS_SCHEMA_VERSION = 1


class TruthValue(StrEnum):
    """§6.6 / AER §8.2 dimension 1: what the currently admissible evidence supports."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class Validity(StrEnum):
    """AER §8.2 dimension 2: whether that support still satisfies the current policy."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    REVOKED = "REVOKED"


class Availability(StrEnum):
    """AER §8.2 dimension 4: readability for the acting subject.

    Not being allowed to read something is not the same as the proposition being
    false, so this never collapses into :class:`TruthValue`.
    """

    READABLE = "READABLE"
    REDACTED = "REDACTED"
    UNAVAILABLE = "UNAVAILABLE"


class PreconditionPhase(StrEnum):
    """TG §9 / decision 5: when a precondition has to hold."""

    SELECT = "SELECT"
    MAINTAIN = "MAINTAIN"
    ACCEPT = "ACCEPT"


#: TG §9 spells the first phase "SELECT/START"; AER §4.1 spells the same phase
#: "START".  Both decode to :attr:`PreconditionPhase.SELECT`.
_PHASE_ALIASES = {"START": PreconditionPhase.SELECT, "SELECT/START": PreconditionPhase.SELECT}

DEFAULT_PRECONDITION_PHASE = PreconditionPhase.SELECT


def parse_phase(value: object, name: str = "phase") -> PreconditionPhase:
    if isinstance(value, str) and value in _PHASE_ALIASES:
        return _PHASE_ALIASES[value]
    return enum_of(PreconditionPhase, value, name)


def phase_check_points(declared: PreconditionPhase | None) -> tuple[PreconditionPhase, ...]:
    """§6.6 v1.3: an undeclared phase defaults to SELECT *and* is re-checked at ACCEPT.

    An explicitly declared phase is checked at exactly that phase; only the
    default carries the extra ACCEPT check, so declaring ``SELECT`` is not a way
    to acquire the implicit re-check and declaring nothing is not a way to skip it.
    """

    if declared is None:
        return (PreconditionPhase.SELECT, PreconditionPhase.ACCEPT)
    return (declared,)


class TemporalUse(StrEnum):
    """AER §10.4 / §4.1: how a fact's time relates to the use being made of it."""

    HISTORICAL_AS_OF = "HISTORICAL_AS_OF"
    CURRENT_AT_USE = "CURRENT_AT_USE"
    CONTINUOUS = "CONTINUOUS"


class QueryCompleteness(StrEnum):
    """Coverage claim of a lookup (AER §14.3).

    Only ``AUTHORITATIVE_WITH_SCOPE`` may back a closed-world FALSE, and then only
    together with an explicit negative observation (§6.6 C28 — no negation as
    failure).
    """

    AUTHORITATIVE_WITH_SCOPE = "AUTHORITATIVE_WITH_SCOPE"
    BEST_EFFORT = "BEST_EFFORT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SupportCount:
    """AER §8.3: positive and negative support for one proposition in one scope."""

    t: int = 0
    f: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", index(self.t, "support.t"))
        object.__setattr__(self, "f", index(self.f, "support.f"))

    @property
    def truth(self) -> TruthValue:
        if self.t > 0 and self.f > 0:
            return TruthValue.CONFLICT
        if self.t > 0:
            return TruthValue.TRUE
        if self.f > 0:
            return TruthValue.FALSE
        return TruthValue.UNKNOWN

    def merge(self, other: SupportCount) -> SupportCount:
        """Accumulate independent supports.  A counter-observation is not outvoted."""

        return SupportCount(t=self.t + other.t, f=self.f + other.f)

    def negate(self) -> SupportCount:
        """AER §8.3: ``not`` swaps the polarities of a single proposition's support."""

        return SupportCount(t=self.f, f=self.t)

    def to_json(self) -> dict[str, Any]:
        return {"t": self.t, "f": self.f}

    @classmethod
    def from_json(cls, value: object, name: str = "support") -> SupportCount:
        data = fields_of(value, name, required=("t", "f"))
        return cls(t=data["t"], f=data["f"])


NO_SUPPORT = SupportCount(0, 0)
POSITIVE_SUPPORT = SupportCount(1, 0)
NEGATIVE_SUPPORT = SupportCount(0, 1)


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """AER §8.1: one real observation, with the coverage that lets it deny.

    ``query_watermark_ms`` and ``coverage`` exist so an *authoritative negative*
    observation can be told apart from "we looked and found nothing".  Only the
    former produces ``f = 1``.
    """

    observation_id: str
    proposition_key: str
    polarity: bool
    source_ref: TypedRef
    observed_at_ms: int
    recorded_at_ms: int
    coverage: QueryCompleteness = QueryCompleteness.BEST_EFFORT
    coverage_scope: str | None = None
    query_watermark_ms: int | None = None
    valid_from_ms: int | None = None
    valid_until_ms: int | None = None
    observer_version: str | None = None
    #: Which registered observer produced this record.  A closed-world denial has
    #: to name one that the predicate's signature actually lists (§6.6 C28); the
    #: identity check itself lives in ``knowledge.predicates`` beside the registry.
    observer_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "observation_id", identifier(self.observation_id, "observation.observation_id")
        )
        object.__setattr__(
            self,
            "proposition_key",
            identifier(self.proposition_key, "observation.proposition_key"),
        )
        object.__setattr__(self, "polarity", flag(self.polarity, "observation.polarity"))
        if not isinstance(self.source_ref, TypedRef):
            raise ContractError("observation.source_ref must be a TypedRef")
        object.__setattr__(
            self, "observed_at_ms", index(self.observed_at_ms, "observation.observed_at_ms")
        )
        object.__setattr__(
            self, "recorded_at_ms", index(self.recorded_at_ms, "observation.recorded_at_ms")
        )
        object.__setattr__(
            self, "coverage", enum_of(QueryCompleteness, self.coverage, "observation.coverage")
        )
        if self.coverage_scope is not None:
            object.__setattr__(
                self,
                "coverage_scope",
                identifier(self.coverage_scope, "observation.coverage_scope"),
            )
        object.__setattr__(
            self,
            "query_watermark_ms",
            optional_index(self.query_watermark_ms, "observation.query_watermark_ms"),
        )
        object.__setattr__(
            self, "valid_from_ms", optional_index(self.valid_from_ms, "observation.valid_from_ms")
        )
        object.__setattr__(
            self,
            "valid_until_ms",
            optional_index(self.valid_until_ms, "observation.valid_until_ms"),
        )
        if self.valid_until_ms is not None and self.valid_from_ms is not None:
            if self.valid_until_ms < self.valid_from_ms:
                raise ContractError("observation.valid_until_ms precedes valid_from_ms")
        if self.observer_version is not None:
            object.__setattr__(
                self,
                "observer_version",
                identifier(self.observer_version, "observation.observer_version"),
            )
        if self.observer_id is not None:
            object.__setattr__(
                self, "observer_id", identifier(self.observer_id, "observation.observer_id")
            )

    @property
    def is_authoritative_negative(self) -> bool:
        """§6.6 C28: a denial needs an authoritative, scoped, watermarked query."""

        return (
            self.polarity is False
            and self.coverage is QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
            and self.query_watermark_ms is not None
            and self.coverage_scope is not None
        )

    @property
    def support(self) -> SupportCount:
        if self.polarity:
            return POSITIVE_SUPPORT
        return NEGATIVE_SUPPORT if self.is_authoritative_negative else NO_SUPPORT

    def to_json(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "proposition_key": self.proposition_key,
            "polarity": self.polarity,
            "source_ref": self.source_ref.to_json(),
            "observed_at_ms": self.observed_at_ms,
            "recorded_at_ms": self.recorded_at_ms,
            "coverage": str(self.coverage),
            "coverage_scope": self.coverage_scope,
            "query_watermark_ms": self.query_watermark_ms,
            "valid_from_ms": self.valid_from_ms,
            "valid_until_ms": self.valid_until_ms,
            "observer_version": self.observer_version,
            "observer_id": self.observer_id,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "observation") -> ObservationRecord:
        data = fields_of(
            value,
            name,
            required=(
                "observation_id",
                "proposition_key",
                "polarity",
                "source_ref",
                "observed_at_ms",
                "recorded_at_ms",
            ),
            optional=(
                "coverage",
                "coverage_scope",
                "query_watermark_ms",
                "valid_from_ms",
                "valid_until_ms",
                "observer_version",
                "observer_id",
            ),
        )
        return cls(
            observation_id=data["observation_id"],
            proposition_key=data["proposition_key"],
            polarity=data["polarity"],
            source_ref=TypedRef.from_json(data["source_ref"], f"{name}.source_ref"),
            observed_at_ms=data["observed_at_ms"],
            recorded_at_ms=data["recorded_at_ms"],
            coverage=data.get("coverage", QueryCompleteness.BEST_EFFORT),
            coverage_scope=data.get("coverage_scope"),
            query_watermark_ms=data.get("query_watermark_ms"),
            valid_from_ms=data.get("valid_from_ms"),
            valid_until_ms=data.get("valid_until_ms"),
            observer_version=data.get("observer_version"),
            observer_id=data.get("observer_id"),
        )


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """The merged state of one proposition inside an :class:`EvidenceSnapshot`."""

    proposition_key: str
    support: SupportCount
    validity: Validity = Validity.CURRENT
    availability: Availability = Availability.READABLE
    observation_refs: tuple[TypedRef, ...] = ()
    authoritative_negative: bool = False
    not_after_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "proposition_key", identifier(self.proposition_key, "entry.proposition_key")
        )
        if not isinstance(self.support, SupportCount):
            raise ContractError("entry.support must be a SupportCount")
        object.__setattr__(self, "validity", enum_of(Validity, self.validity, "entry.validity"))
        object.__setattr__(
            self, "availability", enum_of(Availability, self.availability, "entry.availability")
        )
        refs = sequence_of(
            self.observation_refs,
            "entry.observation_refs",
            lambda item, where: (
                item if isinstance(item, TypedRef) else TypedRef.from_json(item, where)
            ),
        )
        object.__setattr__(self, "observation_refs", refs)
        object.__setattr__(
            self,
            "authoritative_negative",
            flag(self.authoritative_negative, "entry.authoritative_negative"),
        )
        object.__setattr__(
            self, "not_after_ms", optional_index(self.not_after_ms, "entry.not_after_ms")
        )
        if self.support.f > 0 and not self.authoritative_negative and not self.observation_refs:
            raise ContractError(
                "entry.support.f requires a recorded counter-observation; "
                "a missing record never produces negative support"
            )

    @classmethod
    def from_observations(
        cls,
        proposition_key: str,
        observations: tuple[ObservationRecord, ...],
        *,
        validity: Validity = Validity.CURRENT,
        availability: Availability = Availability.READABLE,
        not_after_ms: int | None = None,
    ) -> EvidenceEntry:
        support = NO_SUPPORT
        authoritative_negative = False
        for observation in observations:
            if observation.proposition_key != proposition_key:
                raise ContractError("observation belongs to a different proposition")
            support = support.merge(observation.support)
            authoritative_negative = authoritative_negative or observation.is_authoritative_negative
        return cls(
            proposition_key=proposition_key,
            support=support,
            validity=validity,
            availability=availability,
            observation_refs=tuple(observation.source_ref for observation in observations),
            authoritative_negative=authoritative_negative,
            not_after_ms=not_after_ms,
        )

    def truth(self, *, now_ms: int | None = None) -> TruthValue:
        """The usable conclusion.  Expired or revoked support degrades to UNKNOWN.

        ADR-07 and I19: a fact whose validity is no longer CURRENT is not turned
        into its negation, and it is not silently reused either.
        """

        if self.validity is not Validity.CURRENT:
            return TruthValue.UNKNOWN
        if self.availability is not Availability.READABLE:
            return TruthValue.UNKNOWN
        if now_ms is not None and self.not_after_ms is not None and now_ms > self.not_after_ms:
            return TruthValue.UNKNOWN
        return self.support.truth

    def to_json(self) -> dict[str, Any]:
        return {
            "proposition_key": self.proposition_key,
            "support": self.support.to_json(),
            "validity": str(self.validity),
            "availability": str(self.availability),
            "observation_refs": [ref.to_json() for ref in self.observation_refs],
            "authoritative_negative": self.authoritative_negative,
            "not_after_ms": self.not_after_ms,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "entry") -> EvidenceEntry:
        data = fields_of(
            value,
            name,
            required=("proposition_key", "support"),
            optional=(
                "validity",
                "availability",
                "observation_refs",
                "authoritative_negative",
                "not_after_ms",
            ),
        )
        return cls(
            proposition_key=data["proposition_key"],
            support=SupportCount.from_json(data["support"], f"{name}.support"),
            validity=data.get("validity", Validity.CURRENT),
            availability=data.get("availability", Availability.READABLE),
            observation_refs=tuple(data.get("observation_refs", ())),
            authoritative_negative=data.get("authoritative_negative", False),
            not_after_ms=data.get("not_after_ms"),
        )


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """A read-only world snapshot bound to one scope epoch and support revision.

    Planning reads this; it never reads the live store.  A proposition that is
    absent from ``entries`` is UNKNOWN — the snapshot does not know whether it was
    never asked or asked and unanswered, and neither reading grants a FALSE.
    """

    snapshot_id: str
    as_of_ms: int
    scope_id: str
    scope_epoch: int
    support_revision: int
    entries: tuple[EvidenceEntry, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "snapshot_id", identifier(self.snapshot_id, "snapshot.snapshot_id")
        )
        object.__setattr__(self, "as_of_ms", index(self.as_of_ms, "snapshot.as_of_ms"))
        object.__setattr__(self, "scope_id", identifier(self.scope_id, "snapshot.scope_id"))
        object.__setattr__(self, "scope_epoch", index(self.scope_epoch, "snapshot.scope_epoch"))
        object.__setattr__(
            self, "support_revision", index(self.support_revision, "snapshot.support_revision")
        )
        entries = sequence_of(
            self.entries,
            "snapshot.entries",
            lambda item, where: (
                item if isinstance(item, EvidenceEntry) else EvidenceEntry.from_json(item, where)
            ),
            limit=4 * MAX_LIST,
        )
        keys = [entry.proposition_key for entry in entries]
        if len(set(keys)) != len(keys):
            raise ContractError("snapshot.entries must not repeat a proposition key")
        object.__setattr__(self, "entries", entries)

    def lookup(self, proposition_key: str) -> EvidenceEntry | None:
        for entry in self.entries:
            if entry.proposition_key == proposition_key:
                return entry
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of_ms": self.as_of_ms,
            "scope_id": self.scope_id,
            "scope_epoch": self.scope_epoch,
            "support_revision": self.support_revision,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "snapshot") -> EvidenceSnapshot:
        data = fields_of(
            value,
            name,
            required=(
                "snapshot_id",
                "as_of_ms",
                "scope_id",
                "scope_epoch",
                "support_revision",
            ),
            optional=("entries",),
        )
        return cls(
            snapshot_id=data["snapshot_id"],
            as_of_ms=data["as_of_ms"],
            scope_id=data["scope_id"],
            scope_epoch=data["scope_epoch"],
            support_revision=data["support_revision"],
            entries=tuple(
                EvidenceEntry.from_json(item, f"{name}.entries[]")
                for item in data.get("entries", ())
            ),
        )


class WitnessPurpose(StrEnum):
    """§11.5 / ``validity-witness.schema.json``: what the evidence is used *for*.

    §11.5 prose names PLAN/START/MAINTAIN/ACCEPT/DISCLOSE; the annex schema adds
    CONTEXT and RECOVERY.  The schema is the wire contract, so all seven decode.
    """

    PLAN = "PLAN"
    START = "START"
    MAINTAIN = "MAINTAIN"
    ACCEPT = "ACCEPT"
    CONTEXT = "CONTEXT"
    DISCLOSE = "DISCLOSE"
    RECOVERY = "RECOVERY"


class WitnessDecision(StrEnum):
    USABLE = "USABLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class RecheckOutcome(StrEnum):
    """§11.5: the five outcomes of re-evaluating a witness after an epoch bump."""

    UNCHANGED = "UNCHANGED"
    REBOUND_SUPPORT = "REBOUND_SUPPORT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ValidityWitness:
    """§11.5 / AER §8.1: permission to use one conclusion, once, for one purpose.

    It is not a token: a consumer must compare ``scope_epoch`` against the current
    scope epoch and ``not_after_ms`` against now before reusing a cached TRUE
    (invariant I19).  Field names and enums follow
    ``annex/aer-1.0/schemas/validity-witness.schema.json`` exactly.
    """

    witness_id: str
    consumer_ref: TypedRef
    purpose: WitnessPurpose
    truth: TruthValue
    freshness: Validity
    availability: Availability
    decision: WitnessDecision
    scope_id: str
    scope_epoch: int
    support_revision: int
    as_of_ms: int
    not_after_ms: int | None = None
    support_refs: tuple[TypedRef, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "witness_id", identifier(self.witness_id, "witness.witness_id"))
        if not isinstance(self.consumer_ref, TypedRef):
            raise ContractError("witness.consumer_ref must be a TypedRef")
        object.__setattr__(
            self, "purpose", enum_of(WitnessPurpose, self.purpose, "witness.purpose")
        )
        object.__setattr__(self, "truth", enum_of(TruthValue, self.truth, "witness.truth"))
        object.__setattr__(
            self, "freshness", enum_of(Validity, self.freshness, "witness.freshness")
        )
        object.__setattr__(
            self, "availability", enum_of(Availability, self.availability, "witness.availability")
        )
        object.__setattr__(
            self, "decision", enum_of(WitnessDecision, self.decision, "witness.decision")
        )
        object.__setattr__(self, "scope_id", identifier(self.scope_id, "witness.scope_id"))
        object.__setattr__(self, "scope_epoch", index(self.scope_epoch, "witness.scope_epoch"))
        object.__setattr__(
            self, "support_revision", index(self.support_revision, "witness.support_revision")
        )
        object.__setattr__(self, "as_of_ms", index(self.as_of_ms, "witness.as_of_ms"))
        object.__setattr__(
            self, "not_after_ms", optional_index(self.not_after_ms, "witness.not_after_ms")
        )
        object.__setattr__(
            self,
            "support_refs",
            sequence_of(
                self.support_refs,
                "witness.support_refs",
                lambda item, where: (
                    item if isinstance(item, TypedRef) else TypedRef.from_json(item, where)
                ),
            ),
        )
        object.__setattr__(
            self,
            "reason_codes",
            sequence_of(
                self.reason_codes,
                "witness.reason_codes",
                lambda item, where: text(item, where, limit=512),
            ),
        )
        if self.decision is WitnessDecision.USABLE:
            # AER §8.2 / reference ``usable_for_execution``: usable means TRUE *and*
            # current *and* readable.  A revoked support or an unreadable source is
            # not a weaker yes; it is not a yes at all (invariants I18, I19).
            if self.truth is not TruthValue.TRUE:
                raise ContractError("witness.decision USABLE requires truth TRUE (I18)")
            if self.freshness is not Validity.CURRENT:
                raise ContractError(
                    f"witness.decision USABLE requires freshness CURRENT, not {self.freshness!s}"
                )
            if self.availability is not Availability.READABLE:
                raise ContractError(
                    "witness.decision USABLE requires availability READABLE, "
                    f"not {self.availability!s}"
                )

    def is_fresh_for(self, *, now_ms: int, current_scope_epoch: int) -> bool:
        """I19: a cached witness is unusable once its scope epoch or deadline moved."""

        if self.scope_epoch != current_scope_epoch:
            return False
        if self.not_after_ms is not None and now_ms >= self.not_after_ms:
            # The deadline is exclusive, matching the annex reference's
            # ``now_ms < not_after_ms``: at the instant it expires it is expired.
            return False
        return self.freshness is Validity.CURRENT

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": VALIDITY_WITNESS_SCHEMA_VERSION,
            "witness_id": self.witness_id,
            "consumer_ref": self.consumer_ref.to_json(),
            "purpose": str(self.purpose),
            "truth": str(self.truth),
            "freshness": str(self.freshness),
            "availability": str(self.availability),
            "decision": str(self.decision),
            "scope_id": self.scope_id,
            "scope_epoch": self.scope_epoch,
            "support_revision": self.support_revision,
            "as_of_ms": self.as_of_ms,
            "not_after_ms": self.not_after_ms,
            "support_refs": [ref.to_json() for ref in self.support_refs],
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "validity_witness") -> ValidityWitness:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "witness_id",
                "consumer_ref",
                "purpose",
                "truth",
                "freshness",
                "availability",
                "decision",
                "scope_id",
                "scope_epoch",
                "support_revision",
                "as_of_ms",
                "not_after_ms",
                "support_refs",
                "reason_codes",
            ),
        )
        schema_version(
            data["schema_version"],
            f"{name}.schema_version",
            expected=VALIDITY_WITNESS_SCHEMA_VERSION,
        )
        return cls(
            witness_id=data["witness_id"],
            consumer_ref=TypedRef.from_json(data["consumer_ref"], f"{name}.consumer_ref"),
            purpose=data["purpose"],
            truth=data["truth"],
            freshness=data["freshness"],
            availability=data["availability"],
            decision=data["decision"],
            scope_id=data["scope_id"],
            scope_epoch=data["scope_epoch"],
            support_revision=data["support_revision"],
            as_of_ms=data["as_of_ms"],
            not_after_ms=data["not_after_ms"],
            support_refs=tuple(
                TypedRef.from_json(item, f"{name}.support_refs[]")
                for item in _as_list(data["support_refs"], f"{name}.support_refs")
            ),
            reason_codes=tuple(_as_list(data["reason_codes"], f"{name}.reason_codes")),
        )


def _as_list(value: object, name: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a list")
    return list(value)


@dataclass(frozen=True, slots=True)
class PreconditionWitnessRecord:
    """The snapshot of one precondition's truth taken when a method was selected.

    §6.6 rule 3: if the pre-dispatch re-check disagrees with this record, the
    *method instance* is invalidated — that is a return code, not a CONFLICT
    truth value and not an automatic global re-plan.
    """

    condition_digest: str
    phase: PreconditionPhase
    truth: TruthValue
    witness_ref: TypedRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "condition_digest", identifier(self.condition_digest, "witness.condition_digest")
        )
        object.__setattr__(self, "phase", parse_phase(self.phase, "witness.phase"))
        object.__setattr__(self, "truth", enum_of(TruthValue, self.truth, "witness.truth"))
        if self.witness_ref is not None and not isinstance(self.witness_ref, TypedRef):
            raise ContractError("witness.witness_ref must be a TypedRef or null")

    def to_json(self) -> dict[str, Any]:
        return {
            "condition_digest": self.condition_digest,
            "phase": str(self.phase),
            "truth": str(self.truth),
            "witness_ref": None if self.witness_ref is None else self.witness_ref.to_json(),
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "precondition_witness"
    ) -> PreconditionWitnessRecord:
        data = fields_of(
            value,
            name,
            required=("condition_digest", "phase", "truth"),
            optional=("witness_ref",),
        )
        raw_ref = data.get("witness_ref")
        return cls(
            condition_digest=data["condition_digest"],
            phase=data["phase"],
            truth=data["truth"],
            witness_ref=None if raw_ref is None else TypedRef.from_json(raw_ref, f"{name}.ref"),
        )


def merge_observations(observations: tuple[ObservationRecord, ...]) -> SupportCount:
    """Fold independent observations of one proposition into a single ``(t, f)``."""

    keys = {observation.proposition_key for observation in observations}
    if len(keys) > 1:
        raise ContractError("merge_observations needs a single proposition key")
    support = NO_SUPPORT
    for observation in observations:
        support = support.merge(observation.support)
    return support


def limitation_text(value: object, name: str) -> str:
    return text(value, name, limit=MAX_REASON)


__all__ = (
    "DEFAULT_PRECONDITION_PHASE",
    "NEGATIVE_SUPPORT",
    "NO_SUPPORT",
    "POSITIVE_SUPPORT",
    "VALIDITY_WITNESS_SCHEMA_VERSION",
    "Availability",
    "EvidenceEntry",
    "EvidenceSnapshot",
    "ObservationRecord",
    "PreconditionPhase",
    "PreconditionWitnessRecord",
    "QueryCompleteness",
    "RecheckOutcome",
    "SupportCount",
    "TemporalUse",
    "TruthValue",
    "Validity",
    "ValidityWitness",
    "WitnessDecision",
    "WitnessPurpose",
    "limitation_text",
    "merge_observations",
    "parse_phase",
    "phase_check_points",
)
