# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for BaseAgent bindings, turns and turn results.

Every function takes the caller's open ``sqlite3.Connection`` and never begins,
commits or rolls back a transaction.  ``SqliteExecutionUnitOfWork`` owns the
transaction and exposes thin facades over these helpers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping

from simple_harness.contracts import JsonValue, canonical_json, thaw_json
from simple_harness.execution.base_agent import (
    BASE_AGENT_API_MODE,
    AgentBindingRecord,
    AgentTurnRecord,
    AgentTurnResultRecord,
)
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


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
        lifecycle=str(row["lifecycle"]),
        lifecycle_updated_at=(
            None if row["lifecycle_updated_at"] is None else float(row["lifecycle_updated_at"])
        ),
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
        tool_call_ordinal_from=(
            None if row["tool_call_ordinal_from"] is None else int(row["tool_call_ordinal_from"])
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

    existing = read_binding_by_creation_key(connection, owner_scope, creation_key)
    if existing is not None:
        if existing.config_hash != config_hash or existing.agent_id != agent_id:
            raise UnitOfWorkConflict("creation_key reused with a different BaseAgent")
        return existing
    connection.execute(
        "INSERT INTO base_agent_bindings_v1(agent_id,run_id,owner_scope,api_mode,role,"
        "creation_key,config_json,config_hash,control_generation,created_at,lifecycle)"
        " VALUES (?,?,?,?,?,?,?,?,0,?,'open')",
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
    connection: sqlite3.Connection, owner_scope: str, creation_key: str
) -> AgentBindingRecord | None:
    """Creation keys are unique per owner scope (BA05): two owners may share a key."""

    row = connection.execute(
        "SELECT * FROM base_agent_bindings_v1 WHERE owner_scope=? AND creation_key=?",
        (owner_scope, creation_key),
    ).fetchone()
    return None if row is None else _binding(row)


# --- turns --------------------------------------------------------------------


class PendingInputsExhausted(UnitOfWorkConflict):
    """The Agent's queue of open turns is full (``AgentLimits.max_pending_inputs``)."""


class AgentClosedError(UnitOfWorkConflict):
    """The Agent no longer accepts ordinary inputs (lifecycle closing/closed)."""


def count_open_turns(connection: sqlite3.Connection, agent_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM base_agent_turns_v1 WHERE agent_id=? AND phase IN "
            "('queued','running','result_pending')",
            (agent_id,),
        ).fetchone()[0]
    )


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
    max_pending_inputs: int | None = None,
) -> tuple[AgentTurnRecord, bool]:
    """Persist one queued turn per ``(agent_id, input_id)``.

    Returns ``(record, created)``; an identical replay returns the stored row with
    ``created=False`` (before any lifecycle or quota check: accepted inputs stay
    accepted); a different input under the same ``input_id`` conflicts; a closed
    Agent rejects new inputs; a full queue rejects new inputs.  All decided in the
    caller's transaction.
    """

    existing = read_turn_by_input(connection, agent_id, input_id)
    if existing is not None:
        if existing.input_hash != input_hash or existing.turn_id != turn_id:
            raise UnitOfWorkConflict("input_id reused with different input content")
        return existing, False
    binding = read_binding(connection, agent_id)
    if binding is None:
        raise UnitOfWorkConflict("agent binding is missing")
    if binding.lifecycle != "open":
        raise AgentClosedError(f"agent is {binding.lifecycle}; new inputs are refused")
    if (
        max_pending_inputs is not None
        and count_open_turns(connection, agent_id) >= max_pending_inputs
    ):
        raise PendingInputsExhausted("pending inputs quota for this agent is exhausted")
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
    tool_call_ordinal_from: int | None = None,
) -> AgentTurnRecord:
    """Mark RUNNING; the ``*_ordinal_from`` baselines are written once (first admission)."""

    changed = connection.execute(
        "UPDATE base_agent_turns_v1 SET phase='running', lease_epoch=?, "
        "provider_turn_ordinal_from=COALESCE(provider_turn_ordinal_from, ?), "
        "tool_call_ordinal_from=COALESCE(tool_call_ordinal_from, ?), updated_at=?"
        " WHERE turn_id=? AND phase IN ('queued','running')",
        (lease_epoch, provider_turn_ordinal_from, tool_call_ordinal_from, now, turn_id),
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


def submit_input(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    run_id: str,
    turn_id: str,
    input_id: str,
    input_hash: str,
    input_json: Mapping[str, JsonValue],
    continuation_payload: Mapping[str, JsonValue],
    now: float,
    max_pending_inputs: int | None,
    enqueue_continuation: Callable[..., None],
    object_json: Callable[[Mapping[str, JsonValue], str], str],
) -> AgentTurnRecord:
    """Body of ``uow.submit_agent_input``: queued turn row + its input continuation.

    Runs inside the caller's transaction.  ``enqueue_continuation`` is the unit of
    work's connection-level enqueue (it owns the continuation table's invariants).
    """

    record, created = open_turn(
        connection,
        agent_id=agent_id,
        turn_id=turn_id,
        input_id=input_id,
        input_hash=input_hash,
        input_json=input_json,
        continuation_id=turn_id,
        now=now,
        max_pending_inputs=max_pending_inputs,
    )
    # The durable seq is assigned by the turn row; the driver reads it from the
    # continuation payload, so it is injected here (replays reproduce it).
    payload = {**dict(continuation_payload), "seq": record.seq}
    payload_json = object_json(payload, "continuation_payload")
    existing = connection.execute(
        "SELECT run_id, payload_json FROM continuations WHERE continuation_id=?", (turn_id,)
    ).fetchone()
    if existing is not None:
        if (
            str(existing["run_id"]) != run_id
            or canonical_json(json.loads(str(existing["payload_json"]))) != payload_json
        ):
            raise UnitOfWorkConflict("agent input continuation differs from turn")
        return record
    if not created:
        raise UnitOfWorkConflict("agent turn exists without its continuation")
    enqueue_continuation(
        connection,
        continuation_id=turn_id,
        run_id=run_id,
        payload=payload,
        payload_json=payload_json,
        now=now,
    )
    return record


def finalize_turn(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    expected_version: int,
    turn_id: str,
    event_id: str,
    payload_json: str,
    continuation_claim: object | None,
    execution_lease: object,
    receipt_id: str | None,
    usage_refs: tuple[str, ...],
    now: float,
    fault: Callable[[str], None],
    require_claim: Callable[..., None],
    insert_event: Callable[..., None],
) -> AgentTurnResultRecord:
    """Body of ``uow.commit_agent_turn_result_and_idle`` (one caller-owned transaction).

    Result row → progress receipt + continuation ack CAS → Run WAITING CAS + event →
    closing→closed convergence.  Fault points keep their Slice 1 names and order.
    """

    from .control import read_binding_lifecycle, set_lifecycle

    fault("agent_turn_finalize.result.before_write")
    record = commit_staged_result(
        connection,
        turn_id=turn_id,
        commit_receipt_id=f"{turn_id}:commit",
        usage_refs=usage_refs,
        now=now,
    )
    fault("agent_turn_finalize.result.after_write")
    if continuation_claim is not None:
        assert receipt_id is not None
        require_claim(connection, continuation_claim, execution_lease)
        outcome_hash = hashlib.sha256(
            canonical_json(
                {
                    "run_id": run_id,
                    "expected_version": expected_version,
                    "state": RunState.WAITING.value,
                    "event_id": event_id,
                    "payload": json.loads(payload_json),
                }
            ).encode()
        ).hexdigest()
        claim_id = getattr(continuation_claim, "continuation_id")
        connection.execute(
            "INSERT INTO continuation_progress_receipts("
            "receipt_id,continuation_id,run_id,owner_id,runtime_lease_epoch,"
            "claim_epoch,outcome_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                claim_id,
                run_id,
                getattr(execution_lease, "owner_id"),
                getattr(execution_lease, "epoch"),
                getattr(continuation_claim, "claim_epoch"),
                outcome_hash,
                now,
            ),
        )
        fault("agent_turn_finalize.continuation.before_write")
        changed = connection.execute(
            "UPDATE continuations SET state='acked',acked_at=?,ack_receipt_id=?,"
            "version=version+1 WHERE continuation_id=? AND state='claimed' "
            "AND claimed_by=? AND runtime_lease_epoch=? AND claim_epoch=? AND version=?",
            (
                now,
                receipt_id,
                claim_id,
                getattr(execution_lease, "owner_id"),
                getattr(execution_lease, "epoch"),
                getattr(continuation_claim, "claim_epoch"),
                getattr(continuation_claim, "version"),
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("agent turn continuation ack CAS failed")
        fault("agent_turn_finalize.continuation.after_write")
    fault("agent_turn_finalize.run.before_write")
    changed = connection.execute(
        "UPDATE runs SET state='waiting',version=version+1,updated_at=? "
        "WHERE run_id=? AND version=? AND state NOT IN ('completed','failed','cancelled')",
        (now, run_id, expected_version),
    ).rowcount
    if changed != 1:
        raise UnitOfWorkConflict("agent turn finalize Run CAS failed")
    insert_event(
        connection,
        event_id=event_id,
        run_id=run_id,
        kind="run.waiting",
        payload=json.loads(payload_json),
        now=now,
    )
    fault("agent_turn_finalize.run.after_write")
    # A ``closing`` Agent converges to ``closed`` here, in the execution layer, the
    # moment its last open turn is finalized: no caller has to come back.
    if (
        read_binding_lifecycle(connection, record.agent_id) == "closing"
        and read_open_turn(connection, record.agent_id) is None
    ):
        set_lifecycle(connection, agent_id=record.agent_id, lifecycle="closed", now=now)
    return record


__all__ = (
    "AgentClosedError",
    "PendingInputsExhausted",
    "commit_staged_result",
    "count_open_turns",
    "finalize_turn",
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
    "submit_input",
)
