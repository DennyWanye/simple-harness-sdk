# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.runtime.reconciler import (
    ReconciliationPhase,
    StartupReconciler,
    StartupReconciliationSteps,
)


def test_startup_reconciliation_has_strict_dependency_order() -> None:
    trace: list[str] = []

    def step(name: str):
        async def run() -> None:
            trace.append(name)

        return run

    phases = asyncio.run(
        StartupReconciler(
            StartupReconciliationSteps(
                provider=step("provider"),
                effects=step("effects"),
                child_signals=step("child_signals"),
                deliveries=step("deliveries"),
                recoverable_runs=step("recoverable_runs"),
            )
        ).run()
    )

    assert phases == tuple(ReconciliationPhase)
    assert trace == [
        "provider",
        "effects",
        "child_signals",
        "deliveries",
        "recoverable_runs",
    ]


def test_reconciliation_stops_before_downstream_phase_after_failure() -> None:
    trace: list[str] = []

    def step(name: str, *, fail: bool = False):
        async def run() -> None:
            trace.append(name)
            if fail:
                raise RuntimeError(name)

        return run

    reconciler = StartupReconciler(
        StartupReconciliationSteps(
            provider=step("provider"),
            effects=step("effects", fail=True),
            child_signals=step("child_signals"),
            deliveries=step("deliveries"),
            recoverable_runs=step("recoverable_runs"),
        )
    )

    with pytest.raises(RuntimeError, match="effects"):
        asyncio.run(reconciler.run())
    assert trace == ["provider", "effects"]


def test_startup_reconciler_satisfies_runtime_port_without_reordering() -> None:
    trace: list[str] = []

    def step(name: str):
        async def run() -> None:
            trace.append(name)

        return run

    reconciler = StartupReconciler(
        StartupReconciliationSteps(
            provider=step("provider"),
            effects=step("effects"),
            child_signals=step("child_signals"),
            deliveries=step("deliveries"),
            recoverable_runs=step("recoverable_runs"),
        )
    )
    asyncio.run(reconciler.reconcile())
    assert trace == [phase.value for phase in ReconciliationPhase]
