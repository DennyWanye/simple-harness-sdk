# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.3 red tests: a replacement Task inherits its predecessor's duty (§6.1, §8.4, §15.2).

Three properties are pinned, in this order of importance:

1. **Re-planning never mints a fresh allowance.**  Superseding a running Task,
   handing it to another agent or swapping its method leaves the failure count,
   the consumed budget and the recursion fuel exactly where the ``obligation_id``
   had them.  Only an explicitly new duty opens a new account (§15.2).
2. **The old library is untouched.**  A Mission with no semantic binding walks the
   same supersede path and must leave all thirty migration-16 tables empty *and*
   produce byte-identical event payloads to a run with the hook switched off
   (§18.5 rule 1).
3. **It is one transaction.**  A failure after the ledger was written leaves the
   relation, the successor binding and the counters as they were.

The last block is the mutation self-check: each mutant is a deliberately wrong
implementation of the hook, and the assertion the real tests make must catch it.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from simple_harness.contracts import canonical_json

_STEP05 = Path(__file__).resolve().parents[1] / "step05"
if str(_STEP05) not in sys.path:  # the §7.4 diamond + change builders live there
    sys.path.insert(0, str(_STEP05))

from full_target_world import task_binding  # noqa: E402
from graph_helpers import add, change, drive_to_running, graph_service  # noqa: E402

from agent_orchestrator.contracts import ContractError  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    ContractRevision,
    DispatchGeneration,
    InputBindingRevision,
    MethodInstanceId,
    MethodOccurrenceBinding,
    ObligationId,
    ObligationRelation,
    OccurrenceId,
    Requiredness,
    TaskForm,
)
from agent_orchestrator.contracts.obligations import (  # noqa: E402
    ExpansionRecord,
    Obligation,
    SatisfactionPolicy,
    ShapeChange,
)
from agent_orchestrator.orchestrator.obligation_commits import (  # noqa: E402
    INHERITANCE_REASONS,
)
from agent_orchestrator.storage.htn_schema import TABLES  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.storage.store import StoreError  # noqa: E402

FAILURES = 2
COST_MICROS = 4_200
ATTEMPTS = 3
TOKENS = 7_500
FUEL = 4


# ------------------------------------------------------------------ fixtures / drivers
def _duty(mission_id: str, obligation: str) -> Obligation:
    return Obligation(
        obligation_id=ObligationId(obligation),
        mission_id=mission_id,
        requirement_refs=("c-complete",),
        goal_signature_id="compare-sources",
        parameters={"subject": "alpha"},
        scope="mission",
        requiredness=Requiredness.REQUIRED,
        satisfaction_policy=SatisfactionPolicy(required_criterion_ids=("c-complete",)),
    )


def _bind(service, mission_id: str, task_id: str, *, obligation: str = "obligation-1") -> str:
    """Give one Task a semantic binding and its duty a used-up account."""

    store = ObligationStore(service.store)
    store.register(_duty(mission_id, obligation), recursion_fuel=FUEL)
    store.record_failure(mission_id, ObligationId(obligation), count=FAILURES)
    store.record_spend(
        mission_id,
        ObligationId(obligation),
        cost_micros=COST_MICROS,
        attempts=ATTEMPTS,
        tokens=TOKENS,
    )
    store.consume_fuel(
        mission_id,
        ObligationId(obligation),
        expansion=ExpansionRecord(method_id="compare", parameters_digest="d1", task_id=task_id),
    )
    HtnStore(service.store).put_task_semantics(
        mission_id,
        task_binding(task_id=task_id, obligation=obligation, form=TaskForm.COMPOUND),
    )
    return obligation


def _supersede(service, mission, old_task_id: str, *, key: str = "B2", base: int = 1):
    """The §7.4 replacement: an executing Task is replaced by a new one."""

    proposal = change(
        base,
        [
            add(key, [], goal=f"重做 {key}"),
            {"op": "supersede_task", "task_id": old_task_id, "replacement_key": key},
        ],
    )
    created, receipt = service.commit_graph_change(mission.id, proposal, source={"intent_id": key})
    return created[0], receipt


def _running_diamond(tmp_path, *, key: str = "g-1", bound: bool = True):
    service, mission, tasks = graph_service(tmp_path, key=key)
    drive_to_running(service, tasks["A"], agent="agent-a", turn="turn-a")
    obligation = _bind(service, mission.id, tasks["A"].id) if bound else None
    return service, mission, tasks, obligation


def _account(service, mission_id: str, obligation: str):
    store = ObligationStore(service.store)
    view = store.account(mission_id, ObligationId(obligation))
    return view, store.spent_tokens(mission_id, ObligationId(obligation))


def _assert_untouched(service, mission_id: str, obligation: str, *, shape_changes: int) -> None:
    """Every counter the plan says re-planning must not move (§8.4)."""

    view, tokens = _account(service, mission_id, obligation)
    assert view.failure_count == FAILURES, view
    assert view.consumed_cost_micros == COST_MICROS, view
    assert view.consumed_attempts == ATTEMPTS, view
    assert tokens == TOKENS
    assert (view.fuel_limit, view.fuel_used, view.remaining_fuel) == (FUEL, 1, FUEL - 1), view
    assert view.shape_changes == shape_changes, view


def _new_table_counts(service) -> dict[str, int]:
    return {
        table: int(
            service.store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        )
        for table in TABLES
    }


def _rows(service, table: str) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in service.store.connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
    ]


def _payload_bytes(service, mission_id: str) -> list[tuple[str, str]]:
    rows = service.store.connection.execute(
        "SELECT type, payload_json FROM events WHERE mission_id = ? ORDER BY seq", (mission_id,)
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


# ================================================================== the carried duty
def test_the_successor_is_bound_to_the_very_same_obligation(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    binding = HtnStore(service.store).latest_task_semantics(successor.id)
    assert binding is not None, "the replacement never received a semantic binding"
    assert str(binding.obligation_id) == obligation
    assert str(binding.task_id) == successor.id


def test_the_failure_count_survives_the_replacement(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    _supersede(service, mission, tasks["A"].id)
    assert _account(service, mission.id, obligation)[0].failure_count == FAILURES


def test_the_consumed_cost_and_attempts_survive_the_replacement(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    _supersede(service, mission, tasks["A"].id)
    view, _ = _account(service, mission.id, obligation)
    assert (view.consumed_cost_micros, view.consumed_attempts) == (COST_MICROS, ATTEMPTS)


def test_the_spent_tokens_survive_the_replacement(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    _supersede(service, mission, tasks["A"].id)
    assert _account(service, mission.id, obligation)[1] == TOKENS


def test_the_replacement_does_not_refill_recursion_fuel(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    _supersede(service, mission, tasks["A"].id)
    view, _ = _account(service, mission.id, obligation)
    assert (view.fuel_limit, view.fuel_used, view.remaining_fuel) == (FUEL, 1, FUEL - 1)


def test_the_replacement_is_recorded_as_one_successor_shape_change(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [ShapeChange.SUCCESSOR_TASK]
    assert json.loads(history[0][1]) == {  # machine readable, so the history can be replayed
        "graph_version": 2,
        "new_task_id": successor.id,
        "old_task_id": tasks["A"].id,
        "reason": "REPLACE",
    }
    assert history[0][1] == canonical_json(json.loads(history[0][1]))
    _assert_untouched(service, mission.id, obligation, shape_changes=1)


def test_an_obligation_inherited_event_carries_the_counters_it_did_not_move(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    events = [e for e in service.store.list_events(mission.id) if e.type == "ObligationInherited"]
    assert len(events) == 1, [e.type for e in service.store.list_events(mission.id)]
    payload = events[0].payload
    assert payload["obligation_id"] == obligation
    assert payload["superseded_task"] == tasks["A"].id
    assert payload["successor_task"] == successor.id
    assert payload["reason"] == "REPLACE"
    assert payload["shape_change"] == str(ShapeChange.SUCCESSOR_TASK)
    assert payload["shape_change_ordinal"] == 1 and payload["graph_version"] == 2
    assert payload["failure_count"] == FAILURES
    assert payload["consumed_cost_micros"] == COST_MICROS
    assert payload["spent_tokens"] == TOKENS
    assert payload["fuel_remaining"] == FUEL - 1
    assert payload["relation"] is None and payload["binding_inherited"] is True


def test_no_existing_event_type_gains_or_loses_a_field(tmp_path):
    """§18.5 rule 1: the new mode may *append* an event; it may not rewrite one."""

    service, mission, tasks, _ = _running_diamond(tmp_path)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    by_type = {e.type: e.payload for e in service.store.list_events(mission.id)}
    assert set(by_type["TaskSuperseded"]) == {"reason", "replaced_by"}
    assert by_type["TaskSuperseded"]["replaced_by"] == successor.id
    assert "obligation_id" not in by_type["TaskGraphChanged"]
    assert "obligation_id" not in by_type["TaskCommitted"]


# ================================================================== role / method change
def test_a_role_change_carries_the_duty_without_a_new_allowance(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    with service.store.transaction() as connection:
        carried = service._inherit_obligation_on_replacement(
            connection, mission.id, tasks["A"].id, tasks["A"].id, reason="ROLE_CHANGE"
        )
    assert str(carried) == obligation
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [ShapeChange.AGENT_REASSIGNED]
    _assert_untouched(service, mission.id, obligation, shape_changes=1)


def test_a_method_change_carries_the_duty_without_a_new_allowance(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    with service.store.transaction() as connection:
        service._inherit_obligation_on_replacement(
            connection, mission.id, tasks["A"].id, tasks["A"].id, reason="METHOD_CHANGE"
        )
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [ShapeChange.METHOD_SWITCHED]
    _assert_untouched(service, mission.id, obligation, shape_changes=1)


def test_a_rename_is_recorded_as_a_task_rename(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    with service.store.transaction() as connection:
        service._inherit_obligation_on_replacement(
            connection, mission.id, tasks["A"].id, tasks["A"].id, reason="RENAME"
        )
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [ShapeChange.TASK_RENAMED]


def test_replacing_then_reassigning_then_reswitching_accumulates_history_only(tmp_path):
    """T069 / T025, persistence half: three shape changes, not one reset counter."""

    service, mission, tasks, obligation = _running_diamond(tmp_path)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    with service.store.transaction() as connection:
        service._inherit_obligation_on_replacement(
            connection, mission.id, successor.id, successor.id, reason="ROLE_CHANGE"
        )
        service._inherit_obligation_on_replacement(
            connection, mission.id, successor.id, successor.id, reason="METHOD_CHANGE"
        )
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [
        ShapeChange.SUCCESSOR_TASK,
        ShapeChange.AGENT_REASSIGNED,
        ShapeChange.METHOD_SWITCHED,
    ]
    _assert_untouched(service, mission.id, obligation, shape_changes=3)


def test_the_same_reason_twice_writes_two_events_and_two_shape_changes(tmp_path):
    """Review fix 1: the event key carries the ordinal, so no re-plan is swallowed."""

    service, mission, tasks, obligation = _running_diamond(tmp_path)
    with service.store.transaction() as connection:
        for _ in range(2):
            service._inherit_obligation_on_replacement(
                connection, mission.id, tasks["A"].id, tasks["A"].id, reason="ROLE_CHANGE"
            )
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert [change for change, _ in history] == [
        ShapeChange.AGENT_REASSIGNED,
        ShapeChange.AGENT_REASSIGNED,
    ]
    events = [e for e in service.store.list_events(mission.id) if e.type == "ObligationInherited"]
    assert [e.payload["shape_change_ordinal"] for e in events] == [1, 2]
    assert len({e.id for e in events}) == 2, "the second re-plan was silently dropped"
    _assert_untouched(service, mission.id, obligation, shape_changes=2)


def test_an_unrelated_duty_keeps_its_rows_and_timestamps_byte_for_byte(tmp_path):
    """Review fix 2: one row for one duty — no whole-ledger rewrite of the neighbours."""

    service, mission, tasks, obligation = _running_diamond(tmp_path)
    store = ObligationStore(service.store)
    store.register(_duty(mission.id, "obligation-bystander"), recursion_fuel=FUEL)
    store.note_shape_change(
        mission.id, ObligationId("obligation-bystander"), ShapeChange.TASK_RENAMED, detail="earlier"
    )
    before = _rows(service, "obligations") + _rows(service, "obligation_shape_changes")
    _supersede(service, mission, tasks["A"].id)
    after = _rows(service, "obligations") + _rows(service, "obligation_shape_changes")
    untouched = [row for row in before if "bystander" in str(row)]
    assert len(untouched) == 2, untouched  # its obligations row and its history row
    assert [row for row in after if "bystander" in str(row)] == untouched
    assert [row for row in after if "bystander" not in str(row)] != [
        row for row in before if "bystander" not in str(row)
    ]  # the duty that was actually re-planned did gain a row


def test_the_successor_inherits_the_meaning_but_not_the_dispatch_state(tmp_path):
    """Review fix 3: a replacement has dispatched nothing and adopted no method yet."""

    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)
    obligation = _bind(service, mission.id, tasks["A"].id)
    dispatched = dataclasses.replace(
        HtnStore(service.store).latest_task_semantics(tasks["A"].id),
        contract_revision=ContractRevision(7),
        adopted_method_instance_id=MethodInstanceId("instance-9"),
        occurrence_binding=MethodOccurrenceBinding(
            method_instance_id=MethodInstanceId("instance-9"),
            occurrence_id=OccurrenceId("occurrence-3"),
            slot_key="extract",
        ),
        input_binding_revision=InputBindingRevision(5),
        dispatch_generation=DispatchGeneration(4),
    )
    HtnStore(service.store).put_task_semantics(mission.id, dispatched)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    carried = HtnStore(service.store).latest_task_semantics(successor.id)
    assert str(carried.obligation_id) == obligation  # the duty is inherited
    assert carried.goal_signature == dispatched.goal_signature
    assert carried.typed_parameters == dispatched.typed_parameters
    assert carried.requirement_refs == dispatched.requirement_refs
    assert int(carried.contract_revision) == 0  # the dispatch state is not
    assert carried.adopted_method_instance_id is None
    assert carried.occurrence_binding is None
    assert int(carried.input_binding_revision) == 0
    assert int(carried.dispatch_generation) == 0
    assert carried.contract_hash != dispatched.contract_hash
    assert len(carried.contract_hash) == 64


def test_every_accepted_reason_maps_to_a_shape_change_that_moves_no_counter(tmp_path):
    assert set(INHERITANCE_REASONS) == {"REPLACE", "RENAME", "ROLE_CHANGE", "METHOD_CHANGE"}
    assert len(set(INHERITANCE_REASONS.values())) == len(INHERITANCE_REASONS)


# ================================================================== explicitly new duty
def test_a_successor_with_its_own_duty_records_the_parent_child_relation(tmp_path):
    """§6.1: a genuinely new responsibility is created explicitly and keeps the lineage."""

    service, mission, tasks, obligation = _running_diamond(tmp_path)
    proposal = change(
        1,
        [
            add("B2", [], goal="重做 B2"),
            {"op": "supersede_task", "task_id": tasks["A"].id, "replacement_key": "B2"},
        ],
    )
    # the replacement command brings its own duty, registered before the commit
    successor_id = f"{mission.id}:task-5"
    store = ObligationStore(service.store)
    store.register(_duty(mission.id, "obligation-2"), recursion_fuel=FUEL)
    HtnStore(service.store).put_task_semantics(
        mission.id, task_binding(task_id=successor_id, obligation="obligation-2")
    )
    service.commit_graph_change(mission.id, proposal, source={"intent_id": "B2"})
    relations = store.list_relations(mission.id, parent=ObligationId(obligation))
    assert len(relations) == 1, relations
    assert str(relations[0].child_obligation_id) == "obligation-2"
    assert relations[0].kind is ObligationRelation.REFINES_PARENT
    assert relations[0].detail == {
        "reason": "REPLACE",
        "superseded_task": tasks["A"].id,
        "successor_task": successor_id,
    }
    _assert_untouched(service, mission.id, obligation, shape_changes=1)


def test_an_inherited_duty_writes_no_self_relation(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    _supersede(service, mission, tasks["A"].id)
    assert ObligationStore(service.store).list_relations(mission.id) == ()


# ================================================================== refusals
def test_an_unknown_reason_is_refused_and_writes_nothing(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path)
    before = _new_table_counts(service)
    with pytest.raises(ContractError, match="reason"), service.store.transaction() as connection:
        service._inherit_obligation_on_replacement(
            connection, mission.id, tasks["A"].id, "whatever", reason="RESET"
        )
    assert _new_table_counts(service) == before


def test_a_binding_naming_a_duty_the_mission_does_not_carry_is_refused(tmp_path):
    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)
    HtnStore(service.store).put_task_semantics(
        mission.id, task_binding(task_id=tasks["A"].id, obligation="ghost-duty")
    )
    with pytest.raises(StoreError, match="ghost-duty"), service.store.transaction() as connection:
        service._inherit_obligation_on_replacement(
            connection, mission.id, tasks["A"].id, tasks["B"].id, reason="REPLACE"
        )


def test_the_hook_refuses_to_run_outside_the_commit_transaction(tmp_path):
    service, mission, tasks, _ = _running_diamond(tmp_path)
    with pytest.raises(StoreError, match="transaction"):
        service._inherit_obligation_on_replacement(
            service.store.connection, mission.id, tasks["A"].id, tasks["B"].id, reason="REPLACE"
        )


def test_a_failure_after_the_ledger_was_written_rolls_the_whole_step_back(tmp_path):
    service, mission, tasks, obligation = _running_diamond(tmp_path, bound=False)
    obligation = _bind(service, mission.id, tasks["A"].id)
    store = ObligationStore(service.store)
    store.register(_duty(mission.id, "obligation-2"), recursion_fuel=FUEL)
    HtnStore(service.store).put_task_semantics(
        mission.id, task_binding(task_id=tasks["B"].id, obligation="obligation-2")
    )
    before = _new_table_counts(service)
    with pytest.raises(RuntimeError, match="injected"):
        with service.store.transaction() as connection:
            service._inherit_obligation_on_replacement(
                connection, mission.id, tasks["A"].id, tasks["B"].id, reason="REPLACE"
            )
            raise RuntimeError("injected after persist")
    assert _new_table_counts(service) == before
    assert store.list_relations(mission.id) == ()
    assert store.shape_changes(mission.id, ObligationId(obligation)) == ()
    _assert_untouched(service, mission.id, obligation, shape_changes=0)


# ================================================================== legacy zero regression
def test_a_legacy_replacement_leaves_every_migration_16_table_empty(tmp_path):
    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)
    _supersede(service, mission, tasks["A"].id)
    counts = _new_table_counts(service)
    assert set(counts) == set(TABLES) and len(counts) >= 30  # the whole migration, not a subset
    assert counts == dict.fromkeys(TABLES, 0), {k: v for k, v in counts.items() if v}


def test_a_legacy_replacement_emits_byte_identical_event_payloads_with_the_hook_off(
    tmp_path, monkeypatch
):
    """§18.5: the same legacy flow, hook disabled vs. enabled, byte for byte."""

    off_service, off_mission, off_tasks, _ = _running_diamond(tmp_path / "off", bound=False)
    monkeypatch.setattr(
        type(off_service),
        "_inherit_obligation_on_replacement",
        lambda *args, **kwargs: None,
    )
    _supersede(off_service, off_mission, off_tasks["A"].id)
    disabled = _payload_bytes(off_service, off_mission.id)
    monkeypatch.undo()
    on_service, on_mission, on_tasks, _ = _running_diamond(tmp_path / "on", bound=False)
    _supersede(on_service, on_mission, on_tasks["A"].id)
    enabled = _payload_bytes(on_service, on_mission.id)
    assert on_mission.id == off_mission.id  # deterministic ids, or the comparison is vacuous
    assert enabled == disabled
    assert all(kind != "ObligationInherited" for kind, _ in enabled)


def test_a_task_without_a_binding_touches_nothing_beyond_the_first_lookup(tmp_path, monkeypatch):
    """The legacy branch must return on the lookup, before the obligation layer."""

    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the legacy branch reached the obligation layer")

    monkeypatch.setattr(
        "agent_orchestrator.orchestrator.obligation_commits.ObligationStore", _explode
    )
    monkeypatch.setattr(HtnStore, "put_task_semantics", _explode)
    with service.store.transaction() as connection:
        assert (
            service._inherit_obligation_on_replacement(
                connection, mission.id, tasks["A"].id, tasks["B"].id, reason="REPLACE"
            )
            is None
        )


def test_a_legacy_mission_keeps_the_old_supersede_semantics(tmp_path):
    from agent_orchestrator.contracts import TaskStatus

    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)
    successor, receipt = _supersede(service, mission, tasks["A"].id)
    assert service.store.get_task(tasks["A"].id).status is TaskStatus.CANCELLED
    assert receipt["superseded"] == {tasks["A"].id: successor.id}
    assert successor.context["supersedes_task"] == tasks["A"].id


# ================================================================== mutation self-checks
def _mutant(service, monkeypatch, body) -> None:
    monkeypatch.setattr(type(service), "_inherit_obligation_on_replacement", body)


def test_mutation_a_reset_failure_count_is_caught(tmp_path, monkeypatch):
    service, mission, tasks, obligation = _running_diamond(tmp_path)

    def reset(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        conn.execute("UPDATE obligations SET failure_count = 0")

    _mutant(service, monkeypatch, reset)
    _supersede(service, mission, tasks["A"].id)
    with pytest.raises(AssertionError):
        _assert_untouched(service, mission.id, obligation, shape_changes=0)


def test_mutation_a_refilled_fuel_tank_is_caught(tmp_path, monkeypatch):
    service, mission, tasks, obligation = _running_diamond(tmp_path)

    def refill(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        conn.execute("UPDATE obligations SET fuel_used = 0, fuel_remaining = fuel_limit")

    _mutant(service, monkeypatch, refill)
    _supersede(service, mission, tasks["A"].id)
    with pytest.raises(AssertionError):
        _assert_untouched(service, mission.id, obligation, shape_changes=0)


def test_mutation_a_brand_new_obligation_for_the_successor_is_caught(tmp_path, monkeypatch):
    service, mission, tasks, obligation = _running_diamond(tmp_path)

    def escape(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        store = ObligationStore(self.store)
        store.register(_duty(mission_id, "obligation-escape"), recursion_fuel=FUEL)
        HtnStore(self.store).put_task_semantics(
            mission_id, task_binding(task_id=new_task_id, obligation="obligation-escape")
        )

    _mutant(service, monkeypatch, escape)
    successor, _ = _supersede(service, mission, tasks["A"].id)
    binding = HtnStore(service.store).latest_task_semantics(successor.id)
    assert binding is not None
    with pytest.raises(AssertionError):
        assert str(binding.obligation_id) == obligation

    escaped, _ = _account(service, mission.id, "obligation-escape")
    with pytest.raises(AssertionError):  # the escape hatch is a full, unused allowance
        assert escaped.failure_count == FAILURES


def test_mutation_a_missing_shape_change_record_is_caught(tmp_path, monkeypatch):
    service, mission, tasks, obligation = _running_diamond(tmp_path)

    def silent(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        return ObligationId("obligation-1")

    _mutant(service, monkeypatch, silent)
    _supersede(service, mission, tasks["A"].id)
    assert ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation)) == ()
    with pytest.raises(AssertionError):
        _assert_untouched(service, mission.id, obligation, shape_changes=1)


def test_mutation_an_event_key_without_the_ordinal_is_caught(tmp_path, monkeypatch):
    """Review fix 1's own guard: a key that repeats swallows the second re-plan."""

    service, mission, tasks, obligation = _running_diamond(tmp_path)

    def stale_key(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        view = ObligationStore(self.store).note_shape_change(
            mission_id, ObligationId(obligation), ShapeChange.AGENT_REASSIGNED, detail="x"
        )
        self._emit(
            "ObligationInherited",
            mission_id,
            key=f"{mission_id}:{old_task_id}:{new_task_id}:{reason}",  # the ordinal is missing
            task_id=new_task_id,
            payload={"shape_change_ordinal": view.shape_changes},
        )
        return ObligationId(obligation)

    _mutant(service, monkeypatch, stale_key)
    with service.store.transaction() as connection:
        for _ in range(2):
            service._inherit_obligation_on_replacement(
                connection, mission.id, tasks["A"].id, tasks["A"].id, reason="ROLE_CHANGE"
            )
    history = ObligationStore(service.store).shape_changes(mission.id, ObligationId(obligation))
    assert len(history) == 2  # both re-plans were recorded in the ledger
    events = [e for e in service.store.list_events(mission.id) if e.type == "ObligationInherited"]
    with pytest.raises(AssertionError):  # but only one of them became an event
        assert [e.payload["shape_change_ordinal"] for e in events] == [1, 2]


def test_mutation_a_write_on_the_legacy_path_is_caught(tmp_path, monkeypatch):
    service, mission, tasks, _ = _running_diamond(tmp_path, bound=False)

    def eager(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        HtnStore(self.store).put_task_semantics(
            mission_id, task_binding(task_id=new_task_id, obligation="obligation-1")
        )

    _mutant(service, monkeypatch, eager)
    _supersede(service, mission, tasks["A"].id)
    with pytest.raises(AssertionError):
        assert _new_table_counts(service) == dict.fromkeys(TABLES, 0)


def test_mutation_a_rewritten_legacy_event_payload_is_caught(tmp_path, monkeypatch):
    """Guards the byte comparison itself: a hook that edits an old payload must fail it."""

    clean, clean_mission, clean_tasks, _ = _running_diamond(tmp_path / "clean", bound=False)
    _supersede(clean, clean_mission, clean_tasks["A"].id)
    baseline = _payload_bytes(clean, clean_mission.id)

    def rewrite(self, conn, mission_id, old_task_id, new_task_id, *, reason, **rest):
        row = conn.execute(
            "SELECT seq, payload_json FROM events WHERE type = 'TaskSuperseded'"
        ).fetchone()
        payload = json.loads(row[1]) | {"obligation_id": "obligation-1"}
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE seq = ?", (json.dumps(payload), row[0])
        )
        return None

    dirty, dirty_mission, dirty_tasks, _ = _running_diamond(tmp_path / "dirty", bound=False)
    _mutant(dirty, monkeypatch, rewrite)
    _supersede(dirty, dirty_mission, dirty_tasks["A"].id)
    mutated = _payload_bytes(dirty, dirty_mission.id)
    assert dirty_mission.id == clean_mission.id  # or the comparison compares nothing
    assert [kind for kind, _ in mutated] == [kind for kind, _ in baseline]
    with pytest.raises(AssertionError):
        assert mutated == baseline
