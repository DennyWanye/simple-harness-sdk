# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import CallId
from simple_harness.tools import (
    ReconciliationObservation,
    ReconciliationState,
    ToolResult,
)


def test_reconciliation_has_exactly_three_states_and_requires_evidence() -> None:
    assert set(ReconciliationState) == {
        ReconciliationState.CONFIRMED_NOT_STARTED,
        ReconciliationState.COMPLETED,
        ReconciliationState.STILL_UNKNOWN,
    }
    with pytest.raises(ValueError):
        ReconciliationObservation(ReconciliationState.STILL_UNKNOWN, "")


def test_completed_observation_requires_result() -> None:
    with pytest.raises(ValueError):
        ReconciliationObservation(ReconciliationState.COMPLETED, "evidence:1")

    observation = ReconciliationObservation(
        ReconciliationState.COMPLETED,
        "evidence:2",
        ToolResult.succeeded(CallId("call-1"), {"ok": True}),
    )

    assert observation.result is not None
    assert observation.result.outcome.value == "succeeded"


def test_unknown_or_not_started_cannot_smuggle_a_result() -> None:
    for state in (
        ReconciliationState.CONFIRMED_NOT_STARTED,
        ReconciliationState.STILL_UNKNOWN,
    ):
        with pytest.raises(ValueError):
            ReconciliationObservation(
                state,
                "evidence:3",
                ToolResult.succeeded(CallId("call-1")),
            )
