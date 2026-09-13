# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P34-A05/P35-A04: materialized system allocations count once in dynamic admission."""

from dataclasses import replace

import pytest
from graph_helpers import add, change, graph_service, node

from agent_orchestrator.contracts import Budget, TaskStatus
from agent_orchestrator.graph.changes import ChangeLimits, GraphChangeRejected, validate_change
from agent_orchestrator.graph.task_graph import GraphRejected, TaskGraphProposal, validate_graph
from agent_orchestrator.orchestrator.commit_service import CommitRejected


def _graph(tmp_path, audit=400_000, conflict=0):
    return graph_service(
        tmp_path,
        nodes=[
            node("A", tokens=audit),
            node("B", tokens=480_000),
            node("C", ["A", "B"], tokens=400_000),
        ],
        budget=Budget(max_tokens=2_000_000, max_attempts=24),
        conflict_reserve_tokens=conflict,
        synthesis={
            "goal": "final S",
            "success_criteria": ["file:final.md"],
            "verification_policy": ["format_check", "rule_check"],
            "outputs": ["final.md"],
            "budget": {"max_tokens": 240_000, "max_attempts": 2},
        },
    )


def _proposal(mission, tasks, tokens):
    return change(
        mission.final_report["graph_version"],
        [
            add("F", [], tokens=tokens, parent_task_ids=[tasks["A"].id]),
        ],
    )


def _validate(mission, tasks, proposal, *, committed=None):
    return validate_change(
        mission,
        tasks,
        proposal,
        limits=ChangeLimits(),
        proposals_by_attempt={},
        committed_tokens_by_task=committed or {},
    )


def test_materialized_synthesis_allows_1920k_and_replays_commit_after_cold_open(tmp_path):
    from agent_orchestrator.orchestrator.commit_service import CommitService
    from agent_orchestrator.storage.store import Store

    service, mission, tasks = _graph(tmp_path)
    assert tasks["S"].kind == "synthesis"
    proposal = _proposal(mission, tasks, 400_000)
    created, receipt = service.commit_graph_change(mission.id, proposal, source={"test": "F"})
    assert created[0].budget.max_tokens == 400_000
    assert sum(t.budget.max_tokens for t in service.store.list_tasks(mission.id)) == 1_920_000
    service.store.close()
    cold = CommitService(Store.open(tmp_path / "orchestrator.db"))
    again, replay = cold.commit_graph_change(mission.id, proposal, source={"test": "F"})
    assert replay == receipt and [t.id for t in again] == [t.id for t in created]
    assert cold.store.count_events(mission.id, "TaskGraphChanged") == 1
    cold.store.close()


def test_2080k_still_rejected_and_graph_is_unchanged(tmp_path):
    service, mission, tasks = _graph(tmp_path, audit=480_000)
    with pytest.raises(CommitRejected, match="budget"):
        service.commit_graph_change(mission.id, _proposal(mission, tasks, 480_000), source={})
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 1
    assert len(service.store.list_tasks(mission.id)) == 4


def test_unmaterialized_synthesis_and_initial_planner_keep_full_reserve(tmp_path):
    _, mission, tasks = _graph(tmp_path)
    work = [t for t in tasks.values() if t.kind != "synthesis"]
    with pytest.raises(GraphChangeRejected, match="budget"):
        _validate(mission, work, _proposal(mission, tasks, 500_000))
    with pytest.raises(GraphRejected):
        validate_graph(
            mission, TaskGraphProposal.from_json({"tasks": [node("X", tokens=1_800_000)]})
        )


@pytest.mark.parametrize("materialized", [False, True])
def test_conflict_allocation_plus_remaining_reserve_is_protected_once(tmp_path, materialized):
    _, mission, tasks = _graph(tmp_path, conflict=100_000)
    current = list(tasks.values())
    if materialized:
        current.append(
            replace(
                tasks["A"],
                id="conflict-task",
                kind="conflict",
                budget=Budget(max_tokens=40_000),
                goal="independent conflict",
            )
        )
        mission = replace(
            mission, final_report={**mission.final_report, "conflict_reserve_remaining": 60_000}
        )
    # Existing work+S=1520K, conflict allocations+remaining=100K: exactly380K left.
    _validate(mission, current, _proposal(mission, tasks, 380_000))
    with pytest.raises(GraphChangeRejected, match="budget"):
        _validate(mission, current, _proposal(mission, tasks, 380_001))


@pytest.mark.parametrize("remaining", [None, -1, 100_001, 0])
def test_missing_or_inconsistent_conflict_balance_cannot_create_free_budget(tmp_path, remaining):
    _, mission, tasks = _graph(tmp_path, conflict=100_000)
    report = dict(mission.final_report)
    if remaining is None:
        report.pop("conflict_reserve_remaining")
    else:
        report["conflict_reserve_remaining"] = remaining
    mission = replace(mission, final_report=report)
    with pytest.raises(GraphChangeRejected, match="budget"):
        _validate(mission, list(tasks.values()), _proposal(mission, tasks, 400_000))


@pytest.mark.parametrize("cancel_in_proposal", [False, True])
def test_cancelled_task_keeps_settled_and_inflight_commitment(tmp_path, cancel_in_proposal):
    _, mission, tasks = _graph(tmp_path)
    current = list(tasks.values())
    operations = []
    if cancel_in_proposal:
        operations.extend(
            [
                {"op": "cancel_task", "task_id": tasks["A"].id},
                {
                    "op": "retarget_dependencies",
                    "task_id": tasks["C"].id,
                    "dependencies": [tasks["B"].id],
                },
            ]
        )
    else:
        current = [
            replace(t, status=TaskStatus.CANCELLED)
            if t.id == tasks["A"].id
            else replace(t, dependency_ids=(tasks["B"].id,))
            if t.id == tasks["C"].id
            else t
            for t in current
        ]
    # A400K returns only300K: settled30K+inflight70K remains committed.
    for requested in (780_000, 780_001):
        proposal = change(
            1, [*operations, add("F", [], tokens=requested, parent_task_ids=[tasks["B"].id])]
        )
        if requested == 780_000:
            _validate(mission, current, proposal, committed={tasks["A"].id: 100_000})
        else:
            with pytest.raises(GraphChangeRejected, match="budget"):
                _validate(mission, current, proposal, committed={tasks["A"].id: 100_000})
