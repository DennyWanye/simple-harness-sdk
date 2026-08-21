# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_atomic_child_launch import InjectedFault, raise_at
from test_ticket_generation import claim_child, create_root, issue_ticket

from simple_harness.contracts import RunId
from simple_harness.execution.contracts.children import ChildSignalState
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import (
    ContinuationState,
    ExecutionLease,
    RunState,
    UnitOfWorkConflict,
)

TERMINAL_POINTS = tuple(
    f"child_terminal.{write}.{side}_write"
    for write in ("run", "command", "signal", "event", "receipt", "fence")
    for side in ("before", "after")
)
ACK_POINTS = tuple(
    f"child_signal_ack.{write}.{side}_write"
    for write in ("continuation", "event", "receipt", "signal", "parent")
    for side in ("before", "after")
)


def setup_child(
    path: Path,
) -> tuple[Database, SqliteExecutionUnitOfWork, ExecutionLease, RunFenceLease]:
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    create_root(uow)
    issue_ticket(uow)
    claim_child(uow)
    _, lease = uow.claim_runtime_activation(
        run_id="child-1",
        owner_id="runtime-child",
        namespace="runtime.kernel",
        now=3.5,
        lease_ttl_seconds=100.0,
    )
    fence = asyncio.run(uow.acquire(RunId("child-1"), lease, now=3.5))
    return database, uow, lease, fence


def finalize(
    uow: SqliteExecutionUnitOfWork,
    lease: ExecutionLease,
    fence: RunFenceLease,
    *,
    fault=None,
):
    return uow.finalize_child_and_enqueue_parent_signal(
        command_id="command-1",
        expected_child_version=1,
        terminal_state=RunState.COMPLETED,
        signal_id="signal-1",
        signal_payload={"outcome": "completed", "value": {"answer": 42}},
        event_id="event-child-terminal",
        receipt_id="receipt-child-terminal",
        run_fence=fence,
        execution_lease=lease,
        now=4.0,
        fault=fault,
    )


def claim(uow: SqliteExecutionUnitOfWork, *, owner_id: str = "runtime-a"):
    return uow.claim_next_child_signal(
        parent_run_id="root-1",
        owner_id=owner_id,
        now=5.0,
        lease_seconds=10.0,
    )


def ack(
    uow: SqliteExecutionUnitOfWork,
    *,
    owner_id: str = "runtime-a",
    claim_epoch: int = 1,
    receipt_id: str = "receipt-signal-1",
    continuation_id: str = "continuation-child-1",
    continuation_payload=None,
    event_id: str = "event-signal-acked",
    event_payload=None,
    now: float = 6.0,
    fault=None,
):
    continuation_payload = (
        {"kind": "child_terminal", "signal_id": "signal-1"}
        if continuation_payload is None
        else continuation_payload
    )
    event_payload = (
        {
            "signal_id": "signal-1",
            "continuation_id": continuation_id,
            "receipt_id": receipt_id,
        }
        if event_payload is None
        else event_payload
    )
    return uow.ack_child_signal_and_commit_parent_progress(
        signal_id="signal-1",
        owner_id=owner_id,
        claim_epoch=claim_epoch,
        receipt_id=receipt_id,
        continuation_id=continuation_id,
        continuation_payload=continuation_payload,
        event_id=event_id,
        event_payload=event_payload,
        now=now,
        fault=fault,
    )


@pytest.mark.parametrize("fault_point", TERMINAL_POINTS)
def test_child_terminal_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow, lease, fence = setup_child(path)
    with pytest.raises(InjectedFault, match=fault_point):
        finalize(uow, lease, fence, fault=raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_run("child-1").state is RunState.RUNNING  # type: ignore[union-attr]
        assert uow.read_child_command("command-1").state.value == "pending"  # type: ignore[union-attr]
        assert uow.read_child_signal("signal-1") is None


def test_child_terminal_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow, lease, fence = setup_child(path)
    with pytest.raises(InjectedFault, match="child_terminal.after_commit"):
        finalize(
            uow,
            lease,
            fence,
            fault=raise_at("child_terminal.after_commit"),
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        result = finalize(uow, lease, fence)
        assert result.signal is not None
        assert result.signal.state is ChildSignalState.PENDING
        assert uow.read_run("child-1").state is RunState.COMPLETED  # type: ignore[union-attr]


@pytest.mark.parametrize("fault_point", ACK_POINTS)
def test_signal_ack_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow, lease, fence = setup_child(path)
    finalize(uow, lease, fence)
    claimed = claim(uow)
    assert claimed is not None and claimed.claim_epoch == 1
    with pytest.raises(InjectedFault, match=fault_point):
        ack(uow, fault=raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        signal = uow.read_child_signal("signal-1")
        assert signal is not None and signal.state is ChildSignalState.CLAIMED
        assert signal.claimed_by == "runtime-a" and signal.claim_epoch == 1
        assert uow.read_continuation("continuation-child-1") is None
        assert uow.read_child_signal_ack_receipt("receipt-signal-1") is None
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE event_id = 'event-signal-acked'"
            ).fetchone()[0]
            == 0
        )
        assert uow.read_run("root-1").state is RunState.WAITING  # type: ignore[union-attr]


def test_signal_ack_after_commit_reopens_all_after_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    database, uow, lease, fence = setup_child(path)
    finalize(uow, lease, fence)
    assert claim(uow) is not None
    with pytest.raises(InjectedFault, match="child_signal_ack.after_commit"):
        ack(uow, fault=raise_at("child_signal_ack.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        repeated = ack(uow)
        assert repeated.signal.state is ChildSignalState.ACKED
        assert repeated.receipt.receipt_id == "receipt-signal-1"
        continuation = uow.read_continuation("continuation-child-1")
        assert continuation is not None and continuation.state is ContinuationState.PENDING
        assert uow.read_run("root-1").state is RunState.QUEUED  # type: ignore[union-attr]
        with pytest.raises(UnitOfWorkConflict, match="differently"):
            uow.ack_child_signal_and_commit_parent_progress(
                signal_id="signal-1",
                owner_id="runtime-a",
                claim_epoch=1,
                receipt_id="other-receipt",
                continuation_id="other-continuation",
                continuation_payload={"kind": "other"},
                event_id="other-event",
                event_payload={"kind": "other"},
                now=6.0,
            )


def test_old_ack_entrypoint_is_not_public() -> None:
    assert not hasattr(SqliteExecutionUnitOfWork, "ack_child_signal")
