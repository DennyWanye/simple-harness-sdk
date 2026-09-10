# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Connection-level helpers for idempotent BaseAgent creation batches (Slice 2 · T5).

A batch row is the durable identity of one ``create_many`` call: the same
``(owner_scope, batch_key)`` with the same fingerprint replays the same agent ids;
a different fingerprint is an identity conflict.  Instances are still created one
Run per transaction; the batch row only records what was reserved and committed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from simple_harness.contracts import JsonValue, canonical_json, freeze_json, thaw_json
from simple_harness.execution.base_agent import AgentCreationBatchRecord
from simple_harness.execution.uow import UnitOfWorkConflict


class BatchIdentityConflict(UnitOfWorkConflict):
    """Same ``(owner_scope, batch_key)`` reused with a different batch fingerprint."""


def _batch(row: sqlite3.Row) -> AgentCreationBatchRecord:
    receipt = row["receipt_json"]
    return AgentCreationBatchRecord(
        batch_id=str(row["batch_id"]),
        owner_scope=str(row["owner_scope"]),
        batch_key=str(row["batch_key"]),
        batch_fingerprint=str(row["batch_fingerprint"]),
        agent_ids=tuple(json.loads(str(row["agent_ids_json"]))),
        config_hashes=tuple(json.loads(str(row["config_hashes_json"]))),
        state=str(row["state"]),
        receipt=None if receipt is None else json.loads(str(receipt)),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def read_batch(
    connection: sqlite3.Connection, owner_scope: str, batch_key: str
) -> AgentCreationBatchRecord | None:
    row = connection.execute(
        "SELECT * FROM base_agent_creation_batches_v1 WHERE owner_scope=? AND batch_key=?",
        (owner_scope, batch_key),
    ).fetchone()
    return None if row is None else _batch(row)


def reserve_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    owner_scope: str,
    batch_key: str,
    batch_fingerprint: str,
    agent_ids: Sequence[str],
    config_hashes: Sequence[str],
    now: float,
) -> tuple[AgentCreationBatchRecord, bool]:
    existing = read_batch(connection, owner_scope, batch_key)
    if existing is not None:
        if existing.batch_fingerprint != batch_fingerprint:
            raise BatchIdentityConflict("batch_key reused with a different batch content")
        return existing, False
    connection.execute(
        "INSERT INTO base_agent_creation_batches_v1(batch_id,owner_scope,batch_key,"
        "batch_fingerprint,agent_ids_json,config_hashes_json,state,receipt_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,'reserved',NULL,?,?)",
        (
            batch_id,
            owner_scope,
            batch_key,
            batch_fingerprint,
            canonical_json(list(agent_ids)),
            canonical_json(list(config_hashes)),
            float(now),
            float(now),
        ),
    )
    stored = read_batch(connection, owner_scope, batch_key)
    assert stored is not None
    return stored, True


def commit_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    receipt: Mapping[str, JsonValue],
    now: float,
) -> AgentCreationBatchRecord:
    """``reserved -> committed`` with the receipt; a committed replay is a no-op."""

    row = connection.execute(
        "SELECT owner_scope, batch_key, state FROM base_agent_creation_batches_v1 WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise UnitOfWorkConflict("creation batch is missing")
    if str(row["state"]) == "reserved":
        connection.execute(
            "UPDATE base_agent_creation_batches_v1 SET state='committed', receipt_json=?, "
            "updated_at=? WHERE batch_id=? AND state='reserved'",
            (canonical_json(thaw_json(freeze_json(dict(receipt)))), float(now), batch_id),
        )
    stored = read_batch(connection, str(row["owner_scope"]), str(row["batch_key"]))
    assert stored is not None
    return stored


def count_bindings(connection: sqlite3.Connection, owner_scope: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM base_agent_bindings_v1 WHERE owner_scope=?", (owner_scope,)
        ).fetchone()[0]
    )


__all__ = (
    "BatchIdentityConflict",
    "commit_batch",
    "count_bindings",
    "read_batch",
    "reserve_batch",
)
