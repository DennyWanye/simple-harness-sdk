# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork


def test_two_user_continuations_keep_fifo_without_tentative_memory(tmp_path: Path) -> None:
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
            now=2.0,
        )
        second = uow.enqueue_continuation(
            continuation_id="continuation-2",
            run_id="run-1",
            payload={"kind": "conversation_user", "text": "two"},
            now=3.0,
        )
        assert (first.fifo_seq, second.fifo_seq) == (1, 2)
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM memory_outbox"
            ).fetchone()[0]
            == 0
        )
        assert (
            uow.enqueue_continuation(
                continuation_id="continuation-1",
                run_id="run-1",
                payload={"kind": "conversation_user", "text": "one"},
                now=4.0,
            )
            == first
        )


def test_continuation_replays_without_creating_memory_intent(
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
        assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
