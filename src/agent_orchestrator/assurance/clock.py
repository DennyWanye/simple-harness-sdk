# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure persisted clock transition. Caller writes with the original Commit receipt."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import integer, one_of


@dataclass(frozen=True, slots=True)
class ClockState:
    generation: int
    wall_high_ms: int
    state: str

    def __post_init__(self) -> None:
        integer(self.generation)
        integer(self.wall_high_ms)
        one_of(self.state, {"STABLE", "ROLLBACK"})

    def observe(self, now_ms: int) -> ClockState:
        integer(now_ms)
        if now_ms < self.wall_high_ms:
            return ClockState(
                self.generation + (self.state == "STABLE"), self.wall_high_ms, "ROLLBACK"
            )
        return ClockState(self.generation, max(self.wall_high_ms, now_ms), "STABLE")
