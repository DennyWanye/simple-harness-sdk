# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Predicate observers: read the world, never change it (§5.3–5.4, §6.6, §7.3).

A predicate is *declared* data (``knowledge.predicates``); an **observer** is the
thing that can actually look.  This package is the minimum set §7.3 brings forward
from P6 into P2: at least five observers per pilot domain, each of which only reads.

Three properties every observer here has, and the reasons they are properties of
this module rather than of each implementation:

* **Three answers, never two.**  ``TRUE`` / ``FALSE`` / "I could not ask" are three
  different results.  A service that is not running produces
  :attr:`ObservationOutcome.OBSERVER_UNAVAILABLE` and **no**
  :class:`~...contracts.evidence_state.ObservationRecord` at all, because a record
  has a polarity and inventing one would be forging a FALSE out of an outage
  (AER §8.2 dimension 4).
* **A closed-world denial is admissible or it is not written.**  §6.6 C28 allows a
  CLOSED predicate to be concluded FALSE only from an *authoritative negative
  observation*: a complete, scoped, watermarked query by an observer the signature
  actually lists.  :func:`observed` refuses to build anything else, so an observer
  cannot emit a denial that :func:`~...knowledge.predicates.atom_truth` would then
  honour.  ``QueryCompleteness`` spells that completeness claim
  ``AUTHORITATIVE_WITH_SCOPE``; that is the only value that may back a denial, and
  there is deliberately no weaker "complete enough" one.
* **Only listed observers may speak.**  An observation whose ``observer_id`` is not
  in the signature's ``observer_ids`` is refused at construction rather than
  recorded and filtered later — "complete coverage" is otherwise a claim anybody
  could attach to any lookup.

What this package does *not* do: register itself.  Wiring these into a
``PredicateRegistry``-backed evidence pipeline is the second half of P2.3c; here
they are pure, injectable readers with no store and no global state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ....contracts.evidence_state import ObservationRecord, QueryCompleteness
from ....contracts.models import ContractError
from ....contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ....knowledge.predicates import (
    PredicateRegistry,
    PredicateSignature,
    WorldAssumption,
    authoritative_negative_matches_observer,
    proposition_key,
)

#: The completeness claim that may back a closed-world FALSE (§6.6 C28).  Named here
#: so a call site says *why* it passes this value rather than repeating the enum
#: member and leaving the reader to guess.
COMPLETE_COVERAGE = QueryCompleteness.AUTHORITATIVE_WITH_SCOPE


class ObservationOutcome(StrEnum):
    """Whether the observer answered at all.  Not the answer itself."""

    OBSERVED = "OBSERVED"
    #: The source could not be read.  Not the proposition being false.
    OBSERVER_UNAVAILABLE = "OBSERVER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Observation:
    """One observer's answer about one grounded proposition.

    ``record`` is present exactly when ``outcome`` is ``OBSERVED``; the invariant is
    enforced here so no caller has to remember it, and so an ``OBSERVER_UNAVAILABLE``
    result can never be read as a polarity.
    """

    outcome: ObservationOutcome
    observer_id: str
    predicate_id: str
    detail: str = ""
    record: ObservationRecord | None = None

    def __post_init__(self) -> None:
        if self.outcome is ObservationOutcome.OBSERVED:
            if not isinstance(self.record, ObservationRecord):
                raise ContractError("an OBSERVED observation carries an ObservationRecord")
        elif self.record is not None:
            raise ContractError(
                "an unavailable observer records no ObservationRecord; a record has a "
                "polarity, and an outage is not a polarity (AER §8.2)"
            )

    @property
    def available(self) -> bool:
        return self.outcome is ObservationOutcome.OBSERVED

    @property
    def polarity(self) -> bool | None:
        """The observed polarity, or ``None`` when nothing was observed."""

        return None if self.record is None else self.record.polarity

    @property
    def authoritative_negative(self) -> bool:
        return self.record is not None and self.record.is_authoritative_negative

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "observer_id": self.observer_id,
            "predicate_id": self.predicate_id,
            "detail": self.detail,
            "record": None if self.record is None else self.record.to_json(),
        }


@runtime_checkable
class PredicateObserver(Protocol):
    """What an observer is: an id, the predicates it can read, and a read.

    :meth:`observe` is *read-only* by contract.  It may run a query, a lookup or a
    read-only command; it may not write a file, move a branch, submit a form or
    record anything anywhere.  Recording is the caller's job, and keeping the two
    apart is what makes "the observation changed the world it observed" impossible
    to do by accident (§6.6).
    """

    @property
    def observer_id(self) -> str: ...

    def predicate_ids(self) -> tuple[str, ...]: ...

    def observe(
        self,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation: ...


def unavailable(observer_id: str, predicate_id: str, detail: str) -> Observation:
    """ "I could not ask" — the third answer, with no record and no polarity."""

    return Observation(
        outcome=ObservationOutcome.OBSERVER_UNAVAILABLE,
        observer_id=observer_id,
        predicate_id=predicate_id,
        detail=detail,
    )


def observation_source(observer_id: str, key: str, observer_version: str | None) -> TypedRef:
    """The observation's own reference, attributed to the tool that produced it.

    ``Provenance.TOOL`` is bound here by the *system side*: a model may only ever
    claim ``model`` / ``human`` for itself (``contracts.semantic_base``), so an
    attribution written here is exactly the signal
    ``verification.acceptance_rules.receipt_source`` treats as trustworthy.
    """

    digest = content_hash_of(
        {"observer_id": observer_id, "proposition": key, "observer_version": observer_version}
    )
    return TypedRef(
        kind=TypedRefKind.OBSERVATION,
        id=f"obs-{digest[:32]}",
        revision=1,
        content_hash=digest,
        produced_by=Provenance.TOOL,
    )


def observed(
    signature: PredicateSignature,
    arguments: Mapping[str, Any],
    *,
    polarity: bool,
    observer_id: str,
    now_ms: int,
    detail: str = "",
    coverage: QueryCompleteness = QueryCompleteness.BEST_EFFORT,
    coverage_scope: str | None = None,
    query_watermark_ms: int | None = None,
    observer_version: str | None = None,
) -> Observation:
    """Build one observation, refusing every inadmissible shape (§6.6 C28, §5.4).

    Two refusals, both at construction rather than downstream:

    * an ``observer_id`` the signature does not list may not observe this predicate
      at all — the identity check is what makes a coverage claim mean something;
    * a **negative** observation of a CLOSED predicate must be authoritative:
      ``AUTHORITATIVE_WITH_SCOPE`` coverage, an explicit scope and a watermark.
      Without them a closed domain still answers UNKNOWN, and an observer that
      built the record anyway would be doing negation-as-failure one layer down.
    """

    if observer_id not in signature.observer_ids:
        raise ContractError(
            f"observer {observer_id!r} is not listed by predicate "
            f"{signature.predicate_ref.id!r}; only a listed observer may observe it (§6.6 C28)"
        )
    problems = arguments_ok(signature, arguments)
    if problems:
        raise ContractError(
            f"the arguments do not type-check against predicate "
            f"{signature.predicate_ref.id!r}: {'; '.join(problems)}"
        )
    key = proposition_key(signature, dict(arguments))
    identity = content_hash_of({"key": key, "at": now_ms, "by": observer_id})
    record = ObservationRecord(
        observation_id=f"obsrec-{identity[:32]}",
        proposition_key=key,
        polarity=polarity,
        source_ref=observation_source(observer_id, key, observer_version),
        observed_at_ms=now_ms,
        recorded_at_ms=now_ms,
        coverage=coverage,
        coverage_scope=coverage_scope,
        query_watermark_ms=query_watermark_ms,
        observer_version=observer_version,
        observer_id=observer_id,
    )
    if (
        polarity is False
        and signature.world_assumption is WorldAssumption.CLOSED
        and not authoritative_negative_matches_observer(signature, record)
    ):
        raise ContractError(
            f"a negative observation of the CLOSED predicate {signature.predicate_ref.id!r} "
            "needs AUTHORITATIVE_WITH_SCOPE coverage, a coverage scope, a query watermark "
            "and a listed observer; without them absence is UNKNOWN, not FALSE (§6.6 C28)"
        )
    return Observation(
        outcome=ObservationOutcome.OBSERVED,
        observer_id=observer_id,
        predicate_id=signature.predicate_ref.id,
        detail=detail,
        record=record,
    )


def denial(
    signature: PredicateSignature,
    arguments: Mapping[str, Any],
    *,
    observer_id: str,
    now_ms: int,
    coverage_scope: str,
    detail: str = "",
    observer_version: str | None = None,
) -> Observation:
    """An authoritative negative observation: a complete, scoped, watermarked query.

    This is the only shape a CLOSED predicate's FALSE may be built from, and it is a
    separate function so that a call site has to *say* it enumerated the domain.
    """

    return observed(
        signature,
        arguments,
        polarity=False,
        observer_id=observer_id,
        now_ms=now_ms,
        detail=detail,
        coverage=COMPLETE_COVERAGE,
        coverage_scope=coverage_scope,
        query_watermark_ms=now_ms,
        observer_version=observer_version,
    )


def arguments_ok(signature: PredicateSignature, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Type-check one grounded argument set against the declaration; problems only.

    ``PredicateRegistry.check_arguments`` is a pure function of the signature it is
    handed, so a throwaway registry is the whole of what it needs — an observer does
    not have to be given the deployment's registry just to type-check its own call.
    """

    return PredicateRegistry().check_arguments(signature, dict(arguments)).messages()


__all__ = (
    "COMPLETE_COVERAGE",
    "Observation",
    "ObservationOutcome",
    "PredicateObserver",
    "arguments_ok",
    "denial",
    "observation_source",
    "observed",
    "unavailable",
)
