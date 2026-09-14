"""Physical capacity waits precede SDK handoff and share one nested grant."""

import asyncio
import os

import pytest

from simple_harness.contracts import RunId
from simple_harness.execution.deployment_capacity import CapacityProvider, DeploymentCapacity
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.shared_capacity import CapacityLedger
from simple_harness.providers import CancelToken

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider
from .test_provider_dispatch_atomic import LEASE, _coordinator, _request


def capacity(tmp_path, weight=60):
    return DeploymentCapacity(
        CapacityLedger(tmp_path / "capacity.db", pool_id="dgx", max_slots=2, max_tokens=100),
        estimate=lambda request: weight,
        wait_seconds=1,
    )


@pytest.mark.asyncio
async def test_wait_cancel_is_zero_handoff_and_zero_physical_usage(tmp_path):
    cap = capacity(tmp_path)
    held = cap.ledger.enqueue("occupier", weight=60, owner="test", pid=os.getpid())
    assert cap.ledger.try_acquire(held)
    cap.ledger.mark_handed_off(held)
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    physical = RecordingProvider()
    provider = CapacityProvider(physical, cap)
    task = asyncio.create_task(
        _coordinator(uow, provider).invoke(
            RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
        )
    )
    async with asyncio.timeout(2):
        while not cap.waiting("request-1"):
            await asyncio.sleep(0.01)
    assert list(uow.records.values())[0].state is ProviderInvocationState.CLAIMED
    assert physical.calls == 0 and "hand_off" not in uow.operations
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cap.ledger.snapshot().held_tokens == 60
    assert all(row.state != "WAITING" for row in cap.ledger.snapshot().rows)
    cap.ledger.finish(held, known_terminal=True)


@pytest.mark.asyncio
async def test_nested_coordinator_provider_uses_one_grant_and_replay_none(tmp_path):
    cap = capacity(tmp_path)
    physical = RecordingProvider()
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    physical.uow = uow
    coordinator = _coordinator(uow, CapacityProvider(physical, cap))
    await coordinator.invoke(
        RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
    )
    assert len(cap.ledger.snapshot().rows) == 1
    assert cap.ledger.snapshot().held_slots == 0
    assert physical.calls == 1
    await coordinator.invoke(
        RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
    )
    assert physical.calls == 1 and len(cap.ledger.snapshot().rows) == 1


@pytest.mark.asyncio
async def test_after_handoff_cancel_keeps_unknown_capacity(tmp_path):
    cap = capacity(tmp_path)
    physical = RecordingProvider(release=asyncio.Event())
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    task = asyncio.create_task(
        _coordinator(uow, CapacityProvider(physical, cap)).invoke(
            RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
        )
    )
    await physical.entered.wait()
    task.cancel()
    with pytest.raises(Exception, match="unknown"):
        await task
    snapshot = cap.ledger.snapshot()
    assert snapshot.held_tokens == 60 and snapshot.held_slots == 1
    assert snapshot.rows[0].state == "UNKNOWN"


@pytest.mark.asyncio
async def test_guard_tracks_cancel_token_without_cancelling_task(tmp_path):
    cap = capacity(tmp_path)
    held = cap.ledger.enqueue("occupier", weight=100, owner="test", pid=os.getpid())
    assert cap.ledger.try_acquire(held)
    cancel = CancelToken()
    physical = RecordingProvider()
    task = asyncio.create_task(CapacityProvider(physical, cap).invoke(_request(), cancel=cancel))
    async with asyncio.timeout(2):
        while not cap.waiting("request-1"):
            await asyncio.sleep(0.01)
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert physical.calls == 0
    cap.ledger.cancel_waiter(held)
    assert cap.ledger.snapshot().held_slots == 0


@pytest.mark.asyncio
async def test_sdk_terminal_commit_recovers_capacity_after_missed_callback(tmp_path, monkeypatch):
    cap = capacity(tmp_path)
    physical = RecordingProvider()
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    real_finish = cap.ledger.finish
    monkeypatch.setattr(cap.ledger, "finish", lambda *args, **kwargs: None)
    await _coordinator(uow, CapacityProvider(physical, cap)).invoke(
        RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
    )
    assert cap.ledger.snapshot().held_slots == 1
    monkeypatch.setattr(cap.ledger, "finish", real_finish)
    reopened = capacity(tmp_path)
    other_db = FakeProviderInvocationUnitOfWork()
    other_db.records = uow.records.copy()
    other_db.capacity_namespace = str(tmp_path / "other-execution-db")
    reopened.recover(other_db)
    assert reopened.ledger.snapshot().held_slots == 1
    reopened.recover(uow)
    row = reopened.ledger.snapshot().rows[0]
    assert row.state == "SETTLED" and row.evidence_ref.startswith("sdk:")
    assert reopened.ledger.snapshot().held_slots == 0
    assert physical.calls == 1


@pytest.mark.asyncio
async def test_response_without_usage_cannot_release_unknown_capacity(tmp_path):
    cap = capacity(tmp_path)
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    physical = RecordingProvider(usage=None)
    await _coordinator(uow, CapacityProvider(physical, cap)).invoke(
        RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
    )
    assert next(iter(uow.records.values())).state is ProviderInvocationState.SUCCEEDED
    capacity(tmp_path).recover(uow)
    assert cap.ledger.snapshot().rows[0].state == "UNKNOWN"
    assert cap.ledger.snapshot().held_slots == 1


@pytest.mark.asyncio
async def test_capacity_marker_without_sdk_handoff_has_zero_call_proof(tmp_path):
    cap = capacity(tmp_path)
    uow = FakeProviderInvocationUnitOfWork()
    uow.capacity_namespace = str(tmp_path)
    physical = RecordingProvider()
    coordinator = _coordinator(uow, physical)
    record = await coordinator.prepare_claim(RunId("run-1"), _request(), execution_lease=LEASE)
    ticket = cap.ledger.enqueue(
        cap.record_key(uow, record), weight=60, owner="old", pid=os.getpid()
    )
    assert cap.ledger.try_acquire(ticket)
    cap.ledger.mark_handed_off(ticket)
    cap.ledger.finish(ticket, known_terminal=False)
    cap.recover(uow)
    assert cap.ledger.snapshot().held_slots == 0 and physical.calls == 0
    assert next(iter(uow.records.values())).handoff_attempt == 0


def test_different_nested_capacity_is_rejected_before_any_transport(tmp_path):
    cap = capacity(tmp_path)
    provider = CapacityProvider(RecordingProvider(), cap)
    assert CapacityProvider(provider, cap).deployment_capacity is cap
    with pytest.raises(ValueError, match="different deployment capacity"):
        CapacityProvider(provider, capacity(tmp_path))
    assert cap.ledger.snapshot().held_slots == 0
