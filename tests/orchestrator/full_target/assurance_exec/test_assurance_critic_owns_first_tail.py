# SPDX-License-Identifier: Apache-2.0
"""An Assurance TASK_CONTENT review may use its Attempt's protected first-Critic
budget only when the durable review binding proves it reviews that Attempt's Result.

Host real-model run 12 (2026-09-23, Grok lane): the Assurance transport names its
Critic intent ``<mission>:assurance:<review key>:<n>``; the protected tail accepted
only ``<attempt>:critic:<n>`` and refused every assured review, forever.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.protected_tail_commits import ProtectedTailCommitsMixin

MISSION, ATTEMPT, OTHER = "mission-1", "task-1:attempt-1", "task-2:attempt-1"
KEY = "assurance-content:" + "a" * 64
SUBJECT = f"{MISSION}:assurance:{KEY}:1"
check = ProtectedTailCommitsMixin._protected_critic_subject


def _store(purpose="TASK_CONTENT", result_attempt=ATTEMPT, with_table=True, with_row=True):
    connection = sqlite3.connect(":memory:")
    if with_table:
        connection.execute("CREATE TABLE assurance_review_bindings (review_key TEXT, mission_id TEXT, binding_json TEXT)")
        if with_row:
            body = {"subject": {"purpose": purpose, "target": {"kind": "result", "pin": {"id": "result-1"}}}}
            connection.execute("INSERT INTO assurance_review_bindings VALUES (?,?,?)", (KEY, MISSION, json.dumps(body)))
    results = {"result-1": SimpleNamespace(envelope=SimpleNamespace(mission_id=MISSION, attempt_id=result_attempt))}
    return SimpleNamespace(connection=connection, get_result=results.get)


def test_ordinary_critic_subject_unchanged():
    check(ATTEMPT, f"{ATTEMPT}:critic:1")
    check(ATTEMPT, f"{ATTEMPT}:critic:2", _store())
    with pytest.raises(BudgetError):
        check(ATTEMPT, f"{OTHER}:critic:1", _store())
    with pytest.raises(BudgetError):
        check(ATTEMPT, f"{ATTEMPT}:critic:0")


def test_assurance_subject_proven_by_binding_is_accepted():
    check(ATTEMPT, SUBJECT, _store())
    check(ATTEMPT, f"{MISSION}:assurance:{KEY}:2", _store())


@pytest.mark.parametrize("store", [
    None,
    _store(result_attempt=OTHER),
    _store(purpose="METHOD_PLAN"),
    _store(with_row=False),
    _store(with_table=False),
])
def test_assurance_subject_without_proof_is_refused(store):
    with pytest.raises(BudgetError):
        check(ATTEMPT, SUBJECT, store)


def test_assurance_subject_shape_is_exact():
    with pytest.raises(BudgetError):
        check(ATTEMPT, f"{MISSION}:assurance:{KEY}:3", _store())
    with pytest.raises(BudgetError):
        check(ATTEMPT, f"other-mission:assurance:{KEY}:1", _store())
