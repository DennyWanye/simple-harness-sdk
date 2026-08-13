# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from simple_harness.contracts import RunId
from simple_harness.execution.delivery import DeliverySpec, DeliveryState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState


class InjectedFault(RuntimeError):
    pass


def raise_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return inject


def setup_root(path: Path):
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"messages": []},
        event_id="event-root",
        now=1.0,
    )
    fence = asyncio.run(uow.acquire(RunId("run-1"), "runtime-1"))
    return database, uow, fence


def deliveries() -> tuple[DeliverySpec, ...]:
    return (
        DeliverySpec("delivery-1", "presenter", "terminal:run-1:presenter", {"answer": 42}),
        DeliverySpec("delivery-2", "artifact", "terminal:run-1:artifact", {"paths": []}),
    )


def commit_terminal(uow, fence, *, fault=None, items=None):
    return uow.commit_root_terminal_with_deliveries(
        run_id="run-1",
        expected_version=0,
        terminal_state=RunState.COMPLETED,
        event_id="event-terminal",
        terminal_payload={"answer": 42},
        deliveries=deliveries() if items is None else items,
        fence=fence,
        terminal_fence_receipt_ref="receipt://terminal/run-1/1",
        now=2.0,
        fault=fault,
    )


WRITE_POINTS = (
    "root_terminal.event.before_write",
    "root_terminal.event.after_write",
    "root_terminal.delivery.0.before_write",
    "root_terminal.delivery.0.after_write",
    "root_terminal.delivery.1.before_write",
    "root_terminal.delivery.1.after_write",
    "root_terminal.fence.before_write",
    "root_terminal.fence.after_write",
    "root_terminal.run.before_write",
    "root_terminal.run.after_write",
)


@pytest.mark.parametrize("fault_point", WRITE_POINTS)
def test_terminal_delivery_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    with pytest.raises(InjectedFault, match=fault_point):
        commit_terminal(uow, fence, fault=raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        assert reopened.connection.execute("SELECT state FROM runs WHERE run_id='run-1'").fetchone()[0] == "created"
        assert reopened.connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events WHERE event_id='event-terminal'").fetchone()[0] == 0
        assert reopened.connection.execute("SELECT state FROM run_fences WHERE run_id='run-1'").fetchone()[0] == "active"


def test_terminal_delivery_after_commit_reopens_all_after_and_replays(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    with pytest.raises(InjectedFault, match="root_terminal.after_commit"):
        commit_terminal(uow, fence, fault=raise_at("root_terminal.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        result = commit_terminal(uow, fence)
        assert result.run.state is RunState.COMPLETED
        assert [item.state for item in result.deliveries] == [DeliveryState.PENDING] * 2
        assert reopened.connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 2
        event = reopened.connection.execute("SELECT payload_json FROM run_events WHERE event_id='event-terminal'").fetchone()
        payload = json.loads(event[0])
        assert payload["terminal_fence_receipt_ref"] == "receipt://terminal/run-1/1"
        assert payload["fence_epoch"] == fence.epoch
        assert reopened.connection.execute("SELECT state FROM run_fences WHERE run_id='run-1'").fetchone()[0] == "released"


def test_zero_delivery_terminal_still_commits_fence_receipt(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    result = commit_terminal(uow, fence, items=())
    assert result.deliveries == ()
    row = database.connection.execute("SELECT payload_json FROM run_events WHERE event_id='event-terminal'").fetchone()
    assert json.loads(row[0])["terminal_fence_receipt_ref"] == "receipt://terminal/run-1/1"
    database.close()


@pytest.mark.parametrize(
    "fault_point",
    (
        "delivery_claim.expired.before_write",
        "delivery_claim.expired.after_write",
        "delivery_claim.delivery.before_write",
        "delivery_claim.delivery.after_write",
    ),
)
def test_delivery_claim_fault_reopens_pending(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    commit_terminal(uow, fence)
    with pytest.raises(InjectedFault, match=fault_point):
        uow.claim_delivery(
            sink_kinds=("presenter",),
            now=3.0,
            claim_ttl_seconds=30.0,
            fault=raise_at(fault_point),
        )
    database.close()
    with Database.open(path) as reopened:
        delivery = SqliteExecutionUnitOfWork(reopened).read_delivery("delivery-1")
        assert delivery is not None and delivery.state is DeliveryState.PENDING


def test_claim_after_commit_reopens_claimed_then_expiry_reclaims(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    commit_terminal(uow, fence)
    with pytest.raises(InjectedFault, match="delivery_claim.after_commit"):
        uow.claim_delivery(
            sink_kinds=("presenter",),
            now=3.0,
            claim_ttl_seconds=30.0,
            fault=raise_at("delivery_claim.after_commit"),
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        stranded = uow.read_delivery("delivery-1")
        assert stranded is not None and stranded.state is DeliveryState.CLAIMED
        reclaimed = uow.claim_delivery(
            sink_kinds=("presenter",), now=34.0, claim_ttl_seconds=30.0
        )
        assert reclaimed is not None and reclaimed.delivery_id == "delivery-1"
        assert reclaimed.version == stranded.version + 2


@pytest.mark.parametrize("command", ("complete", "release"))
@pytest.mark.parametrize("side", ("before_write", "after_write"))
def test_delivery_settle_fault_reopens_claimed(
    tmp_path: Path, command: str, side: str
) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    commit_terminal(uow, fence)
    claimed = uow.claim_delivery(
        sink_kinds=("presenter",), now=3.0, claim_ttl_seconds=30.0
    )
    assert claimed is not None
    fault_point = f"delivery_{command}.delivery.{side}"
    method = getattr(uow, f"{command}_delivery")
    with pytest.raises(InjectedFault, match=fault_point):
        method(
            claimed.delivery_id,
            expected_version=claimed.version,
            now=4.0,
            fault=raise_at(fault_point),
        )
    database.close()
    with Database.open(path) as reopened:
        record = SqliteExecutionUnitOfWork(reopened).read_delivery("delivery-1")
        assert record is not None and record.state is DeliveryState.CLAIMED


@pytest.mark.parametrize(
    "command,expected_state",
    (("complete", DeliveryState.DELIVERED), ("release", DeliveryState.PENDING)),
)
def test_delivery_settle_after_commit_reopens_all_after(
    tmp_path: Path, command: str, expected_state: DeliveryState
) -> None:
    path = tmp_path / "execution.db"
    database, uow, fence = setup_root(path)
    commit_terminal(uow, fence)
    claimed = uow.claim_delivery(
        sink_kinds=("presenter",), now=3.0, claim_ttl_seconds=30.0
    )
    assert claimed is not None
    method = getattr(uow, f"{command}_delivery")
    fault_point = f"delivery_{command}.after_commit"
    with pytest.raises(InjectedFault, match=fault_point):
        method(
            claimed.delivery_id,
            expected_version=claimed.version,
            now=4.0,
            fault=raise_at(fault_point),
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert (
            method.__name__
            in {"complete_delivery", "release_delivery"}
        )
        persisted = uow.read_delivery("delivery-1")
        assert persisted is not None and persisted.state is expected_state
        replay = getattr(uow, f"{command}_delivery")(
            claimed.delivery_id, expected_version=claimed.version, now=5.0
        )
        assert replay.state is expected_state
