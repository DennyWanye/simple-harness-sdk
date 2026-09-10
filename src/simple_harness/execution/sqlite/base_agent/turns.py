# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for BaseAgent bindings, turns and turn results.

Every function takes the caller's open ``sqlite3.Connection`` and never begins,
commits or rolls back a transaction.  ``SqliteExecutionUnitOfWork`` owns the
transaction and exposes thin facades over these helpers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from simple_harness.contracts import JsonValue, canonical_json, thaw_json
from simple_harness.execution.base_agent import (
    BASE_AGENT_API_MODE,
    AgentBindingRecord,
    AgentTurnRecord,
    AgentTurnResultRecord,
)
from simple_harness.execution.uow import UnitOfWorkConflict


def _binding(row: sqlite3.Row) -> AgentBindingRecord:
    return AgentBindingRecord(
        agent_id=str(row["agent_id"]),
        run_id=str(row["run_id"]),
        owner_scope=str(row["owner_scope"]),
        api_mode=str(row["api_mode"]),
        role=str(row["role"]),
        creation_key=str(row["creation_key"]),
        config_json=json.loads(str(row["config_json"])),
        config_hash=str(row["config_hash"]),
        control_generation=int(row["control_generation"]),
        created_at=float(row["created_at"]),
    )


def _turn(row: sqlite3.Row) -> AgentTurnRecord:
    staged = row["staged_result_json"]
    return AgentTurnRecord(
        turn_id=str(row["turn_id"]),
        agent_id=str(row["agent_id"]),
        input_id=str(row["input_id"]),
        input_hash=str(row["input_hash"]),
        input_json=json.loads(str(row["input_json"])),
        continuation_id=None if row["continuation_id"] is None else str(row["continuation_id"]),
        seq=int(row["seq"]),
        phase=str(row["phase"]),
        staged_result_hash=(
            None if row["staged_result_hash"] is None else str(row["staged_result_hash"])
        ),
        staged_result_json=None if staged is None else json.loads(str(staged)),
        provider_turn_ordinal_from=(
            None
            if row["provider_turn_ordinal_from"] is None
            else int(row["provider_turn_ordinal_from"])
        ),
        provider_turn_ordinal_to=(
            None
            if row["provider_turn_ordinal_to"] is None
            else int(row["provider_turn_ordinal_to"])
        ),
        lease_epoch=None if row["lease_epoch"] is None else int(row["lease_epoch"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _result(row: sqlite3.Row) -> AgentTurnResultRecord:
    refs = row["usage_refs_json"]
    return AgentTurnResultRecord(
        turn_id=str(row["turn_id"]),
        agent_id=str(row["agent_id"]),
        result_hash=str(row["result_hash"]),
        result_json=json.loads(str(row["result_json"])),
        commit_receipt_id=str(row["commit_receipt_id"]),
        usage_refs=() if refs is None else tuple(str(ref) for ref in json.loads(str(refs))),
        committed_at=float(row["committed_at"]),
    )


# --- bindings -----------------------------------------------------------------


def insert_binding(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    run_id: str,
    owner_scope: str,
    role: str,
    creation_key: str,
    config_json: Mapping[str, JsonValue],
    config_hash: str,
    now: float,
) -> AgentBindingRecord:
    """Insert one binding; replays return the identical row, differing replays conflict."""

    existing = read_binding_by_creation_key(connection, creation_key)
    if existing is not None:
        if existing.config_hash != config_hash or existing.agent_id != agent_id:
            raise UnitOfWorkConflict("creation_key reused with a different BaseAgent")
        return existing
    connection.execute(
        "INSERT INTO base_agent_bindings_v1(agent_id,run_id,owner_scope,api_mode,role,"
        "creation_key,config_json,config_hash,control_generation,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,0,?)",
        (
            agent_id,
            run_id,
            owner_scope,
            BASE_AGENT_API_MODE,
            role,
            creation_key,
            canonical_json(dict(config_json)),
            config_hash,
            now,
        ),
    )
    created = read_binding(connection, agent_id)
    assert created is not None
    return created


def read_binding(connection: sqlite3.Connection, agent_id: str) -> AgentBindingRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_bindings_v1 WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return None if row is None else _binding(row)


def read_binding_by_run(connection: sqlite3.Connection, run_id: str) -> AgentBindingRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_bindings_v1 WHERE run_id=?", (run_id,)
    ).fetchone()
    return None if row is None else _binding(row)


def read_binding_by_creation_key(
    connection: sqlite3.Connection, creation_key: str
) -> AgentBindingRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_bindings_v1 WHERE creation_key=?", (creation_key,)
    ).fetchone()
    return None if row is None else _binding(row)


# --- turns --------------------------------------------------------------------


def open_turn(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    turn_id: str,
    input_id: str,
    input_hash: str,
    input_json: Mapping[str, JsonValue],
    continuation_id: str | None,
    now: float,
) -> tuple[AgentTurnRecord, bool]:
    """Persist one queued turn per ``(agent_id, input_id)``.

    Returns ``(record, created)``; an identical replay returns the stored row with
    ``created=False``; a different input under the same ``input_id`` conflicts.
    """

    existing = read_turn_by_input(connection, agent_id, input_id)
    if existing is not None:
        if existing.input_hash != input_hash or existing.turn_id != turn_id:
            raise UnitOfWorkConflict("input_id reused with different input content")
        return existing, False
    seq = int(
        connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM base_agent_turns_v1 WHERE agent_id=?",
            (agent_id,),
        ).fetchone()[0]
    )
    connection.execute(
        "INSERT INTO base_agent_turns_v1(turn_id,agent_id,input_id,input_hash,input_json,"
        "continuation_id,seq,phase,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
        (
            turn_id,
            agent_id,
            input_id,
            input_hash,
            canonical_json(dict(input_json)),
            continuation_id,
            seq,
            now,
            now,
        ),
    )
    created = read_turn(connection, turn_id)
    assert created is not None
    return created, True


def read_turn(connection: sqlite3.Connection, turn_id: str) -> AgentTurnRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_turns_v1 WHERE turn_id=?", (turn_id,)
    ).fetchone()
    return None if row is None else _turn(row)


def read_turn_by_input(
    connection: sqlite3.Connection, agent_id: str, input_id: str
) -> AgentTurnRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_turns_v1 WHERE agent_id=? AND input_id=?",
        (agent_id, input_id),
    ).fetchone()
    return None if row is None else _turn(row)


def read_pending_turn(connection: sqlite3.Connection, agent_id: str) -> AgentTurnRecord | None:
    """The single ``result_pending`` turn of an Agent, if any (oldest first)."""

    row = connection.execute(
        "SELECT * FROM base_agent_turns_v1 WHERE agent_id=? AND phase='result_pending'"
        " ORDER BY seq ASC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return None if row is None else _turn(row)


def read_open_turn(connection: sqlite3.Connection, agent_id: str) -> AgentTurnRecord | None:
    """Oldest turn that still needs work (queued / running / result_pending)."""

    row = connection.execute(
        "SELECT * FROM base_agent_turns_v1 WHERE agent_id=? AND phase IN "
        "('queued','running','result_pending') ORDER BY seq ASC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return None if row is None else _turn(row)


def list_turns(connection: sqlite3.Connection, agent_id: str) -> tuple[AgentTurnRecord, ...]:
    return tuple(
        _turn(row)
        for row in connection.execute(
            "SELECT * FROM base_agent_turns_v1 WHERE agent_id=? ORDER BY seq ASC", (agent_id,)
        )
    )


def list_runs_with_open_turns(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Run ids whose Agent still has queued/running/result_pending turns (wake on start)."""

    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT b.run_id FROM base_agent_turns_v1 t"
            " JOIN base_agent_bindings_v1 b ON b.agent_id = t.agent_id"
            " WHERE t.phase IN ('queued','running','result_pending')"
        )
    )


def mark_turn_running(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
    lease_epoch: int,
    provider_turn_ordinal_from: int | None,
    now: float,
) -> AgentTurnRecord:
    changed = connection.execute(
        "UPDATE base_agent_turns_v1 SET phase='running', lease_epoch=?, "
        "provider_turn_ordinal_from=COALESCE(provider_turn_ordinal_from, ?), updated_at=?"
        " WHERE turn_id=? AND phase IN ('queued','running')",
        (lease_epoch, provider_turn_ordinal_from, now, turn_id),
    ).rowcount
    if changed != 1:
        raise UnitOfWorkConflict("agent turn is not runnable")
    record = read_turn(connection, turn_id)
    assert record is not None
    return record


def stage_result(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
    result_hash: str,
    result_json: Mapping[str, JsonValue],
    provider_turn_ordinal_from: int | None,
    provider_turn_ordinal_to: int | None,
    lease_epoch: int,
    now: float,
) -> AgentTurnRecord:
    """Freeze the turn result as RESULT_PENDING; identical replays are no-ops."""

    current = read_turn(connection, turn_id)
    if current is None:
        raise UnitOfWorkConflict("agent turn is missing")
    if current.phase == "result_pending":
        if current.staged_result_hash != result_hash:
            raise UnitOfWorkConflict("a different result is already staged for this turn")
        return current
    if current.phase in {"committed", "failed"}:
        raise UnitOfWorkConflict("agent turn result is already committed")
    connection.execute(
        "UPDATE base_agent_turns_v1 SET phase='result_pending', staged_result_hash=?,"
        " staged_result_json=?, provider_turn_ordinal_from=COALESCE(?, provider_turn_ordinal_from),"
        " provider_turn_ordinal_to=?, lease_epoch=?, updated_at=? WHERE turn_id=?",
        (
            result_hash,
            canonical_json(dict(result_json)),
            provider_turn_ordinal_from,
            provider_turn_ordinal_to,
            lease_epoch,
            now,
            turn_id,
        ),
    )
    record = read_turn(connection, turn_id)
    assert record is not None
    return record


def commit_staged_result(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
    commit_receipt_id: str,
    usage_refs: tuple[str, ...],
    now: float,
) -> AgentTurnResultRecord:
    """Copy the staged result into the immutable result row and close the turn."""

    turn = read_turn(connection, turn_id)
    if turn is None:
        raise UnitOfWorkConflict("agent turn is missing")
    if turn.phase != "result_pending" or turn.staged_result_hash is None:
        raise UnitOfWorkConflict("agent turn has no staged result to commit")
    assert turn.staged_result_json is not None
    body = thaw_json(turn.staged_result_json)
    assert isinstance(body, dict)
    final_phase = "failed" if body.get("state") == "failed" else "committed"
    connection.execute(
        "INSERT INTO base_agent_turn_results_v1(turn_id,agent_id,result_hash,result_json,"
        "commit_receipt_id,usage_refs_json,committed_at) VALUES (?,?,?,?,?,?,?)",
        (
            turn_id,
            turn.agent_id,
            turn.staged_result_hash,
            canonical_json(body),
            commit_receipt_id,
            json.dumps(list(usage_refs)),
            now,
        ),
    )
    connection.execute(
        "UPDATE base_agent_turns_v1 SET phase=?, updated_at=? WHERE turn_id=?",
        (final_phase, now, turn_id),
    )
    record = read_result(connection, turn_id)
    assert record is not None
    return record


def read_result(connection: sqlite3.Connection, turn_id: str) -> AgentTurnResultRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_turn_results_v1 WHERE turn_id=?", (turn_id,)
    ).fetchone()
    return None if row is None else _result(row)


__all__ = (
    "commit_staged_result",
    "insert_binding",
    "list_runs_with_open_turns",
    "list_turns",
    "mark_turn_running",
    "open_turn",
    "read_binding",
    "read_binding_by_creation_key",
    "read_binding_by_run",
    "read_open_turn",
    "read_pending_turn",
    "read_result",
    "read_turn",
    "read_turn_by_input",
    "stage_result",
)
