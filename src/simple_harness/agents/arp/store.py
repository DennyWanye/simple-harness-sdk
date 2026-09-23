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
) -> SessionRow:
    """CREATING row; a replay with identical identity returns the stored row."""

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
            or existing.creation_key != creation_key
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
) -> tuple[JobRow, ...]:
    """PENDING → LEASED (or expired LEASED re-lease), ≤limit per tick, ≤per_session each."""

    require_transaction(connection)
    rows = connection.execute(
        f"SELECT {_JOB_COLUMNS} FROM arp_jobs WHERE (state='PENDING' AND next_at_ms<=?)"
        " OR (state='LEASED' AND lease_until_ms<?) ORDER BY next_at_ms, job_id LIMIT ?",
        (now_ms, now_ms, limit * 4),
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


def json_text(value: object) -> str:
    """Canonical JSON text for callers that store bodies in their own columns."""

    return _json_column(value)


def load_json(text: str) -> Any:
    return _load(text)


__all__ = (
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
