# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 (Host 真机): the planning bound counts one question, not a life.

A seven-step Mission answered six repair rounds, then died on PLANNING_BOUND_REACHED
because two provider hiccups in an early round and one reply that spelt a severity
``"high"`` were all charged against the same lifetime bound of two.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import UncertaintySeverity, enum_of
from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _count(*events: tuple[str, dict]) -> int:
    store = SimpleNamespace(list_events=lambda mission_id: [
        SimpleNamespace(type=kind, payload=payload) for kind, payload in events
    ])
    return Orchestrator._planning_attempts(SimpleNamespace(store=store), "m1")


def test_refusals_before_the_first_commit_count_as_before() -> None:
    assert _count(("PlanningRejected", {}), ("TaskGraphRejected", {})) == 2


def test_a_committed_decision_starts_the_next_question_at_zero() -> None:
    assert _count(
        ("PlanningRejected", {}), ("PlanningRejected", {}),
        ("PlanningDecisionEvaluated", {"status": "COMMITTED"}),
        ("PlanningRejected", {}),
        ("PlanningDecisionEvaluated", {"status": "REJECTED"}),
    ) == 1


def test_a_severity_in_another_case_is_that_severity() -> None:
    assert enum_of(UncertaintySeverity, "high", "uncertainty.severity") is UncertaintySeverity.HIGH
    with pytest.raises(ContractError, match="must be one of"):
        enum_of(UncertaintySeverity, "severe", "uncertainty.severity")
