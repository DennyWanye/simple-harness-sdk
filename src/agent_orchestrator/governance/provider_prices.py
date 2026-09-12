# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Exact integer pricing using the SDK's immutable per-invocation price snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from simple_harness.contracts import canonical_json
from simple_harness.execution.budget import FrozenPriceEstimator

from .budgets import BudgetError


@dataclass(frozen=True, slots=True)
class ProviderPrice:
    estimator: FrozenPriceEstimator

    @classmethod
    def from_record(cls, record) -> ProviderPrice | None:
        snapshot = record.estimator_snapshot
        if snapshot is None:
            if record.estimator_digest is not None:
                raise BudgetError("provider price digest without its frozen snapshot")
            return None
        if not isinstance(snapshot, Mapping):
            raise BudgetError("provider price must be a frozen SDK snapshot")
        raw = dict(snapshot)
        if raw.pop("protocol", None) != "simple-harness-price-estimator-v1":
            raise BudgetError("unsupported provider price protocol")
        try:
            estimator = FrozenPriceEstimator(**raw)
        except (TypeError, ValueError) as exc:
            raise BudgetError("invalid frozen provider price") from exc
        if estimator.snapshot_digest != record.estimator_digest:
            raise BudgetError("provider price snapshot digest differs")
        return cls(estimator)

    @property
    def json(self) -> str:
        return canonical_json(self.estimator.snapshot_json())

    @property
    def digest(self) -> str:
        return self.estimator.snapshot_digest

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        # Keep the two independently rounded billing dimensions; no floats and
        # no inference that a cheaper/new price applies to an old invocation.
        return (
            input_tokens * self.estimator.input_micros_per_million_tokens + 999_999
        ) // 1_000_000 + (
            output_tokens * self.estimator.output_micros_per_million_tokens + 999_999
        ) // 1_000_000

    def known_charge(self, record, *, input_tokens: int, output_tokens: int) -> int | None:
        actual = record.budget_charge
        if str(actual.kind) != "trusted_usage" or actual.amount_micros is None:
            return None
        if actual.estimator_snapshot_id != self.estimator.snapshot_id:
            return None
        # Preserve a larger actually observed trusted charge as overrun evidence;
        # a lower/inconsistent amount is not proof that the frozen price was paid.
        if actual.amount_micros < self.cost(input_tokens, output_tokens):
            return None
        return actual.amount_micros


__all__ = ("ProviderPrice",)
