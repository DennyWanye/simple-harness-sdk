# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Step 2 · slice A2/A5: single-writer store with CAS and idempotent events; budget
Reserve/Settle on the account chain; usage facts imported at most once; unpriced
settlements never written as zero."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import Budget, Event, Mission, MissionStatus
from agent_orchestrator.governance.budgets import BudgetExhausted, BudgetLedger, UsageFact
from agent_orchestrator.storage.store import InjectedCrash, Store, StoreConflict


def _mission(mission_id="mission-1"):
    return Mission(
        id=mission_id,
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(max_tokens=1000, max_attempts=2),
        tenant_id="t",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key=mission_id,
    )


def _event(key, mission_id="mission-1"):
    return Event(
        id=f"event-{key}",
        type="Probe",
        trace_id="trace",
        mission_id=mission_id,
        task_id=None,
        attempt_id=None,
        actor_type="system",
        actor_id="test",
        payload={"k": key},
        idempotency_key=key,
        created_at=1.0,
    )


def test_store_opens_validates_and_reopens(tmp_path):
    path = tmp_path / "orchestrator.db"
    store = Store.open(path)
    store.insert_mission(_mission(), spec_hash="h")
    store.close()
    again = Store.open(path)
    assert again.get_mission("mission-1").status is MissionStatus.CREATED
    again.close()
    (tmp_path / "foreign.db").write_bytes(b"")
    import sqlite3

    sqlite3.connect(tmp_path / "foreign.db").execute("CREATE TABLE x(a)")
    with pytest.raises(Exception):
        Store.open(tmp_path / "foreign.db")


def test_cas_and_idempotent_events(tmp_path):
    store = Store.open(tmp_path / "o.db")
    mission = _mission()
    store.insert_mission(mission, spec_hash="h")
    updated = Mission(
        **{**mission.to_json(), "status": "PLANNING", "version": 2, "budget": mission.budget}
    )
    store.update_mission(updated, expected_version=1)
    with pytest.raises(StoreConflict):
        store.update_mission(updated, expected_version=1)
    first = store.append_event(_event("MissionCreated:mission-1"))
    second = store.append_event(_event("MissionCreated:mission-1"))
    assert first.seq == second.seq == 1
    assert store.count_events("mission-1") == 1
    # a crash inside a transaction rolls everything back
    store.arm("probe")
    with pytest.raises(InjectedCrash):
        with store.transaction():
            store.append_event(_event("Second"))
            store.fault("probe")
    assert store.count_events("mission-1") == 1
    assert store.fired == ["probe"]


def test_budget_chain_reserve_settle_and_unpriced(tmp_path):
    store = Store.open(tmp_path / "o.db")
    ledger = BudgetLedger(store)
    with store.transaction():
        ledger.open_account(
            account_id="budget:mission-1",
            scope="mission",
            parent_id=None,
            mission_id="mission-1",
            limits=Budget(max_tokens=1000, max_attempts=2),
        )
        ledger.open_account(
            account_id="budget:task-1",
            scope="task",
            parent_id="budget:mission-1",
            mission_id="mission-1",
            limits=Budget(max_tokens=600, max_attempts=2),
        )
        first = ledger.reserve(
            account_id="budget:task-1",
            subject_id="attempt-1",
            tokens=500,
            cost_micros=0,
            counts_attempt=True,
        )
        assert (
            ledger.reserve(
                account_id="budget:task-1",
                subject_id="attempt-1",
                tokens=500,
                cost_micros=0,
                counts_attempt=True,
            )
            == first
        )
        assert ledger.account("budget:mission-1").reserved_tokens == 500
        with pytest.raises(BudgetExhausted) as exc:  # task account has 100 left
            ledger.reserve(
                account_id="budget:task-1",
                subject_id="attempt-2",
                tokens=200,
                cost_micros=0,
                counts_attempt=True,
            )
        assert exc.value.dimension == "tokens"
        assert (
            ledger.import_usage(
                subject_id="attempt-1",
                mission_id="mission-1",
                facts=[
                    UsageFact("provider-request:r1", 120, 30, None),
                    UsageFact("provider-request:r1", 120, 30, None),
                ],
            )
            == 1
        )
        settled = ledger.settle(subject_id="attempt-1")
        assert (
            settled["settled_tokens"] == 150
            and settled["settled_cost_micros"] is None
            and settled["unpriced"] == 1
        )
        mission_account = ledger.account("budget:mission-1")
        assert mission_account.reserved_tokens == 0 and mission_account.settled_tokens == 150
        assert (
            mission_account.unpriced_settlements == 1 and mission_account.settled_cost_micros == 0
        )
        # attempts are counted on the chain and enforced
        ledger.reserve(
            account_id="budget:task-1",
            subject_id="attempt-2",
            tokens=100,
            cost_micros=0,
            counts_attempt=True,
        )
        with pytest.raises(BudgetExhausted) as exc:
            ledger.reserve(
                account_id="budget:task-1",
                subject_id="attempt-3",
                tokens=1,
                cost_micros=0,
                counts_attempt=True,
            )
        assert exc.value.dimension == "attempts"
    report = ledger.costs_report("mission-1")
    assert len(report["usage"]) == 1 and report["usage"][0]["unpriced"] == 1
