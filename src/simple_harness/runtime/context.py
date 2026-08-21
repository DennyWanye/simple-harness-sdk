# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable, epoch-fenced ReAct context snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from simple_harness.contracts import CallId, JsonValue, RunId, canonical_json
from simple_harness.contracts.messages import ContentBlock, Message, MessageRole
from simple_harness.execution.sqlite.database import Database
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ExecutionLease,
    UnitOfWorkConflict,
)

CONTEXT_NAMESPACE = "react.context.v1"


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    revision: int
    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("context revision must be non-negative")
        object.__setattr__(self, "messages", tuple(self.messages))
        if not all(isinstance(message, Message) for message in self.messages):
            raise TypeError("context entries must be Message values")


@runtime_checkable
class ContextPort(Protocol):
    def load(self, run_id: RunId) -> ContextSnapshot: ...

    def append(
        self,
        run_id: RunId,
        execution_lease: ExecutionLease,
        expected_revision: int,
        append_id: str,
        entries: Sequence[Message],
    ) -> ContextSnapshot: ...


class SqliteContextPort:
    """Official context authority backed by workflow checkpoint schema v1."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._clock = clock

    def load(self, run_id: RunId) -> ContextSnapshot:
        value = _run_id(run_id)
        row = self._database.connection.execute(
            """
            SELECT checkpoint_json FROM workflow_checkpoints
            WHERE run_id = ? AND namespace = ?
            ORDER BY version DESC LIMIT 1
            """,
            (value, CONTEXT_NAMESPACE),
        ).fetchone()
        return ContextSnapshot(0, ()) if row is None else _snapshot(row[0])

    def append(
        self,
        run_id: RunId,
        execution_lease: ExecutionLease,
        expected_revision: int,
        append_id: str,
        entries: Sequence[Message],
    ) -> ContextSnapshot:
        now = float(self._clock())
        if now < 0:
            raise ValueError("context clock must be non-negative")
        with self._database.transaction() as connection:
            return _append_context_in_transaction(
                connection,
                run_id,
                execution_lease,
                expected_revision,
                append_id,
                entries,
                now=now,
            )


def _append_context_in_transaction(
    connection: sqlite3.Connection,
    run_id: RunId,
    execution_lease: ExecutionLease,
    expected_revision: int,
    append_id: str,
    entries: Sequence[Message],
    *,
    now: float,
) -> ContextSnapshot:
    """Append once using the caller's already-open persistence transaction."""

    value = _run_id(run_id)
    if execution_lease.run_id != value:
        raise UnitOfWorkConflict("context append lease belongs to another Run")
    if execution_lease.namespace != RUNTIME_LEASE_NAMESPACE:
        raise UnitOfWorkConflict("context append requires the canonical runtime lease")
    if isinstance(expected_revision, bool) or expected_revision < 0:
        raise ValueError("expected_revision must be non-negative")
    if not isinstance(append_id, str) or not append_id.strip():
        raise ValueError("append_id is required")
    items = tuple(entries)
    if not items or not all(isinstance(entry, Message) for entry in items):
        raise TypeError("entries must contain at least one Message")
    append_payload: list[JsonValue] = [entry.to_dict() for entry in items]
    append_hash = hashlib.sha256(canonical_json(append_payload).encode("utf-8")).hexdigest()
    lease = connection.execute(
        """
        SELECT owner_id, epoch, expires_at FROM workflow_leases
        WHERE run_id = ? AND namespace = ?
        """,
        (value, execution_lease.namespace),
    ).fetchone()
    if (
        lease is None
        or str(lease["owner_id"]) != execution_lease.owner_id
        or int(lease["epoch"]) != execution_lease.epoch
        or float(lease["expires_at"]) <= now
    ):
        raise UnitOfWorkConflict("context append runtime lease is stale or expired")
    row = connection.execute(
        """
        SELECT checkpoint_json FROM workflow_checkpoints
        WHERE run_id = ? AND namespace = ?
        ORDER BY version DESC LIMIT 1
        """,
        (value, CONTEXT_NAMESPACE),
    ).fetchone()
    current_payload = (
        {"revision": 0, "messages": [], "append_receipts": {}}
        if row is None
        else json.loads(str(row["checkpoint_json"]))
    )
    receipts = current_payload.get("append_receipts")
    if not isinstance(receipts, dict):
        raise TypeError("stored context append receipts are invalid")
    existing_hash = receipts.get(append_id)
    if existing_hash is not None:
        if existing_hash != append_hash:
            raise UnitOfWorkConflict("context append identity reused with different payload")
        return _snapshot_from_payload(current_payload)
    revision = current_payload.get("revision")
    if revision != expected_revision:
        raise UnitOfWorkConflict("context revision CAS failed")
    messages = current_payload.get("messages")
    if not isinstance(messages, list):
        raise TypeError("stored context messages are invalid")
    next_revision = expected_revision + 1
    next_payload: dict[str, JsonValue] = {
        "revision": next_revision,
        "messages": [*messages, *append_payload],
        "append_receipts": {**receipts, append_id: append_hash},
    }
    checkpoint_json = canonical_json(next_payload)
    checkpoint_hash = hashlib.sha256(checkpoint_json.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO workflow_checkpoints(
            checkpoint_id, run_id, namespace, checkpoint_json,
            checkpoint_hash, lease_epoch, version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{value}:{CONTEXT_NAMESPACE}:{next_revision}",
            value,
            CONTEXT_NAMESPACE,
            checkpoint_json,
            checkpoint_hash,
            execution_lease.epoch,
            next_revision,
            now,
        ),
    )
    return _snapshot_from_payload(next_payload)


def _run_id(value: RunId) -> str:
    if not isinstance(value, RunId):
        raise TypeError("run_id must use RunId")
    return value.value


def _snapshot(value: object) -> ContextSnapshot:
    payload = json.loads(str(value))
    if not isinstance(payload, dict):
        raise TypeError("stored context checkpoint must be a JSON object")
    return _snapshot_from_payload(payload)


def _snapshot_from_payload(payload: Mapping[str, object]) -> ContextSnapshot:
    revision = payload.get("revision")
    values = payload.get("messages")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("stored context revision is invalid")
    if not isinstance(values, list):
        raise TypeError("stored context messages are invalid")
    return ContextSnapshot(revision, tuple(_message(value) for value in values))


def _message(value: object) -> Message:
    if not isinstance(value, dict):
        raise TypeError("stored context entry must be an object")
    role = value.get("role")
    content = value.get("content")
    name = value.get("name")
    call_id = value.get("call_id")
    metadata = value.get("metadata", {})
    if not isinstance(role, str) or not isinstance(content, (str, list)):
        raise TypeError("stored context entry is invalid")
    normalized_content = (
        content
        if isinstance(content, str)
        else tuple(ContentBlock.from_dict(block) for block in content if isinstance(block, Mapping))
    )
    if isinstance(content, list) and len(normalized_content) != len(content):
        raise TypeError("stored context content block is invalid")
    if name is not None and not isinstance(name, str):
        raise TypeError("stored message name is invalid")
    if call_id is not None and not isinstance(call_id, str):
        raise TypeError("stored message call_id is invalid")
    if not isinstance(metadata, dict):
        raise TypeError("stored message metadata is invalid")
    return Message(
        MessageRole(role),
        normalized_content,
        name=name,
        call_id=None if call_id is None else CallId(call_id),
        metadata=metadata,
    )


__all__ = ("CONTEXT_NAMESPACE", "ContextPort", "ContextSnapshot", "SqliteContextPort")
