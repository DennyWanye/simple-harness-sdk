# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for the BaseAgent session Journal (Slice 3).

The Journal is append-only: one row per message, never a rewritten snapshot.
Every append is idempotent by ``(agent_id, append_id)``; a replay with the same
hash returns the stored rows, a different hash conflicts.  All functions run in
the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import cast

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.execution.base_agent import (
    AgentContextSelectionRecord,
    AgentJournalRecord,
    AgentSummaryRecord,
)
from simple_harness.execution.uow import UnitOfWorkConflict


def _record(row: sqlite3.Row) -> AgentJournalRecord:
    return AgentJournalRecord(
        record_id=str(row["record_id"]),
        agent_id=str(row["agent_id"]),
        seq=int(row["seq"]),
        append_id=str(row["append_id"]),
        kind=str(row["kind"]),
        turn_id=None if row["turn_id"] is None else str(row["turn_id"]),
        protocol_group_id=str(row["protocol_group_id"]),
        message_json=json.loads(str(row["message_json"])),
        content_hash=str(row["content_hash"]),
        provenance=str(row["provenance"]),
        visibility=str(row["visibility"]),
        full_record_seq=None if row["full_record_seq"] is None else int(row["full_record_seq"]),
        lease_epoch=int(row["lease_epoch"]),
        created_at=float(row["created_at"]),
    )


def highwater(connection: sqlite3.Connection, agent_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM base_agent_session_journal_v1 WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    return int(row[0])


def read_records(
    connection: sqlite3.Connection,
    agent_id: str,
    *,
    from_seq: int = 1,
    to_seq: int | None = None,
) -> tuple[AgentJournalRecord, ...]:
    if to_seq is None:
        rows = connection.execute(
            "SELECT * FROM base_agent_session_journal_v1 WHERE agent_id=? AND seq>=? ORDER BY seq",
            (agent_id, int(from_seq)),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM base_agent_session_journal_v1 WHERE agent_id=? AND seq>=? AND seq<=? "
            "ORDER BY seq",
            (agent_id, int(from_seq), int(to_seq)),
        ).fetchall()
    return tuple(_record(row) for row in rows)


def read_append(
    connection: sqlite3.Connection, agent_id: str, append_id: str
) -> tuple[str, int, int] | None:
    row = connection.execute(
        "SELECT append_hash, seq_from, seq_to FROM base_agent_journal_appends_v1 "
        "WHERE agent_id=? AND append_id=?",
        (agent_id, append_id),
    ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]), int(row[2]))


def append_records(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    append_id: str,
    append_hash: str,
    expected_highwater: int,
    entries: Sequence[Mapping[str, JsonValue]],
    lease_epoch: int,
    now: float,
) -> tuple[tuple[AgentJournalRecord, ...], bool, int]:
    """Append ``entries`` (each: message_json, kind, turn_id, protocol_group_id,
    provenance, visibility, full_record_offset) after ``expected_highwater``.

    Returns ``(records, created, highwater_after)``.  A same-hash replay returns
    the stored rows with ``created=False``.
    """

    existing = read_append(connection, agent_id, append_id)
    if existing is not None:
        stored_hash, seq_from, seq_to = existing
        if stored_hash != append_hash:
            raise UnitOfWorkConflict("journal append identity reused with different payload")
        return (
            read_records(connection, agent_id, from_seq=seq_from, to_seq=seq_to),
            False,
            highwater(connection, agent_id),
        )
    current = highwater(connection, agent_id)
    if current != expected_highwater:
        raise UnitOfWorkConflict("journal revision CAS failed")
    seq = current
    seq_from = current + 1
    for entry in entries:
        seq += 1
        message_json = canonical_json(entry["message_json"])
        offset = entry.get("full_record_offset")
        full_seq = None if offset is None else seq_from + int(cast(int, offset))
        connection.execute(
            "INSERT INTO base_agent_session_journal_v1(record_id,agent_id,seq,append_id,kind,"
            "turn_id,protocol_group_id,message_json,content_hash,provenance,visibility,"
            "full_record_seq,lease_epoch,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{agent_id}:journal:{seq}",
                agent_id,
                seq,
                append_id,
                str(entry["kind"]),
                entry.get("turn_id"),
                str(entry["protocol_group_id"]),
                message_json,
                hashlib.sha256(message_json.encode("utf-8")).hexdigest(),
                str(entry["provenance"]),
                str(entry["visibility"]),
                full_seq,
                int(lease_epoch),
                float(now),
            ),
        )
    connection.execute(
        "INSERT INTO base_agent_journal_appends_v1(agent_id,append_id,append_hash,seq_from,"
        "seq_to,created_at) VALUES (?,?,?,?,?,?)",
        (agent_id, append_id, append_hash, seq_from, seq, float(now)),
    )
    return read_records(connection, agent_id, from_seq=seq_from, to_seq=seq), True, seq


def _selection(row: sqlite3.Row) -> AgentContextSelectionRecord:
    return AgentContextSelectionRecord(
        selection_id=str(row["selection_id"]),
        agent_id=str(row["agent_id"]),
        turn_id=None if row["turn_id"] is None else str(row["turn_id"]),
        revision=int(row["revision"]),
        source_highwater=int(row["source_highwater"]),
        selected_seqs=tuple(int(v) for v in json.loads(str(row["selected_seqs_json"]))),
        dropped_ranges=tuple(
            (int(a), int(b)) for a, b in json.loads(str(row["dropped_ranges_json"]))
        ),
        required_over_budget=bool(row["required_over_budget"]),
        message_tokens=int(row["message_tokens"]),
        tool_tokens=int(row["tool_tokens"]),
        budget_tokens=int(row["budget_tokens"]),
        policy_hash=str(row["policy_hash"]),
        tokenizer_fingerprint=str(row["tokenizer_fingerprint"]),
        query_hash=None if row["query_hash"] is None else str(row["query_hash"]),
        index_generation=(
            None if row["index_generation"] is None else int(row["index_generation"])
        ),
        provider_request_id=(
            None if row["provider_request_id"] is None else str(row["provider_request_id"])
        ),
        request_hash=None if row["request_hash"] is None else str(row["request_hash"]),
        request_tokens=None if row["request_tokens"] is None else int(row["request_tokens"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def record_selection(
    connection: sqlite3.Connection,
    *,
    selection_id: str,
    agent_id: str,
    turn_id: str | None,
    revision: int,
    source_highwater: int,
    selected_seqs: Sequence[int],
    dropped_ranges: Sequence[tuple[int, int]],
    required_over_budget: bool,
    message_tokens: int,
    tool_tokens: int,
    budget_tokens: int,
    policy_hash: str,
    tokenizer_fingerprint: str,
    now: float,
) -> AgentContextSelectionRecord:
    """Idempotent by ``selection_id``: a replay returns the stored row unchanged."""

    row = connection.execute(
        "SELECT * FROM base_agent_context_selections_v1 WHERE selection_id=?", (selection_id,)
    ).fetchone()
    if row is not None:
        return _selection(row)
    connection.execute(
        "INSERT INTO base_agent_context_selections_v1(selection_id,agent_id,turn_id,revision,"
        "source_highwater,selected_seqs_json,dropped_ranges_json,required_over_budget,"
        "message_tokens,tool_tokens,budget_tokens,policy_hash,tokenizer_fingerprint,"
        "query_hash,index_generation,provider_request_id,request_hash,request_tokens,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?)",
        (
            selection_id,
            agent_id,
            turn_id,
            int(revision),
            int(source_highwater),
            canonical_json([int(v) for v in selected_seqs]),
            canonical_json([[int(a), int(b)] for a, b in dropped_ranges]),
            1 if required_over_budget else 0,
            int(message_tokens),
            int(tool_tokens),
            int(budget_tokens),
            policy_hash,
            tokenizer_fingerprint,
            float(now),
            float(now),
        ),
    )
    row = connection.execute(
        "SELECT * FROM base_agent_context_selections_v1 WHERE selection_id=?", (selection_id,)
    ).fetchone()
    assert row is not None
    return _selection(row)


def latest_selection(
    connection: sqlite3.Connection, agent_id: str
) -> AgentContextSelectionRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_context_selections_v1 WHERE agent_id=? "
        "ORDER BY revision DESC, created_at DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return None if row is None else _selection(row)


def read_selection_by_request(
    connection: sqlite3.Connection, provider_request_id: str
) -> AgentContextSelectionRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_context_selections_v1 WHERE provider_request_id=?",
        (provider_request_id,),
    ).fetchone()
    return None if row is None else _selection(row)


def bind_selection_request(
    connection: sqlite3.Connection,
    *,
    selection_id: str,
    provider_request_id: str,
    request_hash: str,
    request_tokens: int,
    now: float,
) -> AgentContextSelectionRecord:
    """Freeze the request identity onto the selection (write-once per request id)."""

    existing = read_selection_by_request(connection, provider_request_id)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise UnitOfWorkConflict("provider request re-bound with a different request")
        return existing
    connection.execute(
        "UPDATE base_agent_context_selections_v1 SET provider_request_id=?, request_hash=?, "
        "request_tokens=?, updated_at=? WHERE selection_id=? AND provider_request_id IS NULL",
        (provider_request_id, request_hash, int(request_tokens), float(now), selection_id),
    )
    row = connection.execute(
        "SELECT * FROM base_agent_context_selections_v1 WHERE selection_id=?", (selection_id,)
    ).fetchone()
    if row is None:
        raise UnitOfWorkConflict("context selection is missing")
    return _selection(row)


def _summary(row: sqlite3.Row) -> AgentSummaryRecord:
    return AgentSummaryRecord(
        summary_id=str(row["summary_id"]),
        agent_id=str(row["agent_id"]),
        scope=str(row["scope"]),
        from_seq=int(row["from_seq"]),
        to_seq=int(row["to_seq"]),
        source_hash=str(row["source_hash"]),
        summary_json=json.loads(str(row["summary_json"])),
        generated_by=str(row["generated_by"]),
        validity=str(row["validity"]),
        created_at=float(row["created_at"]),
    )


def upsert_summary(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    scope: str,
    from_seq: int,
    to_seq: int,
    source_hash: str,
    summary_json: Mapping[str, JsonValue],
    generated_by: str,
    now: float,
) -> AgentSummaryRecord:
    summary_id = f"{agent_id}:summary:{from_seq}:{to_seq}:{source_hash[:16]}"
    row = connection.execute(
        "SELECT * FROM base_agent_session_summaries_v1 WHERE summary_id=?", (summary_id,)
    ).fetchone()
    if row is not None:
        return _summary(row)
    connection.execute(
        "INSERT INTO base_agent_session_summaries_v1(summary_id,agent_id,scope,from_seq,to_seq,"
        "source_hash,summary_json,generated_by,validity,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'valid',?)",
        (
            summary_id,
            agent_id,
            scope,
            int(from_seq),
            int(to_seq),
            source_hash,
            canonical_json(dict(summary_json)),
            generated_by,
            float(now),
        ),
    )
    row = connection.execute(
        "SELECT * FROM base_agent_session_summaries_v1 WHERE summary_id=?", (summary_id,)
    ).fetchone()
    assert row is not None
    return _summary(row)


def list_summaries(connection: sqlite3.Connection, agent_id: str) -> tuple[AgentSummaryRecord, ...]:
    rows = connection.execute(
        "SELECT * FROM base_agent_session_summaries_v1 WHERE agent_id=? ORDER BY from_seq, to_seq",
        (agent_id,),
    ).fetchall()
    return tuple(_summary(row) for row in rows)


__all__ = (
    "append_records",
    "bind_selection_request",
    "highwater",
    "latest_selection",
    "list_summaries",
    "read_append",
    "read_records",
    "read_selection_by_request",
    "record_selection",
    "upsert_summary",
)
