# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable pre-start private context staging contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from simple_harness.contracts import JsonValue, canonical_json

from .sqlite.database import Database
from .uow import UnitOfWorkConflict


class ContextStageKind(StrEnum):
    ROOT = "root"
    CONTINUATION = "continuation"


class ContextStageState(StrEnum):
    PREPARING = "preparing"
    STAGED = "staged"
    CONSUMED = "consumed"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class ContextStageRecord:
    stage_id: str
    kind: ContextStageKind
    identity_key: str
    user_id: str
    session_id: str
    input_hash: str
    mode: str
    state: ContextStageState
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: float | None
    memory_result_id: str | None
    memory_result_hash: str | None
    private_snapshot: Mapping[str, JsonValue] | None
    private_snapshot_hash: str | None
    consumed_run_id: str | None
    consumed_continuation_id: str | None
    created_at: float
    updated_at: float
    memory_query_hash: str | None = None
    memory_write_fence: str | None = None
    outcome: str | None = None
    error_code: str | None = None
    product_result_hash: str | None = None
    source_snapshot_ref: str | None = None
    turn_started_at: float | None = None


@dataclass(frozen=True, slots=True)
class ContextStageClaim:
    record: ContextStageRecord
    owner: bool


class ContextStagingRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(
        self,
        *,
        stage_id: str,
        kind: ContextStageKind,
        identity_key: str,
        user_id: str,
        session_id: str,
        input_hash: str,
        mode: str,
        owner_id: str,
        now: float,
        lease_seconds: float,
        source_snapshot_ref: str | None = None,
    ) -> ContextStageClaim:
        for value, name in (
            (stage_id, "stage_id"),
            (identity_key, "identity_key"),
            (user_id, "user_id"),
            (session_id, "session_id"),
            (owner_id, "owner_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        _digest(input_hash, "input_hash")
        if source_snapshot_ref is not None and (
            not isinstance(source_snapshot_ref, str)
            or not source_snapshot_ref.strip()
            or "\x00" in source_snapshot_ref
        ):
            raise ValueError("source_snapshot_ref must be non-blank and contain no NUL")
        if mode not in {"sdk_prepared", "consumer_prepared"}:
            raise ValueError("context preparation mode is invalid")
        _time(now, allow_zero=True)
        _time(lease_seconds)
        token = uuid4().hex
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM context_preparation_staging WHERE identity_key=?",
                (identity_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT OR IGNORE INTO execution_users(user_id,created_at) VALUES(?,?)",
                    (user_id, now),
                )
                session = connection.execute(
                    "SELECT user_id FROM execution_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if session is not None and str(session[0]) != user_id:
                    raise UnitOfWorkConflict("execution session belongs to another user")
                connection.execute(
                    "INSERT OR IGNORE INTO execution_sessions(session_id,user_id,created_at) "
                    "VALUES(?,?,?)",
                    (session_id, user_id, now),
                )
                connection.execute(
                    "INSERT INTO context_preparation_staging("
                    "stage_id,kind,identity_key,user_id,session_id,input_hash,mode,state,"
                    "lease_owner,lease_token,lease_expires_at,memory_result_id,"
                    "memory_result_hash,private_snapshot,private_snapshot_hash,"
                    "consumed_run_id,consumed_continuation_id,source_snapshot_ref,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'preparing',?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,?,?,?)",
                    (
                        stage_id,
                        ContextStageKind(kind).value,
                        identity_key,
                        user_id,
                        session_id,
                        input_hash,
                        mode,
                        owner_id,
                        token,
                        now + lease_seconds,
                        source_snapshot_ref,
                        now,
                        now,
                    ),
                )
                created = self._read_with(connection, identity_key)
                assert created is not None
                return ContextStageClaim(created, True)
            record = _stage(row)
            _same_stage_intent(
                record,
                stage_id=stage_id,
                kind=kind,
                user_id=user_id,
                session_id=session_id,
                input_hash=input_hash,
                mode=mode,
                source_snapshot_ref=source_snapshot_ref,
            )
            if (
                record.state is ContextStageState.PREPARING
                and record.lease_expires_at is not None
                and record.lease_expires_at <= now
            ):
                changed = connection.execute(
                    "UPDATE context_preparation_staging SET lease_owner=?,lease_token=?,"
                    "lease_expires_at=?,updated_at=? WHERE stage_id=? AND state='preparing' "
                    "AND lease_expires_at<=?",
                    (owner_id, token, now + lease_seconds, now, stage_id, now),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("context stage takeover CAS failed")
                taken = self._read_with(connection, identity_key)
                assert taken is not None
                return ContextStageClaim(taken, True)
            return ContextStageClaim(record, False)

    def complete(
        self,
        claim: ContextStageRecord,
        *,
        private_snapshot: Mapping[str, JsonValue],
        memory_result_id: str | None,
        memory_result_hash: str | None,
        now: float,
        memory_query_hash: str | None = None,
        memory_write_fence: str | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        product_result_hash: str | None = None,
        source_snapshot_ref: str | None = None,
        turn_started_at: float | None = None,
        release_id: str | None = None,
        release_query_id: str | None = None,
        release_query_hash: str | None = None,
        release_result_id: str | None = None,
        release_result_hash: str | None = None,
        release_write_fence: str | None = None,
        release_retry_at: float | None = None,
    ) -> ContextStageRecord:
        if claim.state is not ContextStageState.PREPARING:
            raise UnitOfWorkConflict("only a preparing context stage can complete")
        if (
            source_snapshot_ref is not None
            and claim.source_snapshot_ref is not None
            and source_snapshot_ref != claim.source_snapshot_ref
        ):
            raise UnitOfWorkConflict("context source snapshot ref differs from durable claim")
        effective_source_snapshot_ref = source_snapshot_ref or claim.source_snapshot_ref
        snapshot_json = canonical_json(dict(private_snapshot))
        snapshot_bytes = snapshot_json.encode("utf-8")
        snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
        if (memory_result_id is None) != (memory_result_hash is None):
            raise ValueError("Memory result id/hash must be present together")
        if memory_result_hash is not None:
            _digest(memory_result_hash, "memory_result_hash")
        for digest, name in (
            (memory_query_hash, "memory_query_hash"),
            (product_result_hash, "product_result_hash"),
        ):
            if digest is not None:
                _digest(digest, name)
        if outcome is not None and outcome not in {"ready", "degraded_empty"}:
            raise ValueError("context stage outcome is invalid")
        if turn_started_at is not None:
            _time(turn_started_at, allow_zero=True)
        release_values = (
            release_id,
            release_query_id,
            release_query_hash,
            release_result_id,
            release_result_hash,
            release_retry_at,
        )
        if any(value is not None for value in release_values) and not all(
            value is not None for value in release_values
        ):
            raise ValueError("recall release fields must be present together")
        if release_id is not None:
            assert release_query_id is not None
            assert release_query_hash is not None
            assert release_result_id is not None
            assert release_result_hash is not None
            assert release_retry_at is not None
            for value, name in (
                (release_id, "release_id"),
                (release_query_id, "release_query_id"),
                (release_result_id, "release_result_id"),
            ):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{name} is required")
            _digest(release_query_hash, "release_query_hash")
            _digest(release_result_hash, "release_result_hash")
            if release_write_fence is not None and not release_write_fence.strip():
                raise ValueError("release_write_fence must be non-blank")
            _time(release_retry_at, allow_zero=True)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE context_preparation_staging SET state='staged',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,memory_result_id=?,"
                "memory_result_hash=?,memory_query_hash=?,memory_write_fence=?,outcome=?,"
                "error_code=?,product_result_hash=?,source_snapshot_ref=?,turn_started_at=?,"
                "private_snapshot=?,private_snapshot_hash=?,"
                "updated_at=? WHERE stage_id=? AND state='preparing' AND lease_owner=? "
                "AND lease_token=?",
                (
                    memory_result_id,
                    memory_result_hash,
                    memory_query_hash,
                    memory_write_fence,
                    outcome,
                    error_code,
                    product_result_hash,
                    effective_source_snapshot_ref,
                    turn_started_at,
                    snapshot_bytes,
                    snapshot_hash,
                    now,
                    claim.stage_id,
                    claim.lease_owner,
                    claim.lease_token,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("context stage completion CAS failed")
            if release_id is not None:
                connection.execute(
                    "INSERT INTO memory_recall_releases("
                    "release_id,stage_id,query_id,query_hash,result_id,result_hash,write_fence,"
                    "state,attempt_count,retry_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',0,?,?)",
                    (
                        release_id,
                        claim.stage_id,
                        release_query_id,
                        release_query_hash,
                        release_result_id,
                        release_result_hash,
                        release_write_fence,
                        release_retry_at,
                        now,
                    ),
                )
        result = self.get(claim.stage_id)
        assert result is not None
        return result

    def get(self, stage_id: str) -> ContextStageRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM context_preparation_staging WHERE stage_id=?", (stage_id,)
        ).fetchone()
        return None if row is None else _stage(row)

    def abandon(self, stage_id: str, *, now: float) -> ContextStageRecord:
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE context_preparation_staging SET state='abandoned',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                "private_snapshot=NULL,private_snapshot_hash=COALESCE("
                "private_snapshot_hash,lower(hex(zeroblob(32)))),updated_at=? "
                "WHERE stage_id=? AND state IN ('preparing','staged')",
                (now, stage_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("context stage cannot be abandoned")
        result = self.get(stage_id)
        assert result is not None
        return result

    def cleanup(self, *, now: float, older_than: float, limit: int) -> int:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        threshold = now - older_than
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT stage_id FROM context_preparation_staging WHERE "
                "(state='preparing' AND lease_expires_at<=?) OR "
                "(state IN ('abandoned','staged') AND updated_at<=?) "
                "ORDER BY updated_at,stage_id LIMIT ?",
                (now, threshold, limit),
            ).fetchall()
            count = 0
            for row in rows:
                stage_id = str(row[0])
                released = connection.execute(
                    "UPDATE context_preparation_staging SET lease_expires_at=0,"
                    "updated_at=? WHERE stage_id=? "
                    "AND state='preparing' AND lease_expires_at<=?",
                    (now, stage_id, now),
                ).rowcount
                if released:
                    count += 1
                    continue
                count += connection.execute(
                    "DELETE FROM context_preparation_staging WHERE stage_id=? "
                    "AND state IN ('abandoned','staged') AND updated_at<=?",
                    (stage_id, threshold),
                ).rowcount
            return count

    @staticmethod
    def consume_in_transaction(
        connection,
        *,
        stage_id: str,
        expected_hash: str,
        kind: ContextStageKind,
        consumed_run_id: str | None,
        consumed_continuation_id: str | None,
        now: float,
    ) -> Mapping[str, JsonValue]:  # type: ignore[no-untyped-def]
        row = connection.execute(
            "SELECT * FROM context_preparation_staging WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise UnitOfWorkConflict("context stage does not exist")
        record = _stage(row)
        if record.kind is not ContextStageKind(kind):
            raise UnitOfWorkConflict("context stage kind differs")
        if record.private_snapshot_hash != expected_hash:
            raise UnitOfWorkConflict("context stage hash differs")
        if record.state is ContextStageState.CONSUMED:
            target = consumed_run_id or consumed_continuation_id
            actual = record.consumed_run_id or record.consumed_continuation_id
            if target != actual:
                raise UnitOfWorkConflict("context stage was consumed by another command")
            raise UnitOfWorkConflict("consumed context bytes are no longer available")
        if record.state is not ContextStageState.STAGED or record.private_snapshot is None:
            raise UnitOfWorkConflict("context stage is not ready")
        changed = connection.execute(
            "UPDATE context_preparation_staging SET state='consumed',"
            "private_snapshot=NULL,lease_owner=NULL,lease_token=NULL,"
            "lease_expires_at=NULL,consumed_run_id=?,consumed_continuation_id=?,"
            "updated_at=? WHERE stage_id=? AND state='staged' AND "
            "private_snapshot_hash=?",
            (
                consumed_run_id,
                consumed_continuation_id,
                now,
                stage_id,
                expected_hash,
            ),
        ).rowcount
        if changed != 1:
            raise UnitOfWorkConflict("context stage consume CAS failed")
        return record.private_snapshot

    @staticmethod
    def _read_with(connection, identity_key: str) -> ContextStageRecord | None:  # type: ignore[no-untyped-def]
        row = connection.execute(
            "SELECT * FROM context_preparation_staging WHERE identity_key=?",
            (identity_key,),
        ).fetchone()
        return None if row is None else _stage(row)


def _stage(row) -> ContextStageRecord:  # type: ignore[no-untyped-def]
    snapshot: Mapping[str, JsonValue] | None = None
    if row["private_snapshot"] is not None:
        value = json.loads(bytes(row["private_snapshot"]).decode("utf-8"))
        if not isinstance(value, dict):
            raise UnitOfWorkConflict("context stage private snapshot is corrupt")
        snapshot = value
    return ContextStageRecord(
        stage_id=str(row["stage_id"]),
        kind=ContextStageKind(str(row["kind"])),
        identity_key=str(row["identity_key"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        input_hash=str(row["input_hash"]),
        mode=str(row["mode"]),
        state=ContextStageState(str(row["state"])),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_token=None if row["lease_token"] is None else str(row["lease_token"]),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else float(row["lease_expires_at"])
        ),
        memory_result_id=(
            None if row["memory_result_id"] is None else str(row["memory_result_id"])
        ),
        memory_result_hash=(
            None if row["memory_result_hash"] is None else str(row["memory_result_hash"])
        ),
        private_snapshot=snapshot,
        private_snapshot_hash=(
            None if row["private_snapshot_hash"] is None else str(row["private_snapshot_hash"])
        ),
        consumed_run_id=(None if row["consumed_run_id"] is None else str(row["consumed_run_id"])),
        consumed_continuation_id=(
            None
            if row["consumed_continuation_id"] is None
            else str(row["consumed_continuation_id"])
        ),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        memory_query_hash=(
            None if row["memory_query_hash"] is None else str(row["memory_query_hash"])
        ),
        memory_write_fence=(
            None if row["memory_write_fence"] is None else str(row["memory_write_fence"])
        ),
        outcome=None if row["outcome"] is None else str(row["outcome"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        product_result_hash=(
            None if row["product_result_hash"] is None else str(row["product_result_hash"])
        ),
        source_snapshot_ref=(
            None if row["source_snapshot_ref"] is None else str(row["source_snapshot_ref"])
        ),
        turn_started_at=(None if row["turn_started_at"] is None else float(row["turn_started_at"])),
    )


def _same_stage_intent(
    record: ContextStageRecord,
    *,
    stage_id: str,
    kind: ContextStageKind,
    user_id: str,
    session_id: str,
    input_hash: str,
    mode: str,
    source_snapshot_ref: str | None,
) -> None:
    if (
        record.stage_id != stage_id
        or record.kind is not ContextStageKind(kind)
        or record.user_id != user_id
        or record.session_id != session_id
        or record.input_hash != input_hash
        or record.mode != mode
        or record.source_snapshot_ref != source_snapshot_ref
    ):
        raise UnitOfWorkConflict("context preparation identity reused differently")


def _digest(value: str, name: str) -> None:
    if len(value) != 64 or any(item not in "0123456789abcdef" for item in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _time(value: float, *, allow_zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise ValueError("time value is invalid")


__all__ = (
    "ContextStageClaim",
    "ContextStageKind",
    "ContextStageRecord",
    "ContextStageState",
    "ContextStagingRepository",
)
