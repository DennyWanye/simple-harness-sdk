# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState
from simple_harness.runtime.user_continuations import UserContinuationRuntime


def _root(uow: SqliteExecutionUnitOfWork) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="root-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"catalog_generation": 1},
        event_id="event-root",
        now=1.0,
    )


def test_continuation_reopen_preserves_fifo_and_claim_owner(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _root(uow)
    for index in range(1, 3):
        uow.enqueue_continuation(
            continuation_id=f"continuation-{index}",
            run_id="root-1",
            payload={"index": index},
            now=float(index + 1),
        )
    first = UserContinuationRuntime(uow, owner_id="runtime-a").claim(
        run_id="root-1", now=4.0
    )
    assert first is not None and first.continuation_id == "continuation-1"
    database.close()

    with Database.open(path) as reopened:
        runtime_a = UserContinuationRuntime(
            SqliteExecutionUnitOfWork(reopened), owner_id="runtime-a"
        )
        acked = runtime_a.acknowledge(first, now=5.0)
        assert acked.state is ContinuationState.ACKED
        second = runtime_a.claim(run_id="root-1", now=6.0)
        assert second is not None and second.continuation_id == "continuation-2"
