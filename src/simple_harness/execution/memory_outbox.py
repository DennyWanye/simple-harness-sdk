# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable terminal-only committed-turn outbox and dispatcher."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from simple_harness.contracts import canonical_json
from simple_harness.runtime.agent_memory import (
    AgentMemoryError,
    AgentMemoryErrorCode,
    AgentMemoryPort,
    CommittedTurn,
    CommittedTurnReceipt,
    CommittedTurnStatus,
)

from .sqlite.database import Database
from .uow import UnitOfWorkConflict

logger = logging.getLogger(__name__)


class MemoryOutboxState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    APPLIED = "applied"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class CommittedTurnSpec:
    intent_id: str
    turn: CommittedTurn
    payload_json: str
    payload_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.turn, CommittedTurn):
            raise TypeError("turn must use CommittedTurn")
        expected_id = f"agent-memory-turn/v1/{self.turn.turn_id}"
        if self.intent_id != expected_id:
            raise ValueError("committed-turn intent identity differs")
        if self.payload_json != canonical_json(self.turn.canonical_payload()):
            raise ValueError("committed-turn canonical payload differs")
        if self.payload_hash != self.turn.payload_hash:
            raise ValueError("committed-turn payload hash differs")

    @classmethod
    def from_domain(cls, turn: CommittedTurn) -> CommittedTurnSpec:
        if not isinstance(turn, CommittedTurn):
            raise TypeError("turn must use CommittedTurn")
        return cls(
            intent_id=f"agent-memory-turn/v1/{turn.turn_id}",
            turn=turn,
            payload_json=canonical_json(turn.canonical_payload()),
            payload_hash=turn.payload_hash,
        )


@dataclass(frozen=True, slots=True)
class MemoryOutboxRecord:
    intent_id: str
    run_id: str
    turn_id: str
    deployment_id: str
    household_id: str
    actor_id: str
    session_id: str
    payload_json: str
    payload_hash: str
    state: MemoryOutboxState
    claim_owner: str | None
    claim_epoch: int
    claim_expires_at: float | None
    attempt_count: int
    retry_at: float
    error_code: str | None
    created_at: float
    settled_at: float | None

    def committed_turn(self) -> CommittedTurn:
        try:
            value: object = json.loads(self.payload_json)
            if not isinstance(value, dict):
                raise TypeError("committed-turn payload is not an object")
            turn = CommittedTurn.from_json(value)
        except (TypeError, ValueError) as error:
            raise UnitOfWorkConflict("committed-turn payload is invalid") from error
        if (
            turn.turn_id != self.turn_id
            or turn.payload_hash != self.payload_hash
            or turn.identity.deployment_id != self.deployment_id
            or turn.identity.household_id != self.household_id
            or turn.identity.actor_id != self.actor_id
            or turn.identity.session_id != self.session_id
        ):
            raise UnitOfWorkConflict("committed-turn payload identity differs")
        return turn


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
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id is required")
        _positive_time(now, "now", allow_zero=True)
        _positive_time(lease_seconds, "lease_seconds")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT intent_id FROM memory_outbox WHERE "
                "(state IN ('pending','retry_wait') AND retry_at<=?) OR "
                "(state='claimed' AND claim_expires_at<=?) "
                "ORDER BY retry_at,created_at,intent_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            intent_id = str(row[0])
            changed = connection.execute(
                "UPDATE memory_outbox SET state='claimed',claim_owner=?,"
                "claim_epoch=claim_epoch+1,claim_expires_at=?,attempt_count=attempt_count+1 "
                "WHERE intent_id=? AND ((state IN ('pending','retry_wait') AND retry_at<=?) "
                "OR (state='claimed' AND claim_expires_at<=?))",
                (owner_id, now + lease_seconds, intent_id, now, now),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("memory outbox claim CAS failed")
        result = self.read(intent_id)
        assert result is not None
        return result

    def applied(
        self,
        claim: MemoryOutboxRecord,
        *,
        now: float,
        error_code: str | None = None,
    ) -> MemoryOutboxRecord:
        return self._settle(
            claim,
            state=MemoryOutboxState.APPLIED,
            now=now,
            error_code=error_code,
        )

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
        _positive_time(now, "now", allow_zero=True)
        _positive_time(backoff_seconds, "backoff_seconds", allow_zero=True)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_outbox SET state='retry_wait',claim_owner=NULL,"
                "claim_expires_at=NULL,retry_at=?,error_code=? WHERE intent_id=? "
                "AND state='claimed' AND claim_owner=? AND claim_epoch=?",
                (
                    now + backoff_seconds,
                    error_code,
                    claim.intent_id,
                    claim.claim_owner,
                    claim.claim_epoch,
                ),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("memory outbox release CAS failed")
        result = self.read(claim.intent_id)
        assert result is not None
        return result

    def backlog(self) -> dict[str, int]:
        result = {state.value: 0 for state in MemoryOutboxState}
        rows = self.database.connection.execute(
            "SELECT state,COUNT(*) FROM memory_outbox GROUP BY state"
        ).fetchall()
        result.update({str(row[0]): int(row[1]) for row in rows})
        return result

    def cleanup_applied(self, *, settled_before: float, limit: int) -> int:
        _positive_time(settled_before, "settled_before", allow_zero=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be positive")
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT intent_id FROM memory_outbox WHERE state='applied' "
                "AND settled_at<=? ORDER BY settled_at,intent_id LIMIT ?",
                (settled_before, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "DELETE FROM memory_outbox WHERE intent_id=? AND state='applied'",
                    (str(row[0]),),
                )
        return len(rows)

    def _settle(
        self,
        claim: MemoryOutboxRecord,
        *,
        state: MemoryOutboxState,
        now: float,
        error_code: str | None,
    ) -> MemoryOutboxRecord:
        if state not in {MemoryOutboxState.APPLIED, MemoryOutboxState.DEAD_LETTER}:
            raise ValueError("memory outbox settlement state is invalid")
        _positive_time(now, "now", allow_zero=True)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_outbox SET state=?,claim_owner=NULL,claim_expires_at=NULL,"
                "error_code=?,settled_at=? WHERE intent_id=? AND state='claimed' "
                "AND claim_owner=? AND claim_epoch=?",
                (
                    state.value,
                    error_code,
                    now,
                    claim.intent_id,
                    claim.claim_owner,
                    claim.claim_epoch,
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
        memory: AgentMemoryPort,
        *,
        owner_id: str,
        clock: Callable[[], float],
        lease_seconds: float = 30.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self.repository = repository
        self.memory = memory
        self.owner_id = owner_id
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._closed = False
        self._run_lock = asyncio.Lock()

    async def run_once(self, *, fault: Callable[[str], None] | None = None) -> bool:
        async with self._run_lock:
            return await self._run_once(fault=fault)

    async def _run_once(self, *, fault: Callable[[str], None] | None = None) -> bool:
        if self._closed:
            return False
        claim = self.repository.claim(
            owner_id=self.owner_id,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return False
        try:
            receipt = await self.memory.record_committed_turn(claim.committed_turn())
            if fault is not None:
                fault("memory_dispatcher.after_record_before_ack")
            self._validate_receipt(claim, receipt)
        except AgentMemoryError as error:
            if error.code in {AgentMemoryErrorCode.CONFLICT, AgentMemoryErrorCode.PERMANENT}:
                self.repository.dead_letter(
                    claim,
                    error_code=error.code.value,
                    now=self.clock(),
                )
            else:
                self._release_transient(claim, error.code.value)
            return True
        except UnitOfWorkConflict:
            self.repository.dead_letter(
                claim,
                error_code=AgentMemoryErrorCode.CONFLICT.value,
                now=self.clock(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._release_transient(claim, AgentMemoryErrorCode.TRANSIENT.value)
            return True
        if receipt.status is CommittedTurnStatus.CONFLICT:
            self.repository.dead_letter(
                claim,
                error_code=AgentMemoryErrorCode.CONFLICT.value,
                now=self.clock(),
            )
        elif receipt.status is CommittedTurnStatus.REJECTED_ERASED:
            logger.info(
                "memory.committed_turn_rejected_erased",
                extra={
                    "turn_id": claim.turn_id,
                    "payload_hash": claim.payload_hash,
                    "attempt": claim.attempt_count,
                    "error_code": CommittedTurnStatus.REJECTED_ERASED.value,
                },
            )
            self.repository.applied(
                claim,
                now=self.clock(),
                error_code=CommittedTurnStatus.REJECTED_ERASED.value,
            )
        else:
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
        self._closed = True

    @staticmethod
    def _validate_receipt(
        claim: MemoryOutboxRecord, receipt: CommittedTurnReceipt
    ) -> None:
        if not isinstance(receipt, CommittedTurnReceipt):
            raise AgentMemoryError(AgentMemoryErrorCode.CONFLICT)
        if receipt.turn_id != claim.turn_id or receipt.payload_hash != claim.payload_hash:
            raise AgentMemoryError(AgentMemoryErrorCode.CONFLICT)

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
        run_id=str(row["run_id"]),
        turn_id=str(row["turn_id"]),
        deployment_id=str(row["deployment_id"]),
        household_id=str(row["household_id"]),
        actor_id=str(row["actor_id"]),
        session_id=str(row["session_id"]),
        payload_json=str(row["payload_json"]),
        payload_hash=str(row["payload_hash"]),
        state=MemoryOutboxState(str(row["state"])),
        claim_owner=None if row["claim_owner"] is None else str(row["claim_owner"]),
        claim_epoch=int(row["claim_epoch"]),
        claim_expires_at=(
            None if row["claim_expires_at"] is None else float(row["claim_expires_at"])
        ),
        attempt_count=int(row["attempt_count"]),
        retry_at=float(row["retry_at"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        created_at=float(row["created_at"]),
        settled_at=None if row["settled_at"] is None else float(row["settled_at"]),
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
    "CommittedTurnSpec",
    "MemoryDispatcher",
    "MemoryOutboxRecord",
    "MemoryOutboxRepository",
    "MemoryOutboxState",
)
