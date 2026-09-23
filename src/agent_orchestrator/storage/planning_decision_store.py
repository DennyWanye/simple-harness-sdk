# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The durable identity of a planning request and its decisions (V2 §8.2, §34–§36).

This module is the *only* writer of ``mission_planning_protocols``,
``planning_requests`` and ``planning_decisions``.  It composes an existing
:class:`~agent_orchestrator.storage.store.Store` and runs inside that store's
``transaction()``, so a request binding, its decision row and whatever the commit
service writes beside them either all land or all roll back.  It never opens or
holds a connection of its own.

Three load-bearing rules:

* **Absent means legacy.**  ``get_mission_protocol`` returns ``None`` when a
  Mission has no binding row.  The mode is never guessed from the environment or
  reconstructed during recovery (§8.2); it is read from the library or not at all.
* **Identity is the raw output.**  A decision is keyed on ``(request_id,
  attempt_ordinal)``.  Replaying the same raw output returns the same row; the same
  ordinal with different bytes is a ``StoreConflict``, never a silent overwrite
  (§35).  A raw ``sqlite3.IntegrityError`` never reaches the caller in its place.
* **Status only moves forward.**  The rank of a status is its position in the §36
  list, and a status that ends an attempt (``UNREADABLE``, ``REJECTED``,
  ``COMMIT_REJECTED``, ``COMMITTED``, ``NO_STATE_CHANGE``) is absorbed: nothing may
  follow it and nothing may leave it (§36).

Everything is validated *before* the statement runs, so a refused call leaves the
row byte for byte as it was.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from simple_harness.contracts import canonical_json as _canonical_json

from ..contracts.models import ContractError
from ..contracts.planning_decisions import (
    PlanningDecisionRejectionCode,
    PlanningDecisionStatus,
    PlanningRequestBinding,
)
from ..contracts.semantic_base import enum_of, hash_hex, identifier, index, json_object, sequence_of
from .store import Store, StoreConflict

#: §36, in the order the plan lists the eight values.  That order is the progression:
#: a later value is progress, an earlier one is a rollback.
STATUS_ORDER: tuple[PlanningDecisionStatus, ...] = (
    PlanningDecisionStatus.UNREADABLE,
    PlanningDecisionStatus.DECODED,
    PlanningDecisionStatus.REJECTED,
    PlanningDecisionStatus.ADMITTED,
    PlanningDecisionStatus.COMPILED,
    PlanningDecisionStatus.COMMIT_REJECTED,
    PlanningDecisionStatus.COMMITTED,
    PlanningDecisionStatus.NO_STATE_CHANGE,
)

#: §36: the statuses that end one attempt's life.  Nothing follows them.
TERMINAL_STATUSES: frozenset[PlanningDecisionStatus] = frozenset(
    {
        PlanningDecisionStatus.UNREADABLE,
        PlanningDecisionStatus.REJECTED,
        PlanningDecisionStatus.COMMIT_REJECTED,
        PlanningDecisionStatus.COMMITTED,
        PlanningDecisionStatus.NO_STATE_CHANGE,
    }
)

_REQUEST_FIELDS = (
    "request_id",
    "mission_id",
    "protocol_version",
    "package_version",
    "package_hash",
    "base_plan_revision",
    "requirements_revision",
    "scope_epoch_digest",
    "subject_bindings_hash",
    "visible_refs_digest",
    "prompt_version",
    "prompt_hash",
    "intent_id",
    "created_at",
)

_DECISION_COLUMNS = (
    "decision_id",
    "request_id",
    "attempt_ordinal",
    "raw_output_hash",
    "raw_artifact_ref",
    "canonical_json",
    "canonical_hash",
    "decision_type",
    "status",
    "rejection_codes_json",
    "detail_json",
    "created_at",
)


def _status(value: object) -> PlanningDecisionStatus:
    return enum_of(PlanningDecisionStatus, value, "planning_decision.status")


def _rejection_codes(value: object) -> tuple[PlanningDecisionRejectionCode, ...]:
    return sequence_of(
        value,
        "planning_decision.rejection_codes",
        lambda entry, where: enum_of(PlanningDecisionRejectionCode, entry, where),
        limit=len(PlanningDecisionRejectionCode),
    )


def _expected_intent(value: object) -> str:
    """The opener's intent id, as the caller read it from the row it is re-answering."""

    return identifier(value, "planning_requests.expected_opener_intent_id")


def _decision_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The stored row as a plain document: the JSON columns and the enum decoded."""

    document: dict[str, Any] = {
        column: row[column]
        for column in _DECISION_COLUMNS
        if column not in ("rejection_codes_json", "detail_json")
    }
    document["rejection_codes"] = json.loads(row["rejection_codes_json"])
    document["detail"] = json.loads(row["detail_json"])
    document["status"] = str(row["status"])
    return document


class PlanningDecisionStore:
    """Reads and writes the planning-decision tables of one orchestrator library."""

    #: Addendum §5.2: one request is asked again for an unreadable reply exactly once.
    #: This is the highest attempt ordinal that re-ask may leave behind — the opener is
    #: attempt 0, the one re-ask is attempt 1 — so a decision at or past it means the
    #: request's ladder is spent and its answering intent is frozen.  It is the store's
    #: own reader of §36 for ``planning_requests``: the refusal lives beside the rows it
    #: closes rather than in whichever caller remembers to check.
    MAX_SAME_REQUEST_FORMAT_RETRIES = 1

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ mission protocol
    def bind_mission_protocol(
        self,
        mission_id: str,
        *,
        protocol_version: str,
        package_version: int,
        prompt_version: str,
        binding_hash: str,
    ) -> None:
        """Record which wire a Mission speaks, inside the caller's transaction.

        The first binding wins.  Repeating the identical binding is a no-op; any
        difference is a ``StoreConflict`` because a Mission may not switch protocol
        after it was created (§8.2).
        """

        mission = identifier(mission_id, "mission_planning_protocols.mission_id")
        protocol = identifier(protocol_version, "mission_planning_protocols.protocol_version")
        package = index(package_version, "mission_planning_protocols.package_version", minimum=1)
        prompt = identifier(prompt_version, "mission_planning_protocols.prompt_version")
        digest = hash_hex(binding_hash, "mission_planning_protocols.binding_hash")
        now = self._store.now
        with self._store.transaction() as connection:
            existing = connection.execute(
                "SELECT protocol_version,package_version,prompt_version,binding_hash"
                " FROM mission_planning_protocols WHERE mission_id = ?",
                (mission,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["protocol_version"],
                    existing["package_version"],
                    existing["prompt_version"],
                    existing["binding_hash"],
                ) == (protocol, package, prompt, digest)
                if same:
                    return
                raise StoreConflict(
                    f"mission {mission} is already bound to protocol"
                    f" {existing['protocol_version']!r}/package {existing['package_version']};"
                    f" a Mission may not switch protocol (§8.2)"
                )
            connection.execute(
                "INSERT INTO mission_planning_protocols(mission_id,protocol_version,"
                "package_version,prompt_version,binding_hash,created_at) VALUES (?,?,?,?,?,?)",
                (mission, protocol, package, prompt, digest, now),
            )

    def get_mission_protocol(self, mission_id: str) -> dict[str, Any] | None:
        """The persisted binding, or ``None`` for a legacy Mission (§8.2)."""

        mission = identifier(mission_id, "mission_planning_protocols.mission_id")
        row = self._store.connection.execute(
            "SELECT mission_id,protocol_version,package_version,prompt_version,binding_hash,"
            "created_at FROM mission_planning_protocols WHERE mission_id = ?",
            (mission,),
        ).fetchone()
        if row is None:
            return None
        return {
            "mission_id": row["mission_id"],
            "protocol_version": row["protocol_version"],
            "package_version": row["package_version"],
            "prompt_version": row["prompt_version"],
            "binding_hash": row["binding_hash"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------ requests
    def insert_planning_request(self, binding: PlanningRequestBinding) -> PlanningRequestBinding:
        """Store a request binding; identical content is idempotent (§34, BL-7)."""

        if not isinstance(binding, PlanningRequestBinding):
            raise StoreConflict("insert_planning_request expects a PlanningRequestBinding")
        values = tuple(binding.to_json()[field] for field in _REQUEST_FIELDS)
        with self._store.transaction() as connection:
            existing = connection.execute(
                f"SELECT {','.join(_REQUEST_FIELDS)} FROM planning_requests WHERE request_id = ?",
                (binding.request_id,),
            ).fetchone()
            if existing is not None:
                stored = PlanningRequestBinding.from_json(dict(existing))
                if stored == binding:
                    return stored
                raise StoreConflict(
                    f"planning request {binding.request_id} already exists with different"
                    f" content; a request identity is frozen once written (§34)"
                )
            try:
                connection.execute(
                    f"INSERT INTO planning_requests({','.join(_REQUEST_FIELDS)})"
                    f" VALUES ({','.join('?' * len(_REQUEST_FIELDS))})",
                    values,
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"planning request {binding.request_id} could not be stored: {error}"
                ) from error
            return binding

    def get_planning_request(self, request_id: str) -> PlanningRequestBinding | None:
        request = identifier(request_id, "planning_requests.request_id")
        row = self._store.connection.execute(
            f"SELECT {','.join(_REQUEST_FIELDS)} FROM planning_requests WHERE request_id = ?",
            (request,),
        ).fetchone()
        return None if row is None else PlanningRequestBinding.from_json(dict(row))

    def get_planning_request_for_intent(
        self, intent_id: str
    ) -> PlanningRequestBinding | None:
        """Return the request currently answered by ``intent_id``.

        A format retry keeps the opener's request id while the binding's
        ``intent_id`` moves to the retry.  The collector therefore cannot derive
        request identity from the dispatch intent id alone after a rebind.
        """

        intent = identifier(intent_id, "planning_requests.intent_id")
        row = self._store.connection.execute(
            f"SELECT {','.join(_REQUEST_FIELDS)} FROM planning_requests WHERE intent_id = ?",
            (intent,),
        ).fetchone()
        return None if row is None else PlanningRequestBinding.from_json(dict(row))

    def rebind_planning_request_intent(
        self,
        request_id: str,
        expected_opener_intent_id: str,
        retry_intent_id: str,
    ) -> PlanningRequestBinding:
        """Move one request's ``intent_id`` to the intent that actually answered it (§34).

        An opener and its one format retry are the *same* request: they share one row
        and one identity, and the retry is the only case where the answering intent
        differs from the opener's.  ``insert_planning_request`` cannot express that —
        identical content is idempotent and any difference is a conflict — so this is
        the one explicit, constrained way to move that column:

        * **It never inserts.**  A request with no row is a ``StoreConflict``; a retry
          re-answers a question that exists, it does not ask a new one.
        * **It never invents identity.**  ``expected_opener_intent_id`` is the intent
          the caller read from the row: the rebind is refused unless the stored row
          still names exactly that intent, so two intents cannot both claim one
          request.  Re-applying the move already made is a no-op and moves no clock —
          and it is answered *before* the ladder below, so a retry that already landed
          replays itself even though its attempt 1 is recorded.
        * **It moves one column.**  The rest of the row — including ``created_at``, the
          moment the request was established — is the frozen §34 content and is not
          written, so the stored row cannot drift from the request that was asked.
        * **Nothing is derived.**  The retry's proposed content is read *from the stored
          row*, not from the caller's binding, so a concurrent writer that slipped a
          different binding in cannot be laundered into the answer.  The caller passes
          the intent only.
        * **It re-answers a request the ladder is still asking.**  The permitted re-ask
          is "one unreadable reply, then one re-ask", so the request it re-answers has
          no decision at the last attempt the ladder opened and nothing recorded beyond
          that attempt: recording either is how a closed request is spelled — the retry
          was spent, or a decision landed past it (§36).  A closed request is a
          ``StoreConflict``; the ladder ends there rather than asking again.  This gate
          applies only to a call that would actually write: the replay above returns
          first, so the state it describes — a spent re-ask — cannot make a completed
          retry's own re-application fail.
        * **It is one clock.**  Every read and the single ``UPDATE`` are validated
          inside the caller's ``transaction()``, so the row cannot move between what was
          checked and what was written.
        """

        request = identifier(request_id, "planning_requests.request_id")
        expected = _expected_intent(expected_opener_intent_id)
        answering = _expected_intent(retry_intent_id)
        with self._store.transaction() as connection:
            row = connection.execute(
                f"SELECT {','.join(_REQUEST_FIELDS)} FROM planning_requests WHERE request_id = ?",
                (request,),
            ).fetchone()
            if row is None:
                raise StoreConflict(
                    f"planning request {request} has no stored binding; a rebind re-answers"
                    f" an existing request and never inserts one (§34)"
                )
            stored = PlanningRequestBinding.from_json(dict(row))
            if stored.intent_id != expected:
                raise StoreConflict(
                    f"planning request {request} was opened by intent {stored.intent_id};"
                    f" a rebind must name that opener, not {expected}"
                )
            if stored.intent_id == answering:
                return stored
            self._require_reopenable(connection, request=stored.request_id)
            try:
                connection.execute(
                    "UPDATE planning_requests SET intent_id = ? WHERE request_id = ?",
                    (answering, request),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"planning request {request} could not be re-bound to intent {answering}:"
                    f" {error}"
                ) from error
            rebound = connection.execute(
                f"SELECT {','.join(_REQUEST_FIELDS)} FROM planning_requests WHERE request_id = ?",
                (request,),
            ).fetchone()
            return PlanningRequestBinding.from_json(dict(rebound))

    def _require_reopenable(self, connection: sqlite3.Connection, *, request: str) -> None:
        """Refuse to re-answer a request whose one permitted re-ask is already spent.

        §36 gives the request's decisions their own lifecycle — an ``UNREADABLE``
        attempt 1 is the end of the ladder — and §36 is enforced here, beside the
        request it closes, rather than inferred by whoever calls the rebind.  A request
        the ladder opened is re-answerable exactly while its last attempt is still blank
        (the re-ask has not landed yet) and nothing is recorded past it.  Either fact
        recorded is a closed request:

        * attempt 1 already exists, so the re-ask happened: a second rebind would let a
          *third* intent claim a request §5.2 allows two, and would rewrite an answer
          that was already refused;
        * the newest attempt is past what ``attempt_ordinal`` allows, so a decision
          landed past the retry: the row that names attempts would no longer reach the
          intent the column names.

        A request with no decisions at all is the opener's own round: it is re-answerable
        too, which is what makes the opener -> retry move possible in one step.

        It is called *after* the caller's idempotent replay has returned, so this gate
        never sees a rebind that writes nothing.  The two conditions above describe a
        request that may not be re-bound **again**; a caller whose move is already the
        stored row is not asking to re-bind it, and must be answered by the replay rather
        than by this refusal.
        """

        newest = connection.execute(
            "SELECT time_ordinal FROM (SELECT MAX(attempt_ordinal) AS time_ordinal"
            " FROM planning_decisions WHERE request_id = ?)",
            (request,),
        ).fetchone()
        newest_ordinal = None if newest is None else newest["time_ordinal"]
        if newest_ordinal is None:
            return
        highest = self.MAX_SAME_REQUEST_FORMAT_RETRIES
        if newest_ordinal >= highest:
            raise StoreConflict(
                f"planning request {request} already has a decision at attempt"
                f" {newest_ordinal}; §36 closes the request there, so no further intent may"
                f" claim it (§34)"
            )

    # ------------------------------------------------------------------ decisions
    def record_planning_decision(
        self,
        *,
        request_id: str,
        attempt_ordinal: int,
        raw_output_hash: str,
        decision_id: str,
        status: PlanningDecisionStatus,
        rejection_codes: Sequence[str],
        detail: Mapping[str, Any],
        raw_artifact_ref: str | None = None,
        canonical_json: str | None = None,
        canonical_hash: str | None = None,
        decision_type: str | None = None,
    ) -> dict[str, Any]:
        """Record one attempt's outcome, or replay it (§35, §36).

        The same ``(request_id, attempt_ordinal, raw_output_hash)`` returns the row
        that is already there.  The same ``(request_id, attempt_ordinal)`` with a
        different raw output is an identity conflict.

        A later call may advance the status and fill in what this attempt has learned.
        The row is the one attempt's evaluation record, so a step that carries nothing
        new must not delete what an earlier step stored: every learned column is only
        overwritten by a value, and an empty ``detail`` / ``rejection_codes`` is "no
        new information", not "erase the column".  The status never moves backwards.
        """

        request = identifier(request_id, "planning_decisions.request_id")
        ordinal = index(attempt_ordinal, "planning_decisions.attempt_ordinal")
        raw_hash = hash_hex(raw_output_hash, "planning_decisions.raw_output_hash")
        decision = identifier(decision_id, "planning_decisions.decision_id")
        lifecycle = _status(status)
        codes = _rejection_codes(rejection_codes)
        document = json_object(detail, "planning_decisions.detail")
        artifact = (
            None
            if raw_artifact_ref is None
            else identifier(raw_artifact_ref, "planning_decisions.raw_artifact_ref")
        )
        canonical = None if canonical_json is None else _canonical_text(canonical_json)
        canonical_digest = (
            None if canonical_hash is None else hash_hex(canonical_hash, "canonical_hash")
        )
        kind = (
            None
            if decision_type is None
            else identifier(decision_type, "planning_decisions.decision_type")
        )
        codes_json = _canonical_json([str(code) for code in codes])
        detail_json = _canonical_json(document)
        now = self._store.now

        with self._store.transaction() as connection:
            existing = connection.execute(
                f"SELECT {','.join(_DECISION_COLUMNS)} FROM planning_decisions"
                " WHERE request_id = ? AND attempt_ordinal = ?",
                (request, ordinal),
            ).fetchone()
            if existing is not None:
                self._check_replay(existing, raw_hash=raw_hash, decision_id=decision)
                if existing["status"] == str(lifecycle):
                    return _decision_row(existing)
                _require_forward(
                    _status(existing["status"]),
                    lifecycle,
                    request_id=request,
                    attempt_ordinal=ordinal,
                )
                # An empty evaluation carries no information: it must not blank what
                # an earlier step of the same attempt stored (§35: the row *is* the
                # attempt's record).  Non-empty values are the later step's answer and
                # do replace the earlier one.
                connection.execute(
                    "UPDATE planning_decisions SET status = ?, raw_artifact_ref ="
                    " COALESCE(?, raw_artifact_ref), canonical_json = COALESCE(?, canonical_json),"
                    " canonical_hash = COALESCE(?, canonical_hash), decision_type ="
                    " COALESCE(?, decision_type), rejection_codes_json ="
                    " COALESCE(?, rejection_codes_json), detail_json = COALESCE(?, detail_json)"
                    " WHERE decision_id = ?",
                    (
                        str(lifecycle),
                        artifact,
                        canonical,
                        canonical_digest,
                        kind,
                        None if not codes else codes_json,
                        None if not document else detail_json,
                        decision,
                    ),
                )
                return self._read_decision(connection, decision_id=decision)

            try:
                connection.execute(
                    "INSERT INTO planning_decisions(decision_id,request_id,attempt_ordinal,"
                    "raw_output_hash,raw_artifact_ref,canonical_json,canonical_hash,decision_type,"
                    "status,rejection_codes_json,detail_json,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision,
                        request,
                        ordinal,
                        raw_hash,
                        artifact,
                        canonical,
                        canonical_digest,
                        kind,
                        str(lifecycle),
                        codes_json,
                        detail_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"planning decision {decision} for request {request} attempt {ordinal}"
                    f" could not be stored: {error}"
                ) from error
            return self._read_decision(connection, decision_id=decision)

    @staticmethod
    def _check_replay(existing: Mapping[str, Any], *, raw_hash: str, decision_id: str) -> None:
        """A row at this attempt may only be replayed with the bytes that made it."""

        if existing["raw_output_hash"] != raw_hash:
            raise StoreConflict(
                f"planning request {existing['request_id']} attempt"
                f" {existing['attempt_ordinal']} was already decided from raw output"
                f" {existing['raw_output_hash']}; different bytes are a new attempt ordinal,"
                f" not a rewrite (§35)"
            )
        if existing["decision_id"] != decision_id:
            raise StoreConflict(
                f"planning request {existing['request_id']} attempt"
                f" {existing['attempt_ordinal']} already has decision {existing['decision_id']};"
                f" {decision_id} is a second identity for the same attempt"
            )

    def get_planning_decision(self, decision_id: str) -> dict[str, Any] | None:
        decision = identifier(decision_id, "planning_decisions.decision_id")
        return self._read_decision(self._store.connection, decision_id=decision)

    def get_planning_decision_by_attempt(
        self, request_id: str, attempt_ordinal: int
    ) -> dict[str, Any] | None:
        request = identifier(request_id, "planning_decisions.request_id")
        ordinal = index(attempt_ordinal, "planning_decisions.attempt_ordinal")
        row = self._store.connection.execute(
            f"SELECT {','.join(_DECISION_COLUMNS)} FROM planning_decisions"
            " WHERE request_id = ? AND attempt_ordinal = ?",
            (request, ordinal),
        ).fetchone()
        return None if row is None else _decision_row(row)

    @staticmethod
    def _read_decision(
        connection: sqlite3.Connection, *, decision_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            f"SELECT {','.join(_DECISION_COLUMNS)} FROM planning_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return None if row is None else _decision_row(row)


def _canonical_text(value: object) -> str:
    """Validate the caller's canonical decision text and store it **verbatim** (§15).

    The canonicalisation belongs to the codec: ``canonical_hash`` is the digest of
    exactly these bytes, so the store re-spelling them would make the hash describe
    something that is not in the library.  The store only refuses a column that is
    not JSON at all, because that could not be an audit record of a decision.
    """

    if not isinstance(value, str) or not value:
        raise ContractError("planning_decisions.canonical_json must be a non-empty string")
    try:
        json.loads(value)
    except ValueError as error:
        raise ContractError("planning_decisions.canonical_json must be valid JSON") from error
    return value


def _require_forward(
    current: PlanningDecisionStatus,
    target: PlanningDecisionStatus,
    *,
    request_id: str,
    attempt_ordinal: int,
) -> None:
    """§36: terminal statuses absorb; every other move must be an increase in rank."""

    if current in TERMINAL_STATUSES:
        raise StoreConflict(
            f"planning decision for request {request_id} attempt {attempt_ordinal} is"
            f" {current}; a terminal status never changes"
        )
    if STATUS_ORDER.index(target) <= STATUS_ORDER.index(current):
        raise StoreConflict(
            f"planning decision for request {request_id} attempt {attempt_ordinal} is"
            f" {current}; status only moves forward (§36), not to {target}"
        )


__all__ = ("STATUS_ORDER", "TERMINAL_STATUSES", "PlanningDecisionStore")
