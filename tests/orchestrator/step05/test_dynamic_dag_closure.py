# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 5 · the ORCH §7.4 demo end to end on fixtures: a change of plan in the middle of a
Mission keeps every unrelated completed result (S5-05: no new Attempt, no new charge, no
new model call for A/D), the superseded Worker's late candidate is history only (S5-06),
and the user can read v1 → v2 with its basis and the old work off the graph history."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_orchestrator.__main__ import EXIT_OK, main
from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.observability.graph_history import graph_history
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RECORDER_IMPL,
    RECORDER_SEED,
    RECORDER_SPEC,
    RECORDER_TASKS,
    demo_dynamic_dag_provider,
    knowledge_envelope_step,
    recorder_scripts,
    typed_claim,
)


def spec(key, **overrides):
    base = dict(
        goal=RECORDER_SPEC["goal"],
        success_criteria=tuple(RECORDER_SPEC["success_criteria"]),
        tenant_id="tenant-5",
        idempotency_key=key,
        allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=300_000, max_attempts=24),
        workspace_seed=RECORDER_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


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


async def wait_until(predicate, *, timeout=30.0, interval=0.05):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


def test_s5_05_and_s5_06_unrelated_results_are_kept_and_the_superseded_late_candidate_is_history(
    tmp_path,
):
    hold = asyncio.Event()
    scripts = recorder_scripts()
    late_candidate = [
        ("workspace_write_file", {"path": "recorder.py", "content": RECORDER_IMPL}),
        knowledge_envelope_step(
            summary="迟到的实现",
            artifacts=["recorder.py"],
            claims=[typed_claim("late", evidence=["recorder.py"])],
            cite_knowledge=False,
        ),
    ]
    per_attempt = {
        "A": [scripts["A"], scripts["A"]],
        "D": [scripts["D"], scripts["D"]],
        "B": [scripts["B"], late_candidate],  # the second candidate is held until B is superseded
        "E": [scripts["E"], scripts["E"]],
        "B2": [scripts["B2"], scripts["B2"]],
        "C": [scripts["C"], scripts["C"]],
    }
    provider = demo_dynamic_dag_provider(per_attempt=per_attempt, holds={"B": [None, hold]})

    async def case():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence",
            max_concurrency=2,
            candidates_per_task=2,
            test_timeout_seconds=60,
        )
        async with Orchestrator(config, provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s5-05"))
            runner = asyncio.create_task(orchestrator.run())
            store = orchestrator.store
            await wait_until(
                lambda: any(t.status is TaskStatus.CANCELLED for t in store.list_tasks(mission.id))
            )
            t = by_key(store, mission.id)
            a_attempts_at_change = len(store.list_attempts(t["A"].id))
            d_attempts_at_change = len(store.list_attempts(t["D"].id))
            a_calls_at_change = provider.calls_by_key.get("A", 0)
            reserved_at_change = store.count_events(mission.id, "BudgetReserved")
            hold.set()  # the superseded candidate now finishes its turn
            await runner
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            t = by_key(store, mission.id)
            # S5-05: A and D were not redone or re-charged after the change
            assert t["A"].status is TaskStatus.COMPLETED and t["D"].status is TaskStatus.COMPLETED
            assert len(store.list_attempts(t["A"].id)) == a_attempts_at_change
            assert len(store.list_attempts(t["D"].id)) == d_attempts_at_change
            assert provider.calls_by_key.get("A", 0) == a_calls_at_change
            events = store.list_events(mission.id)
            changed_seq = next(e.seq for e in events if e.type == "TaskGraphChanged")
            for key in ("A", "D"):
                assert not any(
                    e.type == "BudgetReserved" and e.task_id == t[key].id and e.seq > changed_seq
                    for e in events
                )
            assert (
                store.count_events(mission.id, "BudgetReserved") > reserved_at_change
            )  # only the new work reserved
            # S5-06: B's held candidate was cancelled by the change and its late result is history
            b_attempts = store.list_attempts(t["B"].id)
            statuses = sorted(a.status.value for a in b_attempts)
            assert statuses == ["CANCELLED", "RETRY_WAIT"]
            late = next(a for a in b_attempts if a.status is AttemptStatus.CANCELLED)
            rejected = [e for e in events if e.type == "ResultRejected" and e.attempt_id == late.id]
            assert rejected and rejected[0].payload["reason"] == "superseded"
            assert t["B"].status is TaskStatus.CANCELLED and t["B"].accepted_result_id is None
            intent = store.get_intent_for_subject(late.id)
            assert intent.state == "SETTLED"  # collected: usage imported, reservation settled
            with store.transaction():
                reservation = orchestrator.commit.ledger.reservation(late.id)
            assert reservation["state"] == "SETTLED"
            # v1 → v2 readable: basis, operations, old work of the superseded Task
            history = graph_history(store, mission.id)
            assert history["current_version"] == 2 and [
                v["version"] for v in history["versions"]
            ] == [1, 2]
            v2 = history["versions"][1]["change"]
            assert v2["basis"]["trigger"].startswith("outcome:") and v2["superseded"] == {
                t["B"].id: t["B2"].id
            }
            assert v2["old_work"][0]["task_id"] == t["B"].id
            v1_ids = {x["task_id"] for x in history["versions"][0]["tasks"]}
            v2_ids = {x["task_id"] for x in history["versions"][1]["tasks"]}
            assert (
                t["B"].id in v1_ids
                and t["B"].id not in v2_ids
                and {t["E"].id, t["B2"].id} <= v2_ids
            )

    asyncio.run(case())


def test_demo_dynamic_dag_on_fixtures_writes_evidence(tmp_path, capsys):
    evidence = Path(tmp_path) / "evidence" / "s5"
    code = main(
        [
            "demo",
            "--scenario",
            "dynamic-dag",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            "demo-s5",
            "--max-concurrency",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    report = json.loads(out)
    assert report["status"] == "COMPLETED" and report["graph_version"] == 2
    assert len(report["graph_changes"]) == 1 and report["graph_changes"][0]["superseded"]
    kinds = sorted((t["kind"], t["status"]) for t in report["tasks"])
    assert kinds.count(("work", "CANCELLED")) == 1 and kinds.count(("work", "COMPLETED")) == 5
    present = {p.name for p in evidence.iterdir() if p.is_file()}
    assert {
        "baseline.json",
        "events.jsonl",
        "final_state.json",
        "verification.json",
        "costs.json",
        "test-report.json",
        "graph_history.json",
        "lineage.json",
    } <= present
    history = json.loads((evidence / "graph_history.json").read_text())
    assert history["versions"][1]["change"]["operations"]
    events = [
        json.loads(line)["type"] for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    for expected in (
        "OutcomeRecorded",
        "ManagementRequested",
        "TaskGraphChanged",
        "TaskSuperseded",
        "TaskDependenciesRewritten",
        "ManagementDecided",
        "MissionCompleted",
    ):
        assert expected in events, expected
