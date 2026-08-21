# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_atomic_child_launch import InjectedFault, raise_at
from test_ticket_generation import create_root, issue_ticket, launch_request

from simple_harness.contracts import RunId
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState, UnitOfWorkConflict

TERMINAL_WRITE_POINTS = tuple(
    f"child_terminal.{write}.{side}_write"
    for write in ("run", "command", "signal", "event", "receipt", "fence")
    for side in ("before", "after")
)
TERMINAL_STATES = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)
POLICIES = (
    AttachmentPolicy.ATTACHED,
    AttachmentPolicy.ROOT_TERMINAL_CHILD,
    AttachmentPolicy.DETACHED,
)


def _setup(path: Path, policy: AttachmentPolicy, *, ttl: float = 100.0):
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    create_root(uow)
    request = launch_request()
    issue_ticket(uow, request=request)
    uow.claim_profile_launch_and_commit_child(
        ticket_id="ticket-1",
        expected_catalog_generation=7,
        launch_request=request,  # type: ignore[arg-type]
        command_id="command-1",
        child_run_id="child-1",
        request_id="request-child-1",
        attachment_policy=policy,
        start_snapshot={"launch": request},  # type: ignore[dict-item]
        event_id="event-child-created",
        now=3.0,
    )
    _, lease = uow.claim_runtime_activation(
        run_id="child-1",
        owner_id="child-owner",
        namespace="runtime.kernel",
        now=4.0,
        lease_ttl_seconds=ttl,
    )
    fence = asyncio.run(uow.acquire(RunId("child-1"), lease, now=4.0))
    return database, uow, lease, fence


def _terminalize(uow, lease, fence, policy, state, *, payload=None, fault=None):
    terminal_payload = {"state": state.value, "answer": 42}
    if payload is not None:
        terminal_payload = payload
    common = {
        "command_id": "command-1",
        "expected_child_version": 1,
        "terminal_state": state,
        "event_id": f"child-1:1:{state.value}:event",
        "receipt_id": f"child-1:1:{state.value}:receipt",
        "run_fence": fence,
        "execution_lease": lease,
        "now": 5.0,
        "fault": fault,
    }
    if policy is AttachmentPolicy.DETACHED:
        return uow.commit_detached_child_terminal(
            terminal_payload=terminal_payload,
            **common,
        )
    return uow.finalize_child_and_enqueue_parent_signal(
        signal_id=f"child-1:1:{state.value}:signal",
        signal_payload=terminal_payload,
        **common,
    )


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_policy_and_terminal_state_are_durable_and_receipt_first(
    tmp_path: Path, policy: AttachmentPolicy, state: RunState
) -> None:
    path = tmp_path / f"{policy.value}-{state.value}.db"
    database, uow, lease, fence = _setup(path, policy)
    result = _terminalize(uow, lease, fence, policy, state)
    assert result.terminal_state == state.value
    assert (result.signal is None) is (policy is AttachmentPolicy.DETACHED)
    assert uow.read_run("child-1").state is state  # type: ignore[union-attr]
    assert (
        database.connection.execute(
            "SELECT state FROM run_fences WHERE run_id='child-1'"
        ).fetchone()[0]
        == "released"
    )
    database.close()

    with Database.open(path) as reopened:
        reopened_uow = SqliteExecutionUnitOfWork(reopened)
        replay = _terminalize(reopened_uow, lease, fence, policy, state)
        assert replay.receipt == result.receipt
        with pytest.raises(UnitOfWorkConflict, match="receipt differs"):
            _terminalize(
                reopened_uow,
                lease,
                fence,
                policy,
                state,
                payload={"state": state.value, "answer": "changed"},
            )
        signal_count = reopened.connection.execute(
            "SELECT COUNT(*) FROM child_signals WHERE child_run_id='child-1'"
        ).fetchone()[0]
        assert signal_count == (0 if policy is AttachmentPolicy.DETACHED else 1)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("state", TERMINAL_STATES)
@pytest.mark.parametrize("point", TERMINAL_WRITE_POINTS)
def test_every_child_terminal_write_fault_rolls_back(
    tmp_path: Path,
    policy: AttachmentPolicy,
    state: RunState,
    point: str,
) -> None:
    if policy is AttachmentPolicy.DETACHED and ".signal." in point:
        database, uow, lease, fence = _setup(tmp_path / f"detached-no-{point}.db", policy)
        result = _terminalize(uow, lease, fence, policy, state)
        assert result.signal is None
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM child_signals WHERE child_run_id='child-1'"
            ).fetchone()[0]
            == 0
        )
        database.close()
        return
    path = tmp_path / f"{policy.value}-{state.value}-{point}.db"
    database, uow, lease, fence = _setup(path, policy)
    with pytest.raises(InjectedFault, match=point):
        _terminalize(uow, lease, fence, policy, state, fault=raise_at(point))
    database.close()
    with Database.open(path) as reopened:
        assert (
            reopened.connection.execute("SELECT state FROM runs WHERE run_id='child-1'").fetchone()[
                0
            ]
            == "running"
        )
        assert (
            reopened.connection.execute("SELECT COUNT(*) FROM child_terminal_receipts").fetchone()[
                0
            ]
            == 0
        )
        assert (
            reopened.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='child-1'"
            ).fetchone()[0]
            == "active"
        )


@pytest.mark.parametrize("new_owner", ("child-owner", "other-owner"))
def test_old_epoch_cannot_terminalize_after_runtime_takeover(
    tmp_path: Path, new_owner: str
) -> None:
    database, uow, old_lease, old_fence = _setup(
        tmp_path / f"takeover-{new_owner}.db", AttachmentPolicy.ATTACHED, ttl=1.0
    )
    _, new_lease = uow.claim_runtime_activation(
        run_id="child-1",
        owner_id=new_owner,
        namespace="runtime.kernel",
        now=5.0,
        lease_ttl_seconds=100.0,
    )
    new_fence = asyncio.run(uow.acquire(RunId("child-1"), new_lease, now=5.0))
    before = tuple(
        database.connection.execute(
            "SELECT (SELECT COUNT(*) FROM child_terminal_receipts),"
            "(SELECT COUNT(*) FROM child_signals),"
            "(SELECT COUNT(*) FROM run_events WHERE kind LIKE 'child.%')"
        ).fetchone()
    )
    with pytest.raises(UnitOfWorkConflict, match="stale or expired"):
        _terminalize(
            uow,
            old_lease,
            old_fence,
            AttachmentPolicy.ATTACHED,
            RunState.COMPLETED,
        )
    after = tuple(
        database.connection.execute(
            "SELECT (SELECT COUNT(*) FROM child_terminal_receipts),"
            "(SELECT COUNT(*) FROM child_signals),"
            "(SELECT COUNT(*) FROM run_events WHERE kind LIKE 'child.%')"
        ).fetchone()
    )
    assert after == before
    result = _terminalize(
        uow,
        new_lease,
        new_fence,
        AttachmentPolicy.ATTACHED,
        RunState.COMPLETED,
    )
    assert result.signal is not None
    database.close()


def test_caller_cannot_override_durable_attachment_policy(tmp_path: Path) -> None:
    database, uow, lease, fence = _setup(tmp_path / "policy.db", AttachmentPolicy.DETACHED)
    with pytest.raises(UnitOfWorkConflict, match="durable policy"):
        uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-1",
            expected_child_version=1,
            terminal_state=RunState.COMPLETED,
            signal_id="wrong-signal",
            signal_payload={"answer": 42},
            event_id="wrong-event",
            receipt_id="wrong-receipt",
            run_fence=fence,
            execution_lease=lease,
            now=5.0,
        )
    assert (
        database.connection.execute("SELECT COUNT(*) FROM child_terminal_receipts").fetchone()[0]
        == 0
    )
    database.close()
