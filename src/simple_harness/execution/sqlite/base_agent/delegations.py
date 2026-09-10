# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for ``agent.delegate`` records (schema v10).

The pre-fence step (closure E1/E2) writes, in the caller's transaction and before
the child Run exists: the ``base-agent/v1`` namespace binding, the child's
``conversation_run_modes`` row, its context-use admission row and the delegation
row in state ``reserved``.  The child binding row is written only after the
kernel launched the child (its ``run_id`` foreign key needs the runs row).
"""

from __future__ import annotations

import sqlite3

from simple_harness.execution.base_agent import AgentDelegationRecord
from simple_harness.execution.uow import UnitOfWorkConflict

BASE_AGENT_NAMESPACE = "base-agent/v1"
BASE_AGENT_PROJECTION_KEY_ID = "base-agent-v1"


def _record(row: sqlite3.Row) -> AgentDelegationRecord:
    return AgentDelegationRecord(
        delegation_id=str(row["delegation_id"]),
        parent_agent_id=str(row["parent_agent_id"]),
        parent_turn_id=str(row["parent_turn_id"]),
        ordinal=int(row["ordinal"]),
        child_agent_id=str(row["child_agent_id"]),
        child_run_id=str(row["child_run_id"]),
        ticket_id=str(row["ticket_id"]),
        state=str(row["state"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def read_delegation(
    connection: sqlite3.Connection, delegation_id: str
) -> AgentDelegationRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_delegations_v1 WHERE delegation_id=?", (delegation_id,)
    ).fetchone()
    return None if row is None else _record(row)


def count_for_turn(connection: sqlite3.Connection, parent_turn_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM base_agent_delegations_v1 WHERE parent_turn_id=?",
            (parent_turn_id,),
        ).fetchone()[0]
    )


def reserve_delegation(
    connection: sqlite3.Connection,
    *,
    delegation_id: str,
    parent_agent_id: str,
    parent_turn_id: str,
    child_agent_id: str,
    child_run_id: str,
    ticket_id: str,
    intent_hash: str,
    context_use_scope: str | None,
    now: float,
) -> AgentDelegationRecord:
    """Fence the future child Run and record the delegation as ``reserved`` (one tx)."""

    from simple_harness.execution.command_ingress import RunApiMode, _bind_namespace

    from ..context_use_requirements import bind

    existing = read_delegation(connection, delegation_id)
    if existing is not None:
        if (
            existing.parent_agent_id != parent_agent_id
            or existing.parent_turn_id != parent_turn_id
            or existing.child_agent_id != child_agent_id
        ):
            raise UnitOfWorkConflict("delegation_id reused for a different delegation")
        return existing
    ordinal = count_for_turn(connection, parent_turn_id) + 1
    _bind_namespace(connection, BASE_AGENT_NAMESPACE, BASE_AGENT_PROJECTION_KEY_ID, now)
    mode = connection.execute(
        "SELECT namespace,api_mode,intent_hash FROM conversation_run_modes WHERE run_id=?",
        (child_run_id,),
    ).fetchone()
    if mode is None:
        connection.execute(
            "INSERT INTO conversation_run_modes(run_id,namespace,api_mode,intent_hash,created_at)"
            " VALUES (?,?,?,?,?)",
            (child_run_id, BASE_AGENT_NAMESPACE, RunApiMode.LEGACY.value, intent_hash, now),
        )
        bind(
            connection,
            child_run_id,
            context_use_scope,
            "legacy_admission",
            child_run_id,
            intent_hash,
        )
    elif tuple(mode) != (BASE_AGENT_NAMESPACE, RunApiMode.LEGACY.value, intent_hash):
        raise UnitOfWorkConflict("child Run identity is already fenced differently")
    connection.execute(
        "INSERT INTO base_agent_delegations_v1(delegation_id,parent_agent_id,parent_turn_id,"
        "ordinal,child_agent_id,child_run_id,ticket_id,state,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,'reserved',?,?)",
        (
            delegation_id,
            parent_agent_id,
            parent_turn_id,
            ordinal,
            child_agent_id,
            child_run_id,
            ticket_id,
            now,
            now,
        ),
    )
    created = read_delegation(connection, delegation_id)
    assert created is not None
    return created


def set_state(
    connection: sqlite3.Connection, *, delegation_id: str, state: str, now: float
) -> AgentDelegationRecord:
    allowed = {
        "reserved": {"launched", "failed"},
        "launched": {"settled", "failed", "launched"},
        "settled": {"settled"},
        "failed": {"failed"},
    }
    current = read_delegation(connection, delegation_id)
    if current is None:
        raise UnitOfWorkConflict("delegation is missing")
    if state not in allowed[current.state]:
        raise UnitOfWorkConflict(f"delegation cannot move from {current.state} to {state}")
    if state != current.state:
        connection.execute(
            "UPDATE base_agent_delegations_v1 SET state=?, updated_at=? WHERE delegation_id=?",
            (state, now, delegation_id),
        )
    updated = read_delegation(connection, delegation_id)
    assert updated is not None
    return updated


__all__ = ("count_for_turn", "read_delegation", "reserve_delegation", "set_state")
