# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §0.6 overlap 3: an operation review is not a Planner round.

Operation proposal / outcome reviews ride on ``plan`` intents and run in the middle
of a Mission; counting them as "the Planner was already asked" held another
branch's repair round until an unrelated publish had been reviewed.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _in_flight(*roles):
    intents = [SimpleNamespace(kind="plan", mission_id="m1", config={"role": role} if role else {})
               for role in roles]
    fake = SimpleNamespace(store=SimpleNamespace(list_intents=lambda *states: intents))
    return Orchestrator._planner_intents_in_flight(fake, "m1")


def test_only_planner_and_root_review_rounds_count():
    assert _in_flight(None)
    assert _in_flight("root_reviewer")
    assert not _in_flight("method_synthesizer")
    assert not _in_flight("operation_proposal_reviewer", "operation_outcome_reviewer")
    assert not _in_flight()


def test_a_deferred_planner_round_survives_a_restart(tmp_path):
    """NEXT-TG-1.0 §3.6: the pool-cooldown deferral was memory only; a restart
    forgot the round. It is kept in scheduler_state, ``since`` included."""

    from agent_orchestrator.orchestrator.event_handler import DeferredPlanning
    from agent_orchestrator.storage.store import Store

    store = Store.open(tmp_path / "o.db")
    first = DeferredPlanning()
    first.bind(store)
    first["m1"] = (100.0, 2)
    first["m2"] = (200.0, 1)
    first.pop("m2")
    second = DeferredPlanning()
    second.bind(store)
    assert dict(second) == {"m1": (100.0, 2)}
    version = store.connection.execute(
        "SELECT version FROM scheduler_state WHERE key='deferred_planning'").fetchone()[0]
    second["m1"] = (100.0, 2)  # re-setting the same wait writes nothing
    assert store.connection.execute(
        "SELECT version FROM scheduler_state WHERE key='deferred_planning'").fetchone()[0] == version
