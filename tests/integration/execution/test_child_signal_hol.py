# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_atomic_child_terminal_signal import ack
from test_ticket_generation import (
    claim_child,
    create_root,
    issue_ticket,
    launch_request,
)

from simple_harness.execution.contracts.children import ChildSignalState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


def _two_signals(path: Path) -> None:
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        create_root(uow)
        first_request = launch_request(objective="first")
        issue_ticket(uow, ticket_id="ticket-1", request=first_request)
        claim_child(
            uow,
            ticket_id="ticket-1",
            child_run_id="child-1",
            command_id="command-1",
            request=first_request,
        )
        second_request = launch_request(objective="second")
        issue_ticket(uow, ticket_id="ticket-2", request=second_request)
        claim_child(
            uow,
            ticket_id="ticket-2",
            child_run_id="child-2",
            command_id="command-2",
            request=second_request,
        )
        uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-1",
            expected_child_version=0,
            terminal_state=RunState.COMPLETED,
            signal_id="signal-1",
            signal_payload={"ordinal": 1},
            event_id="event-child-1-terminal",
            now=5.0,
        )
        uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-2",
            expected_child_version=0,
            terminal_state=RunState.COMPLETED,
            signal_id="signal-2",
            signal_payload={"ordinal": 2},
            event_id="event-child-2-terminal",
            now=5.0,
        )


def test_concurrent_claim_has_one_owner_and_never_skips_active_head(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    _two_signals(path)

    def attempt(owner_id: str):
        with Database.open(path, timeout=10.0) as database:
            return SqliteExecutionUnitOfWork(database).claim_next_child_signal(
                parent_run_id="root-1",
                owner_id=owner_id,
                now=10.0,
                lease_seconds=10.0,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(attempt, ("runtime-a", "runtime-b")))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].signal_id == "signal-1"
    assert claimed[0].claim_epoch == 1
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        assert (
            uow.claim_next_child_signal(
                parent_run_id="root-1",
                owner_id="runtime-c",
                now=19.0,
                lease_seconds=10.0,
            )
            is None
        )
        second = uow.read_child_signal("signal-2")
        assert second is not None and second.state is ChildSignalState.PENDING


def test_expired_head_reclaims_with_new_epoch_before_second_signal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    _two_signals(path)
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        first = uow.claim_next_child_signal(
            parent_run_id="root-1",
            owner_id="runtime-a",
            now=10.0,
            lease_seconds=5.0,
        )
        assert first is not None and first.signal_id == "signal-1"
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        with pytest.raises(UnitOfWorkConflict, match="stale or expired"):
            ack(uow, owner_id="runtime-a", claim_epoch=1, now=15.0)
        reclaimed = uow.claim_next_child_signal(
            parent_run_id="root-1",
            owner_id="runtime-b",
            now=15.0,
            lease_seconds=10.0,
        )
        assert reclaimed is not None and reclaimed.signal_id == "signal-1"
        assert reclaimed.claim_epoch == 2 and reclaimed.claimed_by == "runtime-b"
        with pytest.raises(UnitOfWorkConflict, match="stale or expired"):
            ack(uow, owner_id="runtime-a", claim_epoch=1, now=16.0)
        result = ack(uow, owner_id="runtime-b", claim_epoch=2, now=16.0)
        assert result.signal.state is ChildSignalState.ACKED
        second = uow.claim_next_child_signal(
            parent_run_id="root-1",
            owner_id="runtime-b",
            now=17.0,
            lease_seconds=10.0,
        )
        assert second is not None and second.signal_id == "signal-2"


def test_ack_retry_rejects_same_receipt_with_different_payload(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    _two_signals(path)
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        assert uow.claim_next_child_signal(
            parent_run_id="root-1",
            owner_id="runtime-a",
            now=10.0,
            lease_seconds=10.0,
        ) is not None
        ack(uow, now=11.0)
        with pytest.raises(UnitOfWorkConflict, match="differently"):
            ack(
                uow,
                continuation_payload={"kind": "changed"},
                now=12.0,
            )
