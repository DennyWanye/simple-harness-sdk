# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for the BaseAgent control plane (Slice 2 · T3).

All functions run inside the caller's transaction; they never open one.  The only
state they touch is the binding's ``lifecycle`` / ``control_generation`` columns
and ``base_agent_control_commands_v1``.  Nothing here calls the kernel: ``close``
is a persisted intent, never a Run cancel.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from simple_harness.contracts import JsonValue, canonical_json, freeze_json, thaw_json
from simple_harness.execution.base_agent import (
    AGENT_LIFECYCLES,
    AgentBindingRecord,
    AgentControlCommandRecord,
)
from simple_harness.execution.uow import UnitOfWorkConflict

from .turns import read_binding

_LIFECYCLE_NEXT: dict[str, tuple[str, ...]] = {
    "open": ("open", "closing", "closed"),
    "closing": ("closing", "closed"),
    "closed": ("closed",),
}


def _command(row: sqlite3.Row) -> AgentControlCommandRecord:
    return AgentControlCommandRecord(
        command_id=str(row["command_id"]),
        agent_id=str(row["agent_id"]),
        kind=str(row["kind"]),
        target_turn_id=None if row["target_turn_id"] is None else str(row["target_turn_id"]),
        control_generation=int(row["control_generation"]),
        request_hash=str(row["request_hash"]),
        receipt=json.loads(str(row["receipt_json"])),
        created_at=float(row["created_at"]),
    )


def read_binding_lifecycle(connection: sqlite3.Connection, agent_id: str) -> str | None:
    row = connection.execute(
        "SELECT lifecycle FROM base_agent_bindings_v1 WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return None if row is None else str(row["lifecycle"])


def read_control_command(
    connection: sqlite3.Connection, command_id: str
) -> AgentControlCommandRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_control_commands_v1 WHERE command_id=?", (command_id,)
    ).fetchone()
    return None if row is None else _command(row)


def record_control_command(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    agent_id: str,
    kind: str,
    target_turn_id: str | None,
    control_generation: int,
    request_hash: str,
    receipt: Mapping[str, JsonValue],
    now: float,
) -> tuple[AgentControlCommandRecord, bool]:
    """Persist one control command; a same-hash replay returns the stored row."""

    existing = read_control_command(connection, command_id)
    if existing is not None:
        if existing.request_hash != request_hash or existing.agent_id != agent_id:
            raise UnitOfWorkConflict("command_id reused with a different control request")
        return existing, False
    connection.execute(
        "INSERT INTO base_agent_control_commands_v1(command_id,agent_id,kind,target_turn_id,"
        "control_generation,request_hash,receipt_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            command_id,
            agent_id,
            kind,
            target_turn_id,
            int(control_generation),
            request_hash,
            canonical_json(thaw_json(freeze_json(dict(receipt)))),
            float(now),
        ),
    )
    stored = read_control_command(connection, command_id)
    assert stored is not None
    return stored, True


def set_lifecycle(
    connection: sqlite3.Connection, *, agent_id: str, lifecycle: str, now: float
) -> AgentBindingRecord:
    """Move ``open -> closing -> closed`` (one way); a same-state replay is a no-op."""

    if lifecycle not in AGENT_LIFECYCLES:
        raise ValueError("unknown agent lifecycle")
    current = read_binding_lifecycle(connection, agent_id)
    if current is None:
        raise UnitOfWorkConflict("agent binding is missing")
    if lifecycle not in _LIFECYCLE_NEXT[current]:
        raise UnitOfWorkConflict(f"agent lifecycle cannot move from {current} to {lifecycle}")
    if lifecycle != current:
        connection.execute(
            "UPDATE base_agent_bindings_v1 SET lifecycle=?, lifecycle_updated_at=? "
            "WHERE agent_id=? AND lifecycle=?",
            (lifecycle, float(now), agent_id, current),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise UnitOfWorkConflict("agent lifecycle changed concurrently")
    binding = read_binding(connection, agent_id)
    assert binding is not None
    return binding


def read_pending_cancel_for_turn(
    connection: sqlite3.Connection, turn_id: str
) -> AgentControlCommandRecord | None:
    """The durable cancel intent for one turn, if any (oldest command wins)."""

    row = connection.execute(
        "SELECT * FROM base_agent_control_commands_v1 WHERE kind='cancel_turn' "
        "AND target_turn_id=? ORDER BY created_at, command_id LIMIT 1",
        (turn_id,),
    ).fetchone()
    return None if row is None else _command(row)


def bump_control_generation(connection: sqlite3.Connection, agent_id: str) -> int:
    connection.execute(
        "UPDATE base_agent_bindings_v1 SET control_generation=control_generation+1 "
        "WHERE agent_id=?",
        (agent_id,),
    )
    row = connection.execute(
        "SELECT control_generation FROM base_agent_bindings_v1 WHERE agent_id=?", (agent_id,)
    ).fetchone()
    if row is None:
        raise UnitOfWorkConflict("agent binding is missing")
    return int(row["control_generation"])


__all__ = (
    "bump_control_generation",
    "read_binding_lifecycle",
    "read_control_command",
    "read_pending_cancel_for_turn",
    "record_control_command",
    "set_lifecycle",
)
