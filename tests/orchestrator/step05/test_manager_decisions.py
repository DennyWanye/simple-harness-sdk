# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 5 · slice B (D5-5…D5-7, D5-9, D5-10): a non-candidate outcome never reaches
verification, it opens one deduplicated management decision; the Manager's proposal
changes the graph only through Commit (S5-01), repeated no-progress ends in a change of
approach or an explicit stop (S5-02), a refused proposal is fed back once (S5-08) and the
same trigger never opens two decisions (S5-07)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RECORDER_SEED,
    RECORDER_SPEC,
    RECORDER_TASKS,
    demo_dynamic_dag_provider,
    graph_change_step,
    outcome_step,
    recorder_manager_change,
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


def by_key(store, mission_id):
    goals = {t["goal"]: t["key"] for t in RECORDER_TASKS}
    found = {}
    for task in store.list_tasks(mission_id):
        if task.goal in goals:
            found[goals[task.goal]] = task
        elif task.goal.startswith("确认时间戳"):
            found["E"] = task
        elif task.goal.startswith("按 FORMAT.md"):
            found["B2"] = task
    return found


def only(keys):
    return [t for t in RECORDER_TASKS if t["key"] in keys]


# ------------------------------------------------------------------ S5-01 (+ §7.4 on fixtures)
def test_s5_01_a_worker_proposal_reaches_the_graph_only_through_the_manager(tmp_path):
    provider = demo_dynamic_dag_provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s5-01"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            t = by_key(store, mission.id)
            assert set(t) == {"A", "B", "C", "D", "E", "B2"}
            events = store.list_events(mission.id)
            # B's blocked outcome: recorded, never verified, Worker proposal kept as data only
            b_attempts = store.list_attempts(t["B"].id)
            assert [a.status for a in b_attempts] == [AttemptStatus.RETRY_WAIT]
            assert b_attempts[0].failure["reason"] == "outcome_blocked" and b_attempts[0].failure[
                "proposed_tasks"
            ][0]["goal"].startswith("确认")
            b_result = store.find_result_for_attempt(b_attempts[0].id)
            assert (
                b_result.verification_state == "REJECTED" and b_result.verdict == "outcome:blocked"
            )
            assert not any(
                e.type == "VerificationStarted" and e.task_id == t["B"].id for e in events
            )
            outcome_seq = next(e.seq for e in events if e.type == "OutcomeRecorded")
            requested = [e for e in events if e.type == "ManagementRequested"]
            changed = [e for e in events if e.type == "TaskGraphChanged"]
            assert (
                len(requested) == 1
                and len(changed) == 1
                and outcome_seq < requested[0].seq < changed[0].seq
            )
            assert changed[0].payload["from_version"] == 1 and changed[0].payload["to_version"] == 2
            assert changed[0].payload["basis"]["result_id"] == b_result.envelope.id
            assert changed[0].payload["superseded"] == {t["B"].id: t["B2"].id}
            assert (
                t["E"].parent_task_ids == (t["B"].id,)
                and t["B2"].context["supersedes_task"] == t["B"].id
            )
            assert t["B"].status is TaskStatus.CANCELLED and t["B2"].status is TaskStatus.COMPLETED
            assert set(t["B2"].dependency_ids) == {t["A"].id, t["E"].id} and t[
                "C"
            ].dependency_ids == (t["B2"].id,)
            # the Worker's proposal itself never became a task: E exists because the Manager added it
            committed = [e for e in events if e.type == "TaskCommitted" and e.task_id == t["E"].id]
            assert committed[0].payload["source"]["template"] == "change"
            assert final.final_report["graph_version"] == 2
            decided = [e for e in events if e.type == "ManagementDecided"]
            assert decided[-1].payload["decision"] == "changed"

    asyncio.run(case())


# ------------------------------------------------------------------ S5-02
def test_s5_02_repeated_no_progress_without_a_change_of_approach_stops_explicitly(tmp_path):
    scripts = {"A": [outcome_step(outcome="no_progress", summary="没有进展")] * 3}
    provider = demo_dynamic_dag_provider(
        tasks=only("A"), scripts=scripts, manager_steps=[graph_change_step([])] * 3
    )

    async def case():
        async with Orchestrator(config(tmp_path, no_progress_limit=2), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s5-02a", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "no_progress"
            task = store.list_tasks(mission.id)[0]
            assert task.status is TaskStatus.FAILED and task.failure_reason == "no_progress"
            attempts = store.list_attempts(task.id)
            assert len(attempts) == 2 and all(
                a.failure["reason"] == "outcome_no_progress" for a in attempts
            )
            assert store.count_events(mission.id, "ManagementRequested") == 2
            assert final.final_report["detail"]["no_progress_count"] == 2
            assert len(store.list_tasks(mission.id)) == 1  # no splitting

    asyncio.run(case())


def test_s5_02_a_change_of_role_continues_the_task_with_the_new_approach(tmp_path):
    good = recorder_scripts()["A"]
    scripts = {"A": [outcome_step(outcome="no_progress", summary="没有进展")] * 2 + good}

    def switch(package):
        return [{"op": "set_role", "task_id": package["trigger"]["task_id"], "role": "simplifier"}]

    provider = demo_dynamic_dag_provider(
        tasks=only("A"),
        scripts=scripts,
        manager_steps=[graph_change_step([]), graph_change_step(switch)],
    )

    async def case():
        async with Orchestrator(config(tmp_path, no_progress_limit=2), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s5-02b", success_criteria=("file:analysis.md",))
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
            assert (
                attempts[2].role == "simplifier" and attempts[2].prompt_version == "simplifier-v1"
            )
            assert store.get_intent_for_subject(attempts[2].id).config["role"] == "simplifier"
            assert any(
                e.type == "TaskRoleChanged" and e.payload["role"] == "simplifier"
                for e in store.list_events(mission.id)
            )
            assert task.context["role"] == "simplifier"

    asyncio.run(case())


# ------------------------------------------------------------------ S5-07 / S5-08
def test_s5_07_and_s5_08_one_decision_per_trigger_and_a_refused_proposal_is_fed_back_once(tmp_path):
    def too_deep(package):  # X depends on the blocked task's own chain deeper than allowed
        task_id = package["trigger"]["task_id"]
        return [
            {
                "op": "add_task",
                "key": "X1",
                "goal": "第一层细化",
                "rationale": "细化",
                "dependencies": [task_id],
                "success_criteria": ["file:x1.md"],
                "verification_policy": ["format_check", "rule_check"],
                "budget": {"max_tokens": 5000},
                "parent_task_ids": [task_id],
                "outputs": ["x1.md"],
            },
            {
                "op": "add_task",
                "key": "X2",
                "goal": "第二层细化",
                "rationale": "细化",
                "dependencies": ["X1"],
                "success_criteria": ["file:x2.md"],
                "verification_policy": ["format_check", "rule_check"],
                "budget": {"max_tokens": 5000},
                "parent_task_ids": [task_id],
                "outputs": ["x2.md"],
            },
            {
                "op": "add_task",
                "key": "X3",
                "goal": "第三层细化",
                "rationale": "细化",
                "dependencies": ["X2"],
                "success_criteria": ["file:x3.md"],
                "verification_policy": ["format_check", "rule_check"],
                "budget": {"max_tokens": 5000},
                "parent_task_ids": [task_id],
                "outputs": ["x3.md"],
            },
        ]

    seen = {}

    def second(package):
        seen["rejections"] = package.get("rejections", [])
        return recorder_manager_change(package)

    provider = demo_dynamic_dag_provider(
        manager_steps=[graph_change_step(too_deep), graph_change_step(second)]
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, max_graph_depth=4), provider
        ) as orchestrator:  # §7.4 itself is 4 deep
            mission = await orchestrator.submit_mission(spec("s5-08"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            events = store.list_events(mission.id)
            rejected = [e for e in events if e.type == "TaskGraphChangeRejected"]
            assert (
                len(rejected) == 1
                and rejected[0].payload["reason"] == "depth"
                and "max_graph_depth=4" in rejected[0].payload["detail"]
            )
            assert (
                seen["rejections"] and seen["rejections"][0]["reason"] == "depth"
            )  # S5-08: fed back, not bypassed
            requested = [e.payload["trigger"] for e in events if e.type == "ManagementRequested"]
            assert (
                len(requested) == 2
                and requested[1].endswith(":retry-1")
                and requested[0] == requested[1].split(":retry-")[0]
            )
            # S5-07: the same trigger opened exactly one decision; the retry is a distinct, linked trigger
            intents = [i for i in store.list_intents("SETTLED", "FAILED") if i.kind == "manager"]
            assert len(intents) == 2 and len({i.config["result_id"] for i in intents}) == 1
            assert store.count_events(mission.id, "TaskGraphChanged") == 1
            assert final.final_report["graph_version"] == 2

    asyncio.run(case())


def test_s5_07_requesting_management_twice_for_one_trigger_creates_one_intent(tmp_path):
    provider = demo_dynamic_dag_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s5-07", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            first = await orchestrator._request_management(
                store.get_mission(mission.id),
                task,
                trigger="outcome:result-x",
                result_id=None,
                attempt_id=None,
            )
            second = await orchestrator._request_management(
                store.get_mission(mission.id),
                task,
                trigger="outcome:result-x",
                result_id=None,
                attempt_id=None,
            )
            assert first is not None and second is not None and first.intent_id == second.intent_id
            assert store.count_events(mission.id, "ManagementRequested") == 1

    asyncio.run(case())
