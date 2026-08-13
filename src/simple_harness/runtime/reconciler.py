# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict startup reconciliation sequence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class ReconciliationPhase(StrEnum):
    PROVIDER = "provider"
    EFFECTS = "effects"
    CHILD_SIGNALS = "child_signals"
    DELIVERIES = "deliveries"
    RECOVERABLE_RUNS = "recoverable_runs"


STARTUP_RECONCILIATION_ORDER = tuple(ReconciliationPhase)
ReconciliationStep = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StartupReconciliationSteps:
    provider: ReconciliationStep
    effects: ReconciliationStep
    child_signals: ReconciliationStep
    deliveries: ReconciliationStep
    recoverable_runs: ReconciliationStep


class StartupReconciler:
    def __init__(self, steps: StartupReconciliationSteps) -> None:
        self._steps = steps

    async def run(self) -> tuple[ReconciliationPhase, ...]:
        completed: list[ReconciliationPhase] = []
        for phase in STARTUP_RECONCILIATION_ORDER:
            step = getattr(self._steps, phase.value)
            await step()
            completed.append(phase)
        return tuple(completed)

    async def reconcile(self) -> None:
        """Satisfy the Runtime reconciliation Port without losing test receipts."""

        await self.run()


__all__ = (
    "STARTUP_RECONCILIATION_ORDER",
    "ReconciliationPhase",
    "StartupReconciler",
    "StartupReconciliationSteps",
)
