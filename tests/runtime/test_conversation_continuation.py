# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.memory_outbox import MemoryIntentSpec
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime import ConversationMemoryIntent, ConversationMemoryRole


def _spec(identity: str, text: str) -> MemoryIntentSpec:
    return MemoryIntentSpec.from_conversation(
        ConversationMemoryIntent(
            f"harness-memory/v1/user-continuation/{identity}",
            "user-1",
            "session-1",
            ConversationMemoryRole.USER,
            text,
        )
    )


def test_two_user_continuations_keep_fifo_and_distinct_intents(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"schema_version": 5},
            event_id="run-1:created",
            user_id="user-1",
            now=1.0,
        )
        first = uow.enqueue_continuation(
            continuation_id="continuation-1",
            run_id="run-1",
            payload={"kind": "conversation_user", "text": "one"},
            memory_intent=_spec("continuation-1", "one"),
            now=2.0,
        )
        second = uow.enqueue_continuation(
            continuation_id="continuation-2",
            run_id="run-1",
            payload={"kind": "conversation_user", "text": "two"},
            memory_intent=_spec("continuation-2", "two"),
            now=3.0,
        )
        assert (first.fifo_seq, second.fifo_seq) == (1, 2)
        assert database.connection.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE continuation_id IS NOT NULL"
        ).fetchone()[0] == 2
        assert (
            uow.enqueue_continuation(
                continuation_id="continuation-1",
                run_id="run-1",
                payload={"kind": "conversation_user", "text": "one"},
                memory_intent=_spec("continuation-1", "one"),
                now=4.0,
            )
            == first
        )
        with pytest.raises(UnitOfWorkConflict, match="memory intent replay differs"):
            uow.enqueue_continuation(
                continuation_id="continuation-1",
                run_id="run-1",
                payload={"kind": "conversation_user", "text": "one"},
                memory_intent=None,
                now=4.5,
            )
        with pytest.raises(UnitOfWorkConflict, match="memory intent replay differs"):
            uow.enqueue_continuation(
                continuation_id="continuation-1",
                run_id="run-1",
                payload={"kind": "conversation_user", "text": "one"},
                memory_intent=_spec("continuation-1", "different"),
                now=5.0,
            )


def test_continuation_without_memory_intent_replays_only_without_intent(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"schema_version": 5},
            event_id="run-1:created",
            user_id="user-1",
            now=1.0,
        )
        kwargs = {
            "continuation_id": "continuation-no-memory",
            "run_id": "run-1",
            "payload": {"kind": "conversation_user", "text": "one"},
            "now": 2.0,
        }
        first = uow.enqueue_continuation(**kwargs)
        assert uow.enqueue_continuation(**kwargs) == first
        with pytest.raises(UnitOfWorkConflict, match="memory intent replay differs"):
            uow.enqueue_continuation(
                **kwargs,
                memory_intent=_spec("continuation-no-memory", "one"),
            )
