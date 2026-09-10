# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · slice A: the whole graph is committed atomically with a replayable receipt;
a rejected graph leaves the formal library untouched; dependents unblock only when every
dependency is COMPLETED."""

from __future__ import annotations

import pytest
from test_task_graph import DAG, TOOLS, node

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
)
from agent_orchestrator.storage.store import Store

SPEC = MissionSpec(
    goal="实现两个功能并交付",
    success_criteria=("pytest:tests",),
    tenant_id="t",
    idempotency_key="graph-1",
    allowed_tools=TOOLS,
    budget=Budget(max_tokens=100_000, max_attempts=3),
)


def _planning_service(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    return service, planning


def test_graph_commit_is_atomic_and_replayable(tmp_path):
    service, mission = _planning_service(tmp_path)
    proposal = TaskGraphProposal.from_json(DAG)
    tasks, receipt = service.commit_task_graph(
        mission.id, proposal, base_version=mission.version, source={"planner": 1}
    )
    assert [t.id.rsplit("-", 1)[1] for t in tasks] == ["1", "2", "3", "4", "5"]
    assert [t.status for t in tasks] == [TaskStatus.READY] + [TaskStatus.BLOCKED] * 4
    assert tasks[3].dependency_ids == (tasks[1].id, tasks[2].id)
    assert receipt["terminal_task_id"] == tasks[4].id
    assert service.store.get_mission(mission.id).status is MissionStatus.ACTIVE
    again, receipt2 = service.commit_task_graph(
        mission.id, proposal, base_version=mission.version, source={"planner": 1}
    )
    assert receipt2 == receipt and [t.id for t in again] == [t.id for t in tasks]
    assert service.store.count_events(mission.id, "TaskCommitted") == 5
    assert service.store.count_events(mission.id, "TaskGraphCommitted") == 1
    with service.store.transaction():
        for task in tasks:
            assert service.ledger.account(f"budget:{task.id}").limits.max_tokens == 10_000


def test_rejected_graph_writes_only_the_rejection_event(tmp_path):
    service, mission = _planning_service(tmp_path)
    cyclic = TaskGraphProposal.from_json({"tasks": [node("A", ["B"]), node("B", ["A"])]})
    with pytest.raises(CommitRejected) as exc:
        service.commit_task_graph(mission.id, cyclic, base_version=mission.version, source={})
    assert "cycle" in str(exc.value)
    assert service.store.list_tasks(mission.id) == []
    assert service.store.get_mission(mission.id).status is MissionStatus.PLANNING
    assert service.store.count_events(mission.id, "TaskGraphRejected") == 1
    # the Planner may re-propose against the same base version
    tasks, _ = service.commit_task_graph(
        mission.id, TaskGraphProposal.from_json(DAG), base_version=mission.version, source={}
    )
    assert len(tasks) == 5
    with pytest.raises(CommitRejected):  # a second graph is refused in step 3 (static DAG)
        service.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": [node("X")]}),
            base_version=service.store.get_mission(mission.id).version,
            source={},
        )


def test_dependents_unblock_only_when_all_dependencies_completed(tmp_path):
    service, mission = _planning_service(tmp_path)
    tasks, _ = service.commit_task_graph(
        mission.id, TaskGraphProposal.from_json(DAG), base_version=mission.version, source={}
    )
    a, b, c, d, e = tasks
    # simulate A completed through the store (the closure tests do it through the loop)
    from agent_orchestrator.orchestrator.state_machine import next_task

    def complete(task):
        current = service.store.get_task(task.id)
        active = next_task(current, TaskStatus.ACTIVE)
        service.store.update_task(active, expected_version=current.version)
        verifying = next_task(active, TaskStatus.VERIFYING)
        service.store.update_task(verifying, expected_version=active.version)
        service.store.update_task(
            next_task(verifying, TaskStatus.COMPLETED), expected_version=verifying.version
        )

    complete(a)
    unblocked = service.unblock_dependents(a.id)
    assert {t.id for t in unblocked} == {b.id, c.id}
    assert service.store.get_task(d.id).status is TaskStatus.BLOCKED
    complete(b)
    assert service.unblock_dependents(b.id) == []  # D still waits for C
    complete(c)
    assert [t.id for t in service.unblock_dependents(c.id)] == [d.id]
    assert service.store.get_task(e.id).status is TaskStatus.BLOCKED
    assert service.store.count_events(mission.id, "TaskUnblocked") == 3
