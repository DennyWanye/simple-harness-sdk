# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable BaseAgent configuration contracts (BA-v1.0 §4.1, Slice 1 subset).

Slice 1 deliberately omits ``ContextPolicy`` (bounded Context arrives in Slice 3).
Per-turn limits are recorded on the configuration; the Slice 1 driver still
enforces them through the Run-level termination totals plus the per-turn provider
ordinal range recorded in ``base_agent_turns_v1`` (see journal known gaps).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields
from typing import Literal

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.runtime.termination import TerminationLimits

MAX_NAME_LENGTH = 128
MAX_INSTRUCTIONS_LENGTH = 32_768
MAX_MODEL_PROFILE_REF_LENGTH = 128
MAX_TOOL_NAMES = 256
MAX_TOOL_NAME_LENGTH = 128


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _bounded_text(value: object, name: str, limit: int, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if "\x00" in value or len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters without NUL")
    if not allow_blank and not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """Execution guard-rails for one BaseAgent instance.

    ``*_per_turn`` values bound a single AgentTurn; ``lifetime_*`` values bound the
    whole instance and feed the Run-level ``TerminationLimits`` (totals never reset).
    """

    max_pending_inputs: int = 8
    max_model_calls_per_turn: int = 32
    max_tool_calls_per_turn: int = 64
    turn_deadline_seconds: float = 900.0
    lifetime_cost_limit_micros: int | None = None
    max_delegations_per_turn: int = 1
    delegation_wait_seconds: float = 20.0
    lifetime_model_calls: int = 10_000
    lifetime_tool_calls: int = 20_000
    lifetime_wall_seconds: float = 365.0 * 86_400.0

    def __post_init__(self) -> None:
        for name in (
            "max_pending_inputs",
            "max_model_calls_per_turn",
            "max_tool_calls_per_turn",
            "max_delegations_per_turn",
            "lifetime_model_calls",
            "lifetime_tool_calls",
        ):
            _positive_int(getattr(self, name), name)
        for name in ("turn_deadline_seconds", "delegation_wait_seconds", "lifetime_wall_seconds"):
            object.__setattr__(self, name, _positive_float(getattr(self, name), name))
        if self.lifetime_cost_limit_micros is not None:
            _positive_int(self.lifetime_cost_limit_micros, "lifetime_cost_limit_micros")
        if self.lifetime_model_calls < self.max_model_calls_per_turn:
            raise ValueError("lifetime_model_calls must cover at least one full turn")
        if self.lifetime_tool_calls < self.max_tool_calls_per_turn:
            raise ValueError("lifetime_tool_calls must cover at least one full turn")

    def to_json(self) -> dict[str, JsonValue]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def termination_limits(self) -> TerminationLimits:
        """Run-level limits handed to the ReAct core (lifetime totals, never per turn)."""

        return TerminationLimits(
            max_turns=self.lifetime_model_calls,
            max_tool_calls=self.lifetime_tool_calls,
            max_wall_seconds=self.lifetime_wall_seconds,
            max_cost_micros=(
                10_000_000_000
                if self.lifetime_cost_limit_micros is None
                else self.lifetime_cost_limit_micros
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable per-instance configuration; hashed into ``config_hash`` on creation."""

    name: str
    instructions: str
    model_profile_ref: str
    tool_names: tuple[str, ...] = ()
    limits: AgentLimits = AgentLimits()
    short_memory_mode: Literal["hybrid"] = "hybrid"

    def __post_init__(self) -> None:
        _bounded_text(self.name, "name", MAX_NAME_LENGTH)
        _bounded_text(self.instructions, "instructions", MAX_INSTRUCTIONS_LENGTH, allow_blank=True)
        _bounded_text(self.model_profile_ref, "model_profile_ref", MAX_MODEL_PROFILE_REF_LENGTH)
        if isinstance(self.tool_names, str) or not isinstance(self.tool_names, (tuple, list)):
            raise TypeError("tool_names must be a tuple of strings")
        names = tuple(self.tool_names)
        if len(names) > MAX_TOOL_NAMES:
            raise ValueError(f"tool_names must contain at most {MAX_TOOL_NAMES} entries")
        for name in names:
            _bounded_text(name, "tool name", MAX_TOOL_NAME_LENGTH)
        if len(set(names)) != len(names):
            raise ValueError("tool_names must be unique")
        object.__setattr__(self, "tool_names", names)
        if not isinstance(self.limits, AgentLimits):
            raise TypeError("limits must use AgentLimits")
        if self.short_memory_mode != "hybrid":
            raise ValueError("short_memory_mode must be 'hybrid'")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "instructions": self.instructions,
            "model_profile_ref": self.model_profile_ref,
            "tool_names": list(self.tool_names),
            "limits": self.limits.to_json(),
            "short_memory_mode": self.short_memory_mode,
        }

    @classmethod
    def from_json(cls, value: object) -> AgentConfig:
        if not isinstance(value, dict):
            raise TypeError("AgentConfig JSON must be an object")
        limits_value = value.get("limits", {})
        if not isinstance(limits_value, dict):
            raise TypeError("limits must be an object")
        tool_names = value.get("tool_names", [])
        if not isinstance(tool_names, list):
            raise TypeError("tool_names must be a list")
        return cls(
            name=value.get("name"),  # type: ignore[arg-type]
            instructions=value.get("instructions"),  # type: ignore[arg-type]
            model_profile_ref=value.get("model_profile_ref"),  # type: ignore[arg-type]
            tool_names=tuple(tool_names),
            limits=AgentLimits(**limits_value),
            short_memory_mode=value.get("short_memory_mode", "hybrid"),  # type: ignore[arg-type]
        )


# Frozen field inventory: user memory / mission / task fields are forbidden by BA-v1.0 §4.1.
AGENT_CONFIG_FIELDS = frozenset(
    {"name", "instructions", "model_profile_ref", "tool_names", "limits", "short_memory_mode"}
)


def config_hash(config: AgentConfig) -> str:
    """Canonical SHA-256 of the configuration; identical inputs hash identically."""

    if not isinstance(config, AgentConfig):
        raise TypeError("config must use AgentConfig")
    return hashlib.sha256(canonical_json(config.to_json()).encode("utf-8")).hexdigest()


__all__ = ("AGENT_CONFIG_FIELDS", "AgentConfig", "AgentLimits", "config_hash")
