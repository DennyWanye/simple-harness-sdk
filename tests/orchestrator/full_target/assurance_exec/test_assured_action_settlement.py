# SPDX-License-Identifier: Apache-2.0
"""An assured Action reservation settles on the original operation proof.

Real run 2026-09-28 (mission-e5f82ae8c9f24ff8): the publish succeeded, the root
was resolved, but the Action's reservation was held forever ("original executor
is not closed" — an Action has no Agent intent) and the closeout drained on
OPEN_RESERVATIONS.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator import assurance_settlement, taskgraph_action_settlement
from agent_orchestrator.runtime.planning_operations import SourceUnavailable


class _Connection:
    in_transaction = True


class _Store:
    connection = _Connection()

    def __init__(self, actions):
        self._actions = actions

    def get_intent_for_subject(self, subject_id):
        return None

    def list_actions(self, mission_id):
        return list(self._actions)


def _settlement(actions):
    settlement = assurance_settlement.AssuranceSettlement.__new__(
        assurance_settlement.AssuranceSettlement
    )
    settlement.orchestrator = SimpleNamespace(store=_Store(actions))
    return settlement


def test_action_reservation_uses_the_original_operation_proof(monkeypatch):
    calls = []
    monkeypatch.setattr(
        taskgraph_action_settlement, "require_action_settlement",
        lambda orchestrator, subject, mission: calls.append((subject, mission)),
    )
    settlement = _settlement([{"reservation_subject": "action:a:v1"}])
    settlement.require_settled_locked("action:a:v1", "m-1")
    assert calls == [("action:a:v1", "m-1")]


def test_unresolved_or_unreadable_action_proof_keeps_the_hold(monkeypatch):
    def unresolved(orchestrator, subject, mission):
        raise BudgetError("TASKGRAPH_ACTION_PHYSICAL_WORK_UNRESOLVED")

    monkeypatch.setattr(taskgraph_action_settlement, "require_action_settlement", unresolved)
    settlement = _settlement([{"reservation_subject": "action:a:v1"}])
    with pytest.raises(BudgetError):
        settlement.require_settled_locked("action:a:v1", "m-1")

    def missing(orchestrator, subject, mission):
        raise SourceUnavailable("taskgraph_action_settlement_source_missing")

    monkeypatch.setattr(taskgraph_action_settlement, "require_action_settlement", missing)
    with pytest.raises(BudgetError):
        settlement.require_settled_locked("action:a:v1", "m-1")


def test_a_subject_that_is_neither_intent_nor_action_is_still_held():
    settlement = _settlement([{"reservation_subject": "action:other:v1"}])
    with pytest.raises(BudgetError, match="executor is not closed"):
        settlement.require_settled_locked("action:a:v1", "m-1")
