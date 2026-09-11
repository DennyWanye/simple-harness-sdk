# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.1 遗留修复（plans/2026-09-12-phase3/p31-fixes）· FX-1..FX-5.

F-ORCH-1: a Task budget below what one model turn can cost (plus the Critic's own share
when the Task names critic_review) is structurally unable to succeed — the Graph Manager
refuses it (``task_budget_below_floor``) and the proposer is told why; it never invents a
number for the model.  System tasks (synthesis / conflict) are not proposals and are not
bound by the floor.
F-ORCH-3: the artifacts of an accepted result are VERIFIED in the accept transaction; the
artifacts of a failed or superseded result stay UNVERIFIED.

DRAFT (test first): lives in the plan directory until the plan review has concluded; then
it moves to tests/orchestrator/host_support/.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.graph.changes import ChangeLimits, GraphChangeRejected, validate_change
from agent_orchestrator.graph.task_graph import (
    GraphRejected,
    TaskBudgetFloor,
    TaskGraphProposal,
    validate_graph,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    package_of,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "step05"))
from graph_helpers import add, change, graph_service  # noqa: E402

TOOLS3 = ("workspace_read_file", "workspace_write_file", "workspace_list")
OFF = DeploymentPolicy(allowed_tools=TOOLS3, local_code_execution=False)
WITH_CRITIC = ["format_check", "rule_check", "critic_review"]
NO_CRITIC = ["format_check", "rule_check"]
FLOOR = TaskBudgetFloor(base=4096, critic=6000)


# ------------------------------------------------------------------ helpers
def mission(**overrides):
    base = dict(
        id="mission-floor",
        goal="写 NOTES.md",
        success_criteria=("file:NOTES.md",),
        stop_conditions=(),
        allowed_tools=TOOLS3,
        risk_level="sandbox",
        budget=Budget(max_attempts=3),  # max_tokens None: the Host / UI "left blank" case
        tenant_id="t",
        status=MissionStatus.PLANNING,
        created_at=1.0,
        version=2,
        idempotency_key="floor",
    )
    base.update(overrides)
    return Mission(**base)


def node(key, *, tokens, policy, deps=()):
    budget = {"max_attempts": 2}
    if tokens is not None:
        budget["max_tokens"] = tokens
    return {
        "key": key,
        "goal": f"任务 {key}",
        "rationale": f"{key} 是计划的一部分",
        "dependencies": list(deps),
        "success_criteria": [f"file:{key}.md"],
        "verification_policy": list(policy),
        "allowed_tools": list(TOOLS3),
        "budget": budget,
        "outputs": [f"{key}.md"],
    }


def proposal(*nodes):
    return TaskGraphProposal.from_json({"tasks": list(nodes)})


# ------------------------------------------------------------------ FX-1 the graph gate
@pytest.mark.parametrize(
    ("tokens", "policy", "floor", "refused"),
    [
        (800, WITH_CRITIC, FLOOR, True),  # the native run: 800 with critic_review
        (800, NO_CRITIC, FLOOR, True),  # below one turn even without a Critic
        (4095, NO_CRITIC, FLOOR, True),
        (4096, NO_CRITIC, FLOOR, False),  # exactly the floor passes
        (10_095, WITH_CRITIC, FLOOR, True),
        (10_096, WITH_CRITIC, FLOOR, False),  # base + critic share
        (800, WITH_CRITIC, TaskBudgetFloor(base=0, critic=6000), False),  # 0 switches it off
    ],
)
def test_the_graph_gate_refuses_a_task_budget_below_the_floor(tokens, policy, floor, refused):
    graph = proposal(node("A", tokens=tokens, policy=policy))
    if refused:
        with pytest.raises(GraphRejected) as rejected:
            validate_graph(mission(), graph, task_floor=floor)
        assert rejected.value.reason == "budget"
        text = str(rejected.value)
        assert "task_budget_below_floor" in text
        assert str(floor.floor_for(policy)) in text  # the floor the proposer must meet
    else:
        validate_graph(mission(), graph, task_floor=floor)


def test_a_pool_share_below_the_floor_is_refused_too():
    # the Mission bounds tokens, the Planner leaves them blank: 3 × (20000 // 3) < 10096
    bounded = mission(budget=Budget(max_tokens=20_000, max_attempts=3))
    graph = proposal(*(node(k, tokens=None, policy=WITH_CRITIC) for k in "ABC"))
    with pytest.raises(GraphRejected) as rejected:
        validate_graph(bounded, graph, task_floor=FLOOR)
    assert "task_budget_below_floor" in str(rejected.value)
    # the same shares without a Critic clear the base floor (6666 ≥ 4096)
    validate_graph(bounded, proposal(*(node(k, tokens=None, policy=NO_CRITIC) for k in "ABC")), task_floor=FLOOR)


def test_no_floor_argument_keeps_the_old_behaviour():
    validate_graph(mission(), proposal(node("A", tokens=800, policy=WITH_CRITIC)))


def test_a_synthesis_template_is_not_bound_by_the_floor():
    template = {
        "goal": "汇总各部分",
        "success_criteria": ["file:SUMMARY.md"],
        "budget": {"max_tokens": 1000, "max_attempts": 1},
    }
    bounded = mission(budget=Budget(max_tokens=100_000, max_attempts=6), final_report={"synthesis": template})
    validate_graph(bounded, proposal(node("A", tokens=30_000, policy=WITH_CRITIC)), task_floor=FLOOR)


# ------------------------------------------------------------------ FX-2 the change gate
def test_a_manager_add_task_below_the_floor_is_refused(tmp_path):
    service, current, t = graph_service(tmp_path)
    tasks = service.store.list_tasks(current.id)
    low = change(1, [add("E", [t["C"].id], budget={"max_tokens": 800, "max_attempts": 2}, parent_task_ids=[t["C"].id])])
    with pytest.raises(GraphChangeRejected) as rejected:
        validate_change(
            current,
            tasks,
            low,
            limits=ChangeLimits(),
            proposals_by_attempt={},
            committed_tokens_by_task={},
            task_floor=FLOOR,
        )
    assert rejected.value.reason == "budget" and "task_budget_below_floor" in str(rejected.value)
    enough = change(1, [add("E", [t["C"].id], budget={"max_tokens": 5000, "max_attempts": 2}, parent_task_ids=[t["C"].id])])
    validate_change(
        current,
        tasks,
        enough,
        limits=ChangeLimits(),
        proposals_by_attempt={},
        committed_tokens_by_task={},
        task_floor=FLOOR,
    )


def test_a_default_share_below_the_floor_is_refused(tmp_path):
    # DIAMOND commits 4 × 20000; a pool of 82000 leaves 2000 for a new node without a budget
    service, current, t = graph_service(tmp_path, budget=Budget(max_tokens=82_000, max_attempts=12))
    tasks = service.store.list_tasks(current.id)
    blank = change(1, [add("E", [t["C"].id], budget={"max_attempts": 2}, parent_task_ids=[t["C"].id])])
    with pytest.raises(GraphChangeRejected) as rejected:
        validate_change(
            current,
            tasks,
            blank,
            limits=ChangeLimits(),
            proposals_by_attempt={},
            committed_tokens_by_task={},
            task_floor=FLOOR,
        )
    assert "task_budget_below_floor" in str(rejected.value)


# ------------------------------------------------------------------ FX-3 feedback and replan
def _config(tmp_path, **overrides):
    base = dict(
        evidence_root=Path(tmp_path) / "evidence",
        max_concurrency=1,
        test_timeout_seconds=60,
        dynamic_graph=False,
        deployment_policy=OFF,
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def _spec(key, *, budget=None):
    return MissionSpec(
        goal="写一份 NOTES.md，列出三个要点",
        success_criteria=("file:NOTES.md",),
        tenant_id="tenant-floor",
        idempotency_key=key,
        allowed_tools=TOOLS3,
        budget=budget or Budget(max_attempts=6),  # tokens unbounded, as the native run
    )


def _task(tokens, policy=WITH_CRITIC, attempts=2):
    return {
        "key": "A",
        "goal": "写 NOTES.md",
        "rationale": "Mission 只有这一件工作",
        "dependencies": [],
        "success_criteria": ["file:NOTES.md"],
        "verification_policy": list(policy),
        "outputs": ["NOTES.md"],
        "allowed_tools": list(TOOLS3),
        "budget": {"max_tokens": tokens, "max_attempts": attempts},
        "priority": 1.0,
    }


def _worker():
    return [
        ("workspace_write_file", {"path": "NOTES.md", "content": "- 一\n- 二\n- 三\n"}),
        envelope_step(summary="写好了", artifacts=["NOTES.md"], claims=["NOTES.md 有三个要点"]),
    ]


def _capturing(step, seen):
    def wrapped(request):
        seen.append(package_of(request))
        return step(request) if callable(step) else step

    return wrapped


def test_a_refused_budget_reaches_the_planner_and_the_replan_completes(tmp_path):
    seen: list[dict] = []
    provider = RoleScriptedProvider(
        {
            "planner": [
                _capturing(graph_proposal_step([_task(800)]), seen),
                _capturing(graph_proposal_step([_task(30_000)]), seen),
            ],
            "worker": _worker(),
            "critic": [critic_step(verdict="PASS", criteria_met=True) for _ in range(4)],
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            created = await orchestrator.submit_mission(_spec("fx3"))
            await orchestrator.run()
            store = orchestrator.store
            return store.get_mission(created.id), store.list_events(created.id), store.list_tasks(created.id)

    current, events, tasks = asyncio.run(run())
    assert str(current.status) == "COMPLETED"
    rejected = [e for e in events if e.type == "TaskGraphRejected"]
    assert rejected and "task_budget_below_floor" in json.dumps(rejected[0].payload, ensure_ascii=False)
    assert [t.budget.max_tokens for t in tasks] == [30_000]
    assert len(seen) == 2
    for package in seen:
        assert package["budget_for_tasks"]["min_task_tokens"] == 4096
        assert package["budget_for_tasks"]["min_task_tokens_with_critic_review"] == 10_096
    assert "task_budget_below_floor" in json.dumps(seen[1]["planning_rejected"], ensure_ascii=False)


def test_min_task_tokens_zero_switches_the_floor_off(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task(800)])],
            "worker": _worker(),
            "critic": [critic_step(verdict="PASS", criteria_met=True) for _ in range(4)],
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path, min_task_tokens=0), provider) as orchestrator:
            created = await orchestrator.submit_mission(_spec("fx3-off"))
            await orchestrator.run()
            return orchestrator.store.list_events(created.id), orchestrator.store.list_tasks(created.id)

    events, tasks = asyncio.run(run())
    assert not [e for e in events if e.type == "TaskGraphRejected"]
    assert [t.budget.max_tokens for t in tasks] == [800]


# ------------------------------------------------------------------ FX-5 artifact status
def test_accepted_artifacts_are_verified_and_rejected_ones_stay_unverified(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task(60_000, attempts=3)])],
            "worker": _worker() + _worker(),
            "critic": [critic_step(verdict="FAIL", criteria_met=False, blocker="要点不够具体")]
            + [critic_step(verdict="PASS", criteria_met=True) for _ in range(4)],
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            created = await orchestrator.submit_mission(_spec("fx5", budget=Budget(max_tokens=300_000, max_attempts=6)))
            await orchestrator.run()
            store = orchestrator.store
            return (
                store.get_mission(created.id),
                store.list_tasks(created.id),
                store.list_mission_artifacts(created.id),
            )

    current, tasks, artifacts = asyncio.run(run())
    assert str(current.status) == "COMPLETED"
    accepted = set(tasks[0].accepted_artifacts)
    assert accepted, "the completed Task names the artifacts it accepted"
    assert len(artifacts) >= 2, "the failed first Attempt's artifact is kept as history"
    for artifact in artifacts:
        expected = "VERIFIED" if artifact.id in accepted else "UNVERIFIED"
        assert artifact.verification_status == expected, (artifact.id, artifact.attempt_id)
