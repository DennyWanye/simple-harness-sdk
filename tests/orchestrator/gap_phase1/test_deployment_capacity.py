"""Capacity survives real BaseAgent request restoration and meter nesting."""

import asyncio
import importlib
import os

import pytest
from test_meter_terminal_admission import ScriptedPhysicalProvider, make_meter, request

from simple_harness.execution.deployment_capacity import CapacityProvider, DeploymentCapacity
from simple_harness.execution.shared_capacity import CapacityLedger
from simple_harness.providers import CancelToken, ProviderUsage


def test_real_orchestrator_and_restored_wire_share_single_capacity_grants(tmp_path, monkeypatch):
    contract = importlib.import_module("test_result_output_contract")
    ledger = CapacityLedger(tmp_path / "capacity.db", pool_id="local", max_slots=2, max_tokens=100)
    cap = DeploymentCapacity(ledger, estimate=lambda request: 60)
    original = contract.RoleScriptedProvider
    monkeypatch.setattr(
        contract, "RoleScriptedProvider", lambda *a, **kw: CapacityProvider(original(*a, **kw), cap)
    )
    contract.test_actual_provider_gets_valid_example_but_missing_file_still_fails(
        tmp_path / "sdk", True, True, True
    )
    snapshot = ledger.snapshot()
    assert len(snapshot.rows) == 3
    assert all(row.state == "SETTLED" for row in snapshot.rows)
    assert all(row.key.startswith("sdk|") for row in snapshot.rows)
    assert snapshot.held_slots == 0


@pytest.mark.asyncio
async def test_direct_meter_cancel_while_shared_queue_is_known_zero(tmp_path):
    ledger = CapacityLedger(tmp_path / "capacity.db", pool_id="local", max_slots=1, max_tokens=100)
    cap = DeploymentCapacity(ledger, estimate=lambda request: 60)
    held = ledger.enqueue("holder", weight=100, owner="test", pid=os.getpid())
    assert ledger.try_acquire(held)
    physical = ScriptedPhysicalProvider(ProviderUsage(1, 1, 2))
    meter = make_meter(CapacityProvider(physical, cap))
    task = asyncio.create_task(meter.invoke(request("queued"), cancel=CancelToken()))
    async with asyncio.timeout(2):
        while not cap.waiting("queued"):
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert physical.calls == meter.counters.calls == meter.unknown_usage_calls == 0
    assert meter.admission_denials[0]["physical_calls"] == 0
    ledger.cancel_waiter(held)
    assert ledger.snapshot().held_slots == 0


@pytest.mark.asyncio
async def test_direct_meter_and_capacity_provider_do_not_double_reserve(tmp_path):
    ledger = CapacityLedger(tmp_path / "capacity.db", pool_id="local", max_slots=1, max_tokens=100)
    cap = DeploymentCapacity(ledger, estimate=lambda request: 60)
    physical = ScriptedPhysicalProvider(ProviderUsage(1, 1, 2))
    meter = make_meter(CapacityProvider(physical, cap))
    await meter.invoke(request("one"), cancel=CancelToken())
    assert physical.calls == meter.counters.calls == 1
    assert len(ledger.snapshot().rows) == 1
    assert ledger.snapshot().held_slots == 0
