# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import RequestId, RunId, thaw_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import (
    ProviderInvocationCoordinator,
    ProviderInvocationUnknownError,
)
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderTransportError,
)

from .provider_ledger_fakes import FakeProviderInvocationUnitOfWork, RecordingProvider

LEASE = ExecutionLease("run-1", "runtime.kernel", "test-owner", 1, 100.0)


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
            await coordinator.invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )
        with pytest.raises(ProviderInvocationUnknownError):
            await coordinator.invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )

    asyncio.run(exercise())
    assert provider.calls == 1
    assert next(iter(uow.records.values())).state is ProviderInvocationState.UNKNOWN


def test_error_after_handoff_records_error_class_and_http_status() -> None:
    """P2.3p: the UNKNOWN row keeps the short class name and HTTP status, never the body."""

    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider(
        error=ProviderTransportError(
            public_message="scripted transport loss after handoff",
            status_code=418,
        )
    )

    async def exercise() -> None:
        coordinator = _coordinator(uow, provider)
        with pytest.raises(ProviderInvocationUnknownError):
            await coordinator.invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )

    asyncio.run(exercise())
    record = next(iter(uow.records.values()))
    assert record.state is ProviderInvocationState.UNKNOWN
    assert record.error_code == "provider_error_after_handoff"
    payload = thaw_json(record.usage_json) if record.usage_json is not None else {}
    assert isinstance(payload, dict)
    assert payload.get("error_class") == "ProviderTransportError"
    assert payload.get("http_status") == 418
    dumped = str(payload)
    assert "scripted transport loss" not in dumped
    assert "api_key" not in dumped
    assert "Authorization" not in dumped


def test_unclassified_runtime_error_after_handoff_records_the_short_class_name() -> None:
    uow = FakeProviderInvocationUnitOfWork()

    class AdapterGlitch(RuntimeError):
        pass

    provider = RecordingProvider(error=AdapterGlitch("do-not-store-this-body"))

    async def exercise() -> None:
        coordinator = _coordinator(uow, provider)
        with pytest.raises(ProviderInvocationUnknownError):
            await coordinator.invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=LEASE
            )

    asyncio.run(exercise())
    record = next(iter(uow.records.values()))
    payload = thaw_json(record.usage_json) if record.usage_json is not None else {}
    assert isinstance(payload, dict)
    assert payload.get("error_class") == "AdapterGlitch"
    assert "http_status" not in payload
    assert "do-not-store-this-body" not in str(payload)


def test_recovery_marks_stranded_handed_off_unknown_without_provider_call() -> None:
    uow = FakeProviderInvocationUnitOfWork()
    provider = RecordingProvider()

    async def exercise() -> None:
        coordinator = _coordinator(uow, provider)
        record = await coordinator.prepare_claim(RunId("run-1"), _request(), execution_lease=LEASE)
        uow.hand_off_provider_invocation(
            record.invocation_id,
            expected_version=record.version,
            handed_off_at=2.0,
            execution_lease=LEASE,
        )
        settled = await coordinator.reconcile_incomplete()
        assert settled == 1

    asyncio.run(exercise())
    assert provider.calls == 0
    assert next(iter(uow.records.values())).state is ProviderInvocationState.UNKNOWN
