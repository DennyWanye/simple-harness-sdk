# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 5 · code review round 1 dispositions (reports/code-review-round1.md): one decisive
test per fix — P0-1 pause is only for READY/BLOCKED, P1-1 a retarget naming the superseded
task follows the replacement, P1-2 the two missing Manager triggers (PASS + proposed_tasks,
repeated verification failures), P1-3 the manager intent settles only after the change is
durable, P1-4 ``AllocationDecided`` is on the timeline, P2-1/2/4/7/8/9 as named below."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from graph_helpers import add, change, complete, drive_to_running, graph_service

from agent_orchestrator.artifacts.versioning import ancestors
from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.graph.changes import ChangeLimits, GraphChangeRejected, validate_change
from agent_orchestrator.orchestrator.commit_service import CommitRejected, MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import InjectedCrash
from agent_orchestrator.testing.fixtures import (
    RECORDER_SEED,
    RECORDER_SPEC,
    RECORDER_TASKS,
    _recorder_task,
    _write_files_then,
    demo_dynamic_dag_provider,
    demo_single_task_provider,
    graph_change_step,
    outcome_step,
    recorder_scripts,
)


def spec(key, **overrides):
    base = dict(
        goal=RECORDER_SPEC["goal"],
        success_criteria=tuple(RECORDER_SPEC["success_criteria"]),
        tenant_id="tenant-5",
        idempotency_key=key,
        allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=300_000, max_attempts=16),
        workspace_seed=RECORDER_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


def config(tmp_path, **overrides):
    base = dict(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def only(keys):
    return [t for t in RECORDER_TASKS if t["key"] in keys]


def with_proposed(steps, proposed):
    """The recorder A script, its final envelope carrying ``proposed_tasks``."""

    *head, last = steps

    def step(request):
        text = last(request)
        start, end = (
            text.index("<result_envelope>") + len("<result_envelope>"),
            text.rindex("</result_envelope>"),
        )
        body = json.loads(text[start:end])
        body["proposed_tasks"] = list(proposed)
        return "<result_envelope>" + json.dumps(body, ensure_ascii=False) + "</result_envelope>"

    return [*head, step]


# ------------------------------------------------------------------ P0-1
def test_p0_1_pause_is_legal_only_for_a_ready_or_blocked_task(tmp_path):
    service, mission, t = graph_service(tmp_path)
    complete(service, t["A"])
    drive_to_running(service, t["B"], agent="agent-b", turn="turn-b")
    tasks = service.store.list_tasks(mission.id)
    executing = change(1, [{"op": "pause_task", "task_id": t["B"].id, "reason": "等一等"}])
    with pytest.raises(GraphChangeRejected) as rejected:
        validate_change(
            mission,
            tasks,
            executing,
            limits=ChangeLimits(),
            proposals_by_attempt={},
            committed_tokens_by_task={},
        )
    assert rejected.value.reason == "illegal_transition" and "superseded or cancelled" in str(
        rejected.value
    )
    with pytest.raises(CommitRejected):
        service.commit_graph_change(mission.id, executing, source={"intent_id": "m-1"})
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 1
    # a READY route can be parked, and the Mission still ends: D paused → judged not needed
    parked = change(1, [{"op": "pause_task", "task_id": t["D"].id, "reason": "先不做"}])
    service.commit_graph_change(mission.id, parked, source={"intent_id": "m-2"})
    assert service.store.get_task(t["D"].id).paused is True


# ------------------------------------------------------------------ P1-1
def test_p1_1_a_retarget_naming_the_superseded_task_follows_the_replacement(tmp_path):
    service, mission, t = graph_service(tmp_path)
    complete(service, t["A"])
    proposal = change(
        1,
        [
            add("B2", [t["A"].id]),
            {"op": "supersede_task", "task_id": t["B"].id, "replacement_key": "B2"},
            {"op": "retarget_dependencies", "task_id": t["C"].id, "dependencies": [t["B"].id]},
        ],
    )
    (b2,), receipt = service.commit_graph_change(mission.id, proposal, source={"intent_id": "m-1"})
    c = service.store.get_task(t["C"].id)
    assert c.dependency_ids == (b2.id,) and c.status is TaskStatus.BLOCKED
    assert service.store.get_task(t["B"].id).status is TaskStatus.CANCELLED
    # P2-1: the rewired dependent is on the receipt's affected list
    assert t["C"].id in receipt["affected_task_ids"]
    # P2-2: the retired route contributes nothing downstream
    tasks_by_id = {x.id: x for x in service.store.list_tasks(mission.id)}
    assert {x.id for x in ancestors(c.id, tasks_by_id)} == {t["A"].id, b2.id}
    complete(service, service.store.get_task(b2.id), agent="agent-b2", turn="turn-b2")
    assert service.store.get_task(c.id).status is TaskStatus.READY  # the old B never blocks C


# ------------------------------------------------------------------ P1-2 (a): PASS + proposed_tasks
def test_p1_2_a_passing_result_with_proposed_tasks_opens_one_management_decision(tmp_path):
    proposed = [
        {"goal": "补一份术语表", "reason": "分析里引用了未定义术语", "estimated_cost_tokens": 3000}
    ]
    provider = demo_dynamic_dag_provider(
        tasks=only("AD"),  # D (lower priority) is still open when A's decision is requested
        scripts={
            "A": with_proposed(recorder_scripts()["A"], proposed),
            "D": recorder_scripts()["D"],
        },
        manager_steps=[graph_change_step([], rationale="现有计划已覆盖，保持")],
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p1-2a", success_criteria=("file:DOCS.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            events = store.list_events(mission.id)
            requested = [e for e in events if e.type == "ManagementRequested"]
            assert len(requested) == 1 and requested[0].payload["trigger"].startswith("proposed:")
            passed = next(e.seq for e in events if e.type == "VerificationPassed")
            assert passed < requested[0].seq  # after accept, never before
            decided = [e for e in events if e.type == "ManagementDecided"]
            assert decided[-1].payload["decision"] == "keep"
            assert store.count_events(mission.id, "TaskGraphChanged") == 0
            assert len(store.list_tasks(mission.id)) == 2  # the proposal stayed data (S5-01)

    asyncio.run(case())


# ------------------------------------------------------------------ P1-2 (b): repeated verification failures
def test_p1_2_repeated_verification_failures_open_a_management_decision_that_switches_role(
    tmp_path,
):
    # the Task is the implementation: two wrong recorders fail code_test, the third is right
    task = _recorder_task(
        "A",
        "实现 recorder.py 并通过 tests/test_recorder.py（独立任务）",
        [],
        ["pytest:tests/test_recorder.py"],
        3.0,
        ["recorder.py"],
        policy=["format_check", "rule_check", "code_test"],
    )
    wrong = _write_files_then(
        {"recorder.py": "def parse_line(line):\n    return {}\n"},
        test_path="tests/test_recorder.py",
        summary="实现完成",
        claim="tests/test_recorder.py 通过",
    )
    good = recorder_scripts()["B2"]

    def switch(package):
        return [
            {"op": "set_role", "task_id": package["trigger"]["task_id"], "role": "failure_analyst"}
        ]

    provider = demo_dynamic_dag_provider(
        tasks=[task],
        per_attempt={"A": [wrong, wrong, good]},
        manager_steps=[graph_change_step(switch)],
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, manager_after_failures=2), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p1-2b", success_criteria=("pytest:tests/test_recorder.py",))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            task = store.list_tasks(mission.id)[0]
            attempts = store.list_attempts(task.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert store.count_events(mission.id, "VerificationFailed") == 2
            requested = [
                e for e in store.list_events(mission.id) if e.type == "ManagementRequested"
            ]
            assert (
                len(requested) == 1 and requested[0].payload["trigger"] == f"failures:{task.id}:2"
            )
            assert attempts[2].role == "failure_analyst"

    asyncio.run(case())


# ------------------------------------------------------------------ P1-3
def test_p1_3_a_crash_between_proposal_and_commit_still_applies_the_change_once(tmp_path):
    provider = demo_dynamic_dag_provider()

    async def case():
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.3), provider, owner="orch-1"
        ) as first:
            mission = await first.submit_mission(spec("p1-3"))
            first.arm_fault("before_graph_change", kind="manager")
            with pytest.raises(InjectedCrash):
                await first.run()
            assert first.store.fired == ["before_graph_change:manager"]
            store = first.store
            assert store.count_events(mission.id, "TaskGraphChanged") == 0
            assert store.get_mission(mission.id).final_report["graph_version"] == 1
            pending = [
                i
                for i in store.list_intents("CLAIMED", "SUBMITTED", "PENDING")
                if i.kind == "manager"
            ]
            assert len(pending) == 1  # not settled: the decision is not durable yet
        await asyncio.sleep(0.35)
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.3), provider, owner="orch-2"
        ) as second:
            await second.run()
            store = second.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, second.progress_log
            assert final.final_report["graph_version"] == 2
            assert store.count_events(mission.id, "TaskGraphChanged") == 1
            managers = [i for i in store.list_intents("SETTLED", "FAILED") if i.kind == "manager"]
            assert len(managers) == 1 and managers[0].state == "SETTLED"
            assert (
                provider.by_role.get("manager", 0) == 1
            )  # the same turn was re-collected, not re-run

    asyncio.run(case())


# ------------------------------------------------------------------ P1-4 / S5-09
def test_p1_4_allocation_decided_is_on_the_timeline_and_matches_the_frozen_score(tmp_path):
    provider = demo_dynamic_dag_provider(
        tasks=only("AD"), scripts={"A": recorder_scripts()["A"], "D": recorder_scripts()["D"]}
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p1-4", success_criteria=("file:DOCS.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
            decided = {
                e.attempt_id: e
                for e in store.list_events(mission.id)
                if e.type == "AllocationDecided"
            }
            for task in store.list_tasks(mission.id):
                for attempt in store.list_attempts(task.id):
                    frozen = store.get_intent_for_subject(attempt.id).config["allocation"]
                    assert decided[attempt.id].payload == frozen
                    assert decided[attempt.id].task_id == task.id

    asyncio.run(case())


# ------------------------------------------------------------------ P2-7
def test_p2_7_an_add_task_without_a_budget_gets_a_bounded_share_not_the_pool(tmp_path):
    service, mission, t = graph_service(tmp_path)
    tasks = service.store.list_tasks(mission.id)
    proposal = change(
        1, [add("X", [t["A"].id], budget={"max_attempts": 2}, parent_task_ids=[t["A"].id])]
    )
    validated = validate_change(
        mission,
        tasks,
        proposal,
        limits=ChangeLimits(),
        proposals_by_attempt={},
        committed_tokens_by_task={},
    )
    pool = mission.budget.max_tokens
    assert 0 < validated.budget_pool["X"] <= pool // 4
    (x,), _ = service.commit_graph_change(mission.id, proposal, source={"intent_id": "m-1"})
    assert x.budget.max_tokens == validated.budget_pool["X"]


# ------------------------------------------------------------------ P2-8
def test_p2_8_with_dynamic_graph_off_a_blocked_outcome_just_retries(tmp_path):
    scripts = {"A": [outcome_step(outcome="blocked", summary="先卡一下")] + recorder_scripts()["A"]}
    provider = demo_dynamic_dag_provider(tasks=only("A"), scripts=scripts, manager_steps=[])

    async def case():
        async with Orchestrator(config(tmp_path, dynamic_graph=False), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p2-8", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            attempts = store.list_attempts(store.list_tasks(mission.id)[0].id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert store.count_events(mission.id, "ManagementRequested") == 0
            assert provider.by_role.get("manager", 0) == 0
            assert final.final_report["graph_version"] == 1

    asyncio.run(case())


# ------------------------------------------------------------------ P2-9
def test_p2_9_the_supersede_chain_limit_holds_along_the_real_commit_path(tmp_path):
    service, mission, t = graph_service(tmp_path)
    complete(service, t["A"])
    current = t["B"]
    for version, key in ((1, "B2"), (2, "B3")):
        proposal = change(
            version,
            [
                add(key, [t["A"].id]),
                {"op": "supersede_task", "task_id": current.id, "replacement_key": key},
            ],
        )
        (current,), _ = service.commit_graph_change(
            mission.id,
            proposal,
            source={"intent_id": f"m-{key}"},
            limits=ChangeLimits(max_supersede_chain=2),
        )
    assert current.context["supersede_depth"] == 2
    third = change(
        3,
        [
            add("B4", [t["A"].id]),
            {"op": "supersede_task", "task_id": current.id, "replacement_key": "B4"},
        ],
    )
    with pytest.raises(CommitRejected):
        service.commit_graph_change(
            mission.id,
            third,
            source={"intent_id": "m-B4"},
            limits=ChangeLimits(max_supersede_chain=2),
        )
    events = [
        e for e in service.store.list_events(mission.id) if e.type == "TaskGraphChangeRejected"
    ]
    assert events[-1].payload["reason"] == "supersede_chain"
    assert service.store.get_mission(mission.id).final_report["graph_version"] == 3


# ------------------------------------------------------------------ P2-4
def test_p2_4_the_single_task_path_records_graph_version_and_ready_at(tmp_path):
    provider = demo_single_task_provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                MissionSpec(
                    goal="实现 parse_kv",
                    success_criteria=("file:parse_kv.py",),
                    tenant_id="t",
                    idempotency_key="p2-4",
                    allowed_tools=(
                        "workspace_read_file",
                        "workspace_write_file",
                        "workspace_list",
                        "run_tests",
                    ),
                    budget=Budget(max_tokens=100_000, max_attempts=3),
                )
            )
            await orchestrator.run()
            task = orchestrator.store.list_tasks(mission.id)[0]
            assert task.context["graph_version"] == 1 and task.ready_at is not None

    asyncio.run(case())
