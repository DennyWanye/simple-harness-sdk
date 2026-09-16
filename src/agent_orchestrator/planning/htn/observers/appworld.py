# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read-only observers for the ``appworld`` pilot domain (§7.3 seed library, §6.6).

Five observers over the six predicates the seed ``appworld`` domain declares.  Each
one reads through the *host's* public-read surface —
:meth:`~....evaluation.appworld.AppWorldEpisode.observe_public_api` and the receipt
ledgers beside it — and never through the agent's Python shell: the existing
:data:`~....evaluation.appworld_api_observations.PUBLIC_READ_APIS` policy is an
immutable allowlist of GETs with the exact fields admitted into evidence, which is
precisely the "observers only read" property this package needs.

This file is a **skeleton**, in the sense §7.3 uses: the readers are complete and
the refusals are complete, and the client is injected.  Nothing here starts a
service, and no observer is registered anywhere — wiring these into the evidence
pipeline is the second half of P2.3c.

The distinction that carries the most weight is between *the app says no* and *we
could not ask*:

* an authorised read that comes back and reports the app does not serve this
  request is a negative **observation** (``polarity=False``);
* no client at all, a transport failure, a timeout, or a world whose identity moved
  under the read is :attr:`~.ObservationOutcome.OBSERVER_UNAVAILABLE` with **no**
  record — the service not being up is not evidence about the world (AER §8.2).

``appworld.action-confirmed`` is the domain's CLOSED predicate.  A receipt ledger
*enumerates* the confirmed actions of an episode, so its answer is a complete,
scoped query and a miss is an authoritative negative — the one shape §6.6 C28 lets a
closed domain conclude FALSE from.  A client that exposes no ledger cannot produce
that completeness claim, so it answers UNAVAILABLE rather than a cheap FALSE.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ....contracts.models import ContractError
from ....evaluation.appworld_api_observations import PUBLIC_READ_APIS
from ....knowledge.predicates import PredicateSignature
from . import Observation, denial, observed, unavailable

OBSERVER_VERSION = "appworld-observers-v1"

#: The read this domain's availability and credential observers use.  Both are in
#: the host's frozen ``PUBLIC_READ_APIS`` policy; an observer may not widen it.
ACTIVE_TASK_API = ("supervisor", "show_active_task")
PROFILE_API = ("supervisor", "show_profile")

#: What "the service is not answering" looks like from here.  ``OSError`` covers
#: ``urllib``'s ``URLError``.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError, TimeoutError, RuntimeError)

#: The host raises ``ValueError`` for several unrelated things, and the P2.3c review
#: found all of them being read as "the app said no": a malformed JSON envelope and a
#: world whose identity moved under the read both became a negative **observation**.
#: They are not.  Only a refusal whose text matches one of these is a statement about
#: the world; everything else is OBSERVER_UNAVAILABLE, because a reply we could not
#: parse is a reply we did not get (AER §8.2 dimension 4; §14.3 on lookup contracts).
REFUSAL_MARKERS: tuple[str, ...] = (
    "not authorized for observation",
    "not authorized",
    "api or parameters not authorized",
)

#: A ``ValueError`` whose text matches one of these is an *integrity* problem with
#: the answer, or with the world it came from — never a fact about the world.
INTEGRITY_MARKERS: tuple[str, ...] = (
    "world identity differs",
    "changed during observation",
    "exceeds the evidence limit",
    "exceeds limit",
    "unexpected fields",
    "unexpected field type",
    "not an allowed public read projection",
)


class ReadOutcome(StrEnum):
    """Why one host read produced no projection.  Three answers, never two."""

    #: The host answered, and said this app / API is not served.  A fact.
    REFUSED = "REFUSED"
    #: We could not get a usable answer: transport, parse, or a world that moved.
    UNAVAILABLE = "UNAVAILABLE"


def classify_read_error(error: ValueError) -> ReadOutcome:
    """Which of the two a host ``ValueError`` is (:data:`REFUSAL_MARKERS`).

    The default is ``UNAVAILABLE``: an error this module does not recognise is one it
    cannot interpret, and interpreting an unrecognised error as "the world says no"
    is exactly how a parse failure becomes a FALSE.  Integrity markers are checked
    first so a refusal-sounding phrase inside an integrity message cannot win.
    """

    text = str(error).lower()
    if any(marker in text for marker in INTEGRITY_MARKERS):
        return ReadOutcome.UNAVAILABLE
    if any(marker in text for marker in REFUSAL_MARKERS):
        return ReadOutcome.REFUSED
    return ReadOutcome.UNAVAILABLE


@runtime_checkable
class AppWorldReadOnlyClient(Protocol):
    """The read-only surface an observer needs.

    Structurally satisfied by :class:`~....evaluation.appworld.AppWorldEpisode`,
    which returns ``(receipt, projection)`` from a host-side GET.  The observers only
    ever call this method, so an object that offers it offers nothing else to them —
    in particular no ``execute`` and no checkpoint control.
    """

    def observe_public_api(
        self, app: str, api: str, *, parameters: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, Any]]: ...


@runtime_checkable
class EntityIndex(Protocol):
    """An optional read: how many records the deployment matches to one name.

    ``appworld.entity-ambiguous`` / ``appworld.entity-unique`` are about *cardinality*,
    which the frozen public-read allowlist cannot express.  A client that can answer
    it says so by offering this method; one that cannot leaves the two predicates
    UNAVAILABLE instead of guessing a cardinality from a projection.
    """

    def entity_matches(self, entity: str) -> int: ...


@runtime_checkable
class ReceiptLedger(Protocol):
    """An optional read: the complete, scoped set of confirmed action ids.

    This is what makes the CLOSED ``appworld.action-confirmed`` answerable at all —
    an enumeration with a scope.  ``scope`` names what was enumerated so the denial
    carries the coverage it claims.
    """

    def confirmed_action_ids(self) -> Sequence[str]: ...

    def receipt_scope(self) -> str: ...


@runtime_checkable
class ApiSurface(Protocol):
    """An optional read: which ``(app, api)`` pairs this deployment actually serves."""

    def public_read_apis(self) -> Sequence[tuple[str, str]]: ...


@dataclass(frozen=True, slots=True)
class _Read:
    """One host read's outcome: a projection, a refusal, or neither.

    ``refused`` and ``projection`` are mutually exclusive by construction, so a
    caller cannot read an integrity failure as a polarity by forgetting a branch.
    """

    projection: Mapping[str, Any] | None = None
    refused: bool = False
    problem: str = ""

    def __post_init__(self) -> None:
        if self.refused and self.projection is not None:
            raise ContractError("a refused read carries no projection")
        if self.projection is None and not self.problem:
            raise ContractError("a read with no projection says why")


@dataclass(frozen=True, slots=True)
class AppWorldObserverConfig:
    """The injected client, or ``None`` when no episode is running.

    ``None`` is a first-class configuration: it is what "the AppWorld service is not
    started" looks like, and every observer answers UNAVAILABLE for it.
    """

    client: AppWorldReadOnlyClient | None = None
    scope_id: str = "appworld-episode"

    def __post_init__(self) -> None:
        if self.client is not None and not isinstance(self.client, AppWorldReadOnlyClient):
            raise ContractError(
                "an appworld observer client must offer observe_public_api(app, api); the "
                "agent's Python shell is not an observation channel"
            )
        if not str(self.scope_id).strip():
            raise ContractError("an appworld observer needs a coverage scope id")


class _AppWorldObserver:
    """Shared plumbing: the id, the predicates, and the "no service" answer."""

    observer_name = ""
    predicates: tuple[str, ...] = ()

    def __init__(self, config: AppWorldObserverConfig) -> None:
        self._config = config

    @property
    def observer_id(self) -> str:
        return self.observer_name

    @property
    def client(self) -> AppWorldReadOnlyClient | None:
        return self._config.client

    def predicate_ids(self) -> tuple[str, ...]:
        return self.predicates

    def observe(
        self,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        predicate = signature.predicate_ref.id
        if predicate not in self.predicates:
            raise ContractError(
                f"{self.observer_id} does not observe {predicate!r}; it reads "
                f"{sorted(self.predicates)}"
            )
        if self.client is None:
            return self._no_service(predicate)
        return self._read(predicate, signature, arguments, now_ms=now_ms)

    def _no_service(self, predicate: str) -> Observation:
        return unavailable(
            self.observer_id,
            predicate,
            "no AppWorld episode is attached to this observer; a service that is not "
            "started says nothing about the world (AER §8.2)",
        )

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:  # pragma: no cover - every subclass overrides it
        raise NotImplementedError

    def _get(self, app: str, api: str) -> _Read:
        """One host-side public GET, classified into exactly one of three answers.

        The classification is the whole point.  Before the P2.3c review this method
        folded every ``ValueError`` into "refused", so a malformed envelope and a
        world whose identity moved under the read both came out as a negative
        observation — a FALSE manufactured out of an integrity failure.  Now a refusal
        is a refusal, and anything we could not read is UNAVAILABLE.
        """

        client = self.client
        if client is None:  # pragma: no cover - guarded by observe()
            return _Read(problem="no AppWorld episode is attached")
        try:
            _receipt, projection = client.observe_public_api(app, api)
        except TRANSPORT_ERRORS as error:
            return _Read(problem=f"the host read of {app}.{api} could not be completed: {error}")
        except ValueError as error:
            if classify_read_error(error) is ReadOutcome.REFUSED:
                return _Read(refused=True, problem=str(error))
            return _Read(
                problem=(
                    f"the host read of {app}.{api} did not produce a usable answer ({error}); "
                    "a reply we could not read is a reply we did not get"
                )
            )
        if not isinstance(projection, Mapping):
            return _Read(
                problem=(
                    f"the host read of {app}.{api} returned {type(projection).__name__} rather "
                    "than a field projection"
                )
            )
        return _Read(projection=dict(projection))


class AvailabilityObserver(_AppWorldObserver):
    """Does the named application answer requests?

    A read that comes back and is *refused* for this app is a negative observation —
    the app does not serve it.  A read that cannot be completed at all is
    UNAVAILABLE: those are two different facts and this observer keeps them apart.
    """

    observer_name = "appworld.availability-observer"
    predicates: tuple[str, ...] = ("appworld.app-reachable",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        app = str(arguments.get("app", ""))
        if not app.strip():
            return unavailable(self.observer_id, predicate, "the app argument names nothing")
        api = ACTIVE_TASK_API[1]
        read = self._get(app, api)
        if read.refused:
            return observed(
                signature,
                arguments,
                polarity=False,
                observer_id=self.observer_id,
                now_ms=now_ms,
                detail=f"the host refused the read of {app}.{api}: {read.problem}",
                observer_version=OBSERVER_VERSION,
            )
        if read.projection is None:
            return unavailable(self.observer_id, predicate, read.problem)
        return observed(
            signature,
            arguments,
            polarity=True,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=f"{app}.{api} answered with {len(read.projection)} admitted field(s)",
            observer_version=OBSERVER_VERSION,
        )


class CredentialObserver(_AppWorldObserver):
    """Are the stored credentials for the application accepted?

    The host's profile read is the cheapest authorised proof: it comes back with the
    fields the policy admits only when the call was authorised.  A refusal is a
    negative observation; an unreachable service is UNAVAILABLE.
    """

    observer_name = "appworld.credential-observer"
    predicates: tuple[str, ...] = ("appworld.credentials-valid",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        app = str(arguments.get("app", ""))
        if not app.strip():
            return unavailable(self.observer_id, predicate, "the app argument names nothing")
        read = self._get(app, PROFILE_API[1])
        if read.refused:
            return observed(
                signature,
                arguments,
                polarity=False,
                observer_id=self.observer_id,
                now_ms=now_ms,
                detail=f"the authorised profile read was refused: {read.problem}",
                observer_version=OBSERVER_VERSION,
            )
        if read.projection is None:
            return unavailable(self.observer_id, predicate, read.problem)
        accepted = any(str(value or "").strip() for value in read.projection.values())
        return observed(
            signature,
            arguments,
            polarity=accepted,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=(
                "the authorised profile read returned admitted fields"
                if accepted
                else "the authorised profile read returned an empty projection"
            ),
            observer_version=OBSERVER_VERSION,
        )


class ApiObserver(_AppWorldObserver):
    """Does the application's API cover the requested operation?

    Answered against the deployment's own declared surface when the client offers
    one, and otherwise against the host's frozen
    :data:`~....evaluation.appworld_api_observations.PUBLIC_READ_APIS` policy.  Either
    way it is a *declaration* being read, so this observer performs no call.
    """

    observer_name = "appworld.api-observer"
    predicates: tuple[str, ...] = ("appworld.api-supports-request",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        app = str(arguments.get("app", ""))
        if not app.strip():
            return unavailable(self.observer_id, predicate, "the app argument names nothing")
        client = self.client
        if isinstance(client, ApiSurface):
            surface = {(str(item[0]), str(item[1])) for item in client.public_read_apis()}
            source = "the deployment's declared read surface"
        else:
            surface = set(PUBLIC_READ_APIS)
            source = "the host's frozen PUBLIC_READ_APIS policy"
        covered = any(entry[0] == app for entry in surface)
        return observed(
            signature,
            arguments,
            polarity=covered,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=f"{source} {'covers' if covered else 'does not cover'} {app!r}",
            observer_version=OBSERVER_VERSION,
        )


class EntityObserver(_AppWorldObserver):
    """Does the named entity match exactly one record, or more than one?

    Both predicates come from one cardinality read, so they cannot contradict each
    other.  Without an :class:`EntityIndex` the observer has no way to count and says
    so: an unanswerable cardinality is UNAVAILABLE, never "unique".
    """

    observer_name = "appworld.entity-observer"
    predicates: tuple[str, ...] = ("appworld.entity-ambiguous", "appworld.entity-unique")

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        entity = str(arguments.get("entity", ""))
        if not entity.strip():
            return unavailable(self.observer_id, predicate, "the entity argument names nothing")
        client = self.client
        if not isinstance(client, EntityIndex):
            return unavailable(
                self.observer_id,
                predicate,
                "the attached episode offers no entity_matches() read, so how many records "
                "this name matches cannot be observed here",
            )
        try:
            matches = int(client.entity_matches(entity))
        except TRANSPORT_ERRORS as error:
            return unavailable(
                self.observer_id, predicate, f"the entity read could not be completed: {error}"
            )
        except ValueError as error:
            return unavailable(
                self.observer_id,
                predicate,
                f"the entity read did not produce a usable count ({error}); an unreadable "
                "answer is not a cardinality",
            )
        polarity = matches > 1 if predicate == "appworld.entity-ambiguous" else matches == 1
        return observed(
            signature,
            arguments,
            polarity=polarity,
            observer_id=self.observer_id,
            now_ms=now_ms,
            detail=f"{entity!r} matches {matches} record(s)",
            observer_version=OBSERVER_VERSION,
        )


class ReceiptObserver(_AppWorldObserver):
    """Does the submitted action have a confirming receipt?  (CLOSED, §6.6 C28)

    The receipt ledger enumerates an episode's confirmed actions, so a miss is an
    *authoritative negative* over that scope rather than an absence of information.
    A client without a ledger cannot make that completeness claim and therefore
    cannot deny: it answers UNAVAILABLE.
    """

    observer_name = "appworld.receipt-observer"
    predicates: tuple[str, ...] = ("appworld.action-confirmed",)

    def _read(
        self,
        predicate: str,
        signature: PredicateSignature,
        arguments: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> Observation:
        action = str(arguments.get("action", ""))
        if not action.strip():
            return unavailable(self.observer_id, predicate, "the action argument names nothing")
        client = self.client
        if not isinstance(client, ReceiptLedger):
            return unavailable(
                self.observer_id,
                predicate,
                "the attached episode offers no confirmed_action_ids() enumeration; without a "
                "complete, scoped query a closed domain answers UNKNOWN, not FALSE (§6.6 C28)",
            )
        try:
            confirmed = {str(item) for item in client.confirmed_action_ids()}
            scope = str(client.receipt_scope())
        except TRANSPORT_ERRORS as error:
            return unavailable(
                self.observer_id, predicate, f"the receipt ledger could not be read: {error}"
            )
        except ValueError as error:
            # An enumeration we could not parse is not a complete enumeration, and
            # only a complete one may deny a CLOSED predicate (§6.6 C28).
            return unavailable(
                self.observer_id,
                predicate,
                f"the receipt ledger did not produce a readable enumeration ({error}); "
                "without a complete, scoped query a closed domain answers UNKNOWN",
            )
        if not scope.strip():
            return unavailable(
                self.observer_id,
                predicate,
                "the receipt ledger named no coverage scope; a denial without a scope is "
                "not an authoritative negative observation (§6.6 C28)",
            )
        if action in confirmed:
            return observed(
                signature,
                arguments,
                polarity=True,
                observer_id=self.observer_id,
                now_ms=now_ms,
                detail=f"a confirming receipt for {action!r} is in the ledger",
                observer_version=OBSERVER_VERSION,
            )
        return denial(
            signature,
            arguments,
            observer_id=self.observer_id,
            now_ms=now_ms,
            coverage_scope=scope,
            detail=(
                f"the ledger enumerates {len(confirmed)} confirmed action(s) and {action!r} "
                "is not among them"
            ),
            observer_version=OBSERVER_VERSION,
        )


def appworld_observers(
    client: AppWorldReadOnlyClient | None = None,
    *,
    scope_id: str = "appworld-episode",
) -> tuple[_AppWorldObserver, ...]:
    """The five ``appworld`` observers over one episode, or over no episode at all."""

    config = AppWorldObserverConfig(client=client, scope_id=scope_id)
    return (
        AvailabilityObserver(config),
        CredentialObserver(config),
        ApiObserver(config),
        EntityObserver(config),
        ReceiptObserver(config),
    )


#: Predicate id → the observer ids that can read it, asserted against the seed
#: domain's ``predicates.json`` by the suite.
APPWORLD_OBSERVER_COVERAGE: Mapping[str, tuple[str, ...]] = {
    "appworld.app-reachable": ("appworld.availability-observer",),
    "appworld.credentials-valid": ("appworld.credential-observer",),
    "appworld.api-supports-request": ("appworld.api-observer",),
    "appworld.entity-ambiguous": ("appworld.entity-observer",),
    "appworld.entity-unique": ("appworld.entity-observer",),
    "appworld.action-confirmed": ("appworld.receipt-observer",),
}


__all__ = (
    "ACTIVE_TASK_API",
    "APPWORLD_OBSERVER_COVERAGE",
    "INTEGRITY_MARKERS",
    "OBSERVER_VERSION",
    "PROFILE_API",
    "REFUSAL_MARKERS",
    "TRANSPORT_ERRORS",
    "ReadOutcome",
    "classify_read_error",
    "ApiObserver",
    "ApiSurface",
    "AppWorldObserverConfig",
    "AppWorldReadOnlyClient",
    "AvailabilityObserver",
    "CredentialObserver",
    "EntityIndex",
    "EntityObserver",
    "ReceiptLedger",
    "ReceiptObserver",
    "appworld_observers",
)
