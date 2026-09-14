# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Weighted in-flight admission uses only local fake providers."""

from __future__ import annotations

import asyncio

import pytest

from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
)
from agent_orchestrator.evaluation.metered_provider import (
    ExperimentBudgetExhausted,
    MeteredProvider,
    RunWindowDenied,
    UnknownProviderUsage,
)
from simple_harness import Message, MessageRole
from simple_harness.contracts import RequestId
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)

TARGET = ProviderTarget("fake", "capacity", "pricing", "https://example.test", "v1")


def request(name, estimate):
    return ProviderRequest(
        RequestId(name), (Message(MessageRole.USER, name),), max_output_tokens=estimate
    )


class Provider:
    target = TARGET

    def __init__(self):
        self.entered: asyncio.Queue[str] = asyncio.Queue()
        self.release: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def invoke(self, req, *, cancel):
        name = req.request_id.value
        self.calls.append(name)
        await self.entered.put(name)
        await self.release.setdefault(name, asyncio.Event()).wait()
        if name in self.fail:
            raise RuntimeError("unknown physical result")
        return ProviderResponse(
            req.request_id,
            Message(MessageRole.ASSISTANT, "ok"),
            model=TARGET.model,
            usage=ProviderUsage(1, 1, 2),
        )


def meter(provider, *, cap=100, callback=None, slots=2, seconds=10):
    manifest = ExperimentManifest(
        "capacity-test",
        "fake",
        "capacity",
        ExperimentBudget(1000, 1000, 2000, 20, seconds),
        ("one",),
        1,
        100,
        slots,
        tuple(ArmSpec(arm, arm) for arm in ARMS),
    )
    reports = []
    context = RunContext(manifest, manifest.runs()[0], reports.append)
    wrapped = MeteredProvider(
        provider,
        context,
        estimate_input_tokens=lambda req: int(req.max_output_tokens),
        before_handoff=callback,
        max_inflight_tokens=cap,
    )
    return wrapped, reports


async def enter(provider, name):
    assert await asyncio.wait_for(provider.entered.get(), 1) == name


@pytest.mark.asyncio
async def test_two_small_parallel_large_fifo_wait_and_cancelled_queued_zero_calls():
    provider = Provider()
    wrapped, reports = meter(provider)
    first = asyncio.create_task(wrapped.invoke(request("first", 20), cancel=CancelToken()))
    second = asyncio.create_task(wrapped.invoke(request("second", 20), cancel=CancelToken()))
    await enter(provider, "first")
    await enter(provider, "second")
    assert wrapped.counters.peak_physical_slots == 2
    large = asyncio.create_task(wrapped.invoke(request("large", 45), cancel=CancelToken()))
    later = asyncio.create_task(wrapped.invoke(request("later", 10), cancel=CancelToken()))
    cancelled = asyncio.create_task(wrapped.invoke(request("cancelled", 10), cancel=CancelToken()))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert provider.calls == ["first", "second"]
    provider.release["first"].set()
    await first
    await asyncio.sleep(0)
    assert provider.calls == ["first", "second"]  # Large head still needs second release.
    provider.release["second"].set()
    await second
    await enter(provider, "large")
    assert provider.calls == ["first", "second", "large"]
    provider.release["large"].set()
    await large
    await enter(provider, "later")
    provider.release["later"].set()
    await later
    assert wrapped.counters.calls == len(provider.calls) == 4
    assert reports[-1].calls == 4
    assert wrapped._capacity_reserved == wrapped._capacity_active == 0
    assert not wrapped._capacity_waiters


@pytest.mark.asyncio
async def test_oversize_known_zero_and_unknown_unblocks_waiters_without_handoff():
    provider = Provider()
    provider.fail.add("first")
    wrapped, reports = meter(provider, cap=100, slots=1)
    with pytest.raises(ExperimentBudgetExhausted, match="capacity"):
        await wrapped.invoke(request("oversize", 51), cancel=CancelToken())
    assert wrapped.counters.calls == 0 and not provider.calls
    first = asyncio.create_task(wrapped.invoke(request("first", 45), cancel=CancelToken()))
    await enter(provider, "first")
    waiting = asyncio.create_task(wrapped.invoke(request("waiting", 45), cancel=CancelToken()))
    await asyncio.sleep(0)
    provider.release["first"].set()
    with pytest.raises(UnknownProviderUsage):
        await first
    with pytest.raises(UnknownProviderUsage):
        await waiting
    assert provider.calls == ["first"]
    assert wrapped.counters.calls == reports[-1].calls == 1
    assert wrapped.unknown_usage_calls == 1
    assert wrapped._capacity_reserved == wrapped._capacity_active == 0


@pytest.mark.asyncio
async def test_window_callback_runs_after_weighted_wait_and_denies_known_zero():
    provider = Provider()
    admitted = True
    checks = []

    def before_handoff(queue_seconds):
        checks.append(queue_seconds)
        if not admitted:
            raise RunWindowDenied("window closed while queued")

    wrapped, reports = meter(provider, cap=100, slots=1, callback=before_handoff)
    first = asyncio.create_task(wrapped.invoke(request("first", 40), cancel=CancelToken()))
    await enter(provider, "first")
    second = asyncio.create_task(wrapped.invoke(request("second", 40), cancel=CancelToken()))
    await asyncio.sleep(0)
    admitted = False
    provider.release["first"].set()
    await first
    with pytest.raises(RunWindowDenied):
        await second
    assert provider.calls == ["first"]
    assert len(checks) == 2 and checks[1] >= checks[0]
    assert wrapped.counters.calls == reports[-1].calls == 1
    assert wrapped.unknown_usage_calls == 0
    assert wrapped._capacity_reserved == wrapped._capacity_active == 0


@pytest.mark.asyncio
async def test_queued_deadline_does_not_leak_capacity_or_count_a_call():
    provider = Provider()
    wrapped, _ = meter(provider, slots=1, seconds=0.05)
    first = asyncio.create_task(wrapped.invoke(request("first", 40), cancel=CancelToken()))
    await enter(provider, "first")
    waiting = asyncio.create_task(wrapped.invoke(request("waiting", 40), cancel=CancelToken()))
    with pytest.raises(TimeoutError):
        await waiting
    assert provider.calls == ["first"]
    assert wrapped.counters.calls == 1
    with pytest.raises(TimeoutError):
        await first
    assert wrapped.unknown_usage_calls == 1
    assert wrapped._capacity_active == wrapped._capacity_reserved == 0
    assert not wrapped._capacity_waiters


def test_capacity_validation():
    provider = Provider()
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            meter(provider, cap=bad)


@pytest.mark.asyncio
async def test_cancel_token_wakes_weighted_waiter_before_active_call_finishes():
    provider = Provider()
    wrapped, _ = meter(provider)
    first = asyncio.create_task(wrapped.invoke(request("first", 45), cancel=CancelToken()))
    await enter(provider, "first")
    token = CancelToken()
    waiting = asyncio.create_task(wrapped.invoke(request("waiting", 45), cancel=token))
    await asyncio.sleep(0)
    token.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 0.25)
        assert provider.calls == ["first"]
        assert wrapped.counters.calls == 1
        assert wrapped.unknown_usage_calls == 0
    finally:
        provider.release["first"].set()
        await first
    assert wrapped._capacity_reserved == wrapped._capacity_active == 0
    assert not wrapped._capacity_waiters
