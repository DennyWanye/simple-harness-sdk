# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level ARP Store over the execution library's ``arp_*`` tables (INTERFACES §4).

Every ``*_locked`` function takes the caller's open connection and asserts that a
transaction is active; none of them commit.  Same-identity inserts read the existing
row first: an identical body returns it, a different body is a conflict.  ``OR
REPLACE`` is never used (the DDL's BEFORE INSERT guards refuse it anyway).  No
function converts an exception into an empty result.

Original events: ARP appends its typed bodies as *original* run events through the
execution UOW's own ``run_events`` writer and binds them in ``arp_event_bindings``
(event-catalogue.json); there is no second eventseq source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import PROTOCOL
from .codec import check
from .errors import ArpError
from .pins import Pin
from .strict import canonical, digest, parse_strict

Json = Mapping[str, Any]

SESSION_STATES = ("CREATING", "ACTIVE", "DRAINING", "PURGING", "PURGED", "QUARANTINED")
JOB_KINDS = ("INDEX", "PURGE", "BIND_IMPORT")
JOB_STATES = ("PENDING", "LEASED", "DONE", "BLOCKED", "CANCELLED")
EVENT_TYPES = (
    "AgentContextPolicyAdopted",
    "RuntimeContextPrepared",
    "RuntimeContextExposed",
    "RuntimeSessionStateChanged",
    "RuntimeIndexGenerationPublished",
    "RuntimeJobChanged",
    "RuntimeCatalogueChanged",
)


def require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("ARP store *_locked functions require the caller's open transaction")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ArpError("MISSING_FIELD", field_path=name)
    return value


def _json_column(value: object) -> str:
    return canonical(value).decode("utf-8")


def _load(text: str) -> Any:
    return parse_strict(text.encode("utf-8"), max_bytes=16 * 1024 * 1024)


def _pin_column(pin: Pin) -> str:
    return _json_column(pin.to_json())


# ---- profiles ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileRow:
    profile_id: str
    revision: int
    body_hash: str
    body: Mapping[str, Any]
    activation_receipt: Mapping[str, Any]

    @property
    def pin(self) -> Pin:
        return Pin("profile", self.profile_id, self.revision, self.body_hash)


def read_profile(connection: sqlite3.Connection, profile_id: str, revision: int) -> ProfileRow | None:
    row = connection.execute(
        "SELECT profile_id,revision,body_hash,body_json,activation_receipt_json FROM arp_profiles"
        " WHERE profile_id=? AND revision=?",
        (profile_id, revision),
    ).fetchone()
    if row is None:
        return None
    return ProfileRow(str(row[0]), int(row[1]), str(row[2]), _load(row[3]), _load(row[4]))


def put_profile_locked(
    connection: sqlite3.Connection, body: Json, activation_receipt: Json
) -> ProfileRow:
    require_transaction(connection)
    value = check("RuntimeProfile", dict(body))
    body_hash = digest(value)
    existing = read_profile(connection, value["profile_id"], value["profile_revision"])
    if existing is not None:
        if existing.body_hash != body_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "profile identity already exists with another body")
        return existing
    connection.execute(
        "INSERT INTO arp_profiles(profile_id,revision,body_hash,body_json,activation_receipt_json)"
        " VALUES (?,?,?,?,?)",
        (
            value["profile_id"],
            value["profile_revision"],
            body_hash,
            _json_column(value),
            _json_column(dict(activation_receipt)),
        ),
    )
    return ProfileRow(
        value["profile_id"], value["profile_revision"], body_hash, value, dict(activation_receipt)
    )


# ---- creation intents ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreationIntentRow:
    intent_id: str
    owner_scope: str
    creation_key: str
    command_hash: str
    proposed_agent_id: str
    proposed_run_id: str
    profile_ref: Pin
    state: str
    row_version: int
    original_receipt_ref: Pin


def _intent(row: sqlite3.Row | tuple) -> CreationIntentRow:
    return CreationIntentRow(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        Pin.from_json(_load(row[6]), kinds=("profile",)),
        str(row[7]),
        int(row[8]),
        Pin.from_json(_load(row[9]), kinds=("receipt",)),
    )


_INTENT_COLUMNS = (
    "intent_id,owner_scope,creation_key,command_hash,proposed_agent_id,proposed_run_id,"
    "profile_ref_json,state,row_version,original_receipt_ref_json"
)


def read_creation_intent(
    connection: sqlite3.Connection, owner_scope: str, creation_key: str
) -> CreationIntentRow | None:
    row = connection.execute(
        f"SELECT {_INTENT_COLUMNS} FROM arp_creation_intents WHERE owner_scope=? AND creation_key=?",
        (owner_scope, creation_key),
    ).fetchone()
    return None if row is None else _intent(row)


def put_creation_intent_locked(
    connection: sqlite3.Connection,
    *,
    intent_id: str,
    owner_scope: str,
    creation_key: str,
    command_hash: str,
    proposed_agent_id: str,
    proposed_run_id: str,
    profile_ref: Pin,
    original_receipt_ref: Pin,
) -> CreationIntentRow:
    """PREPARED intent; a replay with the same command hash returns the stored row."""

    require_transaction(connection)
    profile_ref.require_kind("profile")
    original_receipt_ref.require_kind("receipt")
    existing = read_creation_intent(connection, owner_scope, creation_key)
    if existing is not None:
        if (
            existing.command_hash != command_hash
            or existing.proposed_agent_id != proposed_agent_id
            or existing.proposed_run_id != proposed_run_id
            or existing.profile_ref != profile_ref
        ):
            raise ArpError("CREATION_IDENTITY_CONFLICT", "creation_key reused with another command")
        return existing
    connection.execute(
        f"INSERT INTO arp_creation_intents({_INTENT_COLUMNS}) VALUES (?,?,?,?,?,?,?,'PREPARED',1,?)",
        (
            _text(intent_id, "intent_id"),
            _text(owner_scope, "owner_scope"),
            _text(creation_key, "creation_key"),
            command_hash,
            _text(proposed_agent_id, "proposed_agent_id"),
            _text(proposed_run_id, "proposed_run_id"),
            _pin_column(profile_ref),
            _pin_column(original_receipt_ref),
        ),
    )
    intent = read_creation_intent(connection, owner_scope, creation_key)
    assert intent is not None
    return intent


def finalize_creation_locked(
    connection: sqlite3.Connection, intent: CreationIntentRow, *, state: str
) -> CreationIntentRow:
    require_transaction(connection)
    if state not in ("BOUND", "ABORTED"):
        raise ArpError("ENUM", field_path="state")
    if intent.state == state:
        return intent
    updated = connection.execute(
        "UPDATE arp_creation_intents SET state=?, row_version=row_version+1"
        " WHERE intent_id=? AND row_version=? AND state='PREPARED'",
        (state, intent.intent_id, intent.row_version),
    ).rowcount
    if updated != 1:
        raise ArpError("CREATION_IDENTITY_CONFLICT", "creation intent changed concurrently")
    current = read_creation_intent(connection, intent.owner_scope, intent.creation_key)
    assert current is not None
    return current


# ---- protocols ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtocolRow:
    agent_id: str
    protocol: str
    creation_receipt_ref: Pin
    marker_hash: str
    marked_at_ms: int


def read_protocol(connection: sqlite3.Connection, agent_id: str) -> ProtocolRow | None:
    row = connection.execute(
        "SELECT agent_id,protocol,creation_receipt_ref_json,marker_hash,marked_at_ms"
        " FROM arp_agent_protocols WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    return ProtocolRow(
        str(row[0]), str(row[1]), Pin.from_json(_load(row[2]), kinds=("receipt",)), str(row[3]), int(row[4])
    )


def put_protocol_locked(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    creation_receipt_ref: Pin,
    marker_hash: str,
    marked_at_ms: int,
    protocol: str = PROTOCOL,
) -> ProtocolRow:
    require_transaction(connection)
    creation_receipt_ref.require_kind("receipt")
    existing = read_protocol(connection, agent_id)
    if existing is not None:
        if (
            existing.protocol != protocol
            or existing.creation_receipt_ref != creation_receipt_ref
            or existing.marker_hash != marker_hash
        ):
            raise ArpError("CREATION_IDENTITY_CONFLICT", "agent already has a different creation protocol")
        return existing
    connection.execute(
        "INSERT INTO arp_agent_protocols(agent_id,protocol,creation_receipt_ref_json,marker_hash,marked_at_ms)"
        " VALUES (?,?,?,?,?)",
        (agent_id, protocol, _pin_column(creation_receipt_ref), marker_hash, marked_at_ms),
    )
    return ProtocolRow(agent_id, protocol, creation_receipt_ref, marker_hash, marked_at_ms)


# ---- sessions ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_id: str
    agent_id: str
    profile_id: str
    profile_revision: int
    profile_hash: str
    creation_root_id: str
    root_incarnation: str
    creation_key: str
    create_command_hash: str
    relative_directory: str
    state: str
    generation: int
    row_version: int
    journal_seq_from: int
    sealed_highwater: int | None
    destroy_command_id: str | None
    destroy_command_hash: str | None
    purge_progress: Mapping[str, Any] | None
    purge_progress_hash: str | None
    delete_proof_ref: Pin | None
    created_at_ms: int
    updated_at_ms: int

    @property
    def profile_ref(self) -> Pin:
        return Pin("profile", self.profile_id, self.profile_revision, self.profile_hash)

    @property
    def pin(self) -> Pin:
        return Pin("session", self.session_id, self.row_version, self.identity_hash)

    @property
    def identity_hash(self) -> str:
        return digest(
            {
                "session_id": self.session_id,
                "agent_id": self.agent_id,
                "state": self.state,
                "generation": self.generation,
                "row_version": self.row_version,
                "purge_progress_hash": self.purge_progress_hash,
            }
        )


_SESSION_COLUMNS = (
    "session_id,agent_id,profile_id,profile_revision,profile_hash,creation_root_id,root_incarnation,"
    "creation_key,create_command_hash,relative_directory,state,generation,row_version,journal_seq_from,"
    "sealed_highwater,destroy_command_id,destroy_command_hash,purge_progress_json,purge_progress_hash,"
    "delete_proof_ref_json,created_at_ms,updated_at_ms"
)


def _session(row: sqlite3.Row | tuple) -> SessionRow:
    return SessionRow(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        int(row[3]),
        str(row[4]),
        str(row[5]),
        str(row[6]),
        str(row[7]),
        str(row[8]),
        str(row[9]),
        str(row[10]),
        int(row[11]),
        int(row[12]),
        int(row[13]),
        None if row[14] is None else int(row[14]),
        None if row[15] is None else str(row[15]),
        None if row[16] is None else str(row[16]),
        None if row[17] is None else _load(row[17]),
        None if row[18] is None else str(row[18]),
        None if row[19] is None else Pin.from_json(_load(row[19])),
        int(row[20]),
        int(row[21]),
    )


def read_session(connection: sqlite3.Connection, session_id: str) -> SessionRow | None:
    row = connection.execute(
        f"SELECT {_SESSION_COLUMNS} FROM arp_agent_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return None if row is None else _session(row)


def read_live_session(connection: sqlite3.Connection, agent_id: str) -> SessionRow | None:
    row = connection.execute(
        f"SELECT {_SESSION_COLUMNS} FROM arp_agent_sessions WHERE agent_id=? AND state!='PURGED'",
        (agent_id,),
    ).fetchone()
    return None if row is None else _session(row)


def read_session_by_creation_key(connection: sqlite3.Connection, creation_key: str) -> SessionRow | None:
    row = connection.execute(
        f"SELECT {_SESSION_COLUMNS} FROM arp_agent_sessions WHERE creation_key=?", (creation_key,)
    ).fetchone()
    return None if row is None else _session(row)


def list_sessions_in_state(
    connection: sqlite3.Connection, states: Sequence[str], *, limit: int = 64
) -> tuple[SessionRow, ...]:
    marks = ",".join("?" for _ in states)
    rows = connection.execute(
        f"SELECT {_SESSION_COLUMNS} FROM arp_agent_sessions WHERE state IN ({marks})"
        " ORDER BY updated_at_ms, session_id LIMIT ?",
        (*states, limit),
    ).fetchall()
    return tuple(_session(r) for r in rows)


def insert_session_locked(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    agent_id: str,
    profile_ref: Pin,
    creation_root_id: str,
    root_incarnation: str,
    creation_key: str,
    create_command_hash: str,
    relative_directory: str,
    journal_seq_from: int,
    now_ms: int,
    legacy_creation_key: str | None = None,
) -> SessionRow:
    """CREATING row; a replay with identical identity returns the stored row.

    ``legacy_creation_key`` is the bare key an older library stored for this same session
    (found by ``session_id``); replaying such a row is not a mismatch.
    """

    require_transaction(connection)
    profile_ref.require_kind("profile")
    existing = read_session(connection, session_id) or read_session_by_creation_key(
        connection, creation_key
    )
    if existing is not None:
        if (
            existing.session_id != session_id
            or existing.agent_id != agent_id
            or existing.profile_ref != profile_ref
            or existing.creation_root_id != creation_root_id
            or existing.root_incarnation != root_incarnation
            or existing.creation_key not in {creation_key, legacy_creation_key or creation_key}
            or existing.create_command_hash != create_command_hash
            or existing.relative_directory != relative_directory
        ):
            raise ArpError("SESSION_IDENTITY_MISMATCH", "session identity already exists differently")
        return existing
    connection.execute(
        "INSERT INTO arp_agent_sessions(session_id,agent_id,profile_id,profile_revision,profile_hash,"
        "creation_root_id,root_incarnation,creation_key,create_command_hash,relative_directory,state,"
        "generation,row_version,journal_seq_from,created_at_ms,updated_at_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'CREATING',1,1,?,?,?)",
        (
            _text(session_id, "session_id"),
            _text(agent_id, "agent_id"),
            profile_ref.id,
            profile_ref.revision,
            profile_ref.content_hash,
            _text(creation_root_id, "creation_root_id"),
            _text(root_incarnation, "root_incarnation"),
            _text(creation_key, "creation_key"),
            create_command_hash,
            _text(relative_directory, "relative_directory"),
            journal_seq_from,
            now_ms,
            now_ms,
        ),
    )
    created = read_session(connection, session_id)
    assert created is not None
    return created


_UNSET = object()


def transition_session_locked(
    connection: sqlite3.Connection,
    current: SessionRow,
    *,
    now_ms: int,
    state: str | None = None,
    generation: int | None = None,
    sealed_highwater: int | None | object = _UNSET,
    destroy_command: tuple[str, str] | None = None,
    purge_progress: Mapping[str, Any] | None | object = _UNSET,
    delete_proof_ref: Pin | None = None,
) -> SessionRow:
    """One CAS update on ``row_version``; the DDL triggers police the state machine."""

    require_transaction(connection)
    new_state = current.state if state is None else state
    if new_state not in SESSION_STATES:
        raise ArpError("ENUM", field_path="state")
    new_generation = current.generation if generation is None else generation
    sets = ["state=?", "generation=?", "row_version=row_version+1", "updated_at_ms=?"]
    values: list[object] = [new_state, new_generation, max(now_ms, current.created_at_ms)]
    if sealed_highwater is not _UNSET:
        sets.append("sealed_highwater=?")
        values.append(sealed_highwater)
    if destroy_command is not None:
        sets.extend(["destroy_command_id=?", "destroy_command_hash=?"])
        values.extend(destroy_command)
    if purge_progress is not _UNSET:
        if purge_progress is None:
            sets.extend(["purge_progress_json=NULL", "purge_progress_hash=NULL"])
        else:
            body = check("PurgeProgress", dict(purge_progress))  # type: ignore[arg-type]
            sets.extend(["purge_progress_json=?", "purge_progress_hash=?"])
            values.extend([_json_column(body), digest(body)])
    if delete_proof_ref is not None:
        sets.append("delete_proof_ref_json=?")
        values.append(_pin_column(delete_proof_ref))
    values.extend([current.session_id, current.row_version])
    try:
        updated = connection.execute(
            f"UPDATE arp_agent_sessions SET {', '.join(sets)} WHERE session_id=? AND row_version=?",
            values,
        ).rowcount
    except sqlite3.IntegrityError as error:
        raise ArpError("PURGE_STATE_INVALID" if "purge" in str(error) else "STATE_COMBINATION_INVALID", str(error)) from error
    if updated != 1:
        raise ArpError("GENERATION_STALE", "session row changed concurrently")
    after = read_session(connection, current.session_id)
    assert after is not None
    return after


# ---- policy objects and adoptions -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyObjectRow:
    policy_id: str
    revision: int
    content_hash: str
    body: Mapping[str, Any]
    approval_ref: Pin
    source_receipt_ref: Pin

    @property
    def pin(self) -> Pin:
        return Pin("policy", self.policy_id, self.revision, self.content_hash)


def read_policy_object(connection: sqlite3.Connection, policy_id: str, revision: int) -> PolicyObjectRow | None:
    row = connection.execute(
        "SELECT policy_id,revision,content_hash,body_json,approval_ref_json,source_receipt_ref_json"
        " FROM arp_policy_objects WHERE policy_id=? AND revision=?",
        (policy_id, revision),
    ).fetchone()
    if row is None:
        return None
    return PolicyObjectRow(
        str(row[0]), int(row[1]), str(row[2]), _load(row[3]), Pin.from_json(_load(row[4])), Pin.from_json(_load(row[5]))
    )


def latest_policy_revision(connection: sqlite3.Connection, policy_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(revision),0) FROM arp_policy_objects WHERE policy_id=?", (policy_id,)
    ).fetchone()
    return int(row[0])


def put_policy_object_locked(
    connection: sqlite3.Connection,
    *,
    body: Json,
    revision: int,
    approval_ref: Pin,
    source_receipt_ref: Pin,
) -> PolicyObjectRow:
    require_transaction(connection)
    approval_ref.require_kind("authority")
    source_receipt_ref.require_kind("receipt")
    from .profile import check_policy

    value = check_policy(body)
    content_hash = digest(value)
    existing = read_policy_object(connection, value["policy_id"], revision)
    if existing is not None:
        if existing.content_hash != content_hash:
            raise ArpError("POLICY_CONFLICT", "policy revision already exists with another body")
        return existing
    connection.execute(
        "INSERT INTO arp_policy_objects(policy_id,revision,content_hash,body_json,approval_ref_json,"
        "source_receipt_ref_json) VALUES (?,?,?,?,?,?)",
        (
            value["policy_id"],
            revision,
            content_hash,
            _json_column(value),
            _pin_column(approval_ref),
            _pin_column(source_receipt_ref),
        ),
    )
    return PolicyObjectRow(value["policy_id"], revision, content_hash, value, approval_ref, source_receipt_ref)


@dataclass(frozen=True, slots=True)
class PolicyAdoptionRow:
    session_id: str
    adoption_revision: int
    policy_ref: Pin
    policy_body_hash: str
    command_id: str
    command_hash: str
    authority_receipt_ref: Pin
    source_receipt_ref: Pin
    adopted_at_ms: int


def _adoption(row: sqlite3.Row | tuple) -> PolicyAdoptionRow:
    return PolicyAdoptionRow(
        str(row[0]),
        int(row[1]),
        Pin.from_json(_load(row[2]), kinds=("policy",)),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        Pin.from_json(_load(row[6])),
        Pin.from_json(_load(row[7])),
        int(row[8]),
    )


_ADOPTION_COLUMNS = (
    "session_id,adoption_revision,policy_ref_json,policy_body_hash,command_id,command_hash,"
    "authority_receipt_ref_json,source_receipt_ref_json,adopted_at_ms"
)


def latest_adoption(connection: sqlite3.Connection, session_id: str) -> PolicyAdoptionRow | None:
    row = connection.execute(
        f"SELECT {_ADOPTION_COLUMNS} FROM arp_context_policy_adoptions WHERE session_id=?"
        " ORDER BY adoption_revision DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else _adoption(row)


def read_adoption_by_command(connection: sqlite3.Connection, command_id: str) -> PolicyAdoptionRow | None:
    row = connection.execute(
        f"SELECT {_ADOPTION_COLUMNS} FROM arp_context_policy_adoptions WHERE command_id=?", (command_id,)
    ).fetchone()
    return None if row is None else _adoption(row)


def append_policy_adoption_locked(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    policy: PolicyObjectRow,
    command_id: str,
    command_hash: str,
    authority_receipt_ref: Pin,
    source_receipt_ref: Pin,
    now_ms: int,
) -> PolicyAdoptionRow:
    """Next adoption revision for the session; same command replays, other body conflicts."""

    require_transaction(connection)
    replay = read_adoption_by_command(connection, command_id)
    if replay is not None:
        if replay.command_hash != command_hash or replay.policy_ref != policy.pin:
            raise ArpError("EXPECTED_REVISION_MISMATCH", "command_id reused with another adoption")
        return replay
    latest = latest_adoption(connection, session_id)
    revision = 1 if latest is None else latest.adoption_revision + 1
    try:
        connection.execute(
            f"INSERT INTO arp_context_policy_adoptions({_ADOPTION_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                revision,
                _pin_column(policy.pin),
                policy.content_hash,
                _text(command_id, "command_id"),
                command_hash,
                _pin_column(authority_receipt_ref),
                _pin_column(source_receipt_ref),
                now_ms,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ArpError("SESSION_NOT_ACTIVE", str(error)) from error
    created = read_adoption_by_command(connection, command_id)
    assert created is not None
    return created


# ---- original events ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventBindingRow:
    original_event_id: str
    original_eventseq: int
    event_type: str
    body_hash: str
    body: Mapping[str, Any]
    source_receipt_ref: Pin


def append_runtime_event_locked(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    body: Json,
    source_receipt_ref: Pin,
    dedupe_key: str,
    now: float,
) -> EventBindingRow:
    """Append one original run event and bind its typed body (same transaction)."""

    require_transaction(connection)
    if event_type not in EVENT_TYPES:
        raise ArpError("ENUM", field_path="event_type")
    source_receipt_ref.require_kind("receipt")
    body_name = f"{event_type}Body"
    value = check(body_name, dict(body))
    body_hash = digest(value)
    event_id = f"arp:{event_type}:{digest({'dedupe': dedupe_key})[:32]}"
    existing = connection.execute(
        "SELECT original_event_id,original_eventseq,event_type,body_hash,body_json,source_receipt_ref_json"
        " FROM arp_event_bindings WHERE original_event_id=?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[3]) != body_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "event dedupe key reused with another body")
        return EventBindingRow(
            str(existing[0]), int(existing[1]), str(existing[2]), str(existing[3]), _load(existing[4]),
            Pin.from_json(_load(existing[5])),
        )
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(durable_seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    )
    payload = {
        "schema_version": 1,
        "event_type": event_type,
        "body_hash": body_hash,
        "dedupe_key": dedupe_key,
        "source_receipt_ref": source_receipt_ref.to_json(),
    }
    connection.execute(
        "INSERT INTO run_events(event_id, run_id, durable_seq, kind, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, run_id, sequence, "arp.runtime_event.v1", _json_column(payload), now),
    )
    from simple_harness.execution.sqlite.audit_witness import record_event_witness

    record_event_witness(connection, event_id)
    connection.execute(
        "INSERT INTO arp_event_bindings(original_event_id,original_eventseq,event_type,body_hash,"
        "body_json,source_receipt_ref_json) VALUES (?,?,?,?,?,?)",
        (event_id, sequence, event_type, body_hash, _json_column(value), _pin_column(source_receipt_ref)),
    )
    return EventBindingRow(event_id, sequence, event_type, body_hash, value, source_receipt_ref)


def list_event_bindings(
    connection: sqlite3.Connection, *, event_type: str | None = None, limit: int = 256
) -> tuple[EventBindingRow, ...]:
    where = "" if event_type is None else " WHERE event_type=?"
    args: tuple = () if event_type is None else (event_type,)
    rows = connection.execute(
        "SELECT original_event_id,original_eventseq,event_type,body_hash,body_json,source_receipt_ref_json"
        f" FROM arp_event_bindings{where} ORDER BY rowid LIMIT ?",
        (*args, limit),
    ).fetchall()
    return tuple(
        EventBindingRow(str(r[0]), int(r[1]), str(r[2]), str(r[3]), _load(r[4]), Pin.from_json(_load(r[5])))
        for r in rows
    )


# ---- jobs -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobRow:
    job_id: str
    session_id: str
    kind: str
    semantic_key: str
    payload: Mapping[str, Any]
    payload_hash: str
    generation: int
    state: str
    row_version: int
    attempts: int
    next_at_ms: int
    deadline_ms: int
    owner_id: str | None
    lease_until_ms: int | None
    result_receipt_ref: Pin | None


_JOB_COLUMNS = (
    "job_id,session_id,kind,semantic_key,payload_json,payload_hash,generation,state,row_version,"
    "attempts,next_at_ms,deadline_ms,owner_id,lease_until_ms,result_receipt_ref_json"
)


def _job(row: sqlite3.Row | tuple) -> JobRow:
    return JobRow(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), _load(row[4]), str(row[5]), int(row[6]),
        str(row[7]), int(row[8]), int(row[9]), int(row[10]), int(row[11]),
        None if row[12] is None else str(row[12]),
        None if row[13] is None else int(row[13]),
        None if row[14] is None else Pin.from_json(_load(row[14])),
    )


def read_job(connection: sqlite3.Connection, job_id: str) -> JobRow | None:
    row = connection.execute(f"SELECT {_JOB_COLUMNS} FROM arp_jobs WHERE job_id=?", (job_id,)).fetchone()
    return None if row is None else _job(row)


def read_job_by_key(connection: sqlite3.Connection, session_id: str, kind: str, semantic_key: str) -> JobRow | None:
    row = connection.execute(
        f"SELECT {_JOB_COLUMNS} FROM arp_jobs WHERE session_id=? AND kind=? AND semantic_key=?",
        (session_id, kind, semantic_key),
    ).fetchone()
    return None if row is None else _job(row)


def list_session_jobs(
    connection: sqlite3.Connection, session_id: str, *, kinds: Sequence[str] | None = None, states: Sequence[str] | None = None
) -> tuple[JobRow, ...]:
    """Every job of one Session (oldest first), optionally narrowed by kind / state."""

    where = ["session_id=?"]
    args: list[Any] = [session_id]
    if kinds:
        where.append(f"kind IN ({','.join('?' for _ in kinds)})")
        args.extend(kinds)
    if states:
        where.append(f"state IN ({','.join('?' for _ in states)})")
        args.extend(states)
    rows = connection.execute(
        f"SELECT {_JOB_COLUMNS} FROM arp_jobs WHERE {' AND '.join(where)} ORDER BY next_at_ms, job_id", args
    ).fetchall()
    return tuple(_job(r) for r in rows)


def put_job_locked(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    session_id: str,
    kind: str,
    semantic_key: str,
    payload: Json,
    generation: int,
    next_at_ms: int,
    deadline_ms: int,
) -> JobRow:
    require_transaction(connection)
    if kind not in JOB_KINDS:
        raise ArpError("ENUM", field_path="kind")
    payload_name = {"INDEX": "IndexJobPayload", "PURGE": "PurgeJobPayload", "BIND_IMPORT": "BindImportJobPayload"}[kind]
    value = check(payload_name, dict(payload))
    payload_hash = digest(value)
    existing = read_job_by_key(connection, session_id, kind, semantic_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "job semantic key reused with another payload")
        return existing
    connection.execute(
        f"INSERT INTO arp_jobs({_JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,'PENDING',1,0,?,?,NULL,NULL,NULL)",
        (job_id, session_id, kind, semantic_key, _json_column(value), payload_hash, generation, next_at_ms, deadline_ms),
    )
    created = read_job(connection, job_id)
    assert created is not None
    return created


def claim_jobs_locked(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    now_ms: int,
    lease_ms: int,
    limit: int = 8,
    per_session: int = 2,
    session_id: str | None = None,
) -> tuple[JobRow, ...]:
    """PENDING → LEASED (or expired LEASED re-lease), ≤limit per tick, ≤per_session each;
    ``session_id`` restricts the claim to one Session's jobs."""

    require_transaction(connection)
    scope = "" if session_id is None else " AND session_id=?"
    params: tuple[Any, ...] = (now_ms, now_ms) if session_id is None else (now_ms, session_id, now_ms, session_id)
    rows = connection.execute(
        f"SELECT {_JOB_COLUMNS} FROM arp_jobs WHERE ((state='PENDING' AND next_at_ms<=?){scope})"
        f" OR ((state='LEASED' AND lease_until_ms<?){scope}) ORDER BY next_at_ms, job_id LIMIT ?",
        (*params, limit * 4),
    ).fetchall()
    claimed: list[JobRow] = []
    per: dict[str, int] = {}
    for raw in rows:
        job = _job(raw)
        if len(claimed) >= limit or per.get(job.session_id, 0) >= per_session:
            continue
        if job.state == "LEASED":
            # Expired lease: only a new coordinator may continue; the original work
            # (if any) is looked up by its recorded invocation, never re-run blindly.
            updated = connection.execute(
                "UPDATE arp_jobs SET state='PENDING', owner_id=NULL, lease_until_ms=NULL,"
                " row_version=row_version+1 WHERE job_id=? AND row_version=?",
                (job.job_id, job.row_version),
            ).rowcount
            if updated != 1:
                continue
            job = read_job(connection, job.job_id) or job
        updated = connection.execute(
            "UPDATE arp_jobs SET state='LEASED', owner_id=?, lease_until_ms=?, attempts=attempts+1,"
            " row_version=row_version+1 WHERE job_id=? AND row_version=? AND state='PENDING'",
            (owner_id, now_ms + lease_ms, job.job_id, job.row_version),
        ).rowcount
        if updated != 1:
            continue
        claimed.append(read_job(connection, job.job_id))  # type: ignore[arg-type]
        per[job.session_id] = per.get(job.session_id, 0) + 1
    return tuple(claimed)


def complete_job_locked(
    connection: sqlite3.Connection,
    job: JobRow,
    *,
    state: str,
    owner_id: str,
    result_receipt_ref: Pin | None = None,
    next_at_ms: int | None = None,
) -> JobRow:
    require_transaction(connection)
    if state not in ("DONE", "BLOCKED", "CANCELLED", "PENDING"):
        raise ArpError("ENUM", field_path="state")
    if state == "DONE" and result_receipt_ref is None:
        raise ArpError("MISSING_FIELD", field_path="result_receipt_ref")
    if job.state == "LEASED" and job.owner_id != owner_id:
        raise ArpError("JOB_STALE", "job leased by another owner")
    updated = connection.execute(
        "UPDATE arp_jobs SET state=?, owner_id=NULL, lease_until_ms=NULL, result_receipt_ref_json=?,"
        " next_at_ms=COALESCE(?, next_at_ms), row_version=row_version+1 WHERE job_id=? AND row_version=?",
        (
            state,
            None if result_receipt_ref is None else _pin_column(result_receipt_ref),
            next_at_ms,
            job.job_id,
            job.row_version,
        ),
    ).rowcount
    if updated != 1:
        raise ArpError("JOB_STALE", "job row changed concurrently")
    after = read_job(connection, job.job_id)
    assert after is not None
    return after


# ---- blob roots -----------------------------------------------------------------------------


def put_blob_root_locked(
    connection: sqlite3.Connection,
    *,
    root_key: str,
    session_id: str,
    object_ref: Pin,
    purpose: str,
    owner_ref: Pin,
) -> None:
    require_transaction(connection)
    if purpose not in ("REQUEST", "SKILL_USE", "TRANSFER", "AUDIT"):
        raise ArpError("ENUM", field_path="purpose")
    existing = connection.execute(
        "SELECT object_ref_json,owner_ref_json FROM arp_blob_roots WHERE root_key=?", (root_key,)
    ).fetchone()
    if existing is not None:
        if _load(existing[0]) != object_ref.to_json() or _load(existing[1]) != owner_ref.to_json():
            raise ArpError("REF_IDENTITY_MISMATCH", "blob root key reused for another object")
        return
    connection.execute(
        "INSERT INTO arp_blob_roots(root_key,session_id,object_ref_json,purpose,owner_ref_json,state,row_version)"
        " VALUES (?,?,?,?,?,'LIVE',1)",
        (root_key, session_id, _pin_column(object_ref), purpose, _pin_column(owner_ref)),
    )


# ---- context requests (ContextManifest v2, immutable) --------------------------------------


@dataclass(frozen=True, slots=True)
class ContextRequestRow:
    context_id: str
    session_id: str
    agent_id: str
    turn_id: str
    provider_request_ordinal: int
    session_generation: int
    journal_highwater: int
    manifest_hash: str
    manifest: Mapping[str, Any]
    planned_request_hash: str
    original_request_key: str
    input_charge: int
    input_budget: int
    output_reserve: int
    catalog_epoch: int
    source_read_set: Mapping[str, Any]
    created_at_ms: int

    @property
    def pin(self) -> Pin:
        return Pin("context", self.context_id, 0, self.manifest_hash)


_CONTEXT_COLUMNS = (
    "context_id,session_id,agent_id,turn_id,provider_request_ordinal,session_generation,journal_highwater,"
    "manifest_hash,manifest_json,planned_request_hash,original_request_key,input_charge,input_budget,"
    "output_reserve,catalog_epoch,source_read_set_json,created_at_ms"
)


def _context(row: sqlite3.Row | tuple) -> ContextRequestRow:
    return ContextRequestRow(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), int(row[5]), int(row[6]), str(row[7]),
        _load(row[8]), str(row[9]), str(row[10]), int(row[11]), int(row[12]), int(row[13]), int(row[14]),
        _load(row[15]), int(row[16]),
    )


def read_context(connection: sqlite3.Connection, context_id: str) -> ContextRequestRow | None:
    row = connection.execute(f"SELECT {_CONTEXT_COLUMNS} FROM arp_context_requests WHERE context_id=?", (context_id,)).fetchone()
    return None if row is None else _context(row)


def read_context_by_request_key(connection: sqlite3.Connection, original_request_key: str) -> ContextRequestRow | None:
    row = connection.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM arp_context_requests WHERE original_request_key=?", (original_request_key,)
    ).fetchone()
    return None if row is None else _context(row)


def latest_context(connection: sqlite3.Connection, session_id: str) -> ContextRequestRow | None:
    row = connection.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM arp_context_requests WHERE session_id=? ORDER BY created_at_ms DESC, provider_request_ordinal DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else _context(row)


def put_context_locked(
    connection: sqlite3.Connection,
    *,
    manifest: Json,
    original_request_key: str,
    catalog_epoch: int,
    source_read_set: Json,
    now_ms: int,
) -> ContextRequestRow:
    """Freeze one ``ContextManifest``; same request key + same body replays, another body conflicts."""

    require_transaction(connection)
    value = check("ContextManifest", dict(manifest))
    manifest_hash = digest(value)
    existing = read_context_by_request_key(connection, original_request_key)
    if existing is not None:
        if existing.manifest_hash != manifest_hash:
            raise ArpError("REQUEST_HASH_MISMATCH", "request key already frozen with another manifest")
        return existing
    connection.execute(
        f"INSERT INTO arp_context_requests({_CONTEXT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            value["context_id"], value["session_id"], value["agent_id"], value["turn_id"],
            int(value["provider_request_ordinal"]), int(value["session_generation"]), int(value["journal_highwater"]),
            manifest_hash, _json_column(value), value["planned_request_hash"], original_request_key,
            int(value["input_token_charge"]), int(value["effective_input_budget"]), int(value["reserved_output_tokens"]),
            int(catalog_epoch), _json_column(dict(source_read_set)), int(now_ms),
        ),
    )
    created = read_context(connection, value["context_id"])
    assert created is not None
    return created


# ---- context recalls (R3 coordination row) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextRecallRow:
    recall_key: str
    session_id: str
    agent_id: str
    turn_id: str
    original_request_key: str
    provider_request_ordinal: int
    control_generation: int
    request_hash: str
    request: Mapping[str, Any]
    query_id: str
    index_snapshot_ref: Pin | None
    embedding_invocation_ref: Pin | None
    phase: str
    checkpoint: Mapping[str, Any]
    result: Mapping[str, Any] | None
    result_hash: str | None
    deadline_ms: int
    next_wake_at_ms: int
    row_version: int

    @property
    def terminal(self) -> bool:
        return self.phase in ("READY", "SKIPPED", "BLOCKED", "STALE")


_RECALL_COLUMNS = (
    "recall_key,session_id,agent_id,turn_id,original_request_key,provider_request_ordinal,control_generation,"
    "request_hash,request_json,query_id,index_snapshot_ref_json,embedding_invocation_ref_json,phase,progress_json,"
    "result_json,result_hash,deadline_ms,next_wake_at_ms,row_version"
)


def _recall(row: sqlite3.Row | tuple) -> ContextRecallRow:
    return ContextRecallRow(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]), int(row[6]), str(row[7]),
        _load(row[8]), str(row[9]),
        None if row[10] is None else Pin.from_json(_load(row[10])),
        None if row[11] is None else Pin.from_json(_load(row[11])),
        str(row[12]), _load(row[13]),
        None if row[14] is None else _load(row[14]),
        None if row[15] is None else str(row[15]),
        int(row[16]), int(row[17]), int(row[18]),
    )


def read_context_recall(connection: sqlite3.Connection, recall_key: str) -> ContextRecallRow | None:
    row = connection.execute(f"SELECT {_RECALL_COLUMNS} FROM arp_context_recalls WHERE recall_key=?", (recall_key,)).fetchone()
    return None if row is None else _recall(row)


def get_context_recall_exact(connection: sqlite3.Connection, original_request_key: str) -> ContextRecallRow | None:
    row = connection.execute(
        f"SELECT {_RECALL_COLUMNS} FROM arp_context_recalls WHERE original_request_key=?", (original_request_key,)
    ).fetchone()
    return None if row is None else _recall(row)


def list_pending_recalls(
    connection: sqlite3.Connection, *, limit: int = 8, session_id: str | None = None
) -> tuple[ContextRecallRow, ...]:
    scope = "" if session_id is None else " AND session_id=?"
    args: tuple[Any, ...] = (int(limit),) if session_id is None else (session_id, int(limit))
    rows = connection.execute(
        f"SELECT {_RECALL_COLUMNS} FROM arp_context_recalls WHERE phase NOT IN ('READY','SKIPPED','BLOCKED','STALE')"
        f"{scope} ORDER BY next_wake_at_ms, recall_key LIMIT ?",
        args,
    ).fetchall()
    return tuple(_recall(r) for r in rows)


def put_context_recall_locked(connection: sqlite3.Connection, *, request: Json, checkpoint: Json) -> ContextRecallRow:
    """C0: freeze the ``ContextRecallRequest`` and its PREPARING checkpoint under one identity."""

    require_transaction(connection)
    value = check("ContextRecallRequest", dict(request))
    request_hash = digest(value)
    point = check("ContextRecallCheckpoint", dict(checkpoint))
    if point["phase"] != "PREPARING" or point["request_hash"] != request_hash or point["recall_key"] != value["recall_key"]:
        raise ArpError("RECALL_BINDING_INVALID", "initial checkpoint must be PREPARING for this request")
    if point["deadline_ms"] != value["deadline_ms"]:
        raise ArpError("RECALL_BINDING_INVALID", "checkpoint deadline differs from the request")
    existing = get_context_recall_exact(connection, value["original_request_key"])
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ArpError("RECALL_BINDING_INVALID", "request key already coordinated with another request")
        return existing
    connection.execute(
        f"INSERT INTO arp_context_recalls({_RECALL_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,'PREPARING',?,NULL,NULL,?,?,1)",
        (
            value["recall_key"], value["session_id"], value["agent_id"], value["turn_id"], value["original_request_key"],
            int(value["provider_request_ordinal"]), int(value["control_generation"]), request_hash, _json_column(value),
            point["query_id"], _json_column(point), int(value["deadline_ms"]), int(point["next_wake_at_ms"]),
        ),
    )
    created = read_context_recall(connection, value["recall_key"])
    assert created is not None
    return created


def cas_context_recall_locked(
    connection: sqlite3.Connection,
    current: ContextRecallRow,
    *,
    checkpoint: Json,
    result: Json | None = None,
) -> ContextRecallRow:
    """Advance one recall row by CAS on ``row_version``; terminal rows never change."""

    require_transaction(connection)
    if current.terminal:
        raise ArpError("RECALL_PREPARE_BLOCKED", "recall already terminal")
    point = check("ContextRecallCheckpoint", dict(checkpoint))
    if point["recall_key"] != current.recall_key or point["request_hash"] != current.request_hash:
        raise ArpError("RECALL_BINDING_INVALID", "checkpoint identity differs from the row")
    if point["query_id"] != current.query_id or point["deadline_ms"] != current.deadline_ms:
        raise ArpError("RECALL_BINDING_INVALID", "checkpoint query/deadline immutable")
    if current.checkpoint.get("mode") is not None and point["mode"] != current.checkpoint["mode"]:
        raise ArpError("RECALL_BINDING_INVALID", "mode is frozen once chosen")
    if current.checkpoint.get("index_snapshot_ref") is not None and point["index_snapshot_ref"] != current.checkpoint["index_snapshot_ref"]:
        raise ArpError("RECALL_BINDING_INVALID", "snapshot is frozen once chosen")
    if point["last_observed_at_ms"] < int(current.checkpoint["last_observed_at_ms"]):
        raise ArpError("CLOCK_ROLLBACK", "observed clock went backwards")
    phase = point["phase"]
    result_json: str | None = None
    result_hash: str | None = None
    if phase in ("READY", "SKIPPED"):
        if result is None:
            raise ArpError("RECALL_BINDING_INVALID", "terminal READY/SKIPPED needs its result")
        value = check("ContextRecallResult", dict(result))
        if value["recall_key"] != current.recall_key or value["request_hash"] != current.request_hash:
            raise ArpError("RECALL_BINDING_INVALID", "result identity differs from the row")
        if (value["outcome"] == "READY") != (phase == "READY"):
            raise ArpError("RECALL_BINDING_INVALID", "result outcome differs from the phase")
        result_json = _json_column(value)
        result_hash = digest(value)
    elif result is not None:
        raise ArpError("RECALL_BINDING_INVALID", "only READY/SKIPPED carry a result")
    snapshot_ref = point["index_snapshot_ref"]
    embedding_ref = point["embedding_invocation_ref"]
    updated = connection.execute(
        "UPDATE arp_context_recalls SET phase=?, progress_json=?, result_json=?, result_hash=?, next_wake_at_ms=?,"
        " index_snapshot_ref_json=COALESCE(index_snapshot_ref_json, ?), embedding_invocation_ref_json=COALESCE(embedding_invocation_ref_json, ?),"
        " row_version=row_version+1 WHERE recall_key=? AND row_version=?",
        (
            phase, _json_column(point), result_json, result_hash, int(point["next_wake_at_ms"]),
            None if snapshot_ref is None else _json_column(snapshot_ref),
            None if embedding_ref is None else _json_column(embedding_ref),
            current.recall_key, current.row_version,
        ),
    ).rowcount
    if updated != 1:
        raise ArpError("RECALL_BINDING_INVALID", "recall row changed concurrently")
    after = read_context_recall(connection, current.recall_key)
    assert after is not None
    return after


# ---- index publications (the only active-generation pointer) --------------------------------


@dataclass(frozen=True, slots=True)
class IndexPublicationRow:
    session_id: str
    index_generation: int
    partition_id: str
    generation_manifest_hash: str
    source_highwater: int
    state: str
    publish_receipt_ref: Pin

    @property
    def pin(self) -> Pin:
        return Pin("index_generation", f"{self.session_id}:gen:{self.index_generation}", self.index_generation, self.generation_manifest_hash)


def _publication(row: sqlite3.Row | tuple) -> IndexPublicationRow:
    return IndexPublicationRow(str(row[0]), int(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]), Pin.from_json(_load(row[6])))


_PUBLICATION_COLUMNS = "session_id,index_generation,partition_id,generation_manifest_hash,source_highwater,state,publish_receipt_ref_json"


def active_index_publication(connection: sqlite3.Connection, session_id: str) -> IndexPublicationRow | None:
    row = connection.execute(
        f"SELECT {_PUBLICATION_COLUMNS} FROM arp_index_publications WHERE session_id=? AND state='ACTIVE' ORDER BY index_generation DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else _publication(row)


def publish_index_generation_locked(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    index_generation: int,
    partition_id: str,
    generation_manifest_hash: str,
    source_highwater: int,
    publish_receipt_ref: Pin,
) -> IndexPublicationRow:
    """Adopt one READY partition generation centrally; an older ACTIVE one is RETIRED."""

    require_transaction(connection)
    publish_receipt_ref.require_kind("receipt")
    row = connection.execute(
        f"SELECT {_PUBLICATION_COLUMNS} FROM arp_index_publications WHERE session_id=? AND index_generation=?",
        (session_id, index_generation),
    ).fetchone()
    if row is not None:
        existing = _publication(row)
        if existing.generation_manifest_hash != generation_manifest_hash or existing.partition_id != partition_id:
            raise ArpError("GENERATION_STALE", "generation already published with another manifest")
        return existing
    current = active_index_publication(connection, session_id)
    if current is not None:
        if current.index_generation > index_generation:
            raise ArpError("GENERATION_STALE", "a newer generation is already active")
        connection.execute(
            "UPDATE arp_index_publications SET state='RETIRED' WHERE session_id=? AND index_generation=? AND state='ACTIVE'",
            (session_id, current.index_generation),
        )
    connection.execute(
        f"INSERT INTO arp_index_publications({_PUBLICATION_COLUMNS}) VALUES (?,?,?,?,?,'ACTIVE',?)",
        (session_id, index_generation, partition_id, generation_manifest_hash, int(source_highwater), _pin_column(publish_receipt_ref)),
    )
    created = active_index_publication(connection, session_id)
    assert created is not None
    return created


# ---- host command ledger (HOST-DTOS §1) -------------------------------------------------------


def read_host_command(connection: sqlite3.Connection, command_id: str) -> Mapping[str, Any] | None:
    row = connection.execute(
        "SELECT verb,subject_id,command_hash,body_json,body_hash FROM arp_host_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    if row is None:
        return None
    return {"command_id": command_id, "verb": str(row[0]), "subject_id": str(row[1]), "command_hash": str(row[2]), "body": _load(row[3]), "body_hash": str(row[4])}


def put_host_command_locked(
    connection: sqlite3.Connection, *, command_id: str, verb: str, subject_id: str, command_hash: str, body: Json
) -> Mapping[str, Any]:
    """First write wins; the same command id with another command hash is a conflict."""

    require_transaction(connection)
    existing = read_host_command(connection, command_id)
    if existing is not None:
        if existing["command_hash"] != command_hash:
            raise ArpError("EXPECTED_REVISION_MISMATCH", "command id reused with another body")
        return existing
    value = dict(body)
    connection.execute(
        "INSERT INTO arp_host_commands(command_id,verb,subject_id,command_hash,body_json,body_hash) VALUES (?,?,?,?,?,?)",
        (_text(command_id, "command_id"), _text(verb, "verb"), _text(subject_id, "subject_id"), command_hash, _json_column(value), digest(value)),
    )
    created = read_host_command(connection, command_id)
    assert created is not None
    return created


# ---- retention permits (R1: the only key that opens a retained row's DELETE guard) -------------


def retention_row_key(parts: Sequence[object]) -> str:
    """The exact text SQLite's ``json_array(...)`` produces for the same values."""

    return json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))


def put_retention_permit_locked(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    row_key: Sequence[object],
    body_hash: str,
    expires_at_ms: int,
    source_receipt_ref: Pin,
) -> Mapping[str, Any]:
    """One permit per (table, row, body hash); a same-identity replay returns the stored row."""

    require_transaction(connection)
    source_receipt_ref.require_kind("receipt")
    if type(body_hash) is not str or len(body_hash) != 64:
        raise ArpError("STRING_PATTERN", field_path="body_hash")
    if type(expires_at_ms) is not int or expires_at_ms < 0:
        raise ArpError("NUMBER_LIMIT", field_path="expires_at_ms")
    key = retention_row_key(row_key)
    existing = connection.execute(
        "SELECT expires_at_ms, source_receipt_ref_json FROM arp_retention_permits WHERE table_name=? AND row_key=? AND body_hash=?",
        (table_name, key, body_hash),
    ).fetchone()
    if existing is not None:
        if Pin.from_json(_load(existing[1])) != source_receipt_ref:
            raise ArpError("SOURCE_HASH_CONFLICT", "retention permit already granted from another receipt")
        return {"table_name": table_name, "row_key": key, "body_hash": body_hash, "expires_at_ms": int(existing[0]), "source_receipt_ref": source_receipt_ref.to_json()}
    connection.execute(
        "INSERT INTO arp_retention_permits(table_name,row_key,body_hash,expires_at_ms,source_receipt_ref_json) VALUES (?,?,?,?,?)",
        (_text(table_name, "table_name"), key, body_hash, expires_at_ms, _pin_column(source_receipt_ref)),
    )
    return {"table_name": table_name, "row_key": key, "body_hash": body_hash, "expires_at_ms": expires_at_ms, "source_receipt_ref": source_receipt_ref.to_json()}


# ---- original receipts (run_events rows for facts that have no table of their own) -----------


def put_skill_use_locked(connection: sqlite3.Connection, *, use: Mapping[str, Any], use_key: str, original_call_ref: Pin | None) -> Mapping[str, Any]:
    """Persist one immutable ``SkillUse``; the same key with the same body replays."""

    require_transaction(connection)
    use_hash = digest(use)
    existing = connection.execute("SELECT use_hash, body_json FROM arp_skill_uses WHERE session_id=? AND use_key=?", (use["session_id"], use_key)).fetchone()
    if existing is not None:
        if str(existing[0]) != use_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", "skill use key reused with another body")
        return _load(existing[1])
    connection.execute(
        "INSERT INTO arp_skill_uses(use_id,session_id,turn_id,use_key,use_hash,body_json,original_call_ref_json) VALUES (?,?,?,?,?,?,?)",
        (use["use_id"], use["session_id"], use["turn_id"], use_key, use_hash, _json_column(use), None if original_call_ref is None else _json_column(original_call_ref.to_json())),
    )
    return use


def read_skill_uses(connection: sqlite3.Connection, session_id: str, *, mode: str | None = None) -> tuple[Mapping[str, Any], ...]:
    rows = connection.execute("SELECT body_json FROM arp_skill_uses WHERE session_id=? ORDER BY rowid ASC", (session_id,)).fetchall()
    uses = tuple(_load(r[0]) for r in rows)
    return uses if mode is None else tuple(u for u in uses if u["mode"] == mode)


def read_skill_use(connection: sqlite3.Connection, use_id: str) -> Mapping[str, Any] | None:
    raw = connection.execute("SELECT body_json FROM arp_skill_uses WHERE use_id=?", (use_id,)).fetchone()
    return None if raw is None else _load(raw[0])


def append_original_receipt_locked(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    receipt_key: str,
    body: Json,
    now: float,
) -> Pin:
    """Persist one immutable fact as an original run event; same key + same body replays."""

    require_transaction(connection)
    value = dict(body)
    body_hash = digest(value)
    event_id = f"arp:{kind}:{digest({'key': receipt_key})[:32]}"
    existing = connection.execute("SELECT payload_json FROM run_events WHERE event_id=?", (event_id,)).fetchone()
    if existing is not None:
        stored = _load(existing[0])
        if stored.get("body_hash") != body_hash:
            raise ArpError("SOURCE_HASH_CONFLICT", f"{kind} receipt key reused with another body")
        return Pin("receipt", f"{kind}:{receipt_key}", 0, body_hash)
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(durable_seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    )
    payload = {"schema_version": 1, "kind": kind, "receipt_key": receipt_key, "body_hash": body_hash, "body": value}
    connection.execute(
        "INSERT INTO run_events(event_id, run_id, durable_seq, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, run_id, sequence, f"arp.{kind}.v1", _json_column(payload), now),
    )
    from simple_harness.execution.sqlite.audit_witness import record_event_witness

    record_event_witness(connection, event_id)
    return Pin("receipt", f"{kind}:{receipt_key}", 0, body_hash)


def read_original_receipt(connection: sqlite3.Connection, *, kind: str, receipt_key: str) -> Mapping[str, Any] | None:
    event_id = f"arp:{kind}:{digest({'key': receipt_key})[:32]}"
    row = connection.execute("SELECT payload_json FROM run_events WHERE event_id=?", (event_id,)).fetchone()
    return None if row is None else _load(row[0])["body"]


def json_text(value: object) -> str:
    """Canonical JSON text for callers that store bodies in their own columns."""

    return _json_column(value)


def load_json(text: str) -> Any:
    return _load(text)


__all__ = (
    "ContextRecallRow",
    "ContextRequestRow",
    "IndexPublicationRow",
    "CreationIntentRow",
    "EVENT_TYPES",
    "EventBindingRow",
    "JOB_KINDS",
    "JOB_STATES",
    "JobRow",
    "PolicyAdoptionRow",
    "PolicyObjectRow",
    "ProfileRow",
    "ProtocolRow",
    "SESSION_STATES",
    "SessionRow",
    "append_policy_adoption_locked",
    "append_runtime_event_locked",
    "claim_jobs_locked",
    "complete_job_locked",
    "finalize_creation_locked",
    "insert_session_locked",
    "json_text",
    "latest_adoption",
    "latest_policy_revision",
    "list_event_bindings",
    "list_sessions_in_state",
    "load_json",
    "put_blob_root_locked",
    "put_creation_intent_locked",
    "put_job_locked",
    "put_policy_object_locked",
    "put_profile_locked",
    "put_protocol_locked",
    "read_adoption_by_command",
    "read_creation_intent",
    "read_job",
    "read_job_by_key",
    "read_live_session",
    "read_policy_object",
    "read_profile",
    "read_protocol",
    "read_session",
    "read_session_by_creation_key",
    "require_transaction",
    "transition_session_locked",
)
