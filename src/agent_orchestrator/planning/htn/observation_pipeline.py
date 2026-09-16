# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 2: the evidence pipeline that connects observers to the store.

P2.3c part 1 built the observers and said, in so many words, what it did not do:
*"What this package does not do: register itself."*  This module is that half — the
index from predicate id to observer, the one call that runs an observation and
records it, and the mapping from "we could not ask" onto the readiness vocabulary.

Four rules, each of which is a way this could be written wrongly:

* **An unavailable observer writes nothing.**  ``OBSERVER_UNAVAILABLE`` produces no
  ``ObservationRecord``, because a record carries a polarity and a service that is
  down does not have one.  The caller is told
  :attr:`~...graph.eligibility.ReadinessReason.OBSERVER_UNAVAILABLE`, which is a
  different answer from ``WAITING_EVIDENCE`` and leads to a different repair (AER
  §8.2 dimension 4).
* **Only a registered predicate may be observed.**  The signature comes from the
  :class:`~...knowledge.predicates.PredicateRegistry`, not from the observer: an
  observer that could name its own predicate would be able to grant itself the
  ``observer_ids`` membership that makes a closed-world denial admissible (§6.6 C28).
* **One predicate, one observer, per index.**  Two observers offering the same
  predicate is refused when the index is built rather than resolved by order.  Two
  readers of one proposition is how a deployment ends up with a FALSE and a TRUE
  about the same fact and no rule saying which wins.
* **The pipeline does not decide.**  It records what was observed; combining
  observations into a truth value stays in ``knowledge.predicates`` under the §6.6
  priority table, and *this* module has no opinion about NOT, AND or CONFLICT.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...contracts.evidence_state import ObservationRecord
from ...contracts.models import ContractError
from ...contracts.semantic_base import VersionedRef
from ...graph.eligibility import ReadinessReason
from ...knowledge.predicates import PredicateRegistry, PredicateSignature
from .observers import Observation, ObservationOutcome, PredicateObserver, unavailable

#: What a caller is told when nobody in this deployment can read a predicate at all.
#: The same value an outage produces, deliberately: from the plan's point of view "no
#: observer is installed" and "the installed observer is down" are the same fact —
#: this deployment cannot currently answer — and both are repaired by installing or
#: restoring a reader, never by assuming the proposition is false.
NO_OBSERVER = "no_observer_installed"


@dataclass(frozen=True, slots=True)
class ObservationOutcomeRecord:
    """What one run of the pipeline produced.

    ``record`` is present exactly when ``observation.available``; ``reason`` is
    ``None`` exactly then too.  Carrying both rather than only one of them is what
    lets a caller record the evidence *and* report the readiness answer without
    re-deriving either from the other.
    """

    observation: Observation
    recorded: bool = False
    record: ObservationRecord | None = None
    reason: ReadinessReason | None = None

    @property
    def available(self) -> bool:
        return self.observation.available

    def to_json(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_json(),
            "recorded": self.recorded,
            "reason": None if self.reason is None else str(self.reason),
        }


@dataclass(frozen=True, slots=True)
class ObserverIndex:
    """Predicate id → the one observer that may read it.

    Built from ``predicate_ids()``, checked against the registry, and frozen.  The
    registry check is not decoration: a signature's ``observer_ids`` is what makes an
    authoritative negative observation admissible at all, so an observer indexed
    against a predicate the registry has never heard of could produce a denial that
    no signature ever authorised.
    """

    registry: PredicateRegistry
    by_predicate: Mapping[str, PredicateObserver] = field(default_factory=dict)

    def observer_for(self, predicate_id: str) -> PredicateObserver | None:
        return self.by_predicate.get(str(predicate_id))

    def predicate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_predicate))

    def to_json(self) -> dict[str, Any]:
        return {
            "predicates": {
                key: value.observer_id for key, value in sorted(self.by_predicate.items())
            }
        }


def build_index(
    registry: PredicateRegistry, observers: Iterable[PredicateObserver]
) -> ObserverIndex:
    """Index the deployment's observers, refusing anything ambiguous or unregistered.

    Both refusals are :class:`ContractError` at wiring time rather than a silent skip
    at read time, because a deployment that starts with a broken evidence pipeline
    will otherwise only find out when a Mission sits in ``OBSERVER_UNAVAILABLE`` and
    nobody can say whether that is an outage or a missing installation.
    """

    known = {signature.predicate_ref.id for signature in registry.signatures()}
    mapping: dict[str, PredicateObserver] = {}
    for observer in observers:
        for predicate_id in observer.predicate_ids():
            name = str(predicate_id)
            if name not in known:
                raise ContractError(
                    f"observer {observer.observer_id!r} offers predicate {name!r}, which this "
                    "PredicateRegistry does not hold; an observer may not introduce a "
                    "predicate, because the signature is what authorises a denial (§6.6 C28)"
                )
            if name in mapping and mapping[name].observer_id != observer.observer_id:
                raise ContractError(
                    f"predicate {name!r} is offered by two observers "
                    f"({mapping[name].observer_id!r} and {observer.observer_id!r}); two readers "
                    "of one proposition is two answers with no rule saying which wins"
                )
            mapping[name] = observer
    return ObserverIndex(registry=registry, by_predicate=mapping)


def observe_predicate(
    index: ObserverIndex,
    reference: VersionedRef,
    arguments: Mapping[str, Any],
    *,
    now_ms: int,
) -> ObservationOutcomeRecord:
    """Run one observation.  Reads the world; writes nothing.

    The signature is resolved through the registry at the exact content hash the
    caller named, so a predicate republished with different content is *not* read
    through an old reference — which is the same "re-read at the recorded version"
    rule the semantic read-set applies everywhere else.
    """

    signature = index.registry.resolve(reference)
    if signature is None:
        return ObservationOutcomeRecord(
            observation=unavailable(
                NO_OBSERVER,
                str(reference.id),
                f"predicate {reference.id!r} v{reference.version} is not registered at content "
                f"hash {reference.content_hash[:12]}",
            ),
            reason=ReadinessReason.OBSERVER_UNAVAILABLE,
        )
    observer = index.observer_for(str(reference.id))
    if observer is None:
        return ObservationOutcomeRecord(
            observation=unavailable(
                NO_OBSERVER,
                str(reference.id),
                f"this deployment installs no observer for predicate {reference.id!r}",
            ),
            reason=ReadinessReason.OBSERVER_UNAVAILABLE,
        )
    observation = _ask(observer, signature, arguments, now_ms=now_ms)
    if not observation.available:
        return ObservationOutcomeRecord(
            observation=observation, reason=ReadinessReason.OBSERVER_UNAVAILABLE
        )
    return ObservationOutcomeRecord(observation=observation, record=observation.record)


def record_observation(
    store: Any,
    mission_id: str,
    outcome: ObservationOutcomeRecord,
    *,
    scope_id: str = "mission",
) -> ObservationOutcomeRecord:
    """Persist an ``OBSERVED`` result, and persist **nothing** for any other.

    The asymmetry is the whole point of the function existing: a caller that wrote
    "whatever came back" would turn an outage into a stored FALSE the moment an
    observer decided to report one, and ``atom_truth`` would then honour it.
    """

    if outcome.record is None:
        return outcome
    store.insert_observation(mission_id, outcome.record, scope_id=scope_id)
    return ObservationOutcomeRecord(
        observation=outcome.observation,
        recorded=True,
        record=outcome.record,
        reason=None,
    )


def gather(
    index: ObserverIndex,
    store: Any,
    mission_id: str,
    requests: Sequence[tuple[VersionedRef, Mapping[str, Any]]],
    *,
    now_ms: int,
    scope_id: str = "mission",
) -> tuple[ObservationOutcomeRecord, ...]:
    """Run and record a batch of observations — the refinement's evidence round.

    One unavailable observer does not stop the others: the batch reports every answer
    it got, so a planner is told "three of these four are settled and this one could
    not be asked" rather than "the round failed".
    """

    results: list[ObservationOutcomeRecord] = []
    for reference, arguments in requests:
        outcome = observe_predicate(index, reference, arguments, now_ms=now_ms)
        results.append(record_observation(store, mission_id, outcome, scope_id=scope_id))
    return tuple(results)


def _ask(
    observer: PredicateObserver,
    signature: PredicateSignature,
    arguments: Mapping[str, Any],
    *,
    now_ms: int,
) -> Observation:
    """Call one observer, converting *any* escape into "could not ask".

    An observer is a reader of the outside world — a subprocess, an HTTP client, a
    file system — and the failure modes of those are open-ended.  A raised exception
    is "we could not ask", never a polarity: the alternative is that a crashing
    observer silently becomes evidence for whichever side the caller's default
    happens to be.  ``ContractError`` is caught with the rest deliberately, because
    an observation the construction rules refuse is precisely a reading that may not
    be recorded (§6.6 C28).
    """

    try:
        observation = observer.observe(signature, arguments, now_ms=int(now_ms))
    except Exception as error:  # noqa: BLE001 - see the docstring: any escape is an outage
        return unavailable(
            observer.observer_id,
            str(signature.predicate_ref.id),
            f"{type(error).__name__}: {error}",
        )
    if not isinstance(observation, Observation):
        return unavailable(
            observer.observer_id,
            str(signature.predicate_ref.id),
            f"observer returned {type(observation).__name__}, not an Observation",
        )
    if observation.outcome is ObservationOutcome.OBSERVED and observation.record is None:
        # Unreachable through ``Observation.__post_init__``; kept because the value
        # crosses a deployment-supplied boundary and a claimed OBSERVED with no record
        # is exactly the shape that would be read as a polarity nobody produced.
        return unavailable(
            observer.observer_id,
            str(signature.predicate_ref.id),
            "observer claimed OBSERVED with no ObservationRecord",
        )
    return observation


__all__ = (
    "NO_OBSERVER",
    "ObservationOutcomeRecord",
    "ObserverIndex",
    "build_index",
    "gather",
    "observe_predicate",
    "record_observation",
)
