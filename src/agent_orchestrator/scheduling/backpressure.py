# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Backpressure (§18.5, theory 11-9/11-11, ORCH-BUILD §8.2, plan D6-2).

"Budget = how much the whole run may spend; Backpressure = how fast it may go right
now" (theory 11-10).  This module is the single registry of the six caps §18.5 asks
for — running Agents, waiting work, results waiting for verification, Task DAG depth,
Attempts per Task, sub-task proposals per Agent — and the state machine that turns an
observation into a *raised* / *normal* signal.

The watermarks are this build's convention (plan §6.1, no verbatim source): a
dimension is raised when its observation reaches the cap (``high``) and is only
cleared again once the observation has fallen to ``low = floor(high × ratio)`` —
between the two the previous level is kept, which is the hysteresis ORCH-BUILD asks
for ("恢复有滞回").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

BACKPRESSURE_VERSION = "backpressure-v1"
NORMAL = "NORMAL"
RAISED = "RAISED"
STATE_KEY = "backpressure"
# the three dimensions this module observes itself; the other three §18.5 caps are
# enforced at the graph / attempt level (step 5 knobs) and only *registered* here
OBSERVED_DIMENSIONS = ("running_attempts", "pending_dispatch", "pending_verifications")


@dataclass(frozen=True, slots=True)
class BackpressureLimits:
    """§18.5's six caps in one place (plan D6-2)."""

    max_running_attempts: int = 8
    max_pending_dispatch: int = 8
    max_pending_verifications: int = 4
    max_graph_depth: int = 6  # step 5 knob, registered here
    max_attempts_per_task: int | None = (
        None  # per Task budget.max_attempts governs; None = no global cap
    )
    max_proposals_per_agent: int = 3  # step 5 knob, registered here
    low_watermark_ratio: float = 0.5

    def __post_init__(self) -> None:
        for name in ("max_running_attempts", "max_pending_dispatch", "max_pending_verifications"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not 0.0 <= self.low_watermark_ratio < 1.0:
            raise ValueError("low_watermark_ratio must be in [0, 1)")

    def high(self, dimension: str) -> int:
        return int(
            {
                "running_attempts": self.max_running_attempts,
                "pending_dispatch": self.max_pending_dispatch,
                "pending_verifications": self.max_pending_verifications,
            }[dimension]
        )

    def low(self, dimension: str) -> int:
        return int(self.high(dimension) * self.low_watermark_ratio)

    def to_json(self) -> dict[str, Any]:
        return {
            "max_running_attempts": self.max_running_attempts,
            "max_pending_dispatch": self.max_pending_dispatch,
            "max_pending_verifications": self.max_pending_verifications,
            "max_graph_depth": self.max_graph_depth,
            "max_attempts_per_task": self.max_attempts_per_task,
            "max_proposals_per_agent": self.max_proposals_per_agent,
            "low_watermark_ratio": self.low_watermark_ratio,
            "version": BACKPRESSURE_VERSION,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """What the queues look like right now (theory 12-1: running / waiting / to verify)."""

    running_attempts: int
    pending_dispatch: int
    pending_verifications: int
    observed_at: float

    def value(self, dimension: str) -> int:
        return int(getattr(self, dimension))

    def to_json(self) -> dict[str, Any]:
        return {
            "running_attempts": self.running_attempts,
            "pending_dispatch": self.pending_dispatch,
            "pending_verifications": self.pending_verifications,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class Transition:
    dimension: str
    to_level: str  # RAISED | NORMAL
    observed: int
    high: int
    low: int

    @property
    def event_type(self) -> str:
        return "BackpressureRaised" if self.to_level == RAISED else "BackpressureCleared"

    def to_json(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "level": self.to_level,
            "observed": self.observed,
            "high": self.high,
            "low": self.low,
            "version": BACKPRESSURE_VERSION,
        }


@dataclass(frozen=True, slots=True)
class BackpressureState:
    """Durable signal: which dimensions are raised, since when, and the last observation."""

    level: str = NORMAL
    raised: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    since: float | None = None
    observation: Mapping[str, Any] | None = None
    changes: int = 0
    version: str = BACKPRESSURE_VERSION

    @property
    def is_raised(self) -> bool:
        return self.level == RAISED

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "raised": {k: dict(v) for k, v in self.raised.items()},
            "since": self.since,
            "observation": None if self.observation is None else dict(self.observation),
            "changes": self.changes,
            "version": self.version,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> BackpressureState:
        if not value:
            return cls()
        return cls(
            level=str(value.get("level", NORMAL)),
            raised={str(k): dict(v) for k, v in dict(value.get("raised") or {}).items()},
            since=value.get("since"),
            observation=value.get("observation"),
            changes=int(value.get("changes", 0)),
            version=str(value.get("version", BACKPRESSURE_VERSION)),
        )


def evaluate(
    previous: BackpressureState, observation: Observation, limits: BackpressureLimits
) -> tuple[BackpressureState, list[Transition]]:
    """One step of the watermark state machine; pure, so it can be unit-tested."""

    raised: dict[str, dict[str, Any]] = {k: dict(v) for k, v in previous.raised.items()}
    transitions: list[Transition] = []
    for dimension in OBSERVED_DIMENSIONS:
        observed = observation.value(dimension)
        high, low = limits.high(dimension), limits.low(dimension)
        was_raised = dimension in raised
        if not was_raised and observed >= high:
            raised[dimension] = {
                "observed": observed,
                "high": high,
                "low": low,
                "since": observation.observed_at,
            }
            transitions.append(Transition(dimension, RAISED, observed, high, low))
        elif was_raised and observed <= low:
            del raised[dimension]
            transitions.append(Transition(dimension, NORMAL, observed, high, low))
        elif was_raised:
            raised[dimension]["observed"] = observed
    level = RAISED if raised else NORMAL
    since = previous.since
    if level != previous.level:
        since = observation.observed_at
    return (
        BackpressureState(
            level=level,
            raised=raised,
            since=since,
            observation=observation.to_json(),
            changes=previous.changes + len(transitions),
        ),
        transitions,
    )


__all__ = (
    "BACKPRESSURE_VERSION",
    "NORMAL",
    "OBSERVED_DIMENSIONS",
    "RAISED",
    "STATE_KEY",
    "BackpressureLimits",
    "BackpressureState",
    "Observation",
    "Transition",
    "evaluate",
)
