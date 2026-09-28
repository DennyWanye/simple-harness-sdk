# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""MISSION owner mode: the exact Mission sources an Agent is created from (NEXT-TG-1.0 §10).

A MISSION-mode Session is created only from sources the deployment's original
Mission authority names (``MissionSourcePort``, implemented by the orchestrator over
its own records — never a dict a Host labels "MISSION"):

* ``bind(caller=, role=)`` returns the role-typed source set for the dispatch intent the
  caller's receipt names (Worker Attempt + frozen InputManifest, reviewer package +
  purpose, planner request, method synthesis goal ...), or raises a named refusal;
* ``require_current(sources)`` re-reads the same authority and refuses by name when the
  sources moved (other intent, other occurrence, stale input, revoked, old generation).

Creation records the bound set once, as an immutable original receipt keyed by the
Session (same key + same body replays; another body is ``SOURCE_HASH_CONFLICT``), and
its hash enters the creation command hash, so the same creation key can never replay
into another task, occurrence or profile.  Every new (non-replayed) context freeze
re-checks the recorded set before the request can be handed off and pins it in the
manifest's authority refs; a replay of an already frozen request reads what it froze.

A delegated child inherits its parent's recorded Mission sources (its own receipt is
the delegation), so a child is never created without the parent's exact sources.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Protocol

from . import store
from .errors import ArpError
from .pins import Pin
from .ports import TrustedCaller
from .strict import digest

RECEIPT_KIND = "mission-sources"
SCHEMA = "mission-sources/v1"
SOURCE_KINDS = frozenset({"worker", "reviewer", "critic", "planner", "method_synthesizer", "service"})


class MissionSourcePort(Protocol):
    def bind(self, *, caller: TrustedCaller, role: str) -> Mapping[str, Any]: ...

    def require_current(self, sources: Mapping[str, Any]) -> None: ...


def owner_mode_of(connection: sqlite3.Connection, session: store.SessionRow) -> str:
    """The owner mode of the profile revision the Session was created under."""

    row = store.read_profile(connection, session.profile_id, session.profile_revision)
    if row is None:
        raise ArpError("PROFILE_SOURCE_MISSING", "the Session's profile revision is not stored")
    return str(row.body["owner_mode"])


def owner_contract_for(connection: sqlite3.Connection, session: store.SessionRow) -> Pin:
    mode = owner_mode_of(connection, session)
    return Pin("policy", f"{session.profile_id}:owner-mode:{mode}", session.profile_revision, session.profile_hash)


def _checked(sources: Any, caller: TrustedCaller) -> dict[str, Any]:
    if not isinstance(sources, Mapping):
        raise ArpError("SOURCE_UNAVAILABLE", "the Mission source reader returned no source set")
    value = dict(sources)
    if value.get("schema") != SCHEMA or value.get("source_kind") not in SOURCE_KINDS:
        raise ArpError("UNION_MISMATCH", "Mission sources are not a known role-typed source set",
                       detail={"schema": value.get("schema"), "source_kind": value.get("source_kind")})
    intent_id = value.get("intent_id")
    if not isinstance(intent_id, str) or caller.command_receipt_ref.id != f"dispatch-intent:{intent_id}":
        raise ArpError("REF_IDENTITY_MISMATCH", "Mission sources name another dispatch intent than the caller")
    if not isinstance(value.get("mission_id"), str) or not value["mission_id"]:
        raise ArpError("SOURCE_UNAVAILABLE", "Mission sources carry no Mission")
    return value


def read_record(connection: sqlite3.Connection, session_id: str) -> Mapping[str, Any] | None:
    return store.read_original_receipt(connection, kind=RECEIPT_KIND, receipt_key=session_id)


def record_for_creation(
    port: MissionSourcePort | None, connection: sqlite3.Connection, *, caller: TrustedCaller, role: str
) -> dict[str, Any]:
    """The source record a MISSION-mode creation binds, or a named refusal."""

    if port is None:
        raise ArpError("SOURCE_UNAVAILABLE", "MISSION owner mode needs the Mission source reader")
    if role == "child":
        principal = caller.principal_ref.id
        if not principal.startswith("agent:"):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "a delegated child needs its parent Agent as caller")
        parent_agent_id = principal[len("agent:"):]
        parent = store.read_live_session(connection, parent_agent_id)
        inherited = None if parent is None else read_record(connection, parent.session_id)
        if inherited is None:
            raise ArpError("SOURCE_UNAVAILABLE", "the parent Agent has no recorded Mission sources")
        sources = dict(inherited["sources"])
        port.require_current(sources)
        return {
            "schema_version": 1,
            "role": role,
            "sources": sources,
            "sources_hash": digest(sources),
            "delegated_from": {"parent_agent_id": parent_agent_id, "delegation_receipt_ref": caller.command_receipt_ref.to_json()},
        }
    sources = _checked(port.bind(caller=caller, role=role), caller)
    port.require_current(sources)
    return {"schema_version": 1, "role": role, "sources": sources, "sources_hash": digest(sources), "delegated_from": None}


def record_pin(session_id: str, record: Mapping[str, Any]) -> Pin:
    return Pin("authority", f"{RECEIPT_KIND}:{session_id}", 0, digest(dict(record)))


def require_current(port: MissionSourcePort | None, connection: sqlite3.Connection, session: store.SessionRow) -> Pin | None:
    """Before a new request is frozen: a MISSION Session's recorded sources must be current.

    Returns the pin the manifest carries, or ``None`` for a Session created under a
    STANDALONE_CHAT profile revision (its sources were never Mission sources).
    """

    if owner_mode_of(connection, session) != "MISSION":
        return None
    record = read_record(connection, session.session_id)
    if record is None:
        raise ArpError("SOURCE_UNAVAILABLE", "the MISSION Session has no recorded Mission sources")
    if port is None:
        raise ArpError("SOURCE_UNAVAILABLE", "MISSION owner mode needs the Mission source reader")
    port.require_current(dict(record["sources"]))
    return record_pin(session.session_id, record)


def recorded_pin(connection: sqlite3.Connection, session: store.SessionRow) -> Pin | None:
    record = read_record(connection, session.session_id)
    return None if record is None else record_pin(session.session_id, record)


__all__ = (
    "MissionSourcePort",
    "RECEIPT_KIND",
    "SCHEMA",
    "SOURCE_KINDS",
    "owner_contract_for",
    "owner_mode_of",
    "read_record",
    "record_for_creation",
    "record_pin",
    "recorded_pin",
    "require_current",
)
