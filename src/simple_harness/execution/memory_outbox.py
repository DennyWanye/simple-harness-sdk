# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable product-neutral conversation Memory outbox and dispatcher."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from simple_harness.runtime.conversation_memory import (
    ConversationMemoryApplyStatus,
    ConversationMemoryError,
    ConversationMemoryErrorCode,
    ConversationMemoryIntent,
    ConversationMemoryRole,
)
from simple_harness.runtime.ports import ConversationMemorySinkPort

from .sqlite.database import Database
from .uow import UnitOfWorkConflict


class MemoryOutboxState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    DEAD_LETTER = "dead_letter"
    SKIPPED_NON_TEXT = "skipped_non_text"


@dataclass(frozen=True, slots=True)
class MemoryIntentSpec:
    intent_id: str
    source_event_id: str
    user_id: str
    session_id: str
    role: str
    memory_text: str | None
    payload_hash: str

    @classmethod
    def from_conversation(
        cls, intent: ConversationMemoryIntent
    ) -> MemoryIntentSpec:
        return cls(
            intent_id=intent.source_event_id,
            source_event_id=intent.source_event_id,
            user_id=intent.user_id,
            session_id=intent.session_id,
            role=intent.role.value,
            memory_text=intent.memory_text,
            payload_hash=intent.payload_hash,
        )


@dataclass(frozen=True, slots=True)
class MemoryOutboxRecord:
    intent_id: str
    source_event_id: str
    run_id: str
    continuation_id: str | None
    user_id: str
    session_id: str
    role: str
    memory_text: str | None
    payload_hash: str
    state: MemoryOutboxState
    version: int
    attempt_count: int
    claim_owner: str | None
    claim_token: str | None
    claim_expires_at: float | None
    retry_at: float
    error_code: str | None


class MemoryOutboxRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def read(self, intent_id: str) -> MemoryOutboxRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM memory_outbox WHERE intent_id=?", (intent_id,)
        ).fetchone()
        return None if row is None else _record(row)

    def claim(
        self,
        *,
        owner_id: str,
        now: float,
        lease_seconds: float,
    ) -> MemoryOutboxRecord | None:
        _positive_time(now, "now", allow_zero=True)
        _positive_time(lease_seconds, "lease_seconds")
        token = uuid4().hex
        claimed_id: str | None = None
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT intent_id FROM memory_outbox WHERE "
                "(state='pending' AND retry_at<=?) OR "
                "(state='claimed' AND claim_expires_at<=?) "
                "ORDER BY created_at,intent_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            claimed_id = str(row[0])
            changed = connection.execute(
                "UPDATE memory_outbox SET state='claimed',version=version+1,"
                "attempt_count=attempt_count+1,claim_owner=?,claim_token=?,"
                "claim_expires_at=?,updated_at=? WHERE intent_id=? AND "
                "((state='pending' AND retry_at<=?) OR "
                "(state='claimed' AND claim_expires_at<=?))",
                (
                    owner_id,
                    token,
                    now + lease_seconds,
                    now,
                    claimed_id,
                    now,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("memory outbox claim CAS failed")
        assert claimed_id is not None
        result = self.read(claimed_id)
        assert result is not None
        return result

    def applied(self, claim: MemoryOutboxRecord, *, now: float) -> MemoryOutboxRecord:
        return self._settle(claim, state=MemoryOutboxState.APPLIED, now=now)

    def dead_letter(
        self,
        claim: MemoryOutboxRecord,
        *,
        error_code: str,
        now: float,
    ) -> MemoryOutboxRecord:
        return self._settle(
            claim,
            state=MemoryOutboxState.DEAD_LETTER,
            now=now,
            error_code=error_code,
        )

    def release(
        self,
        claim: MemoryOutboxRecord,
        *,
        now: float,
        backoff_seconds: float,
        error_code: str,
    ) -> MemoryOutboxRecord:
        _positive_time(backoff_seconds, "backoff_seconds", allow_zero=True)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_outbox SET state='pending',version=version+1,"
                "claim_owner=NULL,claim_token=NULL,claim_expires_at=NULL,retry_at=?,"
                "error_code=?,updated_at=? WHERE intent_id=? AND state='claimed' "
                "AND version=? AND claim_owner=? AND claim_token=?",
                (
                    now + backoff_seconds,
                    error_code,
                    now,
                    claim.intent_id,
                    claim.version,
                    claim.claim_owner,
                    claim.claim_token,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("memory outbox release CAS failed")
        result = self.read(claim.intent_id)
        assert result is not None
        return result

    def backlog(self) -> dict[str, int]:
        rows = self.database.connection.execute(
            "SELECT state,COUNT(*) FROM memory_outbox GROUP BY state"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def _settle(
        self,
        claim: MemoryOutboxRecord,
        *,
        state: MemoryOutboxState,
        now: float,
        error_code: str | None = None,
    ) -> MemoryOutboxRecord:
        if state not in {MemoryOutboxState.APPLIED, MemoryOutboxState.DEAD_LETTER}:
            raise ValueError("memory outbox settlement state is invalid")
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_outbox SET state=?,version=version+1,claim_owner=NULL,"
                "claim_token=NULL,claim_expires_at=NULL,error_code=?,updated_at=?,"
                "settled_at=? WHERE intent_id=? AND state='claimed' AND version=? "
                "AND claim_owner=? AND claim_token=?",
                (
                    state.value,
                    error_code,
                    now,
                    now,
                    claim.intent_id,
                    claim.version,
                    claim.claim_owner,
                    claim.claim_token,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("memory outbox settlement CAS failed")
        result = self.read(claim.intent_id)
        assert result is not None
        return result


class MemoryDispatcher:
    def __init__(
        self,
        repository: MemoryOutboxRepository,
        sink: ConversationMemorySinkPort,
        *,
        owner_id: str,
        clock: Callable[[], float],
        lease_seconds: float = 30.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self.repository = repository
        self.sink = sink
        self.owner_id = owner_id
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._closed = False

    async def run_once(self, *, fault: Callable[[str], None] | None = None) -> bool:
        if self._closed:
            return False
        claim = self.repository.claim(
            owner_id=self.owner_id,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return False
        if claim.memory_text is None:
            raise UnitOfWorkConflict("skipped non-text intent reached dispatcher")
        intent = ConversationMemoryIntent(
            claim.source_event_id,
            claim.user_id,
            claim.session_id,
            ConversationMemoryRole(claim.role),
            claim.memory_text,
        )
        if intent.payload_hash != claim.payload_hash:
            self.repository.dead_letter(
                claim,
                error_code=ConversationMemoryErrorCode.APPLY_CONFLICT.value,
                now=self.clock(),
            )
            return True
        try:
            result = await self.sink.apply(intent)
            if fault is not None:
                fault("memory_dispatcher.after_apply_before_ack")
            if (
                result.source_event_id != claim.source_event_id
                or result.payload_hash != claim.payload_hash
                or result.status
                not in {
                    ConversationMemoryApplyStatus.APPLIED,
                    ConversationMemoryApplyStatus.ALREADY_APPLIED,
                }
            ):
                raise ConversationMemoryError(
                    ConversationMemoryErrorCode.APPLY_CONFLICT
                )
        except ConversationMemoryError as error:
            if error.code in {
                ConversationMemoryErrorCode.APPLY_CONFLICT,
                ConversationMemoryErrorCode.PERMANENT,
            }:
                self.repository.dead_letter(
                    claim, error_code=error.code.value, now=self.clock()
                )
            else:
                self._release_transient(claim, error.code.value)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._release_transient(
                claim, ConversationMemoryErrorCode.TRANSIENT.value
            )
            return True
        self.repository.applied(claim, now=self.clock())
        return True

    async def drain(self, *, limit: int) -> bool:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        for _ in range(limit):
            if not await self.run_once():
                return True
        return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.sink.close()

    def _release_transient(self, claim: MemoryOutboxRecord, code: str) -> None:
        backoff = min(
            self.max_backoff_seconds,
            float(2 ** min(max(claim.attempt_count - 1, 0), 16)),
        )
        self.repository.release(
            claim,
            now=self.clock(),
            backoff_seconds=backoff,
            error_code=code,
        )


def _record(row) -> MemoryOutboxRecord:  # type: ignore[no-untyped-def]
    return MemoryOutboxRecord(
        intent_id=str(row["intent_id"]),
        source_event_id=str(row["source_event_id"]),
        run_id=str(row["run_id"]),
        continuation_id=(
            None if row["continuation_id"] is None else str(row["continuation_id"])
        ),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),
        memory_text=(None if row["memory_text"] is None else str(row["memory_text"])),
        payload_hash=str(row["payload_hash"]),
        state=MemoryOutboxState(str(row["state"])),
        version=int(row["version"]),
        attempt_count=int(row["attempt_count"]),
        claim_owner=(None if row["claim_owner"] is None else str(row["claim_owner"])),
        claim_token=(None if row["claim_token"] is None else str(row["claim_token"])),
        claim_expires_at=(
            None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
        ),
        retry_at=float(row["retry_at"]),
        error_code=(None if row["error_code"] is None else str(row["error_code"])),
    )


def _positive_time(value: float, name: str, *, allow_zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (value < 0 if allow_zero else value <= 0)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be finite and {qualifier}")


__all__ = (
    "MemoryDispatcher",
    "MemoryIntentSpec",
    "MemoryOutboxRecord",
    "MemoryOutboxRepository",
    "MemoryOutboxState",
)
