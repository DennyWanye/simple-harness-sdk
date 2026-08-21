# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState, RunState
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
    _, lease = uow.claim_runtime_activation(
        run_id="root-1",
        owner_id="runtime-a",
        namespace="runtime.kernel",
        now=3.0,
        lease_ttl_seconds=100.0,
    )
    first = UserContinuationRuntime(uow).claim(run_id="root-1", execution_lease=lease, now=4.0)
    assert first is not None and first.continuation_id == "continuation-1"
    database.close()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        run = uow.read_run("root-1")
        assert run is not None
        acked = uow.commit_runtime_state_and_ack_continuation(
            run_id="root-1",
            expected_version=run.version,
            state=RunState.WAITING,
            event_id="root-1:waiting:continuation-1",
            payload={"handled": "continuation-1"},
            continuation_claim=first,
            execution_lease=lease,
            receipt_id="progress:continuation-1",
            now=5.0,
        )
        assert acked.continuation.state is ContinuationState.ACKED
        second = UserContinuationRuntime(uow).claim(run_id="root-1", execution_lease=lease, now=6.0)
        assert second is not None and second.continuation_id == "continuation-2"
