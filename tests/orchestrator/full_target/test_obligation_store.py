# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.2 red tests: the durable obligation ledger (§6.1, §6.4, ADR-08).

The arithmetic under test is the one thing re-planning must not be able to reset:
a duty's failure count, its consumed budget and its recursion fuel belong to the
``obligation_id``, so renaming the task, swapping the method or handing the work
to another agent leave every counter exactly where it was.  These tests drive the
persistent store, not the in-memory ledger, and additionally pin that a *refused*
call writes nothing at all.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import (
    Budget,
    ContractError,
    Mission,
    MissionStatus,
)
from agent_orchestrator.contracts.htn import ObligationId, ObligationRelation, Requiredness
from agent_orchestrator.contracts.obligations import (
    ExpansionRecord,
    FuelStatus,
    Obligation,
    ObligationLifecycle,
    SatisfactionPolicy,
    ShapeChange,
)
from agent_orchestrator.storage.obligation_store import ObligationStore
from agent_orchestrator.storage.store import Store, StoreConflict

MISSION = "mission-1"


def _mission(mission_id: str = MISSION) -> Mission:
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


def _duty(
    obligation: str = "obligation-1",
    *,
    mission_id: str = MISSION,
    parent: str | None = None,
    lifecycle: ObligationLifecycle = ObligationLifecycle.UNSATISFIED,
    resolution_ref: str | None = None,
    budget_lineage_ref: str | None = None,
) -> Obligation:
    return Obligation(
        obligation_id=ObligationId(obligation),
        mission_id=mission_id,
        requirement_refs=("c-complete",),
        goal_signature_id="compare-sources",
        parameters={"subject": "alpha"},
        scope="mission",
        requiredness=Requiredness.REQUIRED,
        budget_lineage_ref=budget_lineage_ref,
        satisfaction_policy=SatisfactionPolicy(required_criterion_ids=("c-complete",)),
        lifecycle=lifecycle,
        resolution_ref=resolution_ref,
        parent_obligation_id=None if parent is None else ObligationId(parent),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    opened = Store.open(tmp_path / "orchestrator.db")
    opened.insert_mission(_mission(), spec_hash="h")
    return opened


@pytest.fixture
def ledger(store: Store) -> ObligationStore:
    return ObligationStore(store)


# ------------------------------------------------------------------ registration
def test_register_round_trips_the_duty(ledger: ObligationStore) -> None:
    duty = _duty()
    ledger.register(duty, recursion_fuel=2)
    assert ledger.obligation(MISSION, duty.obligation_id) == duty
    assert ledger.obligation_ids(MISSION) == (duty.obligation_id,)
    assert ledger.list_obligations(MISSION) == (duty,)


def test_registering_the_same_id_twice_is_refused(ledger: ObligationStore) -> None:
    ledger.register(_duty())
    with pytest.raises(StoreConflict, match="already registered"):
        ledger.register(_duty())


def test_a_fresh_account_starts_at_zero(ledger: ObligationStore) -> None:
    ledger.register(_duty(), recursion_fuel=3)
    view = ledger.account(MISSION, ObligationId("obligation-1"))
    assert view.failure_count == 0
    assert view.consumed_cost_micros == 0
    assert view.consumed_attempts == 0
    assert view.remaining_fuel == 3
    assert view.lifecycle is ObligationLifecycle.UNSATISFIED
    assert ledger.spent_tokens(MISSION, ObligationId("obligation-1")) == 0


def test_an_unknown_obligation_is_a_conflict(ledger: ObligationStore) -> None:
    with pytest.raises(StoreConflict, match="not registered"):
        ledger.account(MISSION, ObligationId("obligation-missing"))


# ------------------------------------------------------------------ accumulation
def test_failures_accrue_across_shape_changes(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    assert ledger.record_failure(MISSION, target) == 1
    ledger.note_shape_change(MISSION, target, ShapeChange.TASK_RENAMED, detail="renamed to B")
    ledger.note_shape_change(MISSION, target, ShapeChange.METHOD_SWITCHED, detail="method B")
    assert ledger.record_failure(MISSION, target) == 2
    view = ledger.account(MISSION, target)
    assert view.failure_count == 2
    assert view.shape_changes == 2


def test_spend_accumulates_on_the_duty_not_the_task(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_spend(MISSION, target, cost_micros=1200, attempts=1, tokens=340)
    ledger.note_shape_change(MISSION, target, ShapeChange.AGENT_REASSIGNED, detail="agent-2")
    view = ledger.record_spend(MISSION, target, cost_micros=800, attempts=2, tokens=60)
    assert view.consumed_cost_micros == 2000
    assert view.consumed_attempts == 3
    assert ledger.spent_tokens(MISSION, target) == 400


def test_a_refused_spend_leaves_the_row_untouched(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_spend(MISSION, target, cost_micros=500, attempts=1, tokens=10)
    with pytest.raises(ContractError):
        ledger.record_spend(MISSION, target, cost_micros=100, attempts=-1)
    view = ledger.account(MISSION, target)
    assert view.consumed_cost_micros == 500
    assert view.consumed_attempts == 1
    assert ledger.spent_tokens(MISSION, target) == 10


def test_a_refused_failure_count_leaves_the_row_untouched(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_failure(MISSION, target)
    with pytest.raises(ContractError):
        ledger.record_failure(MISSION, target, count=0)
    assert ledger.account(MISSION, target).failure_count == 1


def test_shape_changes_are_history_not_an_allowance(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_failure(MISSION, target, count=2)
    ledger.record_spend(MISSION, target, cost_micros=999)
    for change in ShapeChange:
        ledger.note_shape_change(MISSION, target, change, detail=f"{change!s} happened")
    view = ledger.account(MISSION, target)
    assert view.failure_count == 2
    assert view.consumed_cost_micros == 999
    assert [entry[0] for entry in ledger.shape_changes(MISSION, target)] == list(ShapeChange)


# ------------------------------------------------------------------ lifecycle
def test_satisfied_without_a_resolution_ref_is_refused_and_changes_nothing(
    ledger: ObligationStore,
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    with pytest.raises(StoreConflict, match="resolution_ref"):
        ledger.set_lifecycle(MISSION, target, ObligationLifecycle.SATISFIED)
    assert ledger.account(MISSION, target).lifecycle is ObligationLifecycle.UNSATISFIED
    assert ledger.obligation(MISSION, target).lifecycle is ObligationLifecycle.UNSATISFIED


def test_satisfied_stores_the_resolution_ref_in_the_row_and_the_document(
    ledger: ObligationStore,
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    view = ledger.set_lifecycle(
        MISSION, target, ObligationLifecycle.SATISFIED, resolution_ref="resolution-1"
    )
    assert view.lifecycle is ObligationLifecycle.SATISFIED
    assert view.resolution_ref == "resolution-1"
    stored = ledger.obligation(MISSION, target)
    assert stored.lifecycle is ObligationLifecycle.SATISFIED
    assert stored.resolution_ref == "resolution-1"


# ------------------------------------------------------------------ recursion fuel
def test_fuel_is_spent_per_obligation_and_exhaustion_is_bound_reached(
    ledger: ObligationStore,
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=2)
    first = ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1")
    )
    second = ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-b", parameters_digest="d2")
    )
    third = ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-c", parameters_digest="d3")
    )
    assert first.status is FuelStatus.GRANTED
    assert second.status is FuelStatus.GRANTED
    assert third.status is FuelStatus.BOUND_REACHED
    assert ledger.remaining_fuel(MISSION, target) == 0
    assert ledger.expansion_keys(MISSION, target) == (("m-a", "d1"), ("m-b", "d2"))


def test_a_repeated_expansion_does_not_burn_fuel(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=3)
    expansion = ExpansionRecord(method_id="m-a", parameters_digest="d1", task_id="task-1")
    ledger.consume_fuel(MISSION, target, expansion=expansion)
    repeat = ledger.consume_fuel(MISSION, target, expansion=expansion)
    assert repeat.status is FuelStatus.REPEATED_EXPANSION
    assert ledger.remaining_fuel(MISSION, target) == 2
    assert ledger.expansions(MISSION, target) == (expansion,)


def test_bound_reached_is_persisted_and_survives_reopening(store: Store, tmp_path) -> None:
    ledger = ObligationStore(store)
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=1)
    ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1")
    )
    store.close()
    reopened = Store.open(tmp_path / "orchestrator.db")
    again = ObligationStore(reopened)
    assert again.remaining_fuel(MISSION, target) == 0
    decision = again.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-b", parameters_digest="d2")
    )
    assert decision.status is FuelStatus.BOUND_REACHED
    reopened.close()


def test_a_genuinely_new_duty_gets_its_own_allowance(ledger: ObligationStore) -> None:
    first = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=1)
    ledger.record_failure(MISSION, first, count=3)
    ledger.consume_fuel(
        MISSION, first, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1")
    )
    second = ObligationId("obligation-2")
    ledger.register(_duty("obligation-2", parent="obligation-1"), recursion_fuel=1)
    assert ledger.remaining_fuel(MISSION, second) == 1
    assert ledger.account(MISSION, second).failure_count == 0
    assert ledger.account(MISSION, first).failure_count == 3
    assert ledger.remaining_fuel(MISSION, first) == 0


# ------------------------------------------------------------------ relations
def test_relations_are_indexed_in_both_directions(ledger: ObligationStore) -> None:
    ledger.register(_duty())
    ledger.register(_duty("obligation-2", parent="obligation-1"))
    row = ledger.add_relation(
        MISSION,
        parent=ObligationId("obligation-1"),
        child=ObligationId("obligation-2"),
        kind=ObligationRelation.REFINES_PARENT,
        active_revision=1,
    )
    assert row.kind is ObligationRelation.REFINES_PARENT
    assert ledger.list_relations(MISSION, parent=ObligationId("obligation-1")) == (row,)
    assert ledger.list_relations(MISSION, child=ObligationId("obligation-2")) == (row,)


def test_a_relation_to_an_unregistered_duty_is_refused(ledger: ObligationStore) -> None:
    ledger.register(_duty())
    with pytest.raises(StoreConflict):
        ledger.add_relation(
            MISSION,
            parent=ObligationId("obligation-1"),
            child=ObligationId("obligation-absent"),
            kind=ObligationRelation.REFINES_PARENT,
        )
    with pytest.raises(StoreConflict, match="refine itself"):
        ledger.add_relation(
            MISSION,
            parent=ObligationId("obligation-1"),
            child=ObligationId("obligation-1"),
            kind=ObligationRelation.REFINES_PARENT,
        )


# ------------------------------------------------------------------ ledger bridge
def test_load_ledger_reproduces_every_counter(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=2)
    ledger.record_failure(MISSION, target, count=2)
    ledger.record_spend(MISSION, target, cost_micros=700, attempts=2)
    ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1")
    )
    ledger.note_shape_change(
        MISSION, target, ShapeChange.SUCCESSOR_TASK, detail="task-2 carries on"
    )
    ledger.set_lifecycle(
        MISSION, target, ObligationLifecycle.SATISFIED, resolution_ref="resolution-1"
    )
    memory = ledger.load_ledger(MISSION)
    assert memory.account(target) == ledger.account(MISSION, target)
    assert memory.expansion_keys(target) == ledger.expansion_keys(MISSION, target)
    assert memory.shape_changes(target) == ledger.shape_changes(MISSION, target)
    assert memory.obligation(target) == ledger.obligation(MISSION, target)


def test_persist_writes_a_memory_ledger_back_unchanged(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=3)
    ledger.record_spend(MISSION, target, cost_micros=100, attempts=1, tokens=42)
    memory = ledger.load_ledger(MISSION)
    memory.record_failure(target, count=4)
    memory.consume_fuel(target, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1"))
    memory.note_shape_change(target, ShapeChange.PARAMETERS_REBOUND, detail="subject=beta")
    assert ledger.persist(memory) == (target,)
    view = ledger.account(MISSION, target)
    assert view.failure_count == 4
    assert view.consumed_cost_micros == 100
    assert view.remaining_fuel == 2
    assert ledger.shape_changes(MISSION, target) == (
        (ShapeChange.PARAMETERS_REBOUND, "subject=beta"),
    )


def test_the_token_axis_round_trips_through_the_ledger(ledger: ObligationStore) -> None:
    """The ledger carries tokens now, so a load → persist cycle must preserve them."""

    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_spend(MISSION, target, cost_micros=50, attempts=1, tokens=1234)
    memory = ledger.load_ledger(MISSION)
    assert memory.account(target).consumed_tokens == 1234
    memory.record_spend(target, tokens=66)
    ledger.persist(memory)
    view = ledger.account(MISSION, target)
    assert view.consumed_tokens == 1300
    assert ledger.spent_tokens(MISSION, target) == 1300
    assert view.consumed_cost_micros == 50


def test_persisting_an_untouched_ledger_leaves_every_axis_alone(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_spend(MISSION, target, cost_micros=7, attempts=2, tokens=1234)
    before = ledger.account(MISSION, target)
    ledger.persist(ledger.load_ledger(MISSION))
    assert ledger.account(MISSION, target) == before


def test_an_admitted_demand_round_trips_and_is_admitted_once(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    assert ledger.account(MISSION, target).has_admitted_demand is False
    assert ledger.admit_demand(MISSION, target).has_admitted_demand is True
    with pytest.raises(StoreConflict, match="already has an admitted demand"):
        ledger.admit_demand(MISSION, target)
    assert ledger.load_ledger(MISSION).account(target).has_admitted_demand is True
    assert ledger.withdraw_demand(MISSION, target).has_admitted_demand is False
    with pytest.raises(StoreConflict, match="no admitted demand"):
        ledger.withdraw_demand(MISSION, target)


def test_a_duty_that_is_no_longer_open_may_not_keep_a_demand(ledger: ObligationStore) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.admit_demand(MISSION, target)
    with pytest.raises(StoreConflict):
        ledger.set_lifecycle(
            MISSION, target, ObligationLifecycle.SATISFIED, resolution_ref="resolution-1"
        )
    assert ledger.account(MISSION, target).lifecycle is ObligationLifecycle.UNSATISFIED
    ledger.withdraw_demand(MISSION, target)
    ledger.set_lifecycle(
        MISSION, target, ObligationLifecycle.SATISFIED, resolution_ref="resolution-1"
    )
    with pytest.raises(StoreConflict, match="open obligation"):
        ledger.admit_demand(MISSION, target)


def test_the_store_knows_which_duties_a_mission_carries(ledger: ObligationStore) -> None:
    ledger.register(_duty())
    assert ledger.exists(MISSION, ObligationId("obligation-1")) is True
    assert ledger.exists(MISSION, ObligationId("obligation-9")) is False
    assert ledger.exists("mission-other", ObligationId("obligation-1")) is False


def test_persist_inserts_duties_the_store_has_never_seen(ledger: ObligationStore) -> None:
    memory = ledger.load_ledger(MISSION)
    memory.register(_duty("obligation-9"), recursion_fuel=2)
    memory.record_failure(ObligationId("obligation-9"), count=5)
    ledger.persist(memory)
    assert ledger.account(MISSION, ObligationId("obligation-9")).failure_count == 5
    assert ledger.remaining_fuel(MISSION, ObligationId("obligation-9")) == 2


# ------------------------------------------------------------------ concurrent writers
def _rival(tmp_path, monkeypatch: pytest.MonkeyPatch, store: Store, action) -> None:
    """Let ``action`` commit through a second connection just before ``store`` writes.

    The competing write lands *before* our BEGIN IMMEDIATE, so a store that reads its
    counters outside the transaction and writes an absolute value silently drops it,
    while one that accumulates in SQL sees it.
    """

    rival_store = Store.open(tmp_path / "orchestrator.db")
    rival = ObligationStore(rival_store)
    original = Store.transaction
    fired = {"done": False}

    def racing(self):
        if self is store and not fired["done"]:
            fired["done"] = True
            action(rival)
        return original(self)

    monkeypatch.setattr(Store, "transaction", racing)


def test_a_concurrent_failure_is_not_lost(
    store: Store, ledger: ObligationStore, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    _rival(
        tmp_path, monkeypatch, store, lambda rival: rival.record_failure(MISSION, target, count=3)
    )
    assert ledger.record_failure(MISSION, target, count=1) == 4
    assert ledger.account(MISSION, target).failure_count == 4


def test_a_concurrent_expansion_does_not_double_spend_the_last_fuel(
    store: Store, ledger: ObligationStore, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty(), recursion_fuel=1)
    _rival(
        tmp_path,
        monkeypatch,
        store,
        lambda rival: rival.consume_fuel(
            MISSION, target, expansion=ExpansionRecord(method_id="m-a", parameters_digest="d1")
        ),
    )
    decision = ledger.consume_fuel(
        MISSION, target, expansion=ExpansionRecord(method_id="m-b", parameters_digest="d2")
    )
    assert decision.status is FuelStatus.BOUND_REACHED
    assert ledger.remaining_fuel(MISSION, target) == 0
    assert ledger.expansion_keys(MISSION, target) == (("m-a", "d1"),)


def test_a_concurrent_shape_change_keeps_both_histories(
    store: Store, ledger: ObligationStore, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    _rival(
        tmp_path,
        monkeypatch,
        store,
        lambda rival: rival.note_shape_change(
            MISSION, target, ShapeChange.METHOD_SWITCHED, detail="rival"
        ),
    )
    ledger.note_shape_change(MISSION, target, ShapeChange.TASK_RENAMED, detail="ours")
    assert ledger.shape_changes(MISSION, target) == (
        (ShapeChange.METHOD_SWITCHED, "rival"),
        (ShapeChange.TASK_RENAMED, "ours"),
    )


def test_a_concurrent_spend_is_not_lost(
    store: Store, ledger: ObligationStore, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    _rival(
        tmp_path,
        monkeypatch,
        store,
        lambda rival: rival.record_spend(MISSION, target, cost_micros=500, tokens=7),
    )
    view = ledger.record_spend(MISSION, target, cost_micros=100, tokens=3)
    assert view.consumed_cost_micros == 600
    assert view.consumed_tokens == 10


def test_a_failed_transaction_rolls_back_every_obligation_write(
    store: Store, ledger: ObligationStore
) -> None:
    target = ObligationId("obligation-1")
    ledger.register(_duty())
    ledger.record_failure(MISSION, target, count=1)
    with pytest.raises(RuntimeError, match="deliberate"):
        with store.transaction():
            ledger.record_failure(MISSION, target, count=5)
            ledger.record_spend(MISSION, target, cost_micros=999)
            ledger.register(_duty("obligation-2"))
            raise RuntimeError("deliberate")
    view = ledger.account(MISSION, target)
    assert view.failure_count == 1
    assert view.consumed_cost_micros == 0
    assert ledger.obligation_ids(MISSION) == (target,)
