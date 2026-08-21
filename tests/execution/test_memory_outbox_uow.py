# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.memory_outbox import MemoryIntentSpec
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime import ConversationMemoryIntent, ConversationMemoryRole


class InjectedFault(RuntimeError):
    pass


def _intent(text: str | None = "hello") -> MemoryIntentSpec:
    return MemoryIntentSpec.from_conversation(
        ConversationMemoryIntent(
            "harness-memory/v1/user/run-1",
            "user-1",
            "session-1",
            ConversationMemoryRole.USER,
            text,
        )
    )


def _create(uow: SqliteExecutionUnitOfWork, intent: MemoryIntentSpec, *, fault=None):
    return uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"schema_version": 5, "input": {}},
        event_id="run-1:created",
        user_id="user-1",
        memory_intent=intent,
        now=1.0,
        fault=fault,
    )


def test_start_and_memory_intent_rollback_together(tmp_path: Path) -> None:
    def fault(point: str) -> None:
        if point == "root_start.memory_intent.after_write":
            raise InjectedFault(point)

    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        with pytest.raises(InjectedFault):
            _create(uow, _intent(), fault=fault)
        assert database.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert (
            database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]
            == 0
        )


def test_replay_compares_memory_hash_and_non_text_settles_locally(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _create(uow, _intent(None))
        row = database.connection.execute(
            "SELECT state,memory_text FROM memory_outbox"
        ).fetchone()
        assert tuple(row) == ("skipped_non_text", None)
        _create(uow, _intent(None))
        with pytest.raises(UnitOfWorkConflict, match="memory intent replay differs"):
            _create(uow, _intent("different"))


def test_session_cannot_be_rebound_to_another_user(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _create(uow, _intent())
        other = MemoryIntentSpec.from_conversation(
            ConversationMemoryIntent(
                "harness-memory/v1/user/run-2",
                "user-2",
                "session-1",
                ConversationMemoryRole.USER,
                "second",
            )
        )
        with pytest.raises(UnitOfWorkConflict, match="another user"):
            uow.create_with_start_snapshot(
                execution_session_id="session-1",
                run_id="run-2",
                request_id="request-2",
                profile_key="agent.general",
                driver_kind="react",
                snapshot={"schema_version": 5, "input": {}},
                event_id="run-2:created",
                user_id="user-2",
                memory_intent=other,
                now=2.0,
            )
