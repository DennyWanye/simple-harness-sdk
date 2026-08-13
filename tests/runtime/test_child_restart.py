# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.runtime.child_runs import (
    ChildLaunchRequest,
    ProfileLaunchTicketRef,
)


def _root(uow: SqliteExecutionUnitOfWork) -> None:
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="root-1",
        request_id="request-root",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"catalog_generation": 3},
        event_id="event-root",
        now=1.0,
    )


def test_child_entry_requires_ticket_ref_and_reopens_same_child(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    launch_payload = {
        "profile_key": "workflow.durable_task",
        "driver_kind": "workflow",
        "catalog_generation": 3,
        "objective": "audit",
    }
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _root(uow)
    uow.issue_profile_launch_ticket(
        ProfileLaunchTicket(
            ticket_id="ticket-1",
            parent_run_id="root-1",
            profile_key="workflow.durable_task",
            catalog_generation=3,
            fingerprint=child_launch_fingerprint(launch_payload),
        ),
        now=2.0,
    )
    request = ChildLaunchRequest(
        ticket=ProfileLaunchTicketRef("ticket-1", 3),
        command_id="command-1",
        child_run_id="child-1",
        request_id="request-child-1",
        attachment_policy=AttachmentPolicy.ATTACHED,
        launch_payload=launch_payload,
        start_snapshot={"catalog_generation": 3},
    )
    launched = uow.claim_profile_launch_and_commit_child(
        ticket_id=request.ticket.ticket_id,
        expected_catalog_generation=request.ticket.catalog_generation,
        launch_request=dict(request.launch_payload),
        command_id=request.command_id,
        child_run_id=request.child_run_id,
        request_id=request.request_id,
        attachment_policy=request.attachment_policy,
        start_snapshot=dict(request.start_snapshot),
        event_id="event-child",
        now=3.0,
    )
    database.close()

    with Database.open(path) as reopened:
        recovered = SqliteExecutionUnitOfWork(reopened)
        assert recovered.read_run(launched.child_run_id) is not None
        assert recovered.read_run("root-1").state is RunState.WAITING  # type: ignore[union-attr]
        repeated = recovered.claim_profile_launch_and_commit_child(
            ticket_id=request.ticket.ticket_id,
            expected_catalog_generation=request.ticket.catalog_generation,
            launch_request=dict(request.launch_payload),
            command_id=request.command_id,
            child_run_id=request.child_run_id,
            request_id=request.request_id,
            attachment_policy=request.attachment_policy,
            start_snapshot=dict(request.start_snapshot),
            event_id="event-child",
            now=4.0,
        )
        assert repeated.child_run_id == launched.child_run_id
