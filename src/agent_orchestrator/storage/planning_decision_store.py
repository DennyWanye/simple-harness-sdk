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
