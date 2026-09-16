# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2b: refinement's evidence-gathering occurrences, actually executed.

``refinement.evidence_requests`` turns an UNKNOWN precondition into a *read-only
occurrence* and stops there: it says which observer type could answer, and P2.3c
part 2 left the execution unwired — so an UNKNOWN precondition stayed UNKNOWN
forever and the whole point of ADR-07 ("look, do not guess") was theory.  This
module is the execution, and it deliberately does **not** dispatch an agent:

* the occurrence's task type is a ``read_only`` observer type (``catalog.
  observers_for`` refuses any other), and the thing that can actually read is the
  :class:`~.observation_pipeline.ObserverIndex` this deployment assembled.  Running
  it here means the read happens through the one path that enforces §6.6 C28 on the
  way in, instead of through a Worker that could write;
* an observer that cannot answer produces
  :attr:`~...graph.eligibility.ReadinessReason.OBSERVER_UNAVAILABLE` and **no
  record**.  The proposition stays UNKNOWN and the refinement asks again later or
  gives up visibly — it never becomes FALSE because nobody looked.

The one thing this module has to reconstruct is the *arguments*: a
``proposition_key`` is a digest and an :class:`~.refinement.EvidenceOccurrenceRequest`
carries the key, not the grounded arguments.  :func:`pending_asks` therefore walks
the same conditions with the same ``ground_value`` the evaluator used, so the
arguments an observation is made with are the arguments the truth value was looked
up under — deriving them any other way would observe a *different* proposition and
record it under this one's key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...contracts.evidence_state import EvidenceSnapshot, TruthValue
from ...contracts.semantic_base import VersionedRef
from ...graph.eligibility import ReadinessReason
from ...knowledge.predicates import PredicateRegistry, proposition_key
from .applicability import ground_value
from .observation_pipeline import ObservationOutcomeRecord, ObserverIndex, observe_predicate
from .observation_pipeline import record_observation as _record
from .registry import iter_predicates


@dataclass(frozen=True, slots=True)
class EvidenceAsk:
    """One grounded proposition this round would like to look at."""

    predicate_ref: VersionedRef
    arguments: Mapping[str, Any] = field(default_factory=dict)
    proposition_key: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "predicate_ref": self.predicate_ref.to_json(),
            "arguments": dict(self.arguments),
            "proposition_key": self.proposition_key,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRoundResult:
    """What one round looked at, and what it learned.

    ``unavailable`` is kept apart from ``recorded`` because the two lead to
    different repairs: install or restore a reader, versus plan with the new fact.
    """

    outcomes: tuple[ObservationOutcomeRecord, ...] = ()
    unobservable: tuple[EvidenceAsk, ...] = ()

    @property
    def recorded(self) -> tuple[ObservationOutcomeRecord, ...]:
        return tuple(item for item in self.outcomes if item.recorded)

    @property
    def unavailable(self) -> tuple[ObservationOutcomeRecord, ...]:
        return tuple(
            item for item in self.outcomes if item.reason is ReadinessReason.OBSERVER_UNAVAILABLE
        )

    @property
    def learned_anything(self) -> bool:
        return bool(self.recorded)

    def to_json(self) -> dict[str, Any]:
        return {
            "outcomes": [item.to_json() for item in self.outcomes],
            "unobservable": [item.to_json() for item in self.unobservable],
        }


def pending_asks(
    conditions: Sequence[Any],
    *,
    parameters: Mapping[str, Any],
    registry: PredicateRegistry,
    snapshot: EvidenceSnapshot,
    now_ms: int | None = None,
) -> tuple[EvidenceAsk, ...]:
    """The grounded atoms of ``conditions`` that are UNKNOWN right now.

    Atom by atom, like :func:`~.refinement.unknown_predicates`, and for the same
    reason: the aggregate says the expression is unknown, and what a look needs is
    *which* proposition to look at.  An atom whose arguments do not ground or do not
    type-check is left out — that is a declaration error, not something an observer
    can fix by looking.
    """

    out: list[EvidenceAsk] = []
    seen: set[str] = set()
    for atom in iter_predicates(conditions):
        signature = registry.resolve(atom.predicate_ref)
        if signature is None:
            continue
        errors: list[str] = []
        arguments = {
            name: ground_value(item, parameters, path=f"precondition.{name}", errors=errors)
            for name, item in atom.arguments.items()
        }
        if errors or not registry.check_arguments(signature, arguments).ok:
            continue
        key = proposition_key(signature, arguments)
        if key in seen:
            continue
        from ...knowledge.predicates import atom_truth

        if atom_truth(signature, snapshot.lookup(key), now_ms=now_ms) is not TruthValue.UNKNOWN:
            continue
        seen.add(key)
        out.append(
            EvidenceAsk(predicate_ref=atom.predicate_ref, arguments=arguments, proposition_key=key)
        )
    return tuple(out)


def asks_for_requests(
    requests: Sequence[Any], asks: Sequence[EvidenceAsk]
) -> tuple[EvidenceAsk, ...]:
    """Keep only the asks refinement actually proposed an observer occurrence for.

    An :class:`~.refinement.EvidenceOccurrenceRequest` with ``satisfiable`` false
    names a proposition no registered read-only *task type* can produce.  Looking at
    it anyway through whatever observer happens to be indexed would route around the
    catalogue's declaration of who may observe what, which is the same authority
    ``build_index`` checks on the other side.
    """

    allowed = {
        str(item.proposition_key) for item in requests if getattr(item, "satisfiable", False)
    }
    return tuple(item for item in asks if item.proposition_key in allowed)


def run_round(
    index: ObserverIndex,
    store: Any,
    mission_id: str,
    asks: Sequence[EvidenceAsk],
    *,
    now_ms: int,
    scope_id: str = "mission",
) -> EvidenceRoundResult:
    """Look at every ask, record only what was observed, and report the rest.

    One unreadable proposition does not abandon the round: a planner is told "three
    of these four are settled and this one could not be asked", which is a different
    state from "the round failed" and is repaired differently.

    Review P2-19: an ask this deployment has no observer for is reported **once**.  It
    used to be appended to ``unobservable`` *and* put through ``observe_predicate``
    anyway, so the same proposition appeared twice in one result — once as "nobody can
    read this" and once as the ``NO_OBSERVER`` outcome that says the same thing — and
    a caller counting the round's answers counted it twice.  ``observe_predicate`` is
    still the only path to an actual reading; this only stops asking it a question
    whose answer we already have.
    """

    outcomes: list[ObservationOutcomeRecord] = []
    unobservable: list[EvidenceAsk] = []
    for ask in asks:
        if index.observer_for(str(ask.predicate_ref.id)) is None:
            unobservable.append(ask)
            continue
        outcome = observe_predicate(index, ask.predicate_ref, ask.arguments, now_ms=int(now_ms))
        outcomes.append(_record(store, mission_id, outcome, scope_id=scope_id))
    return EvidenceRoundResult(outcomes=tuple(outcomes), unobservable=tuple(unobservable))


__all__ = (
    "EvidenceAsk",
    "EvidenceRoundResult",
    "asks_for_requests",
    "pending_asks",
    "run_round",
)
