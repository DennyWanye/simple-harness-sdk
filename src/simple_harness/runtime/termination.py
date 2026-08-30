# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable hard limits for the official ReAct Driver."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from simple_harness.contracts import HarnessError, JsonValue
from simple_harness.execution.budget import BudgetSnapshot


class TerminationReason(StrEnum):
    MAX_TURNS = "max_turns"
    MAX_TOOL_CALLS = "max_tool_calls"
    WALL_CLOCK = "wall_clock"
    COST = "cost"
    REPEATED_TOOL = "repeated_tool"
    CANCELLED = "cancelled"


class TerminationBudgetExceeded(HarnessError):
    __slots__ = ("reason",)

    def __init__(self, reason: TerminationReason) -> None:
        self.reason = TerminationReason(reason)
        super().__init__(
            f"react_{self.reason.value}_exceeded",
            f"ReAct execution stopped by the {self.reason.value} hard limit.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class TerminationLimits:
    max_turns: int = 32
    max_tool_calls: int = 64
    max_wall_seconds: float = 900.0
    max_cost_micros: int = 10_000_000
    max_consecutive_same_tool: int = 3

    def __post_init__(self) -> None:
        for name in (
            "max_turns",
            "max_tool_calls",
            "max_cost_micros",
            "max_consecutive_same_tool",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.max_wall_seconds) or self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class TerminationState:
    """Checkpoint payload; totals are reservations and never reset on restart."""

    started_at: float
    last_observed_at: float | None = None
    provider_turns_reserved_total: int = 0
    tool_calls_reserved_total: int = 0
    repeat_key: str | None = None
    repeat_streak: int = 0
    phase: str = "ready"
    provider_request_id: str | None = None
    tool_batch_id: str | None = None
    context_revision: int | None = None
    provider_request_snapshot: JsonValue | None = None
    provider_request_fingerprint: str | None = None
    provider_response_snapshot: JsonValue | None = None
    provider_response_digest: str | None = None
    tool_result_progress: int = 0
    workflow_spawn_wait_receipt_id: str | None = None
    pending_child_completion: JsonValue | None = None
    pending_child_completion_hash: str | None = None
    pending_child_completion_append_id: str | None = None
    last_workflow_spawn_wait_receipt_id: str | None = None
    workflow_catalog_selection: JsonValue | None = None
    workflow_catalog_selection_hash: str | None = None
    tool_exposure_state: JsonValue | None = None
    policy_fingerprint: str = ""
    route_state: str = "unrouted"
    route_receipt: JsonValue | None = None
    route_receipt_hash: str | None = None
    context_authority_receipt: JsonValue | None = None
    context_authority_receipt_hash: str | None = None

    @property
    def turns(self) -> int:
        return self.provider_turns_reserved_total

    @property
    def tool_calls(self) -> int:
        return self.tool_calls_reserved_total

    @property
    def consecutive_same_tool(self) -> int:
        return self.repeat_streak

    def __post_init__(self) -> None:
        if not math.isfinite(self.started_at) or self.started_at < 0:
            raise ValueError("started_at must be a finite Unix epoch")
        observed = self.started_at if self.last_observed_at is None else self.last_observed_at
        if not math.isfinite(observed) or observed < self.started_at:
            raise ValueError("clock rollback detected")
        object.__setattr__(self, "last_observed_at", observed)
        for value in (
            self.provider_turns_reserved_total,
            self.tool_calls_reserved_total,
            self.repeat_streak,
            self.tool_result_progress,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("durable termination totals must be non-negative")
        if self.context_revision is not None and self.context_revision < 0:
            raise ValueError("context_revision must be non-negative")
        pending_values = (
            self.workflow_spawn_wait_receipt_id,
            self.pending_child_completion,
            self.pending_child_completion_hash,
            self.pending_child_completion_append_id,
        )
        if any(value is not None for value in pending_values) and not all(
            value is not None for value in pending_values
        ):
            raise ValueError("pending workflow child completion is incomplete")
        if (
            self.pending_child_completion_hash is not None
            and len(self.pending_child_completion_hash) != 64
        ):
            raise ValueError("pending child completion hash is invalid")
        catalog_pin_values = (
            self.workflow_catalog_selection,
            self.workflow_catalog_selection_hash,
        )
        if any(value is not None for value in catalog_pin_values) and not all(
            value is not None for value in catalog_pin_values
        ):
            raise ValueError("workflow catalog selection pin is incomplete")
        if (
            self.workflow_catalog_selection_hash is not None
            and len(self.workflow_catalog_selection_hash) != 64
        ):
            raise ValueError("workflow catalog selection hash is invalid")
        if self.policy_fingerprint and (
            len(self.policy_fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in self.policy_fingerprint)
        ):
            raise ValueError("termination policy fingerprint must be lowercase SHA-256")
        if self.route_state not in {"unrouted", "routed_standalone", "routed_task"}:
            raise ValueError("invalid durable route state")
        if (self.route_receipt is None) != (self.route_receipt_hash is None):
            raise ValueError("route receipt and hash must be paired")
        if (self.context_authority_receipt is None) != (
            self.context_authority_receipt_hash is None
        ):
            raise ValueError("Context authority receipt and hash must be paired")
        for payload, digest, name in (
            (self.route_receipt, self.route_receipt_hash, "route receipt"),
            (
                self.context_authority_receipt,
                self.context_authority_receipt_hash,
                "Context authority receipt",
            ),
        ):
            if digest is not None:
                import hashlib

                from simple_harness.contracts import canonical_json

                if (
                    len(digest) != 64
                    or hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
                    != digest
                ):
                    raise ValueError(f"{name} hash is invalid")

    def before_provider(
        self, limits: TerminationLimits, *, now: float, budget: BudgetSnapshot
    ) -> TerminationState:
        _check_common(self, limits, now=now, budget=budget)
        if self.provider_turns_reserved_total >= limits.max_turns:
            raise TerminationBudgetExceeded(TerminationReason.MAX_TURNS)
        ordinal = self.provider_turns_reserved_total + 1
        return replace(
            self,
            last_observed_at=now,
            provider_turns_reserved_total=ordinal,
            phase="provider_reserved",
            provider_request_id=f"provider-turn:{ordinal}",
            tool_batch_id=None,
            context_revision=None,
            provider_request_snapshot=None,
            provider_request_fingerprint=None,
            provider_response_snapshot=None,
            provider_response_digest=None,
            tool_result_progress=0,
            context_authority_receipt=None,
            context_authority_receipt_hash=None,
        )

    def before_tool_batch(
        self,
        tool_keys: Sequence[str],
        limits: TerminationLimits,
        *,
        now: float,
        budget: BudgetSnapshot,
    ) -> TerminationState:
        _check_common(self, limits, now=now, budget=budget)
        if self.tool_calls_reserved_total + len(tool_keys) > limits.max_tool_calls:
            raise TerminationBudgetExceeded(TerminationReason.MAX_TOOL_CALLS)
        repeat_key = self.repeat_key
        repeat_streak = self.repeat_streak
        for key in tool_keys:
            repeat_streak = repeat_streak + 1 if repeat_key == key else 1
            repeat_key = key
            if repeat_streak > limits.max_consecutive_same_tool:
                raise TerminationBudgetExceeded(TerminationReason.REPEATED_TOOL)
        return replace(
            self,
            last_observed_at=now,
            tool_calls_reserved_total=self.tool_calls_reserved_total + len(tool_keys),
            repeat_key=repeat_key,
            repeat_streak=repeat_streak,
            phase="tool_batch_reserved",
            tool_batch_id=f"tool-batch:{self.provider_turns_reserved_total}",
        )

    def before_tool(
        self,
        tool_name: str,
        limits: TerminationLimits,
        *,
        now: float,
        budget: BudgetSnapshot,
    ) -> TerminationState:
        return self.before_tool_batch((tool_name,), limits, now=now, budget=budget)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 4,
            "started_at": self.started_at,
            "last_observed_at": self.last_observed_at,
            "provider_turns_reserved_total": self.provider_turns_reserved_total,
            "tool_calls_reserved_total": self.tool_calls_reserved_total,
            "repeat_key": self.repeat_key,
            "repeat_streak": self.repeat_streak,
            "phase": self.phase,
            "provider_request_id": self.provider_request_id,
            "tool_batch_id": self.tool_batch_id,
            "context_revision": self.context_revision,
            "provider_request_snapshot": self.provider_request_snapshot,
            "provider_request_fingerprint": self.provider_request_fingerprint,
            "provider_response_snapshot": self.provider_response_snapshot,
            "provider_response_digest": self.provider_response_digest,
            "tool_result_progress": self.tool_result_progress,
            "workflow_spawn_wait_receipt_id": self.workflow_spawn_wait_receipt_id,
            "pending_child_completion": self.pending_child_completion,
            "pending_child_completion_hash": self.pending_child_completion_hash,
            "pending_child_completion_append_id": (self.pending_child_completion_append_id),
            "last_workflow_spawn_wait_receipt_id": (self.last_workflow_spawn_wait_receipt_id),
            "workflow_catalog_selection": self.workflow_catalog_selection,
            "workflow_catalog_selection_hash": self.workflow_catalog_selection_hash,
            "tool_exposure_state": self.tool_exposure_state,
            "policy_fingerprint": self.policy_fingerprint,
            "route_state": self.route_state,
            "route_receipt": self.route_receipt,
            "route_receipt_hash": self.route_receipt_hash,
            "context_authority_receipt": self.context_authority_receipt,
            "context_authority_receipt_hash": self.context_authority_receipt_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TerminationState:
        if value.get("schema_version") not in {1, 2, 3, 4}:
            raise ValueError("unsupported ReAct checkpoint schema")
        return cls(
            started_at=_float(value["started_at"]),
            last_observed_at=_float(value["last_observed_at"]),
            provider_turns_reserved_total=_int(value["provider_turns_reserved_total"]),
            tool_calls_reserved_total=_int(value["tool_calls_reserved_total"]),
            repeat_key=_optional_string(value.get("repeat_key")),
            repeat_streak=_int(value["repeat_streak"]),
            phase=str(value["phase"]),
            provider_request_id=_optional_string(value.get("provider_request_id")),
            tool_batch_id=_optional_string(value.get("tool_batch_id")),
            context_revision=(
                None if value.get("context_revision") is None else _int(value["context_revision"])
            ),
            provider_request_snapshot=value.get("provider_request_snapshot"),  # type: ignore[arg-type]
            provider_request_fingerprint=_optional_string(
                value.get("provider_request_fingerprint")
            ),
            provider_response_snapshot=value.get("provider_response_snapshot"),  # type: ignore[arg-type]
            provider_response_digest=_optional_string(value.get("provider_response_digest")),
            tool_result_progress=_int(value.get("tool_result_progress", 0)),
            workflow_spawn_wait_receipt_id=_optional_string(
                value.get("workflow_spawn_wait_receipt_id")
            ),
            pending_child_completion=value.get("pending_child_completion"),  # type: ignore[arg-type]
            pending_child_completion_hash=_optional_string(
                value.get("pending_child_completion_hash")
            ),
            pending_child_completion_append_id=_optional_string(
                value.get("pending_child_completion_append_id")
            ),
            last_workflow_spawn_wait_receipt_id=_optional_string(
                value.get("last_workflow_spawn_wait_receipt_id")
            ),
            workflow_catalog_selection=value.get("workflow_catalog_selection"),  # type: ignore[arg-type]
            workflow_catalog_selection_hash=_optional_string(
                value.get("workflow_catalog_selection_hash")
            ),
            tool_exposure_state=value.get("tool_exposure_state"),  # type: ignore[arg-type]
            policy_fingerprint=str(value.get("policy_fingerprint") or ""),
            route_state=str(value.get("route_state") or "unrouted"),
            route_receipt=value.get("route_receipt"),  # type: ignore[arg-type]
            route_receipt_hash=_optional_string(value.get("route_receipt_hash")),
            context_authority_receipt=value.get("context_authority_receipt"),  # type: ignore[arg-type]
            context_authority_receipt_hash=_optional_string(
                value.get("context_authority_receipt_hash")
            ),
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("checkpoint identity must be a non-empty string")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("checkpoint integer is malformed")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("checkpoint number is malformed")
    return float(value)


def _check_common(
    state: TerminationState,
    limits: TerminationLimits,
    *,
    now: float,
    budget: BudgetSnapshot,
) -> None:
    assert state.last_observed_at is not None
    if not math.isfinite(now) or now < state.last_observed_at:
        raise TerminationBudgetExceeded(TerminationReason.WALL_CLOCK)
    if now - state.started_at >= limits.max_wall_seconds:
        raise TerminationBudgetExceeded(TerminationReason.WALL_CLOCK)
    if budget.has_unknown_charge or (
        budget.committed_micros + budget.reserved_micros >= limits.max_cost_micros
    ):
        raise TerminationBudgetExceeded(TerminationReason.COST)


__all__ = (
    "TerminationBudgetExceeded",
    "TerminationLimits",
    "TerminationReason",
    "TerminationState",
)
