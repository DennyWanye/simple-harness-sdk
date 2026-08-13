# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Evidence-bound reconciliation for uncertain Tool effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .authorization import PreparedToolEffect
from .contracts import ToolResult


class ReconciliationState(StrEnum):
    CONFIRMED_NOT_STARTED = "confirmed_not_started"
    COMPLETED = "completed"
    STILL_UNKNOWN = "still_unknown"


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    state: ReconciliationState
    evidence_ref: str
    result: ToolResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReconciliationState):
            object.__setattr__(self, "state", ReconciliationState(self.state))
        if not self.evidence_ref.strip():
            raise ValueError("evidence_ref is required")
        if self.state is ReconciliationState.COMPLETED and self.result is None:
            raise ValueError("completed observation requires result")
        if self.state is not ReconciliationState.COMPLETED and self.result is not None:
            raise ValueError("only completed observation may carry result")


@runtime_checkable
class ToolReconciliationPort(Protocol):
    async def observe(
        self, effect: PreparedToolEffect
    ) -> ReconciliationObservation: ...


__all__ = (
    "ReconciliationObservation",
    "ReconciliationState",
    "ToolReconciliationPort",
)
