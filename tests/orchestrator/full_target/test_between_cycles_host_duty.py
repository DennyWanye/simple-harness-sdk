# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 2A.1g (Host real run 2026-09-27): the host's duties run while ``run()`` waits.

``run()`` stays in its waiting branch while any turn is in flight. The Host issued
planning grants only after ``run()`` returned, so one Mission's long turn held a new
Mission's grant for eleven minutes with nothing written. The host duty now runs
between cycles, at most once per interval, and its failure does not end the run.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace

from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _loop(inflight_rounds: int, duty, every: float = 0.0):
    state = {"inflight": inflight_rounds}

    async def nothing(*args, **kwargs):
        return []

    def has_inflight():
        state["inflight"] -= 1
        return state["inflight"] >= 0

    async def cycle():
        return False

    async def no_stall(*args, **kwargs):
        return False

    fake = SimpleNamespace(
        recover=nothing,
        actions=SimpleNamespace(reconcile=nothing),
        _stall_carry_ons={},
        _cycle=cycle,
        _has_inflight=has_inflight,
        _poll=0,
        _record_hierarchical_stall=nothing,
        _confirm_and_stop_stalled=no_stall,
        _durable_watermark=lambda: (1,),
        _note=lambda text: None,
    )
    Orchestrator.set_between_cycles(fake, duty, every_seconds=every)
    fake._run_between_cycles = functools.partial(Orchestrator._run_between_cycles, fake)
    return fake


def test_the_host_duty_runs_while_a_turn_is_in_flight():
    calls = []

    async def duty():
        calls.append("duty")

    fake = _loop(inflight_rounds=5, duty=duty)
    asyncio.run(Orchestrator.run(fake))
    # once per loop iteration while waiting (interval 0), not only after returning
    assert len(calls) >= 5


def test_the_interval_bounds_the_duty():
    calls = []
    fake = _loop(inflight_rounds=20, duty=lambda: calls.append("duty"), every=3600)
    asyncio.run(Orchestrator.run(fake))
    assert calls == ["duty"]


def test_a_failing_duty_does_not_end_the_run():
    def duty():
        raise RuntimeError("host duty failed")

    fake = _loop(inflight_rounds=3, duty=duty)
    asyncio.run(Orchestrator.run(fake))  # returns normally once idle


def test_a_cycle_that_claims_progress_with_no_durable_change_sleeps(monkeypatch):
    """NEXT-TG-1.0 §3.6 / 2A.1g: claimed progress that wrote nothing durable must not
    skip the poll sleep (busy loop), yet still counts toward ``max_cycles``."""

    from agent_orchestrator.orchestrator import event_handler

    claims = {"left": 5}
    slept = []
    real_sleep = asyncio.sleep

    async def cycle():
        claims["left"] -= 1
        return claims["left"] >= 0

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(event_handler.asyncio, "sleep", sleep)
    fake = _loop(inflight_rounds=0, duty=None)
    fake._cycle = cycle
    fake._poll = 0.25
    asyncio.run(Orchestrator.run(fake))
    assert len([s for s in slept if s >= 0.25]) >= 5  # every hollow cycle waited

    claims["left"] = 5
    slept.clear()
    marks = iter(range(100))
    fake._durable_watermark = lambda: (next(marks),)
    asyncio.run(Orchestrator.run(fake))
    assert len([s for s in slept if s >= 0.25]) <= 2  # real progress keeps going without it


def test_a_wait_that_writes_nothing_backs_off_to_a_bound(monkeypatch):
    """Real run 2026-09-28: polling every 50 ms through a whole model turn held a core
    at 100% with nothing written. The wait doubles up to WAIT_BACKOFF_MAX."""

    from agent_orchestrator.orchestrator import event_handler

    slept = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(event_handler.asyncio, "sleep", sleep)
    fake = _loop(inflight_rounds=12, duty=None)
    fake._poll = 0.05
    asyncio.run(Orchestrator.run(fake))
    waits = [s for s in slept if s]
    assert waits[0] == 0.05 and waits[1] == 0.1
    assert max(waits) == event_handler.WAIT_BACKOFF_MAX
