"""Offline limits for the opt-in new-Mission real WAIT runner."""

from __future__ import annotations

import asyncio
import importlib.util
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "acceptance"
    / "run_real_deepseek_wait_lifecycle.py"
)
_SPEC = importlib.util.spec_from_file_location("run_real_deepseek_wait_lifecycle", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)


def test_new_wait_runner_cap_never_makes_a_second_physical_delegate_call() -> None:
    class Delegate:
        calls = 0

        async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
            self.calls += 1
            return object()

    async def case() -> None:
        gate = asyncio.Event()
        delegate = Delegate()
        provider = _RUNNER.WaitGatedRealProvider(delegate, gate, max_calls=1)
        request = SimpleNamespace(messages=())
        await provider.invoke(request, cancel=None)
        parked = asyncio.create_task(provider.invoke(request, cancel=None))
        await asyncio.wait_for(provider.cap_reached.wait(), timeout=1)
        assert delegate.calls == provider.physical_calls == 1
        parked.cancel()
        with suppress(asyncio.CancelledError):
            await parked

    asyncio.run(case())


def test_new_wait_runner_cap_is_checked_after_a_queued_call_acquires_the_serial_slot() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return object()

    async def case() -> None:
        delegate = Delegate()
        provider = _RUNNER.WaitGatedRealProvider(delegate, asyncio.Event(), max_calls=1)
        request = SimpleNamespace(messages=())
        first = asyncio.create_task(provider.invoke(request, cancel=None))
        await asyncio.wait_for(delegate.entered.wait(), timeout=1)
        second = asyncio.create_task(provider.invoke(request, cancel=None))
        await asyncio.sleep(0)
        delegate.release.set()
        await first
        await asyncio.wait_for(provider.cap_reached.wait(), timeout=1)
        assert delegate.calls == provider.physical_calls == 1
        second.cancel()
        with suppress(asyncio.CancelledError):
            await second

    asyncio.run(case())


def test_complete_success_uses_the_real_budget_account_reservation_keys() -> None:
    event = SimpleNamespace(
        type="PlanningWaitWoken", payload={"settled_tasks": {"task-1": "COMPLETED"}}
    )
    usage = {
        "reserved_tokens": 0,
        "reserved_cost_micros": 0,
        "reserved_attempts": 0,
        "reserved_tool_calls": 0,
    }
    assert _RUNNER._complete_success(SimpleNamespace(status="COMPLETED"), [event], usage)
    assert not _RUNNER._complete_success(
        SimpleNamespace(status="COMPLETED"), [event], {**usage, "reserved_tokens": 1}
    )
