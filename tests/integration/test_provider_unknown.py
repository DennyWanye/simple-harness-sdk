# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import (
    ProviderInvocationCoordinator,
    ProviderInvocationUnknownError,
)
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderTransportError,
)

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider


def _request() -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId("request-unknown"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
        max_output_tokens=100,
    )


def _coordinator(uow, provider):
    return ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(hard_cap_micros=10_000, refuse_on_unknown=True),
        estimator=FrozenPriceEstimator(
            snapshot_id="prices-1",
            pricing_key="model-1",
            input_micros_per_million_tokens=1_000_000,
            output_micros_per_million_tokens=1_000_000,
        ),
    )


def test_error_after_handoff_is_unknown_and_never_replayed() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider(error=ProviderTransportError())

    async def exercise() -> None:
        coordinator = _coordinator(uow, provider)
        with pytest.raises(ProviderInvocationUnknownError):
            await coordinator.invoke(RunId("run-1"), _request(), cancel=CancelToken())
        with pytest.raises(ProviderInvocationUnknownError):
            await coordinator.invoke(RunId("run-1"), _request(), cancel=CancelToken())

    asyncio.run(exercise())
    assert provider.calls == 1
    assert next(iter(uow.records.values())).state is ProviderInvocationState.UNKNOWN


def test_recovery_marks_stranded_handed_off_unknown_without_provider_call() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider()

    async def exercise() -> None:
        coordinator = _coordinator(uow, provider)
        record = await coordinator.prepare_claim(RunId("run-1"), _request())
        uow.hand_off_provider_invocation(
            record.invocation_id, expected_version=record.version, handed_off_at=2.0
        )
        settled = await coordinator.reconcile_incomplete()
        assert settled == 1

    asyncio.run(exercise())
    assert provider.calls == 0
    assert next(iter(uow.records.values())).state is ProviderInvocationState.UNKNOWN
