# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness import AgentIdentity, CommittedTurn, MemoryScopeRef
from simple_harness.contracts import RunId
from simple_harness.execution.delivery import DeliverySpec
from simple_harness.execution.memory_outbox import CommittedTurnSpec
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import ContinuationState, RunState, UnitOfWorkConflict


class InjectedFault(RuntimeError):
    pass


def raise_at(target: str):
    def fault(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return fault


def setup(path: Path, *, ttl: float = 100.0):
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
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
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=ttl,
    )
    return database, uow, lease


def enqueue(uow, identifier: str):
    return uow.enqueue_continuation(
        continuation_id=identifier,
        run_id="run-1",
        payload={"id": identifier},
        now=3.0,
    )


def test_active_claim_is_hol_and_expired_runtime_epoch_reclaims(tmp_path) -> None:
    database, uow, old_lease = setup(tmp_path / "hol.db", ttl=2.0)
    enqueue(uow, "c1")
    enqueue(uow, "c2")
    first = uow.claim_continuation(run_id="run-1", execution_lease=old_lease, now=3.0)
    assert first is not None and first.continuation_id == "c1"
    assert first.claim_epoch == 1 and first.runtime_lease_epoch == old_lease.epoch
    assert uow.claim_continuation(run_id="run-1", execution_lease=old_lease, now=3.5) is None
    _, new_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=5.0,
        lease_ttl_seconds=100.0,
    )
    reclaimed = uow.claim_continuation(run_id="run-1", execution_lease=new_lease, now=5.0)
    assert reclaimed is not None and reclaimed.continuation_id == "c1"
    assert reclaimed.claim_epoch == 2
    assert reclaimed.runtime_lease_epoch == new_lease.epoch
    database.close()


@pytest.mark.parametrize(
    "point",
    (
        "continuation_progress.receipt.before_write",
        "continuation_progress.receipt.after_write",
        "continuation_progress.continuation.before_write",
        "continuation_progress.continuation.after_write",
        "continuation_progress.run.before_write",
        "continuation_progress.run.after_write",
        "continuation_progress.event.before_write",
        "continuation_progress.event.after_write",
    ),
)
def test_waiting_progress_and_ack_fault_reopens_all_before(tmp_path, point) -> None:
    path = tmp_path / f"{point}.db"
    database, uow, lease = setup(path)
    enqueue(uow, "c1")
    claim = uow.claim_continuation(run_id="run-1", execution_lease=lease, now=3.0)
    assert claim is not None
    run = uow.read_run("run-1")
    assert run is not None
    with pytest.raises(InjectedFault, match=point):
        uow.commit_runtime_state_and_ack_continuation(
            run_id="run-1",
            expected_version=run.version,
            state=RunState.WAITING,
            event_id="run-1:waiting:c1",
            payload={"handled": "c1"},
            continuation_claim=claim,
            execution_lease=lease,
            receipt_id="progress:c1",
            now=4.0,
            fault=raise_at(point),
        )
    database.close()
    with Database.open(path) as reopened:
        reopened_uow = SqliteExecutionUnitOfWork(reopened)
        stored = reopened_uow.read_continuation("c1")
        assert stored is not None and stored.state is ContinuationState.CLAIMED
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM continuation_progress_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE event_id='run-1:waiting:c1'"
            ).fetchone()[0]
            == 0
        )


def test_progress_after_commit_is_receipt_first_replay_after_lease_release(
    tmp_path,
) -> None:
    path = tmp_path / "progress-replay.db"
    database, uow, lease = setup(path)
    enqueue(uow, "c1")
    claim = uow.claim_continuation(run_id="run-1", execution_lease=lease, now=3.0)
    run = uow.read_run("run-1")
    assert claim is not None and run is not None
    kwargs = {
        "run_id": "run-1",
        "expected_version": run.version,
        "state": RunState.WAITING,
        "event_id": "run-1:waiting:c1",
        "payload": {"handled": "c1"},
        "continuation_claim": claim,
        "execution_lease": lease,
        "receipt_id": "progress:c1",
        "now": 4.0,
    }
    with pytest.raises(InjectedFault, match="after_commit"):
        uow.commit_runtime_state_and_ack_continuation(
            **kwargs, fault=raise_at("continuation_progress.after_commit")
        )
    uow.release_runtime_lease(lease, now=5.0)
    database.close()
    with Database.open(path) as reopened:
        reopened_uow = SqliteExecutionUnitOfWork(reopened)
        replay = reopened_uow.commit_runtime_state_and_ack_continuation(**kwargs)
        assert replay.continuation.state is ContinuationState.ACKED
        with pytest.raises(UnitOfWorkConflict, match="receipt differs"):
            reopened_uow.commit_runtime_state_and_ack_continuation(
                **{**kwargs, "payload": {"handled": "different"}}
            )


TERMINAL_POINTS = tuple(
    f"continuation_terminal.{write}.{side}_write"
    for write in (
        "receipt",
        "continuation",
        "event",
        "delivery.0",
        "committed_turn",
        "fence",
        "run",
    )
    for side in ("before", "after")
)


def _terminal_setup(path: Path):
    database, uow, lease = setup(path)
    fence = asyncio.run(uow.acquire(RunId("run-1"), lease, now=2.0))
    enqueue(uow, "c1")
    enqueue(uow, "c2")
    claim = uow.claim_continuation(run_id="run-1", execution_lease=lease, now=3.0)
    run = uow.read_run("run-1")
    assert claim is not None and run is not None
    return database, uow, lease, fence, claim, run


def _terminal(
    uow,
    lease,
    fence,
    claim,
    run,
    *,
    payload=None,
    committed_turn: CommittedTurnSpec | None = None,
    fault=None,
):
    return uow.commit_root_terminal_with_deliveries_and_ack_continuation(
        run_id="run-1",
        expected_version=run.version,
        terminal_state=RunState.COMPLETED,
        event_id="run-1:terminal:c1",
        terminal_payload={"answer": 42} if payload is None else payload,
        deliveries=(DeliverySpec("delivery-1", "memory", "delivery-key-1", {"answer": 42}),),
        continuation_claim=claim,
        run_fence=fence,
        execution_lease=lease,
        receipt_id="terminal:c1",
        terminal_fence_receipt_ref="fence:run-1:1",
        now=4.0,
        committed_turn=committed_turn,
        fault=fault,
    )


def _committed_turn(text: str) -> CommittedTurnSpec:
    return CommittedTurnSpec.from_domain(
        CommittedTurn(
            "turn-c1",
            AgentIdentity("deployment-1", "household-1", "actor-1", "session-1"),
            "question",
            text,
            MemoryScopeRef.personal("actor-1"),
            "epoch-1",
            1.0,
        )
    )


@pytest.mark.parametrize("point", TERMINAL_POINTS)
def test_terminal_and_ack_fault_reopens_all_before(tmp_path, point) -> None:
    path = tmp_path / f"terminal-{point}.db"
    database, uow, lease, fence, claim, run = _terminal_setup(path)
    with pytest.raises(InjectedFault, match=point):
        _terminal(
            uow,
            lease,
            fence,
            claim,
            run,
            committed_turn=_committed_turn("answer"),
            fault=raise_at(point),
        )
    database.close()
    with Database.open(path) as reopened:
        stored = SqliteExecutionUnitOfWork(reopened).read_continuation("c1")
        assert stored is not None and stored.state is ContinuationState.CLAIMED
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM continuation_progress_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            reopened.connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
        )
        assert (
            reopened.connection.execute(
                "SELECT state FROM run_fences WHERE run_id='run-1'"
            ).fetchone()[0]
            == "active"
        )


def test_terminal_receipt_replays_after_reopen_and_quarantines_tail(tmp_path) -> None:
    path = tmp_path / "terminal-replay.db"
    database, uow, lease, fence, claim, run = _terminal_setup(path)
    with pytest.raises(InjectedFault, match="after_commit"):
        _terminal(
            uow,
            lease,
            fence,
            claim,
            run,
            fault=raise_at("continuation_terminal.after_commit"),
        )
    database.close()
    with Database.open(path) as reopened:
        reopened_uow = SqliteExecutionUnitOfWork(reopened)
        replay = _terminal(reopened_uow, lease, fence, claim, run)
        assert replay.terminal.run.state is RunState.COMPLETED
        tail = reopened_uow.read_continuation("c2")
        assert tail is not None and tail.state is ContinuationState.QUARANTINED
        with pytest.raises(UnitOfWorkConflict, match="receipt differs"):
            _terminal(
                reopened_uow,
                lease,
                fence,
                claim,
                run,
                payload={"answer": "changed"},
            )
        with pytest.raises(UnitOfWorkConflict, match="rejects new continuations"):
            enqueue(reopened_uow, "c3")


def test_terminal_and_ack_replay_requires_same_committed_turn(tmp_path) -> None:
    database, uow, lease, fence, claim, run = _terminal_setup(
        tmp_path / "terminal-memory-replay.db"
    )
    intent = _committed_turn("answer")
    first = _terminal(uow, lease, fence, claim, run, committed_turn=intent)
    assert _terminal(uow, lease, fence, claim, run, committed_turn=intent) == first
    with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
        _terminal(uow, lease, fence, claim, run, committed_turn=None)
    with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
        _terminal(
            uow,
            lease,
            fence,
            claim,
            run,
            committed_turn=_committed_turn("different"),
        )
    database.close()


def test_terminal_and_ack_without_committed_turn_replays_only_without_turn(
    tmp_path,
) -> None:
    database, uow, lease, fence, claim, run = _terminal_setup(
        tmp_path / "terminal-no-memory-replay.db"
    )
    first = _terminal(uow, lease, fence, claim, run)
    assert _terminal(uow, lease, fence, claim, run) == first
    with pytest.raises(UnitOfWorkConflict, match="committed-turn replay differs"):
        _terminal(
            uow,
            lease,
            fence,
            claim,
            run,
            committed_turn=_committed_turn("answer"),
        )
    database.close()


def test_new_runtime_lease_cannot_pair_with_old_claim_or_fence(tmp_path) -> None:
    database, uow, old_lease, old_fence, old_claim, run = _terminal_setup(
        tmp_path / "mixed-authority.db"
    )
    _, new_lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-2",
        namespace="runtime.kernel",
        now=102.0,
        lease_ttl_seconds=100.0,
    )
    new_fence = asyncio.run(uow.acquire(RunId("run-1"), new_lease, now=102.0))
    before = tuple(
        database.connection.execute(
            "SELECT (SELECT COUNT(*) FROM continuation_progress_receipts),"
            "(SELECT COUNT(*) FROM delivery_outbox),"
            "(SELECT COUNT(*) FROM run_events WHERE kind='run.completed')"
        ).fetchone()
    )
    with pytest.raises(UnitOfWorkConflict):
        _terminal(uow, new_lease, old_fence, old_claim, run)
    with pytest.raises(UnitOfWorkConflict):
        _terminal(uow, old_lease, new_fence, old_claim, run)
    after = tuple(
        database.connection.execute(
            "SELECT (SELECT COUNT(*) FROM continuation_progress_receipts),"
            "(SELECT COUNT(*) FROM delivery_outbox),"
            "(SELECT COUNT(*) FROM run_events WHERE kind='run.completed')"
        ).fetchone()
    )
    assert after == before
    database.close()
