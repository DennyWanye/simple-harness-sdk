# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import (
    BudgetPolicy,
    BudgetUnknownError,
    FrozenPriceEstimator,
)
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import CancelToken, ProviderRequest

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider

LEASE = ExecutionLease("run-1", "runtime.kernel", "test-owner", 1, 100.0)


def test_missing_usage_persists_estimator_upper_bound_across_recovery() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider(usage=None)
    estimator = FrozenPriceEstimator(
        snapshot_id="prices-1",
        pricing_key="model-1",
        input_micros_per_million_tokens=1_000_000,
        output_micros_per_million_tokens=3_000_000,
    )
    policy = BudgetPolicy(hard_cap_micros=100_000, refuse_on_unknown=True)

    async def exercise() -> None:
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=policy,
            estimator=estimator,
        )
        await coordinator.invoke(
            RunId("run-1"),
            ProviderRequest(
                request_id=RequestId("request-1"),
                messages=(Message(role=MessageRole.USER, content="hello"),),
                max_output_tokens=100,
            ),
            cancel=CancelToken(),
            execution_lease=LEASE,
        )
        snapshot = uow.read_provider_budget(RunId("run-1"))
        assert snapshot.committed_micros > 0
        assert not snapshot.has_unknown_charge

    asyncio.run(exercise())


def test_missing_usage_without_estimate_is_unknown_and_blocks_next_call() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider(usage=None)
    policy = BudgetPolicy(hard_cap_micros=None, refuse_on_unknown=True)

    async def exercise() -> None:
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=policy,
            estimator=None,
        )
        await coordinator.invoke(
            RunId("run-1"),
            ProviderRequest(
                request_id=RequestId("request-1"),
                messages=(Message(role=MessageRole.USER, content="hello"),),
            ),
            cancel=CancelToken(),
            execution_lease=LEASE,
        )
        with pytest.raises(BudgetUnknownError):
            await coordinator.invoke(
                RunId("run-1"),
                ProviderRequest(
                    request_id=RequestId("request-2"),
                    messages=(Message(role=MessageRole.USER, content="again"),),
                ),
                cancel=CancelToken(),
                execution_lease=LEASE,
            )

    asyncio.run(exercise())


def test_usage_from_a_different_model_is_unknown_not_mispriced() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider(response_model="unexpected-model")
    estimator = FrozenPriceEstimator(
        snapshot_id="prices-1",
        pricing_key="model-1",
        input_micros_per_million_tokens=1_000_000,
        output_micros_per_million_tokens=3_000_000,
    )

    async def exercise() -> None:
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=BudgetPolicy(hard_cap_micros=100_000),
            estimator=estimator,
        )
        await coordinator.invoke(
            RunId("run-1"),
            ProviderRequest(
                request_id=RequestId("request-1"),
                messages=(Message(role=MessageRole.USER, content="hello"),),
                max_output_tokens=100,
            ),
            cancel=CancelToken(),
            execution_lease=LEASE,
        )
        snapshot = uow.read_provider_budget(RunId("run-1"))
        assert snapshot.has_unknown_charge
        assert snapshot.committed_micros == 0

    asyncio.run(exercise())
