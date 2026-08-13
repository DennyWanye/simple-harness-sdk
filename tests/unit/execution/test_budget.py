# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import (
    BudgetChargeKind,
    BudgetExceededError,
    BudgetPolicy,
    BudgetSnapshot,
    BudgetUnknownError,
    FrozenPriceEstimator,
)
from simple_harness.providers import ProviderRequest, ProviderTarget, ProviderUsage


def _request(*, max_output_tokens: int | None = 100) -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId("request-budget-1"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
        max_output_tokens=max_output_tokens,
    )


def _estimator() -> FrozenPriceEstimator:
    return FrozenPriceEstimator(
        snapshot_id="prices-2026-08-14",
        pricing_key="model-1",
        input_micros_per_million_tokens=2_000_000,
        output_micros_per_million_tokens=8_000_000,
        fixed_request_overhead_tokens=20,
        per_message_overhead_tokens=4,
        per_tool_overhead_tokens=8,
    )


def test_usage_cost_uses_frozen_price_snapshot() -> None:
    charge = _estimator().charge_usage(
        ProviderUsage(input_tokens=100, output_tokens=25, total_tokens=125)
    )
    assert charge.kind is BudgetChargeKind.TRUSTED_USAGE
    assert charge.amount_micros == 400
    assert charge.estimator_snapshot_id == "prices-2026-08-14"


def test_missing_usage_uses_conservative_request_upper_bound() -> None:
    charge = _estimator().estimate_upper_bound(_request())
    assert charge.kind is BudgetChargeKind.ESTIMATED_UPPER_BOUND
    assert charge.amount_micros is not None and charge.amount_micros > 800


def test_missing_output_limit_cannot_be_estimated() -> None:
    assert (
        _estimator().estimate_upper_bound(_request(max_output_tokens=None)).is_unknown
    )


def test_estimator_must_bind_to_the_provider_pricing_key() -> None:
    target = ProviderTarget(
        provider_id="provider-1",
        model="model-1",
        pricing_key="different-price",
        endpoint_identity="https://provider.invalid/v1/chat/completions",
        adapter_key="adapter.v1",
    )
    with pytest.raises(ValueError, match="pricing_key"):
        _estimator().bind(target)


def test_hard_cap_reserves_before_dispatch_and_never_treats_unknown_as_zero() -> None:
    policy = BudgetPolicy(hard_cap_micros=1_000, refuse_on_unknown=True)
    with pytest.raises(BudgetExceededError):
        policy.authorize(
            BudgetSnapshot(committed_micros=600, reserved_micros=300),
            reservation_micros=101,
        )
    with pytest.raises(BudgetUnknownError):
        policy.authorize(
            BudgetSnapshot(
                committed_micros=1,
                reserved_micros=0,
                has_unknown_charge=True,
            ),
            reservation_micros=1,
        )


def test_hard_cap_requires_a_known_reservation() -> None:
    policy = BudgetPolicy(hard_cap_micros=1_000, refuse_on_unknown=True)
    with pytest.raises(BudgetUnknownError):
        policy.authorize(BudgetSnapshot(), reservation_micros=None)
