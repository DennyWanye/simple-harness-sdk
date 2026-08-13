# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import (
    BudgetCharge,
    BudgetChargeKind,
    FrozenPriceEstimator,
)
from simple_harness.execution.provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
    provider_invocation_id,
    provider_request_fingerprint,
)
from simple_harness.providers import ProviderRequest, ProviderTarget


def _request() -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId("request-1"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
    )


def _target() -> ProviderTarget:
    return ProviderTarget(
        provider_id="provider-1",
        model="model-1",
        pricing_key="model-1",
        endpoint_identity="https://provider.invalid/v1/chat/completions",
        adapter_key="adapter.v1",
    )


def _estimator() -> FrozenPriceEstimator:
    return FrozenPriceEstimator(
        snapshot_id="prices-1",
        pricing_key="model-1",
        input_micros_per_million_tokens=1,
        output_micros_per_million_tokens=1,
    )


def test_invocation_identity_and_request_fingerprint_are_deterministic() -> None:
    request = _request()
    fingerprint = provider_request_fingerprint(request)
    first = provider_invocation_id(RunId("run-1"), request.request_id)
    second = provider_invocation_id(RunId("run-1"), request.request_id)
    assert first == second
    assert len(first) == len(fingerprint) == 64
    same_content = ProviderRequest(
        request_id=RequestId("request-2"), messages=request.messages
    )
    assert provider_request_fingerprint(same_content) == fingerprint
    assert provider_invocation_id(RunId("run-1"), same_content.request_id) != first


def test_record_is_immutable_and_transition_graph_is_closed() -> None:
    estimator = _estimator()
    record = ProviderInvocationRecord.claimed(
        invocation_id="a" * 64,
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
        request_fingerprint="b" * 64,
        target=_target(),
        estimator_snapshot=estimator.snapshot_json(),
        estimator_digest=estimator.snapshot_digest,
        reservation=BudgetCharge(
            kind=BudgetChargeKind.ESTIMATED_UPPER_BOUND,
            amount_micros=10,
            estimator_snapshot_id="prices-1",
        ),
        claimed_at=1.0,
    )
    with pytest.raises(FrozenInstanceError):
        record.version = 2  # type: ignore[misc]
    handed_off = record.hand_off(at=2.0, expected_version=1)
    assert handed_off.state is ProviderInvocationState.HANDED_OFF
    succeeded = handed_off.settle_succeeded(
        response_json={"message": {"role": "assistant", "content": "ok"}},
        usage_json={"budget": {"kind": "trusted_usage", "amount_micros": 3}},
        at=3.0,
        expected_version=2,
    )
    assert succeeded.state is ProviderInvocationState.SUCCEEDED
    with pytest.raises(ValueError):
        succeeded.hand_off(at=4.0, expected_version=3)


def test_stale_version_is_rejected() -> None:
    record = ProviderInvocationRecord.claimed(
        invocation_id="a" * 64,
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
        request_fingerprint="b" * 64,
        target=_target(),
        estimator_snapshot=None,
        estimator_digest=None,
        reservation=BudgetCharge.unknown(),
        claimed_at=1.0,
    )
    with pytest.raises(ValueError, match="stale provider invocation version"):
        record.hand_off(at=2.0, expected_version=9)
