# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · D6-2 unit: the watermark state machine (raise at high, clear only at low —
the hysteresis ORCH-BUILD §8.2 asks for), the §18.5 cap registry, and the durable state
document; D6-8 / D6-13: schema v4 and the tool-call budget dimension."""

from __future__ import annotations

import sqlite3

import pytest

from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.budgets import BudgetExhausted, BudgetLedger
from agent_orchestrator.scheduling.backpressure import (
    BACKPRESSURE_VERSION,
    NORMAL,
    OBSERVED_DIMENSIONS,
    RAISED,
    BackpressureLimits,
    BackpressureState,
    Observation,
    evaluate,
)
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store


def obs(pv=0, run=0, disp=0, at=1.0):
    return Observation(
        running_attempts=run, pending_dispatch=disp, pending_verifications=pv, observed_at=at
    )


def test_the_six_caps_are_registered_and_watermarks_derive_from_them():
    limits = BackpressureLimits(max_pending_verifications=4, low_watermark_ratio=0.5)
    assert set(limits.to_json()) >= {
        "max_running_attempts",
        "max_pending_dispatch",
        "max_pending_verifications",
        "max_graph_depth",
        "max_attempts_per_task",
        "max_proposals_per_agent",
    }  # §18.5's six caps, one registry
    assert limits.high("pending_verifications") == 4 and limits.low("pending_verifications") == 2
    assert OBSERVED_DIMENSIONS == ("running_attempts", "pending_dispatch", "pending_verifications")
    with pytest.raises(ValueError):
        BackpressureLimits(low_watermark_ratio=1.0)


def test_raise_at_high_hold_in_between_clear_only_at_low():
    limits = BackpressureLimits(max_pending_verifications=4, low_watermark_ratio=0.5)
    state = BackpressureState()
    state, t = evaluate(state, obs(pv=3, at=1.0), limits)
    assert state.level == NORMAL and t == []
    state, t = evaluate(state, obs(pv=4, at=2.0), limits)  # reaches high → raised
    assert state.level == RAISED and [x.event_type for x in t] == ["BackpressureRaised"]
    assert t[0].dimension == "pending_verifications" and t[0].high == 4 and t[0].low == 2
    assert state.since == 2.0
    state, t = evaluate(state, obs(pv=3, at=3.0), limits)  # between low and high → still raised
    assert (
        state.level == RAISED and t == [] and state.raised["pending_verifications"]["observed"] == 3
    )
    state, t = evaluate(state, obs(pv=2, at=4.0), limits)  # at low → cleared
    assert state.level == NORMAL and [x.event_type for x in t] == ["BackpressureCleared"]
    assert state.since == 4.0 and state.changes == 2
    # a round trip through JSON keeps the signal
    again = BackpressureState.from_json(state.to_json())
    assert again == state and again.version == BACKPRESSURE_VERSION


def test_two_dimensions_raise_and_clear_independently():
    limits = BackpressureLimits(
        max_running_attempts=2, max_pending_verifications=2, low_watermark_ratio=0.5
    )
    state, t = evaluate(BackpressureState(), obs(run=2, pv=2, at=1.0), limits)
    assert {x.dimension for x in t} == {"running_attempts", "pending_verifications"}
    state, t = evaluate(state, obs(run=0, pv=2, at=2.0), limits)
    assert [x.dimension for x in t] == ["running_attempts"] and state.level == RAISED
    state, t = evaluate(state, obs(run=0, pv=1, at=3.0), limits)
    assert state.level == NORMAL and [x.dimension for x in t] == ["pending_verifications"]


# ------------------------------------------------------------------ D6-13 schema v4
def _v3_library(path):
    connection = sqlite3.connect(path)
    for migration in schema.MIGRATIONS[:3]:
        for statement in migration.ddl.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
            (migration.version, migration.name, migration.checksum, 1.0),
        )
    connection.execute(
        "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
        " VALUES ('m1','t','k','CREATED',1,'h','{}',1.0,1.0)"
    )
    connection.execute(
        "INSERT INTO budget_accounts(account_id,scope,parent_id,mission_id,limits_json,version,updated_at)"
        " VALUES ('budget:m1','mission',NULL,'m1','{}',1,1.0)"
    )
    connection.commit()
    connection.close()


def test_a_v3_library_is_backed_up_and_upgraded_to_v4_keeping_its_accounts(tmp_path):
    path = tmp_path / "orchestrator.db"
    _v3_library(path)
    store = Store.open(path)
    assert schema.SCHEMA_VERSION == 4
    rows = store.connection.execute(
        "SELECT version FROM orch_schema_migrations ORDER BY version"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3, 4]
    tables = {
        r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "scheduler_state" in tables
    row = store.connection.execute(
        "SELECT reserved_tool_calls, settled_tool_calls FROM budget_accounts"
    ).fetchone()
    assert tuple(row) == (0, 0)  # the old account survived with the new dimension at zero
    assert (tmp_path / "orchestrator.db.pre-schema-4.backup").is_file()
    assert store.get_scheduler_state("backpressure") is None
    assert store.put_scheduler_state("backpressure", {"level": "NORMAL"}) == 1
    assert store.put_scheduler_state("backpressure", {"level": "RAISED"}) == 2
    assert store.get_scheduler_state("backpressure") == {"level": "RAISED"}
    store.close()


# ------------------------------------------------------------------ D6-8 tool-call dimension
def test_the_tool_call_dimension_reserves_settles_and_exhausts_like_tokens(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    ledger = BudgetLedger(store)
    with store.transaction():
        store.connection.execute(
            "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
            " VALUES ('m1','t','k','CREATED',1,'h','{}',1.0,1.0)"
        )
        ledger.open_account(
            account_id="budget:m1",
            scope="mission",
            parent_id=None,
            mission_id="m1",
            limits=Budget(max_tool_calls=10),
        )
        ledger.open_account(
            account_id="budget:m1:task-1",
            scope="task",
            parent_id="budget:m1",
            mission_id="m1",
            limits=Budget(max_tool_calls=8),
        )
        with pytest.raises(
            Exception
        ):  # §18.2: a child never exceeds its parent on the new dimension either
            ledger.open_account(
                account_id="budget:m1:task-2",
                scope="task",
                parent_id="budget:m1",
                mission_id="m1",
                limits=Budget(max_tool_calls=11),
            )
        ledger.reserve(
            account_id="budget:m1:task-1",
            subject_id="a1",
            tokens=0,
            cost_micros=0,
            counts_attempt=True,
            tool_calls=6,
        )
        assert ledger.account("budget:m1").remaining_tool_calls() == 4
        with pytest.raises(BudgetExhausted) as exhausted:
            ledger.reserve(
                account_id="budget:m1:task-1",
                subject_id="a2",
                tokens=0,
                cost_micros=0,
                counts_attempt=True,
                tool_calls=5,
            )
        assert exhausted.value.dimension == "tool_calls" and exhausted.value.remaining == 2
        settled = ledger.settle(subject_id="a1", tool_calls=3)  # the gateway counted 3 real calls
        assert settled["settled_tool_calls"] == 3
        mission = ledger.account("budget:m1")
        assert mission.reserved_tool_calls == 0 and mission.settled_tool_calls == 3
        assert (
            mission.remaining_tool_calls() == 7 and mission.to_json()["remaining_tool_calls"] == 7
        )
    store.close()
