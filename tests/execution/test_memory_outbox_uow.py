# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness import AgentIdentity, CommittedTurn, MemoryScopeRef, RunId
from simple_harness.execution.memory_outbox import CommittedTurnSpec
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


class InjectedFault(RuntimeError):
    pass


IDENTITY = AgentIdentity("deployment-1", "household-1", "actor-1", "session-1")


def _spec(answer: str = "answer") -> CommittedTurnSpec:
    return CommittedTurnSpec.from_domain(
        CommittedTurn(
            "turn-1",
            IDENTITY,
            "hello",
            answer,
            MemoryScopeRef.personal("actor-1"),
            "epoch-1",
            1.0,
        )
    )


def _setup(path: Path):  # type: ignore[no-untyped-def]
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"schema_version": 5, "input": {}},
        event_id="run-1:created",
        user_id="actor-1",
        now=1.0,
    )
    database.connection.execute(
        "INSERT INTO agent_identity_bindings VALUES(?,?,?,?,?,?)",
        ("session-1", "deployment-1", "household-1", "actor-1", "a" * 64, 1.0),
    )
    _, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="runtime-1",
        namespace="runtime.kernel",
        now=1.1,
        lease_ttl_seconds=100.0,
    )
    fence = asyncio.run(uow.acquire(RunId("run-1"), lease, now=1.1))
    return database, uow, lease, fence


def _terminal(uow, lease, fence, spec, *, state=RunState.COMPLETED, fault=None):  # type: ignore[no-untyped-def]
    run = uow.read_run("run-1")
    assert run is not None
    return uow.commit_root_terminal_with_deliveries(
        run_id="run-1",
        expected_version=run.version,
        terminal_state=state,
        event_id=f"run-1:terminal:{state.value}",
        terminal_payload={"answer": 42},
        deliveries=(),
        fence=fence,
        execution_lease=lease,
        terminal_fence_receipt_ref="receipt-1",
        now=2.0,
        committed_turn=spec,
        fault=fault,
    )


@pytest.mark.parametrize(
    "fault_point",
    (
        "root_terminal.committed_turn.before_write",
        "root_terminal.committed_turn.after_write",
        "root_terminal.run.before_write",
    ),
)
def test_terminal_and_committed_turn_rollback_together(
    tmp_path: Path, fault_point: str
) -> None:
    database, uow, lease, fence = _setup(tmp_path / f"{fault_point}.db")

    def fault(point: str) -> None:
        if point == fault_point:
            raise InjectedFault(point)

    with pytest.raises(InjectedFault, match=fault_point):
        _terminal(uow, lease, fence, _spec(), fault=fault)
    assert uow.read_run("run-1").state is RunState.RUNNING  # type: ignore[union-attr]
    assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
    database.close()


def test_terminal_replay_compares_complete_turn_payload(tmp_path: Path) -> None:
    database, uow, lease, fence = _setup(tmp_path / "replay.db")
    first = _terminal(uow, lease, fence, _spec())
    assert _terminal(uow, lease, fence, _spec()) == first
    with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
        _terminal(uow, lease, fence, None)
    with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
        _terminal(uow, lease, fence, _spec("different"))
    database.close()


@pytest.mark.parametrize("state", (RunState.FAILED, RunState.CANCELLED))
def test_noncompleted_terminal_rejects_committed_turn_and_keeps_outbox_empty(
    tmp_path: Path, state: RunState
) -> None:
    database, uow, lease, fence = _setup(tmp_path / f"{state.value}.db")
    with pytest.raises(UnitOfWorkConflict, match="only COMPLETED"):
        _terminal(uow, lease, fence, _spec(), state=state)
    assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
    database.close()
