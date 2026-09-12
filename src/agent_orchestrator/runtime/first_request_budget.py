# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen FIRST-request input caps and their non-negotiable token floor.

The initial Critic request is special: its protected tail must cover both the
largest public input the frozen context policy permits and the output ceiling
the actual SDK may request.  A historic tail reserve is not evidence that it
covers either value.  This module deliberately returns ``UNKNOWN`` when the
frozen inputs or the admission protocol are absent; legacy pools therefore
retain their previous behaviour until an owner explicitly wires this protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.contracts import JsonValue
from simple_harness.execution.provider_admission import ProviderRequestRejectedError

INPUT_CAP_PROTOCOL = "provider-input-cap-v1"
INPUT_CAP_SCHEMA_VERSION = 1


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ProviderInputCap:
    """Public, frozen input allowance attached to one routed provider profile."""

    profile_id: str
    model: str
    max_input_tokens: int
    context_fingerprint: str
    estimator_fingerprint: str
    schema_version: int = INPUT_CAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INPUT_CAP_SCHEMA_VERSION:
            raise ValueError("unsupported provider input-cap schema")
        _nonempty_string(self.profile_id, "profile_id")
        _nonempty_string(self.model, "model")
        _positive_int(self.max_input_tokens, "max_input_tokens")
        _nonempty_string(self.context_fingerprint, "context_fingerprint")
        _nonempty_string(self.estimator_fingerprint, "estimator_fingerprint")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "model": self.model,
            "max_input_tokens": self.max_input_tokens,
            "context_fingerprint": self.context_fingerprint,
            "estimator_fingerprint": self.estimator_fingerprint,
        }

    @classmethod
    def from_json(cls, value: object) -> ProviderInputCap:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "profile_id", "model", "max_input_tokens",
            "context_fingerprint", "estimator_fingerprint",
        }:
            raise ValueError("invalid provider input-cap payload")
        return cls(
            schema_version=value["schema_version"],
            profile_id=value["profile_id"],
            model=value["model"],
            max_input_tokens=value["max_input_tokens"],
            context_fingerprint=value["context_fingerprint"],
            estimator_fingerprint=value["estimator_fingerprint"],
        )


@dataclass(frozen=True, slots=True)
class FirstRequestBudget:
    """The only token floor that can support a capped first provider request."""

    provider_input_cap: ProviderInputCap
    output_ceiling: int

    def __post_init__(self) -> None:
        _positive_int(self.output_ceiling, "output_ceiling")

    @property
    def minimum_tokens(self) -> int:
        return self.provider_input_cap.max_input_tokens + self.output_ceiling


@dataclass(frozen=True, slots=True)
class FirstRequestBudgetUnknown:
    """An explicit non-decision; callers must not substitute a legacy reserve."""

    reason: str

    def __post_init__(self) -> None:
        _nonempty_string(self.reason, "reason")


def frozen_provider_input_cap(
    *,
    profile_id: str,
    model: str,
    runtime_context: Mapping[str, Any] | None,
    estimator_fingerprint: str | None,
) -> ProviderInputCap | FirstRequestBudgetUnknown:
    """Build a cap only from the public profile snapshot frozen into an intent."""

    if runtime_context is None:
        return FirstRequestBudgetUnknown("runtime_context_missing")
    if runtime_context.get("schema") != 1:
        return FirstRequestBudgetUnknown("runtime_context_schema_unknown")
    fingerprint = runtime_context.get("fingerprint")
    policy_json = runtime_context.get("policy")
    if not isinstance(fingerprint, str) or not fingerprint:
        return FirstRequestBudgetUnknown("context_fingerprint_missing")
    if not isinstance(policy_json, Mapping):
        return FirstRequestBudgetUnknown("context_policy_missing")
    if not isinstance(estimator_fingerprint, str) or not estimator_fingerprint:
        return FirstRequestBudgetUnknown("estimator_fingerprint_missing")
    try:
        policy = ContextPolicy(**dict(policy_json))
        return ProviderInputCap(
            profile_id=profile_id,
            model=model,
            max_input_tokens=policy.input_budget(),
            context_fingerprint=fingerprint,
            estimator_fingerprint=estimator_fingerprint,
        )
    except (TypeError, ValueError):
        return FirstRequestBudgetUnknown("context_policy_invalid")


def actual_output_ceiling(
    *,
    profile_default_max_output_tokens: int | None,
    profile_max_output_tokens_ceiling: int | None,
    config_default_max_output_tokens: int,
    config_max_output_tokens_ceiling: int,
) -> int:
    """Match ``assemble_orchestrator_runtime``'s effective SDK output ceiling."""

    default = _positive_int(config_default_max_output_tokens, "config_default_max_output_tokens")
    configured = _positive_int(
        config_max_output_tokens_ceiling, "config_max_output_tokens_ceiling"
    )
    if profile_default_max_output_tokens is not None:
        default = _positive_int(
            profile_default_max_output_tokens, "profile_default_max_output_tokens"
        )
    if profile_max_output_tokens_ceiling is not None:
        configured = _positive_int(
            profile_max_output_tokens_ceiling, "profile_max_output_tokens_ceiling"
        )
    return max(default, configured)


def first_request_budget(
    *,
    provider_input_cap: ProviderInputCap | FirstRequestBudgetUnknown,
    output_ceiling: int,
    guard_input_cap_protocol: str | None,
) -> FirstRequestBudget | FirstRequestBudgetUnknown:
    """Return the enforceable floor only for a guard that declares this protocol."""

    if isinstance(provider_input_cap, FirstRequestBudgetUnknown):
        return provider_input_cap
    if guard_input_cap_protocol != INPUT_CAP_PROTOCOL:
        return FirstRequestBudgetUnknown("guard_input_cap_protocol_unknown")
    return FirstRequestBudget(provider_input_cap, output_ceiling)


class FirstRequestInputCapExceeded(ProviderRequestRejectedError):
    """The final rendered request exceeded its frozen public input allowance."""

    error_code = "provider_input_cap_exceeded"

    def __init__(self, *, actual_input_tokens: int, provider_input_cap: ProviderInputCap) -> None:
        super().__init__(
            public_message="Final provider wire input exceeds its frozen input cap."
        )
        self.detail = {
            "schema_version": INPUT_CAP_SCHEMA_VERSION,
            "reason_code": "final_wire_input_exceeded",
            "actual_input_tokens": actual_input_tokens,
            "max_input_tokens": provider_input_cap.max_input_tokens,
            "profile_id": provider_input_cap.profile_id,
            "model": provider_input_cap.model,
            "context_fingerprint": provider_input_cap.context_fingerprint,
            "estimator_fingerprint": provider_input_cap.estimator_fingerprint,
        }


def enforce_final_wire_input_cap(
    *, provider_input_cap: ProviderInputCap, actual_input_tokens: int
) -> None:
    """Refuse a final rendered wire request before provider admission/handoff."""

    _positive_int(actual_input_tokens, "actual_input_tokens")
    if actual_input_tokens > provider_input_cap.max_input_tokens:
        raise FirstRequestInputCapExceeded(
            actual_input_tokens=actual_input_tokens, provider_input_cap=provider_input_cap
        )


__all__ = (
    "INPUT_CAP_PROTOCOL",
    "INPUT_CAP_SCHEMA_VERSION",
    "FirstRequestBudget",
    "FirstRequestBudgetUnknown",
    "FirstRequestInputCapExceeded",
    "ProviderInputCap",
    "actual_output_ceiling",
    "enforce_final_wire_input_cap",
    "first_request_budget",
    "frozen_provider_input_cap",
)
