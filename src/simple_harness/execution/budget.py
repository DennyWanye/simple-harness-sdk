# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable provider-cost policy and frozen upper-bound estimation."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    HarnessError,
    JsonValue,
    canonical_json,
    thaw_json,
)
from simple_harness.providers import ProviderRequest, ProviderTarget, ProviderUsage


class BudgetChargeKind(StrEnum):
    """The evidence supporting a durable provider charge."""

    TRUSTED_USAGE = "trusted_usage"
    ESTIMATED_UPPER_BOUND = "estimated_upper_bound"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BudgetCharge:
    """One invocation's cost in integer millionths of the billing currency."""

    kind: BudgetChargeKind
    amount_micros: int | None
    estimator_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", BudgetChargeKind(self.kind))
        if self.amount_micros is not None and (
            isinstance(self.amount_micros, bool)
            or not isinstance(self.amount_micros, int)
            or self.amount_micros < 0
        ):
            raise ValueError("amount_micros must be a non-negative integer or None")
        if self.kind is BudgetChargeKind.UNKNOWN:
            if self.amount_micros is not None:
                raise ValueError("unknown budget charge cannot have an amount")
        elif self.amount_micros is None:
            raise ValueError("known budget charge requires amount_micros")
        if (
            self.estimator_snapshot_id is not None
            and not self.estimator_snapshot_id.strip()
        ):
            raise ValueError("estimator_snapshot_id must not be blank")

    @property
    def is_unknown(self) -> bool:
        return self.kind is BudgetChargeKind.UNKNOWN

    @classmethod
    def unknown(cls) -> BudgetCharge:
        return cls(BudgetChargeKind.UNKNOWN, None)

    def to_json(self) -> dict[str, str | int | None]:
        return {
            "kind": self.kind.value,
            "amount_micros": self.amount_micros,
            "estimator_snapshot_id": self.estimator_snapshot_id,
        }

    @classmethod
    def from_json(cls, value: object) -> BudgetCharge:
        if not isinstance(value, dict):
            raise TypeError("budget charge must be an object")
        kind = value.get("kind")
        amount = value.get("amount_micros")
        snapshot_id = value.get("estimator_snapshot_id")
        if snapshot_id is not None and not isinstance(snapshot_id, str):
            raise ValueError("estimator_snapshot_id must be a string or null")
        return cls(BudgetChargeKind(str(kind)), amount, snapshot_id)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Durable aggregate, including reservations for in-flight calls."""

    committed_micros: int = 0
    reserved_micros: int = 0
    has_unknown_charge: bool = False

    def __post_init__(self) -> None:
        for name in ("committed_micros", "reserved_micros"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


class BudgetExceededError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            "provider_budget_exceeded",
            "Provider budget limit reached.",
            retryable=False,
        )


class BudgetUnknownError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            "provider_budget_unknown",
            "Provider cost is unknown; further invocation was refused.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Fail-closed hard-cap policy evaluated inside the claim transaction."""

    hard_cap_micros: int | None = None
    refuse_on_unknown: bool = True

    def __post_init__(self) -> None:
        if self.hard_cap_micros is not None and (
            isinstance(self.hard_cap_micros, bool)
            or not isinstance(self.hard_cap_micros, int)
            or self.hard_cap_micros < 0
        ):
            raise ValueError("hard_cap_micros must be non-negative or None")
        if self.hard_cap_micros is not None and not self.refuse_on_unknown:
            raise ValueError("hard caps must refuse unknown provider cost")

    def authorize(
        self, snapshot: BudgetSnapshot, *, reservation_micros: int | None
    ) -> None:
        if self.refuse_on_unknown and snapshot.has_unknown_charge:
            raise BudgetUnknownError()
        if reservation_micros is None:
            if self.hard_cap_micros is not None:
                raise BudgetUnknownError()
            return
        if isinstance(reservation_micros, bool) or reservation_micros < 0:
            raise ValueError("reservation_micros must be non-negative or None")
        if self.hard_cap_micros is not None and (
            snapshot.committed_micros + snapshot.reserved_micros + reservation_micros
            > self.hard_cap_micros
        ):
            raise BudgetExceededError()


@dataclass(frozen=True, slots=True)
class FrozenPriceEstimator:
    """A host-frozen price table plus conservative request token bounds.

    The host must certify the overhead values for the selected model/tokenizer.
    UTF-8 byte count is used as the conservative content token upper bound.
    """

    snapshot_id: str
    pricing_key: str
    input_micros_per_million_tokens: int
    output_micros_per_million_tokens: int
    fixed_request_overhead_tokens: int = 0
    per_message_overhead_tokens: int = 0
    per_tool_overhead_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.pricing_key.strip():
            raise ValueError("snapshot_id and pricing_key must not be blank")
        for name in (
            "input_micros_per_million_tokens",
            "output_micros_per_million_tokens",
            "fixed_request_overhead_tokens",
            "per_message_overhead_tokens",
            "per_tool_overhead_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @staticmethod
    def _priced(tokens: int, rate: int) -> int:
        return math.ceil(tokens * rate / 1_000_000)

    def snapshot_json(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness-price-estimator-v1",
            "snapshot_id": self.snapshot_id,
            "pricing_key": self.pricing_key,
            "input_micros_per_million_tokens": self.input_micros_per_million_tokens,
            "output_micros_per_million_tokens": self.output_micros_per_million_tokens,
            "fixed_request_overhead_tokens": self.fixed_request_overhead_tokens,
            "per_message_overhead_tokens": self.per_message_overhead_tokens,
            "per_tool_overhead_tokens": self.per_tool_overhead_tokens,
        }

    @property
    def snapshot_digest(self) -> str:
        payload = canonical_json(self.snapshot_json()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def bind(self, target: ProviderTarget) -> None:
        if target.pricing_key != self.pricing_key:
            raise ValueError("estimator pricing_key does not match provider target")

    def charge_usage(self, usage: ProviderUsage) -> BudgetCharge:
        amount = self._priced(
            usage.input_tokens, self.input_micros_per_million_tokens
        ) + self._priced(usage.output_tokens, self.output_micros_per_million_tokens)
        return BudgetCharge(
            BudgetChargeKind.TRUSTED_USAGE,
            amount,
            self.snapshot_id,
        )

    def estimate_upper_bound(self, request: ProviderRequest) -> BudgetCharge:
        if request.max_output_tokens is None:
            return BudgetCharge.unknown()
        input_payload: JsonValue = {
            "messages": [
                {
                    "role": getattr(message.role, "value", str(message.role)),
                    "content": message.content,
                    "name": message.name,
                    "call_id": None
                    if message.call_id is None
                    else message.call_id.value,
                    "metadata": thaw_json(cast(FrozenJsonValue, message.metadata)),
                }
                for message in request.messages
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": thaw_json(cast(FrozenJsonValue, tool.parameters)),
                }
                for tool in request.tools
            ],
        }
        content_bound = len(canonical_json(input_payload).encode("utf-8"))
        input_bound = (
            content_bound
            + self.fixed_request_overhead_tokens
            + len(request.messages) * self.per_message_overhead_tokens
            + len(request.tools) * self.per_tool_overhead_tokens
        )
        amount = self._priced(
            input_bound, self.input_micros_per_million_tokens
        ) + self._priced(
            request.max_output_tokens, self.output_micros_per_million_tokens
        )
        return BudgetCharge(
            BudgetChargeKind.ESTIMATED_UPPER_BOUND,
            amount,
            self.snapshot_id,
        )


__all__ = (
    "BudgetCharge",
    "BudgetChargeKind",
    "BudgetExceededError",
    "BudgetPolicy",
    "BudgetSnapshot",
    "BudgetUnknownError",
    "FrozenPriceEstimator",
)
