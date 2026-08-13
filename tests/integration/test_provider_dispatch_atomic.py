# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
)

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider

LEASE = ExecutionLease("run-1", "runtime.kernel", "test-owner", 1, 100.0)


def _request(request_id: str = "request-1") -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId(request_id),
        messages=(Message(role=MessageRole.USER, content="hello"),),
        max_output_tokens=100,
    )


def _coordinator(uow, provider, *, output_rate: int = 2_000_000):
    provider.uow = uow
    return ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(hard_cap_micros=50_000, refuse_on_unknown=True),
        estimator=FrozenPriceEstimator(
            snapshot_id="prices-1",
            pricing_key="model-1",
            input_micros_per_million_tokens=1_000_000,
            output_micros_per_million_tokens=output_rate,
            fixed_request_overhead_tokens=10,
            per_message_overhead_tokens=4,
            per_tool_overhead_tokens=8,
        ),
        clock=lambda: 10.0,
    )


def test_claim_and_handoff_are_durable_before_exactly_one_provider_call() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider()

    async def exercise() -> ProviderResponse:
        return await _coordinator(uow, provider).invoke(
            RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
        )

    result = asyncio.run(exercise())
    assert result.message.content == "ok"
    assert provider.calls == 1
    assert uow.operations[:3] == ["claim", "hand_off", "provider_call"]
    assert next(iter(uow.records.values())).state is ProviderInvocationState.SUCCEEDED


def test_concurrent_same_invocation_only_one_cas_winner_calls_provider() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    release = asyncio.Event()
    provider = RecordingProvider(release=release)

    async def exercise() -> None:
        first = asyncio.create_task(
            _coordinator(uow, provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        )
        await provider.entered.wait()
        with pytest.raises(Exception, match="already handed off"):
            await _coordinator(uow, provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        release.set()
        await first

    asyncio.run(exercise())
    assert provider.calls == 1


def test_completed_same_invocation_returns_durable_result_without_replay() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider()

    async def exercise() -> tuple[ProviderResponse, ProviderResponse]:
        coordinator = _coordinator(uow, provider)
        first = await coordinator.invoke(
            RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
        )
        second = await coordinator.invoke(
            RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
        )
        return first, second

    first, second = asyncio.run(exercise())
    assert first == second
    assert provider.calls == 1


def test_same_logical_call_with_different_target_has_one_row_and_zero_second_transport() -> (
    None
):
    uow = FakeProviderInvocationUnitOfWork()
    release = asyncio.Event()
    first_provider = RecordingProvider(release=release)
    second_provider = RecordingProvider(
        endpoint_identity="https://different.invalid/v1/chat/completions"
    )

    async def exercise() -> None:
        first = asyncio.create_task(
            _coordinator(uow, first_provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        )
        await first_provider.entered.wait()
        with pytest.raises(Exception, match="identity conflict"):
            await _coordinator(uow, second_provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        release.set()
        await first

    asyncio.run(exercise())
    assert len(uow.records) == 1
    assert first_provider.calls == 1
    assert second_provider.calls == 0


def test_same_logical_call_with_different_estimator_has_zero_second_transport() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    release = asyncio.Event()
    provider = RecordingProvider(release=release)

    async def exercise() -> None:
        first = asyncio.create_task(
            _coordinator(uow, provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        )
        await provider.entered.wait()
        with pytest.raises(Exception, match="identity conflict"):
            await _coordinator(uow, provider, output_rate=9_000_000).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        release.set()
        await first

    asyncio.run(exercise())
    assert len(uow.records) == 1
    assert provider.calls == 1
