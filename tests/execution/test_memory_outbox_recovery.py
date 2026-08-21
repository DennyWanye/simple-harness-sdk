# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

from simple_harness.execution.memory_outbox import (
    MemoryDispatcher,
    MemoryIntentSpec,
    MemoryOutboxRepository,
    MemoryOutboxState,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.runtime import (
    ConversationMemoryApplyResult,
    ConversationMemoryApplyStatus,
    ConversationMemoryIntent,
    ConversationMemoryRole,
)


class IdempotentSink:
    def __init__(self) -> None:
        self.seen: dict[str, str] = {}
        self.calls = 0
        self.closed = False

    async def apply(self, intent):  # type: ignore[no-untyped-def]
        self.calls += 1
        prior = self.seen.setdefault(intent.source_event_id, intent.payload_hash)
        status = (
            ConversationMemoryApplyStatus.APPLIED
            if self.calls == 1
            else ConversationMemoryApplyStatus.ALREADY_APPLIED
        )
        return ConversationMemoryApplyResult(
            intent.source_event_id, prior, status, "record-1"
        )

    async def close(self) -> None:
        self.closed = True


def test_after_apply_before_ack_reopens_and_replays_idempotently(tmp_path: Path) -> None:
    async def case() -> None:
        path = tmp_path / "execution.db"
        database = Database.open(path)
        uow = SqliteExecutionUnitOfWork(database)
        conversation = ConversationMemoryIntent(
            "harness-memory/v1/user/run-1",
            "user-1",
            "session-1",
            ConversationMemoryRole.USER,
            "hello",
        )
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"schema_version": 5, "input": {}},
            event_id="run-1:created",
            user_id="user-1",
            memory_intent=MemoryIntentSpec.from_conversation(conversation),
            now=1.0,
        )
        clock = [1.0]
        sink = IdempotentSink()
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database),
            sink,
            owner_id="worker-1",
            clock=lambda: clock[0],
            lease_seconds=5.0,
        )

        def crash(point: str) -> None:
            if point == "memory_dispatcher.after_apply_before_ack":
                raise RuntimeError(point)

        assert await dispatcher.run_once(fault=crash)
        pending = dispatcher.repository.read(conversation.source_event_id)
        assert pending is not None and pending.state is MemoryOutboxState.PENDING
        database.close()

        database = Database.open(path)
        clock[0] = 3.0
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database),
            sink,
            owner_id="worker-2",
            clock=lambda: clock[0],
            lease_seconds=5.0,
        )
        assert await dispatcher.run_once()
        applied = dispatcher.repository.read(conversation.source_event_id)
        assert applied is not None and applied.state is MemoryOutboxState.APPLIED
        assert sink.calls == 2
        await dispatcher.close()
        assert sink.closed
        database.close()

    asyncio.run(case())
