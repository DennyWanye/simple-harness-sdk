# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Derived, rebuildable indexes over the session Journal (Slice 4).

* FTS5 tables are *not* part of the frozen v10 descriptor: they are created on
  demand (``ensure_fts``) and rebuilt from the Journal when missing, so a SQLite
  build without FTS5 degrades to exact/substring search instead of refusing to
  open the library (BA25).
* Vectors are keyed by ``embedding_fingerprint``; a different model, version or
  dimension never mixes with the old rows (BA26).
* Index jobs are idempotent by ``(agent_id, record_seq, embedding_fingerprint)``.

All functions run in the caller's transaction.
"""

from __future__ import annotations

import array
import re
import sqlite3
from collections.abc import Sequence

from simple_harness.execution.base_agent import AgentIndexJobRecord, AgentVectorRecord
from simple_harness.execution.uow import UnitOfWorkConflict

FTS_TRIGRAM = "base_agent_journal_fts_trigram"
FTS_WORDS = "base_agent_journal_fts_words"


def fts5_available(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        connection.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def ensure_fts(connection: sqlite3.Connection) -> bool:
    """Create both derived FTS tables if possible; returns availability."""

    if not fts5_available(connection):
        return False
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TRIGRAM} USING fts5("
        "agent_id UNINDEXED, seq UNINDEXED, text, tokenize='trigram')"
    )
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_WORDS} USING fts5("
        "agent_id UNINDEXED, seq UNINDEXED, text, tokenize='unicode61 tokenchars ''_-./''')"
    )
    return True


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./-][A-Za-z0-9_]+)+")


def words_text(text: str) -> str:
    """Text for the words index: identifiers plus their path / dotted components."""

    extras: list[str] = []
    for match in _IDENT.finditer(text):
        token = match.group(0)
        parts = token.split("/")
        extras.extend(parts)
        extras.extend(piece for part in parts for piece in part.split(".") if piece)
    return text if not extras else text + "\n" + " ".join(dict.fromkeys(extras))


def fts_index_record(connection: sqlite3.Connection, *, agent_id: str, seq: int, text: str) -> None:
    for table, body in ((FTS_TRIGRAM, text), (FTS_WORDS, words_text(text))):
        connection.execute(f"DELETE FROM {table} WHERE agent_id=? AND seq=?", (agent_id, int(seq)))
        connection.execute(
            f"INSERT INTO {table}(agent_id, seq, text) VALUES (?,?,?)", (agent_id, int(seq), body)
        )


def fts_highwater(connection: sqlite3.Connection, agent_id: str) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX(seq),0) FROM {FTS_TRIGRAM} WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return int(row[0])


def fts_search(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    table: str,
    match: str,
    limit: int,
) -> tuple[tuple[int, float], ...]:
    """``(seq, bm25)`` for one Agent only; the agent filter is part of the MATCH."""

    if table not in (FTS_TRIGRAM, FTS_WORDS):
        raise ValueError("unknown FTS table")
    rows = connection.execute(
        f"SELECT seq, bm25({table}) AS score FROM {table} "
        f"WHERE {table} MATCH ? AND agent_id=? ORDER BY score LIMIT ?",
        (match, agent_id, int(limit)),
    ).fetchall()
    return tuple((int(row[0]), float(row[1])) for row in rows)


def _vector(row: sqlite3.Row) -> AgentVectorRecord:
    values = array.array("f")
    values.frombytes(bytes(row["vector"]))
    return AgentVectorRecord(
        vector_id=str(row["vector_id"]),
        agent_id=str(row["agent_id"]),
        record_seq=int(row["record_seq"]),
        source_hash=str(row["source_hash"]),
        embedding_fingerprint=str(row["embedding_fingerprint"]),
        dim=int(row["dim"]),
        vector=tuple(values),
        created_at=float(row["created_at"]),
    )


def store_vector(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    record_seq: int,
    source_hash: str,
    embedding_fingerprint: str,
    vector: Sequence[float],
    now: float,
) -> AgentVectorRecord:
    vector_id = f"{agent_id}:vector:{record_seq}:{embedding_fingerprint}"
    existing = connection.execute(
        "SELECT * FROM base_agent_session_vectors_v1 WHERE vector_id=?", (vector_id,)
    ).fetchone()
    if existing is not None:
        record = _vector(existing)
        if record.source_hash != source_hash:
            raise UnitOfWorkConflict("vector exists for a different source hash")
        return record
    blob = array.array("f", [float(v) for v in vector]).tobytes()
    connection.execute(
        "INSERT INTO base_agent_session_vectors_v1(vector_id,agent_id,record_seq,source_hash,"
        "embedding_fingerprint,dim,vector,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            vector_id,
            agent_id,
            int(record_seq),
            source_hash,
            embedding_fingerprint,
            len(vector),
            blob,
            float(now),
        ),
    )
    row = connection.execute(
        "SELECT * FROM base_agent_session_vectors_v1 WHERE vector_id=?", (vector_id,)
    ).fetchone()
    assert row is not None
    return _vector(row)


def list_vectors(
    connection: sqlite3.Connection, *, agent_id: str, embedding_fingerprint: str
) -> tuple[AgentVectorRecord, ...]:
    rows = connection.execute(
        "SELECT * FROM base_agent_session_vectors_v1 WHERE agent_id=? AND embedding_fingerprint=? "
        "ORDER BY record_seq",
        (agent_id, embedding_fingerprint),
    ).fetchall()
    return tuple(_vector(row) for row in rows)


def _job(row: sqlite3.Row) -> AgentIndexJobRecord:
    return AgentIndexJobRecord(
        job_id=str(row["job_id"]),
        agent_id=str(row["agent_id"]),
        record_seq=int(row["record_seq"]),
        source_hash=str(row["source_hash"]),
        embedding_fingerprint=str(row["embedding_fingerprint"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else float(row["lease_expires_at"])
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def enqueue_index_job(
    connection: sqlite3.Connection,
    *,
    agent_id: str,
    record_seq: int,
    source_hash: str,
    embedding_fingerprint: str,
    now: float,
) -> AgentIndexJobRecord:
    job_id = f"{agent_id}:index:{record_seq}:{embedding_fingerprint}"
    row = connection.execute(
        "SELECT * FROM base_agent_index_jobs_v1 WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is not None:
        return _job(row)
    connection.execute(
        "INSERT INTO base_agent_index_jobs_v1(job_id,agent_id,record_seq,source_hash,"
        "embedding_fingerprint,state,attempts,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'pending',0,?,?)",
        (
            job_id,
            agent_id,
            int(record_seq),
            source_hash,
            embedding_fingerprint,
            float(now),
            float(now),
        ),
    )
    row = connection.execute(
        "SELECT * FROM base_agent_index_jobs_v1 WHERE job_id=?", (job_id,)
    ).fetchone()
    assert row is not None
    return _job(row)


def claim_index_jobs(
    connection: sqlite3.Connection,
    *,
    owner: str,
    embedding_fingerprint: str,
    now: float,
    lease_seconds: float,
    limit: int,
    max_attempts: int = 5,
) -> tuple[AgentIndexJobRecord, ...]:
    """Pending jobs, expired claims, and errored jobs below the attempt cap (retry)."""

    rows = connection.execute(
        "SELECT job_id FROM base_agent_index_jobs_v1 WHERE embedding_fingerprint=? AND "
        "(state='pending' OR (state='claimed' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at <= ?) OR (state='error' AND attempts < ?)) "
        "ORDER BY attempts, created_at LIMIT ?",
        (embedding_fingerprint, float(now), int(max_attempts), int(limit)),
    ).fetchall()
    claimed: list[AgentIndexJobRecord] = []
    for row in rows:
        connection.execute(
            "UPDATE base_agent_index_jobs_v1 SET state='claimed', attempts=attempts+1, "
            "lease_owner=?, lease_expires_at=?, updated_at=? WHERE job_id=?",
            (owner, float(now) + float(lease_seconds), float(now), str(row[0])),
        )
        stored = connection.execute(
            "SELECT * FROM base_agent_index_jobs_v1 WHERE job_id=?", (str(row[0]),)
        ).fetchone()
        claimed.append(_job(stored))
    return tuple(claimed)


def settle_index_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    owner: str,
    state: str,
    error_code: str | None,
    now: float,
) -> AgentIndexJobRecord:
    if state not in ("done", "error", "pending"):
        raise ValueError("index job settle state must be done, error or pending")
    changed = connection.execute(
        "UPDATE base_agent_index_jobs_v1 SET state=?, error_code=?, lease_owner=NULL, "
        "lease_expires_at=NULL, updated_at=? WHERE job_id=? AND state='claimed' AND lease_owner=?",
        (state, error_code, float(now), job_id, owner),
    ).rowcount
    if changed != 1:
        raise UnitOfWorkConflict("index job is not claimed by this owner")
    row = connection.execute(
        "SELECT * FROM base_agent_index_jobs_v1 WHERE job_id=?", (job_id,)
    ).fetchone()
    assert row is not None
    return _job(row)


def journal_rows_missing_fts(
    connection: sqlite3.Connection, *, limit: int
) -> tuple[tuple[str, int, str], ...]:
    """``(agent_id, seq, message_json)`` of indexable rows with no trigram FTS row."""

    rows = connection.execute(
        "SELECT j.agent_id, j.seq, j.message_json FROM base_agent_session_journal_v1 j "
        "WHERE j.kind IN ('user_input','assistant','tool_result') AND NOT EXISTS ("
        f"SELECT 1 FROM {FTS_TRIGRAM} f WHERE f.agent_id=j.agent_id AND f.seq=j.seq) "
        "ORDER BY j.agent_id, j.seq LIMIT ?",
        (int(limit),),
    ).fetchall()
    return tuple((str(r[0]), int(r[1]), str(r[2])) for r in rows)


def index_errors(
    connection: sqlite3.Connection, *, agent_id: str, embedding_fingerprint: str
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT error_code FROM base_agent_index_jobs_v1 WHERE agent_id=? AND "
        "embedding_fingerprint=? AND state='error' AND error_code IS NOT NULL",
        (agent_id, embedding_fingerprint),
    ).fetchall()
    return tuple(str(r[0]) for r in rows)


def index_status(
    connection: sqlite3.Connection, *, agent_id: str, embedding_fingerprint: str
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT state, COUNT(*) FROM base_agent_index_jobs_v1 WHERE agent_id=? AND "
        "embedding_fingerprint=? GROUP BY state",
        (agent_id, embedding_fingerprint),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


__all__ = (
    "FTS_TRIGRAM",
    "FTS_WORDS",
    "claim_index_jobs",
    "enqueue_index_job",
    "ensure_fts",
    "fts5_available",
    "fts_highwater",
    "fts_index_record",
    "fts_search",
    "index_errors",
    "index_status",
    "journal_rows_missing_fts",
    "list_vectors",
    "settle_index_job",
    "store_vector",
)
