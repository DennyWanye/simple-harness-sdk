# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Physical accounting with local async fakes only; the parent runs tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace

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
    ProviderIdentityMismatch,
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

TARGET = ProviderTarget("fake", "exact-model", "pricing", "https://example.test/v1", "fake-v1")


def context(*, budget=None, slots=2):
    plan = ExperimentManifest(
        "meter-test",
        "fake",
        "exact-model",
        budget or ExperimentBudget(100, 100, 110, 4, 10),
        ("one",),
        1,
        1,
        slots,
        tuple(ArmSpec(arm, arm) for arm in ARMS),
    )
    snapshots = []
    return RunContext(plan, plan.runs()[0], snapshots.append), snapshots


def request(role, *, cap=10):
    return ProviderRequest(
        RequestId(f"req-{role}"),
        (Message(MessageRole.USER, role),),
        max_output_tokens=cap,
    )


class FakeProvider:
    target = TARGET
    tokenizer_hint = "provider capability"

    def __init__(self, action=None):
        self.calls = []
        self.action = action
        self.active = self.peak = 0

    async def invoke(self, request, *, cancel):
        self.calls.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.action is not None:
                return await self.action(request, cancel)
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "ok"),
                usage=ProviderUsage(12, 3, 15),
                model=TARGET.model,
            )
        finally:
            self.active -= 1


def meter(provider, *, budget=None, slots=2, estimate=lambda _: 15):
    run, snapshots = context(budget=budget, slots=slots)
    return MeteredProvider(provider, run, estimate_input_tokens=estimate), snapshots


def test_shared_all_roles_cap_actual_counters_and_attribute_delegation():
    async def case():
        provider = FakeProvider()
        wrapped, snapshots = meter(
            provider,
            budget=ExperimentBudget(51, 40, 75, 4, 10),
        )
        assert wrapped.target is TARGET
        assert wrapped.tokenizer_hint == provider.tokenizer_hint
        for role in ARMS:
            response = await wrapped.invoke(request(role), cancel=CancelToken())
            assert response.usage.total_tokens == 15
        assert [call.messages[0].content for call in provider.calls] == list(ARMS)
        assert wrapped.counters.calls == 4
        assert (wrapped.counters.input_tokens, wrapped.counters.output_tokens) == (48, 12)
        assert [c.calls for c in snapshots] == [1, 1, 2, 2, 3, 3, 4, 4]
        with pytest.raises(ExperimentBudgetExhausted):
            await wrapped.invoke(request("extra"), cancel=CancelToken())
        assert len(provider.calls) == 4

    asyncio.run(case())


def test_physical_slots_and_in_flight_reservations_are_shared():
    async def case():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow(req, _cancel):
            entered.set()
            await release.wait()
            return ProviderResponse(
                req.request_id,
                Message(MessageRole.ASSISTANT, "ok"),
                usage=ProviderUsage(12, 3, 15),
                model=TARGET.model,
            )

        provider = FakeProvider(slow)
        wrapped, snapshots = meter(
            provider,
            slots=1,
            budget=ExperimentBudget(30, 20, 40, 3, 10),
        )
        first = asyncio.create_task(wrapped.invoke(request("S"), cancel=CancelToken()))
        await entered.wait()
        second = asyncio.create_task(wrapped.invoke(request("R"), cancel=CancelToken()))
        await asyncio.sleep(0)
        assert len(provider.calls) == 1 and wrapped.counters.peak_physical_slots == 1
        release.set()
        await asyncio.gather(first, second)
        assert provider.peak == wrapped.counters.peak_physical_slots == 1
        assert wrapped.counters.calls == 2 and snapshots[-1] == wrapped.counters
        with pytest.raises(ExperimentBudgetExhausted):
            await wrapped.invoke(request("D"), cancel=CancelToken())

    asyncio.run(case())


def test_parallel_in_flight_reservations_block_extra_physical_call():
    async def case():
        release = asyncio.Event()
        both_entered = asyncio.Event()

        async def slow(req, _cancel):
            if provider.active == 2:
                both_entered.set()
            await release.wait()
            return ProviderResponse(
                req.request_id,
                Message(MessageRole.ASSISTANT, "ok"),
                usage=ProviderUsage(12, 3, 15),
                model=TARGET.model,
            )

        provider = FakeProvider(slow)
        wrapped, _ = meter(
            provider,
            slots=3,
            budget=ExperimentBudget(35, 30, 50, 3, 10),
        )
        tasks = [
            asyncio.create_task(wrapped.invoke(request(role), cancel=CancelToken()))
            for role in ("S", "R")
        ]
        await both_entered.wait()
        assert wrapped.counters.peak_physical_slots == 2
        with pytest.raises(ExperimentBudgetExhausted):
            await wrapped.invoke(request("D"), cancel=CancelToken())
        assert len(provider.calls) == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(case())


def test_cancel_in_flight_counts_call_and_preserves_unknown_usage():
    async def case():
        entered = asyncio.Event()

        async def blocked(_req, _cancel):
            entered.set()
            await asyncio.Event().wait()

        provider = FakeProvider(blocked)
        wrapped, snapshots = meter(provider, slots=1)
        task = asyncio.create_task(wrapped.invoke(request("S"), cancel=CancelToken()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.active == 0
        assert wrapped.counters.calls == snapshots[-1].calls == 1
        assert wrapped.counters.total_tokens == 0  # Known lower bound only.
        assert wrapped.unknown_usage_calls == 1
        with pytest.raises(UnknownProviderUsage):
            await wrapped.invoke(request("R"), cancel=CancelToken())
        assert len(provider.calls) == 1

    asyncio.run(case())


@pytest.mark.parametrize("failure", ["raise", "missing"])
def test_failed_or_missing_usage_stops_admission_without_fake_zero(failure):
    async def case():
        count = 0

        async def fail(req, _cancel):
            nonlocal count
            count += 1
            if count == 1:
                return ProviderResponse(
                    req.request_id,
                    Message(MessageRole.ASSISTANT, "ok"),
                    usage=ProviderUsage(12, 3, 15),
                    model=TARGET.model,
                )
            if failure == "raise":
                raise RuntimeError("physical failure")
            return ProviderResponse(req.request_id, Message(MessageRole.ASSISTANT, "ok"))

        provider = FakeProvider(fail)
        wrapped, snapshots = meter(provider)
        await wrapped.invoke(request("S"), cancel=CancelToken())
        match = "physical call failed" if failure == "raise" else "omitted usage"
        with pytest.raises(UnknownProviderUsage, match=match) as caught:
            await wrapped.invoke(request("R"), cancel=CancelToken())
        if failure == "raise":
            assert isinstance(caught.value.__cause__, RuntimeError)
        assert wrapped.unknown_usage_calls == 1
        assert snapshots[-1].calls == 2
        assert snapshots[-1].total_tokens == 15  # Preserve the known lower bound.
        with pytest.raises(UnknownProviderUsage):
            await wrapped.invoke(request("D"), cancel=CancelToken())
        assert len(provider.calls) == 2

    asyncio.run(case())


def test_input_output_deadline_reservation_and_exact_identity():
    # Target mismatch is checked before any physical call.
    async def limits():
        provider = FakeProvider()
        run, _ = context()
        provider.target = replace(TARGET, model="other")
        with pytest.raises(ProviderIdentityMismatch):
            MeteredProvider(provider, run, estimate_input_tokens=lambda _: 15)
        provider.target = TARGET
        wrapped, _ = meter(provider, budget=ExperimentBudget(10, 10, 15, 1, 10))
        with pytest.raises(ExperimentBudgetExhausted):
            await wrapped.invoke(request("S"), cancel=CancelToken())
        assert not provider.calls
        wrapped, _ = meter(
            provider,
            budget=ExperimentBudget(30, 10, 35, 1, 10),
            estimate=lambda _: 11,
        )
        with pytest.raises(ExperimentBudgetExhausted, match="reservation"):
            await wrapped.invoke(request("S"), cancel=CancelToken())
        assert wrapped.counters.input_tokens == 12 and wrapped.counters.calls == 1
        wrapped, _ = meter(FakeProvider())
        wrapped.provider.target = replace(TARGET, model="other")
        with pytest.raises(ProviderIdentityMismatch):
            await wrapped.invoke(request("R"), cancel=CancelToken())
        assert wrapped.counters.calls == 0

        async def wrong_model(req, _cancel):
            return ProviderResponse(
                req.request_id,
                Message(MessageRole.ASSISTANT, "ok"),
                usage=ProviderUsage(12, 3, 15),
                model="substituted",
            )

        wrapped, _ = meter(FakeProvider(wrong_model))
        with pytest.raises(ProviderIdentityMismatch):
            await wrapped.invoke(request("F"), cancel=CancelToken())
        assert wrapped.counters.total_tokens == 15
        provider = FakeProvider()
        wrapped, _ = meter(provider, budget=ExperimentBudget(30, 30, 50, 1, 0.001))
        await asyncio.sleep(0.01)
        with pytest.raises(ExperimentBudgetExhausted, match="deadline"):
            await wrapped.invoke(request("D"), cancel=CancelToken())
        assert not provider.calls

    asyncio.run(limits())


def test_known_failed_usage_retains_sdk_error_and_cache_counters():
    from simple_harness.providers.errors import ProviderProtocolError

    class MalformedWithUsage(ProviderProtocolError):
        detail = {
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "cache_tokens": 5,
                "reasoning_tokens": 2,
            }
        }

    async def failure(request, cancel):
        raise MalformedWithUsage()

    async def case():
        wrapped, _ = meter(FakeProvider(failure))
        with pytest.raises(MalformedWithUsage):
            await wrapped.invoke(request("worker"), cancel=CancelToken())
        assert wrapped.counters.total_tokens == 15
        assert wrapped.unknown_usage_calls == 0
        assert wrapped.observations[0]["cache_tokens"] == 5
        assert wrapped.observations[0]["reasoning_tokens"] == 2
        assert wrapped.observations[0]["status"] == "MalformedWithUsage"

    asyncio.run(case())
