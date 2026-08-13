# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState, UnitOfWorkConflict


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
    "command,fault_point",
    (
        ("claim", "continuation_claim.continuation.before_write"),
        ("claim", "continuation_claim.continuation.after_write"),
        ("ack", "continuation_ack.continuation.before_write"),
        ("ack", "continuation_ack.continuation.after_write"),
    ),
)
def test_claim_and_ack_faults_reopen_all_before(
    tmp_path: Path, command: str, fault_point: str
) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    _enqueue(uow, "continuation-1")
    expected_state = ContinuationState.PENDING
    if command == "claim":
        operation = lambda: uow.claim_continuation(
            run_id="run-1", owner_id="owner-1", now=3.0, fault=_raise_at(fault_point)
        )
    else:
        claimed = uow.claim_continuation(run_id="run-1", owner_id="owner-1", now=3.0)
        assert claimed is not None
        expected_state = ContinuationState.CLAIMED
        operation = lambda: uow.ack_continuation(
            continuation_id="continuation-1",
            owner_id="owner-1",
            expected_version=claimed.version,
            now=4.0,
            fault=_raise_at(fault_point),
        )
    with pytest.raises(InjectedFault, match=fault_point):
        operation()
    database.close()
    with Database.open(path) as reopened:
        record = SqliteExecutionUnitOfWork(reopened).read_continuation("continuation-1")
        assert record is not None and record.state is expected_state


def test_claim_ack_after_commit_reopen_and_fifo_cas(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    for identifier in ("continuation-1", "continuation-2", "continuation-3"):
        _enqueue(uow, identifier)

    with pytest.raises(InjectedFault, match="continuation_claim.after_commit"):
        uow.claim_continuation(
            run_id="run-1",
            owner_id="owner-1",
            now=3.0,
            fault=_raise_at("continuation_claim.after_commit"),
        )
    database.close()

    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    first = uow.read_continuation("continuation-1")
    assert first is not None and first.state is ContinuationState.CLAIMED
    with pytest.raises(UnitOfWorkConflict, match="ack CAS"):
        uow.ack_continuation(
            continuation_id=first.continuation_id,
            owner_id="wrong-owner",
            expected_version=first.version,
            now=4.0,
        )
    with pytest.raises(InjectedFault, match="continuation_ack.after_commit"):
        uow.ack_continuation(
            continuation_id=first.continuation_id,
            owner_id="owner-1",
            expected_version=first.version,
            now=4.0,
            fault=_raise_at("continuation_ack.after_commit"),
        )
    database.close()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        acked = uow.ack_continuation(
            continuation_id=first.continuation_id,
            owner_id="owner-1",
            expected_version=first.version,
            now=5.0,
        )
        assert acked.state is ContinuationState.ACKED
        second = uow.claim_continuation(run_id="run-1", owner_id="owner-2", now=6.0)
        assert second is not None and second.continuation_id == "continuation-2"
        uow.ack_continuation(
            continuation_id=second.continuation_id,
            owner_id="owner-2",
            expected_version=second.version,
            now=7.0,
        )
        third = uow.claim_continuation(run_id="run-1", owner_id="owner-3", now=8.0)
        assert third is not None and third.continuation_id == "continuation-3"
