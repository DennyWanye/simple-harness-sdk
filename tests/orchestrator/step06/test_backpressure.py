# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-02 (D6-2 / D6-3): a slowed Verifier lets results pile up; at the high
watermark the scheduler raises backpressure — no new expansion of formula-tier Tasks,
reduced Worker concurrency, smaller reservations, no new Tasks from the Manager — and
only once the queue has drained to the low watermark is it cleared and work resumes."""

from __future__ import annotations

import asyncio

import pytest
from helpers_step06 import config, spec

from agent_orchestrator.contracts import (
    Attempt,
    AttemptStatus,
    Budget,
    MissionStatus,
    Task,
    TaskStatus,
)
from agent_orchestrator.graph.changes import (
    ChangeLimits,
    GraphChangeRejected,
    TaskGraphChange,
    validate_change,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.scheduling.allocator import allocate
from agent_orchestrator.scheduling.backpressure import RAISED, BackpressureState
from agent_orchestrator.testing.fixtures import (
    _recorder_task,
    _write_files_then,
    critic_step,
    demo_dynamic_dag_provider,
    graph_change_step,
)


def _doc_task(key, priority):
    return _recorder_task(
        key,
        f"写出文档 {key}.md（独立任务 {key}）",
        [],
        [f"file:{key}.md"],
        priority,
        [f"{key}.md"],
        policy=["format_check", "rule_check", "critic_review"],
    )


def _doc_script(key):
    return _write_files_then(
        {f"{key}.md": f"# {key}\n"}, test_path=None, summary=f"{key} 写出", claim=f"{key}.md 写出"
    )


# ------------------------------------------------------------------ S6-02 closure
def test_s6_02_a_slow_verifier_raises_backpressure_and_work_resumes_after_it_clears(tmp_path):
    keys = ["P1", "P2", "P3", "P4"]
    tasks = [_doc_task(k, 4.0 - i) for i, k in enumerate(keys)]
    provider = demo_dynamic_dag_provider(
        tasks=tasks,
        scripts={k: [] for k in keys},
        per_attempt={k: [_doc_script(k), _doc_script(k)] for k in keys},
        # the first round of critics fails every result (so retries are wanted while the
        # queue is backed up); the second round passes
        critic_steps=[critic_step(verdict="FAIL", criteria_met=False, blocker="再检查")] * 4
        + [critic_step(verdict="PASS", criteria_met=True)] * 4,
        critic_delay_seconds=0.25,
        manager_steps=[graph_change_step([])] * 8,
    )

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                max_concurrency=4,
                max_running_attempts=8,
                verifier_workers=1,
                max_pending_verifications=2,
                low_watermark_ratio=0.5,
                manager_after_failures=10,
            ),
            provider,
            poll_interval=0.02,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "bp",
                    success_criteria=tuple(f"file:{k}.md" for k in keys),
                    budget=Budget(max_tokens=600_000, max_attempts=32),
                )
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            events = store.list_events(mission.id)
            raised = [e for e in events if e.type == "BackpressureRaised"]
            cleared = [e for e in events if e.type == "BackpressureCleared"]
            # the queue backed up at least once and every raise was cleared again (hysteresis:
            # cleared only at the low watermark); several raise/clear rounds are legitimate —
            # results keep arriving while the single slow Verifier drains the queue
            assert raised and len(raised) == len(cleared), orchestrator.progress_log
            assert all(e.payload["dimension"] == "pending_verifications" for e in raised)
            assert all(e.payload["high"] == 2 and e.payload["low"] == 1 for e in raised)
            assert all(e.payload["observed"] >= 2 for e in raised)
            assert all(e.payload["observed"] <= 1 for e in cleared)
            windows = list(zip([e.seq for e in raised], [e.seq for e in cleared], strict=True))
            assert all(r < c for r, c in windows)
            created = [e for e in events if e.type == "AttemptCreated"]
            first_round = [e for e in created if e.payload["ordinal"] == 1]
            second_round = [e for e in created if e.payload["ordinal"] == 2]
            assert len(first_round) == 4 and len(second_round) == 4
            # while raised, no expansion of formula-tier Tasks: every Attempt was created outside
            # the raised windows, and every retry waited for the first clearing
            assert not [e for e in created if any(r < e.seq < c for r, c in windows)]
            assert all(e.seq > windows[0][1] for e in second_round)
            assert store.count_events(mission.id, "VerificationFailed") == 4
            assert store.count_events(mission.id, "VerificationPassed") == 4
            # the durable state carries the transition log (the single truth) and the limits
            document = store.get_scheduler_state("backpressure")
            levels = [t["level"] for t in document["log"]]
            assert levels == ["RAISED", "NORMAL"] * (len(levels) // 2) and len(levels) == 2 * len(
                raised
            )
            assert document["limits"]["max_pending_verifications"] == 2
            assert orchestrator.pressure.level == "NORMAL" and orchestrator.pressure.changes == len(
                levels
            )
            # allocation plans made under pressure are visible on the Attempts' intents
            for e in second_round:
                intent = store.get_intent_for_subject(e.attempt_id)
                assert intent.config["allocation"]["allocator_version"] == "allocator-v1"

    asyncio.run(case())


# ------------------------------------------------------------------ D6-3 unit: the gate
def _task(tid, *, priority=1.0, kind="work", ready_at=0.0):
    return Task(
        id=tid,
        mission_id="m",
        parent_task_ids=(),
        dependency_ids=(),
        goal=f"goal {tid}",
        rationale="r",
        success_criteria=("file:x.md",),
        verification_policy=("format_check",),
        allowed_tools=("workspace_list",),
        budget=Budget(max_tokens=1000, max_attempts=3),
        priority=priority,
        status=TaskStatus.READY,
        version=1,
        root_goal="root",
        created_at=0.0,
        kind=kind,
        ready_at=ready_at,
    )


def _attempt(task_id, ordinal, status=AttemptStatus.RETRY_WAIT):
    return Attempt(
        id=f"{task_id}:attempt-{ordinal}",
        task_id=task_id,
        mission_id="m",
        role="worker",
        model="x",
        prompt_version="w",
        context_version="c",
        budget_reserved=Budget(),
        lease_owner=None,
        lease_expires_at=None,
        status=status,
        retry_of=None,
        created_at=0.0,
        version=1,
        ordinal=ordinal,
        creation_key="k",
        input_id="i",
    )


def test_under_pressure_only_conflict_and_starving_tasks_expand_plus_one_exploration_slot():
    raised = BackpressureState(level=RAISED, raised={"pending_verifications": {}}, since=1.0)
    tasks = [
        _task("m:task-1", priority=5.0, ready_at=100.0),  # formula tier, already tried
        _task("m:task-2", priority=4.0, ready_at=100.0),  # formula tier, never tried → exploration
        _task("m:task-3", priority=3.0, ready_at=100.0),  # formula tier, never tried → waits
        _task("m:task-4", priority=0.5, ready_at=0.0),  # starving (waited a whole window)
        _task("m:task-5", priority=0.1, kind="conflict", ready_at=100.0),  # conflict tier
    ]
    attempts = [_attempt("m:task-1", 1)]
    plan = allocate(
        tasks, attempts, concurrency_limit=8, now=350.0, aging_window_seconds=300.0, pressure=raised
    )
    assert plan.concurrency_limit == 4 and plan.pressure == "RAISED"  # halved
    assert [t.id for t, _ in plan.grants] == ["m:task-5", "m:task-4", "m:task-2"]
    calm = allocate(tasks, attempts, concurrency_limit=8, now=350.0, aging_window_seconds=300.0)
    assert len(calm.grants) == 5 and calm.pressure is None
    none = allocate(
        tasks,
        attempts,
        concurrency_limit=8,
        now=350.0,
        aging_window_seconds=300.0,
        pressure=raised,
        exploration_slots=0,
    )
    assert [t.id for t, _ in none.grants] == ["m:task-5", "m:task-4"]


def test_under_pressure_a_manager_may_not_add_tasks_but_may_still_change_roles(tmp_path):
    from graph_helpers6 import add, change, graph_service

    service, mission, t = graph_service(tmp_path)
    tasks = service.store.list_tasks(mission.id)
    growing = change(1, [add("X", [t["A"].id], parent_task_ids=[t["A"].id])])
    with pytest.raises(GraphChangeRejected) as rejected:
        validate_change(
            mission,
            tasks,
            growing,
            limits=ChangeLimits(admit_new_tasks=False),
            proposals_by_attempt={},
            committed_tokens_by_task={},
        )
    assert rejected.value.reason == "backpressure"
    steering = TaskGraphChange.from_json(
        {
            "base_graph_version": 1,
            "basis": {"trigger": "t"},
            "rationale": "换角色",
            "operations": [{"op": "set_role", "task_id": t["A"].id, "role": "simplifier"}],
        }
    )
    validated = validate_change(
        mission,
        tasks,
        steering,
        limits=ChangeLimits(admit_new_tasks=False),
        proposals_by_attempt={},
        committed_tokens_by_task={},
    )
    assert validated.roles == {t["A"].id: "simplifier"}
    validate_change(
        mission,
        tasks,
        growing,
        limits=ChangeLimits(admit_new_tasks=True),
        proposals_by_attempt={},
        committed_tokens_by_task={},
    )
