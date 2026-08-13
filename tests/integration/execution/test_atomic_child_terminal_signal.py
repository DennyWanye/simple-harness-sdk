# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.contracts.children import ChildSignalState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState, RunState, UnitOfWorkConflict

from test_atomic_child_launch import InjectedFault, raise_at
from test_ticket_generation import claim_child, create_root, issue_ticket


TERMINAL_POINTS = tuple(
    f"child_terminal.{write}.{side}_write"
    for write in ("run", "command", "signal", "event")
    for side in ("before", "after")
)
ACK_POINTS = tuple(
    f"child_signal_ack.{write}.{side}_write"
    for write in ("signal", "continuation", "parent", "event")
    for side in ("before", "after")
)


def setup_child(path: Path) -> tuple[Database, SqliteExecutionUnitOfWork]:
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    create_root(uow)
    issue_ticket(uow)
    claim_child(uow)
    return database, uow


def finalize(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.finalize_child_and_enqueue_parent_signal(
        command_id="command-1",
        expected_child_version=0,
        terminal_state=RunState.COMPLETED,
        signal_id="signal-1",
        signal_payload={"outcome": "completed", "value": {"answer": 42}},
        event_id="event-child-terminal",
        now=4.0,
        fault=fault,
    )


def ack(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.ack_child_signal(
        signal_id="signal-1",
        expected_version=0,
        continuation_id="continuation-child-1",
        continuation_payload={"kind": "child_terminal", "signal_id": "signal-1"},
        event_id="event-signal-acked",
        now=5.0,
        fault=fault,
    )


@pytest.mark.parametrize("fault_point", TERMINAL_POINTS)
def test_child_terminal_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow = setup_child(path)
    with pytest.raises(InjectedFault, match=fault_point):
        finalize(uow, fault=raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_run("child-1").state is RunState.CREATED  # type: ignore[union-attr]
        assert uow.read_child_command("command-1").state.value == "pending"  # type: ignore[union-attr]
        assert uow.read_child_signal("signal-1") is None


def test_child_terminal_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow = setup_child(path)
    with pytest.raises(InjectedFault, match="child_terminal.after_commit"):
        finalize(uow, fault=raise_at("child_terminal.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert finalize(uow).state is ChildSignalState.PENDING
        assert uow.read_run("child-1").state is RunState.COMPLETED  # type: ignore[union-attr]


@pytest.mark.parametrize("fault_point", ACK_POINTS)
def test_signal_ack_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow = setup_child(path)
    finalize(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        ack(uow, fault=raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_child_signal("signal-1").state is ChildSignalState.PENDING  # type: ignore[union-attr]
        assert uow.read_continuation("continuation-child-1") is None
        assert uow.read_run("root-1").state is RunState.WAITING  # type: ignore[union-attr]


def test_signal_ack_after_commit_reopens_all_after_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow = setup_child(path)
    finalize(uow)
    with pytest.raises(InjectedFault, match="child_signal_ack.after_commit"):
        ack(uow, fault=raise_at("child_signal_ack.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert ack(uow).state is ChildSignalState.ACKED
        continuation = uow.read_continuation("continuation-child-1")
        assert continuation is not None and continuation.state is ContinuationState.PENDING
        assert uow.read_run("root-1").state is RunState.QUEUED  # type: ignore[union-attr]
        with pytest.raises(UnitOfWorkConflict, match="differently"):
            uow.ack_child_signal(
                signal_id="signal-1",
                expected_version=0,
                continuation_id="other-continuation",
                continuation_payload={"kind": "other"},
                event_id="other-event",
                now=6.0,
            )
