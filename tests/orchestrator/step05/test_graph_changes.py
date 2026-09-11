# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 5 · slice A (D5-1…D5-4, D5-10, D5-11): a Task DAG change is a versioned,
transactional Proposal/Commit — whole-proposal validation (cycle, depth, count,
proposals per Attempt, budget pool, duplicates, goal drift, §25.1 legality), CAS on the
graph version with automatic rebase for disjoint changes, idempotent receipts, superseded
work closed as CANCELLED with its history kept, BLOCKED dependents rewired in place, and
completed work never redone."""

from __future__ import annotations

import pytest
from graph_helpers import add, change, complete, drive_to_running, graph_service

from agent_orchestrator.contracts import AttemptStatus, ContractError, MissionStatus, TaskStatus
from agent_orchestrator.graph.changes import (
    ChangeLimits,
    GraphChangeRejected,
    TaskGraphChange,
    validate_change,
)
from agent_orchestrator.orchestrator.commit_service import CommitRejected


def _rejected(service, mission_id, reason):
    events = [
        e for e in service.store.list_events(mission_id) if e.type == "TaskGraphChangeRejected"
    ]
    assert events and events[-1].payload["reason"] == reason, [e.payload for e in events]
    return events[-1]


# ------------------------------------------------------------------ §7.4: v1 → v2
def test_a_change_adds_a_prerequisite_supersedes_the_executing_task_and_rewires_the_dependent(
    tmp_path,
):
    service, mission, t = graph_service(tmp_path)
    complete(service, t["A"])  # A done; D and B READY
    b_attempt = drive_to_running(service, t["B"], agent="agent-b", turn="turn-b")
    assert service.store.get_task(t["B"].id).status is TaskStatus.ACTIVE
    proposal = change(
        1,
        [
            add("E", [t["A"].id], goal="确认输入格式定义", parent_task_ids=[t["B"].id]),
            add("B2", [t["A"].id, "E"], goal="按确认的格式实现记录器"),
            {"op": "supersede_task", "task_id": t["B"].id, "replacement_key": "B2"},
        ],
        basis={
            "trigger": "outcome:blocked",
            "result_id": "result-x",
            "attempt_id": b_attempt.id,
            "task_id": t["B"].id,
        },
    )
    created, receipt = service.commit_graph_change(
        mission.id, proposal, source={"intent_id": "manager-1"}
    )
    assert [c.goal for c in created] == ["确认输入格式定义", "按确认的格式实现记录器"]
    e, b2 = created
    assert receipt["from_version"] == 1 and receipt["to_version"] == 2
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 2
    # ordinal ≡ topological order: E before B2, both after the original four
    assert e.id.endswith(":task-5") and b2.id.endswith(":task-6")
    assert e.status is TaskStatus.READY and e.ready_at is not None  # A is COMPLETED
    assert b2.status is TaskStatus.BLOCKED and set(b2.dependency_ids) == {t["A"].id, e.id}
    assert b2.context["supersedes_task"] == t["B"].id and b2.context["supersede_depth"] == 1
    assert b2.parent_task_ids == (t["B"].id,) and b2.context["proposed_by_attempt"] == [
        b_attempt.id
    ]
    # B: ACTIVE → CANCELLED, its Attempt CANCELLED (history), C follows B2 in place, A/D untouched
    old_b = service.store.get_task(t["B"].id)
    assert old_b.status is TaskStatus.CANCELLED and old_b.context["replaced_by"] == b2.id
    assert service.store.get_attempt(b_attempt.id).status is AttemptStatus.CANCELLED
    c = service.store.get_task(t["C"].id)
    assert c.status is TaskStatus.BLOCKED and c.dependency_ids == (b2.id,) and c.version == 2
    assert service.store.get_task(t["A"].id).status is TaskStatus.COMPLETED
    assert service.store.get_task(t["D"].id).status is TaskStatus.READY
    assert (
        len(service.store.list_attempts(t["A"].id)) == 1
        and service.store.list_attempts(t["D"].id) == []
    )
    types = [ev.type for ev in service.store.list_events(mission.id)]
    for expected in (
        "TaskGraphChanged",
        "TaskSuperseded",
        "TaskDependenciesRewritten",
        "TaskCommitted",
    ):
        assert expected in types
    changed = [ev for ev in service.store.list_events(mission.id) if ev.type == "TaskGraphChanged"][
        0
    ]
    assert changed.payload["basis"]["result_id"] == "result-x" and changed.payload[
        "superseded"
    ] == {t["B"].id: b2.id}
    history = service.store.list_graph_changes(mission.id)
    assert len(history) == 1 and history[0]["change_id"] == receipt["change_id"]
    # the Mission finishes on v2: E, B2, C, D complete → judgment sees no CANCELLED task
    complete(service, service.store.get_task(e.id), agent="agent-e", turn="turn-e")
    assert service.store.get_task(b2.id).status is TaskStatus.READY
    complete(service, service.store.get_task(b2.id), agent="agent-b2", turn="turn-b2")
    complete(service, service.store.get_task(t["C"].id), agent="agent-c", turn="turn-c")
    complete(service, service.store.get_task(t["D"].id), agent="agent-d", turn="turn-d")
    judged = service.judge_mission(
        mission.id, judgments=[{"criterion": "file:c.md", "met": True}], summary="ok"
    )
    assert judged.status is MissionStatus.COMPLETED
    assert (
        judged.final_report["terminal_task_id"] == t["D"].id
    )  # the last leaf in topological order


# ------------------------------------------------------------------ S5-07 / S5-03
def test_s5_07_the_same_proposal_is_applied_once_and_replays_the_receipt(tmp_path):
    service, mission, t = graph_service(tmp_path)
    proposal = change(1, [add("E", [t["A"].id], parent_task_ids=[t["B"].id])])
    created, receipt = service.commit_graph_change(mission.id, proposal, source={"intent_id": "m1"})
    again, receipt2 = service.commit_graph_change(
        mission.id, proposal, source={"intent_id": "m1-replay"}
    )
    assert receipt2 == receipt and [x.id for x in again] == [x.id for x in created]
    assert service.store.count_events(mission.id, "TaskGraphChanged") == 1
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 2


def test_s5_03_two_managers_on_the_same_base_rebase_when_disjoint_and_are_refused_when_they_overlap(
    tmp_path,
):
    service, mission, t = graph_service(tmp_path)
    first = change(
        1,
        [
            {"op": "set_priority", "task_id": t["D"].id, "priority": 9.0},
            add("E", [t["A"].id], parent_task_ids=[t["B"].id]),
        ],
    )
    _, r1 = service.commit_graph_change(mission.id, first, source={"intent_id": "m1"})
    assert r1["to_version"] == 2
    disjoint = change(1, [{"op": "set_priority", "task_id": t["C"].id, "priority": 5.0}])
    _, r2 = service.commit_graph_change(mission.id, disjoint, source={"intent_id": "m2"})
    assert r2["to_version"] == 3 and r2["rebased_from"] == 1  # applied on top, nothing lost
    assert (
        service.store.get_task(t["D"].id).priority == 9.0
        and service.store.get_task(t["C"].id).priority == 5.0
    )
    overlapping = change(1, [{"op": "set_priority", "task_id": t["D"].id, "priority": 1.0}])
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, overlapping, source={"intent_id": "m3"})
    _rejected(service, mission.id, "stale_base")
    assert service.store.get_task(t["D"].id).priority == 9.0
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 3
    with pytest.raises(CommitRejected):  # a base from the future is never accepted
        service.commit_graph_change(
            mission.id,
            change(9, [{"op": "set_priority", "task_id": t["C"].id, "priority": 2.0}]),
            source={},
        )


# ------------------------------------------------------------------ S5-04
def test_s5_04_cycles_and_goal_drift_are_rejected_and_the_graph_stays(tmp_path):
    service, mission, t = graph_service(tmp_path)
    cyclic = change(
        1,
        [
            {"op": "retarget_dependencies", "task_id": t["C"].id, "dependencies": [t["B"].id, "X"]},
            add("X", [t["C"].id], parent_task_ids=[t["C"].id]),
        ],
    )
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, cyclic, source={})
    assert _rejected(service, mission.id, "cycle").payload["detail"]
    drift = change(
        1, [add("Z", [t["A"].id], goal="顺便研究一下别的")]
    )  # nothing depends on it, replaces nothing
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, drift, source={})
    _rejected(service, mission.id, "goal_drift")
    no_rationale = change(1, [add("Z", [t["A"].id], rationale=" ", parent_task_ids=[t["B"].id])])
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, no_rationale, source={})
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 1
    assert len(service.store.list_tasks(mission.id)) == 4
    assert service.store.get_task(t["C"].id).dependency_ids == (t["B"].id,)


# ------------------------------------------------------------------ S5-08
def test_s5_08_depth_proposal_count_and_budget_limits_are_enforced_with_reasons(tmp_path):
    service, mission, t = graph_service(tmp_path)
    limits = ChangeLimits(max_graph_depth=3, max_proposals_per_agent=1)
    deep = change(
        1, [add("X", [t["C"].id], parent_task_ids=[t["C"].id], goal="第四层")]
    )  # A→B→C→X = depth 4
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, deep, source={}, limits=limits)
    assert "max_graph_depth=3" in _rejected(service, mission.id, "depth").payload["detail"]
    many = change(
        1,
        [
            add("X", [t["A"].id], parent_task_ids=[t["B"].id]),
            add("Y", [t["A"].id], parent_task_ids=[t["B"].id]),
        ],
        basis={"trigger": "t", "attempt_id": "att-1"},
    )
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, many, source={}, limits=limits)
    assert (
        "max_proposals_per_agent=1" in _rejected(service, mission.id, "proposals").payload["detail"]
    )
    greedy = change(
        1, [add("X", [t["A"].id], parent_task_ids=[t["B"].id], tokens=150_000)]
    )  # pool: 200k − 80k committed
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, greedy, source={})
    detail = _rejected(service, mission.id, "budget").payload["detail"]
    assert "remaining=120000" in detail and "dimension=max_tokens" in detail
    # a superseded task's unused allocation returns to the pool (30-13: sub-task budgets come from the parent)
    complete(service, t["A"])
    b_attempt = drive_to_running(service, t["B"], agent="agent-b", turn="turn-b")
    replace_b = change(
        1,
        [
            add("B2", [t["A"].id], tokens=130_000),
            {"op": "supersede_task", "task_id": t["B"].id, "replacement_key": "B2"},
        ],
        basis={"trigger": "t", "attempt_id": b_attempt.id},
    )
    created, receipt = service.commit_graph_change(mission.id, replace_b, source={})
    assert (
        created[0].budget.max_tokens == 130_000
    )  # 200k − (A 20k + C 20k + D 20k + B settled 0) = 140k available
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 2


# ------------------------------------------------------------------ §25.1 legality
def test_state_machine_legality_of_operations(tmp_path):
    service, mission, t = graph_service(tmp_path)
    # a READY task may not have its dependencies rewritten in place (READY→BLOCKED is not an edge)
    with pytest.raises(CommitRejected):
        service.commit_graph_change(
            mission.id,
            change(
                1,
                [
                    add("E", [], parent_task_ids=[t["A"].id]),
                    {"op": "retarget_dependencies", "task_id": t["A"].id, "dependencies": ["E"]},
                ],
            ),
            source={},
        )
    _rejected(service, mission.id, "illegal_transition")
    # a COMPLETED task is never superseded or cancelled
    complete(service, t["A"])
    with pytest.raises(CommitRejected):
        service.commit_graph_change(
            mission.id,
            change(
                1,
                [
                    add("A2", [], parent_task_ids=[t["A"].id]),
                    {"op": "supersede_task", "task_id": t["A"].id, "replacement_key": "A2"},
                ],
            ),
            source={},
        )
    with pytest.raises(CommitRejected):
        service.commit_graph_change(
            mission.id,
            change(1, [{"op": "cancel_task", "task_id": t["A"].id, "reason": "x"}]),
            source={},
        )
    # pause / resume / set_role are data flags on a live task
    _, receipt = service.commit_graph_change(
        mission.id,
        change(
            1,
            [
                {"op": "pause_task", "task_id": t["D"].id, "reason": "低价值"},
                {"op": "set_role", "task_id": t["B"].id, "role": "simplifier"},
            ],
        ),
        source={},
    )
    d = service.store.get_task(t["D"].id)
    assert d.status is TaskStatus.READY and d.paused and d.pause_reason == "低价值"
    assert service.store.get_task(t["B"].id).context["role"] == "simplifier"
    from agent_orchestrator.scheduling.allocator import frontier

    assert [x.id for x in frontier(service.store.list_tasks(mission.id))] == [
        t["B"].id
    ]  # D is paused
    service.commit_graph_change(
        mission.id,
        change(receipt["to_version"], [{"op": "resume_task", "task_id": t["D"].id}]),
        source={},
    )
    assert not service.store.get_task(t["D"].id).paused
    # cancelling a READY task is legal; its dependents can no longer be satisfied unless rewired
    with pytest.raises(
        CommitRejected
    ):  # C depends on B: cancelling B without a replacement leaves C dangling
        service.commit_graph_change(
            mission.id,
            change(
                service.store.get_mission(mission.id).final_report["graph_version"],
                [{"op": "cancel_task", "task_id": t["B"].id, "reason": "放弃路线"}],
            ),
            source={},
        )
    _rejected(service, mission.id, "missing_dependency")


def test_change_proposal_parsing_is_strict():
    with pytest.raises(ContractError):
        TaskGraphChange.from_json({"base_graph_version": 0, "operations": []})
    with pytest.raises(ContractError):
        TaskGraphChange.from_json(
            {"base_graph_version": 1, "operations": [{"op": "rewrite_everything"}]}
        )
    with pytest.raises(ContractError):
        TaskGraphChange.from_json(
            {
                "base_graph_version": 1,
                "operations": [{"op": "set_role", "task_id": "t", "role": "king"}],
            }
        )
    parsed = TaskGraphChange.from_json(
        {
            "base_graph_version": 1,
            "basis": {"trigger": "x"},
            "rationale": "r",
            "operations": [add("E", [])],
        }
    )
    assert parsed.add_tasks()[0].key == "E" and parsed.proposal_hash


def test_validate_change_reports_supersede_chain_limit(tmp_path):
    service, mission, t = graph_service(tmp_path)
    tasks = service.store.list_tasks(mission.id)
    deep = [x for x in tasks if x.goal.endswith("A")][0]  # READY: supersedable in principle
    from dataclasses import replace

    deep = replace(deep, context={"supersede_depth": 2})
    proposal = change(
        1,
        [
            add("B3", [t["A"].id]),
            {"op": "supersede_task", "task_id": deep.id, "replacement_key": "B3"},
        ],
    )
    with pytest.raises(GraphChangeRejected) as rejected:
        validate_change(
            mission,
            [deep if x.id == deep.id else x for x in tasks],
            proposal,
            limits=ChangeLimits(),
            proposals_by_attempt={},
            settled_tokens_by_task={},
        )
    assert rejected.value.reason == "supersede_chain"
