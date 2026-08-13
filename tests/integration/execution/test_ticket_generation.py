# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict


def launch_request(*, generation: int = 7, objective: str = "do work") -> dict[str, object]:
    return {
        "profile_key": "workflow.durable_task",
        "driver_kind": "workflow",
        "catalog_generation": generation,
        "objective": objective,
    }


def create_root(uow: SqliteExecutionUnitOfWork) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="root-1",
        request_id="request-root",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"messages": []},
        event_id="event-root",
        now=1.0,
    )


def issue_ticket(
    uow: SqliteExecutionUnitOfWork,
    *,
    ticket_id: str = "ticket-1",
    request: dict[str, object] | None = None,
) -> ProfileLaunchTicket:
    request = launch_request() if request is None else request
    ticket = ProfileLaunchTicket(
        ticket_id=ticket_id,
        parent_run_id="root-1",
        profile_key="workflow.durable_task",
        catalog_generation=7,
        fingerprint=child_launch_fingerprint(request),  # type: ignore[arg-type]
    )
    return uow.issue_profile_launch_ticket(ticket, now=2.0)


def claim_child(
    uow: SqliteExecutionUnitOfWork,
    *,
    ticket_id: str = "ticket-1",
    child_run_id: str = "child-1",
    command_id: str = "command-1",
    request: dict[str, object] | None = None,
):
    request = launch_request() if request is None else request
    return uow.claim_profile_launch_and_commit_child(
        ticket_id=ticket_id,
        expected_catalog_generation=7,
        launch_request=request,  # type: ignore[arg-type]
        command_id=command_id,
        child_run_id=child_run_id,
        request_id=f"request-{child_run_id}",
        attachment_policy=AttachmentPolicy.ATTACHED,
        start_snapshot={"launch": request},  # type: ignore[dict-item]
        event_id=f"event-{child_run_id}",
        now=3.0,
    )


def test_ticket_fingerprint_binds_generation_profile_driver_and_payload() -> None:
    baseline = launch_request()
    digest = child_launch_fingerprint(baseline)  # type: ignore[arg-type]
    for changed in (
        launch_request(generation=8),
        {**baseline, "profile_key": "workflow.personal_v1"},
        {**baseline, "driver_kind": "react"},
        launch_request(objective="other work"),
    ):
        assert child_launch_fingerprint(changed) != digest  # type: ignore[arg-type]


def test_duplicate_ticket_is_idempotent_but_conflicting_identity_fails(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        create_root(uow)
        first = issue_ticket(uow)
        assert issue_ticket(uow) == first
        conflicting = ProfileLaunchTicket(
            ticket_id="ticket-1",
            parent_run_id="root-1",
            profile_key="workflow.durable_task",
            catalog_generation=7,
            fingerprint="0" * 64,
        )
        with pytest.raises(UnitOfWorkConflict, match="identity conflict"):
            uow.issue_profile_launch_ticket(conflicting, now=3.0)


@pytest.mark.parametrize("state", ("expired", "cancelled"))
def test_stale_or_cancelled_ticket_never_creates_child(tmp_path: Path, state: str) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        create_root(uow)
        issue_ticket(uow)
        database.connection.execute(
            "UPDATE profile_launch_tickets SET state = ? WHERE ticket_id = 'ticket-1'",
            (state,),
        )
        with pytest.raises(UnitOfWorkConflict, match="not claimable"):
            claim_child(uow)
        assert uow.read_run("child-1") is None


def test_stale_generation_and_reused_ticket_fail_closed(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        create_root(uow)
        issue_ticket(uow)
        with pytest.raises(UnitOfWorkConflict, match="generation is stale"):
            uow.claim_profile_launch_and_commit_child(
                ticket_id="ticket-1",
                expected_catalog_generation=8,
                launch_request=launch_request(),  # type: ignore[arg-type]
                command_id="command-1",
                child_run_id="child-1",
                request_id="request-child-1",
                attachment_policy=AttachmentPolicy.ATTACHED,
                start_snapshot={"launch": {}},
                event_id="event-child-1",
                now=3.0,
            )
        first = claim_child(uow)
        assert claim_child(uow) == first
        with pytest.raises(UnitOfWorkConflict, match="already consumed"):
            claim_child(uow, child_run_id="child-2", command_id="command-2")
        assert uow.read_run("child-2") is None


def test_no_ticketless_child_command_entrypoint() -> None:
    assert not hasattr(SqliteExecutionUnitOfWork, "commit_child_command")
    assert not hasattr(SqliteExecutionUnitOfWork, "commit_child_command_and_precreate_child")
