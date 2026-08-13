# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState

from test_ticket_generation import claim_child, create_root, issue_ticket


class InjectedFault(RuntimeError):
    pass


def raise_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return inject


LAUNCH_WRITE_POINTS = tuple(
    f"child_launch.{write}.{side}_write"
    for write in ("ticket", "run", "snapshot", "command", "link", "parent", "event")
    for side in ("before", "after")
)


@pytest.mark.parametrize("fault_point", LAUNCH_WRITE_POINTS)
def test_child_launch_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    create_root(uow)
    issue_ticket(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        request = {
            "profile_key": "workflow.durable_task",
            "driver_kind": "workflow",
            "catalog_generation": 7,
            "objective": "do work",
        }
        uow.claim_profile_launch_and_commit_child(
            ticket_id="ticket-1",
            expected_catalog_generation=7,
            launch_request=request,
            command_id="command-1",
            child_run_id="child-1",
            request_id="request-child-1",
            attachment_policy="attached",  # type: ignore[arg-type]
            start_snapshot={"launch": request},
            event_id="event-child-1",
            now=3.0,
            fault=raise_at(fault_point),
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_profile_launch_ticket("ticket-1").state.value == "issued"  # type: ignore[union-attr]
        assert uow.read_run("child-1") is None
        assert uow.read_child_command("command-1") is None
        assert uow.read_run("root-1").state is RunState.CREATED  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_links").fetchone()[0] == 0


def test_child_launch_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    create_root(uow)
    issue_ticket(uow)
    with pytest.raises(InjectedFault, match="child_launch.after_commit"):
        request = {
            "profile_key": "workflow.durable_task",
            "driver_kind": "workflow",
            "catalog_generation": 7,
            "objective": "do work",
        }
        uow.claim_profile_launch_and_commit_child(
            ticket_id="ticket-1",
            expected_catalog_generation=7,
            launch_request=request,
            command_id="command-1",
            child_run_id="child-1",
            request_id="request-child-1",
            attachment_policy="attached",  # type: ignore[arg-type]
            start_snapshot={"launch": request},
            event_id="event-child-1",
            now=3.0,
            fault=raise_at("child_launch.after_commit"),
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        result = claim_child(uow)
        assert result.ticket.state.value == "claimed"
        assert uow.read_run("child-1").state is RunState.CREATED  # type: ignore[union-attr]
        assert uow.read_run("root-1").state is RunState.WAITING  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_links").fetchone()[0] == 1
