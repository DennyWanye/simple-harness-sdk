# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState, RunState


class InjectedFault(RuntimeError):
    pass


def _raise_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return inject


def _create(uow: SqliteExecutionUnitOfWork):
    return uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"messages": []},
        event_id="event-root-1",
        now=1.0,
    )


def _enqueue(uow: SqliteExecutionUnitOfWork, identifier: str, *, fault=None):
    return uow.enqueue_continuation(
        continuation_id=identifier,
        run_id="run-1",
        payload={"id": identifier},
        now=2.0,
        fault=fault,
    )


def _activate(uow: SqliteExecutionUnitOfWork, *, owner_id: str = "owner-1", now=2.5):
    return uow.claim_runtime_activation(
        run_id="run-1",
        owner_id=owner_id,
        namespace="runtime.kernel",
        now=now,
        lease_ttl_seconds=100.0,
    )[1]


@pytest.mark.parametrize(
    "fault_point",
    (
        "continuation_enqueue.continuation.before_write",
        "continuation_enqueue.continuation.after_write",
    ),
)
def test_enqueue_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        _enqueue(uow, "continuation-1", fault=_raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        assert reopened.connection.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 0


def test_enqueue_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match="continuation_enqueue.after_commit"):
        _enqueue(uow, "continuation-1", fault=_raise_at("continuation_enqueue.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        assert _enqueue(SqliteExecutionUnitOfWork(reopened), "continuation-1").fifo_seq == 1


@pytest.mark.parametrize(
    "fault_point",
    (
        "continuation_claim.continuation.before_write",
        "continuation_claim.continuation.after_write",
    ),
)
def test_claim_faults_reopen_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    _enqueue(uow, "continuation-1")
    lease = _activate(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        uow.claim_continuation(
            run_id="run-1",
            execution_lease=lease,
            now=3.0,
            fault=_raise_at(fault_point),
        )
    database.close()
    with Database.open(path) as reopened:
        record = SqliteExecutionUnitOfWork(reopened).read_continuation("continuation-1")
        assert record is not None and record.state is ContinuationState.PENDING


def test_claim_after_commit_reopen_and_atomic_progress_preserve_fifo(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    for identifier in ("continuation-1", "continuation-2", "continuation-3"):
        _enqueue(uow, identifier)
    lease = _activate(uow)

    with pytest.raises(InjectedFault, match="continuation_claim.after_commit"):
        uow.claim_continuation(
            run_id="run-1",
            execution_lease=lease,
            now=3.0,
            fault=_raise_at("continuation_claim.after_commit"),
        )
    database.close()

    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    first = uow.read_continuation("continuation-1")
    assert first is not None and first.state is ContinuationState.CLAIMED
    run = uow.read_run("run-1")
    assert run is not None
    progress = uow.commit_runtime_state_and_ack_continuation(
        run_id="run-1",
        expected_version=run.version,
        state=RunState.WAITING,
        event_id="run-1:waiting:continuation-1",
        payload={"handled": "continuation-1"},
        continuation_claim=first,
        execution_lease=lease,
        receipt_id="progress:continuation-1",
        now=4.0,
    )
    assert progress.continuation.state is ContinuationState.ACKED
    second = uow.claim_continuation(run_id="run-1", execution_lease=lease, now=5.0)
    assert second is not None and second.continuation_id == "continuation-2"
    run = uow.read_run("run-1")
    assert run is not None
    uow.commit_runtime_state_and_ack_continuation(
        run_id="run-1",
        expected_version=run.version,
        state=RunState.WAITING,
        event_id="run-1:waiting:continuation-2",
        payload={"handled": "continuation-2"},
        continuation_claim=second,
        execution_lease=lease,
        receipt_id="progress:continuation-2",
        now=6.0,
    )
    database.close()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        _, resumed_lease = uow.claim_runtime_activation(
            run_id="run-1",
            owner_id="owner-2",
            namespace="runtime.kernel",
            now=105.0,
            lease_ttl_seconds=100.0,
        )
        third = uow.claim_continuation(run_id="run-1", execution_lease=resumed_lease, now=105.0)
        assert third is not None and third.continuation_id == "continuation-3"


def test_owner_only_ack_entrypoint_is_removed() -> None:
    assert not hasattr(SqliteExecutionUnitOfWork, "ack_continuation")
