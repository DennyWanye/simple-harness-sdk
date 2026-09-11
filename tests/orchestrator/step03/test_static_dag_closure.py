# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · S3-01 / S3-02 / S3-03 / S3-05 / S3-06 / S3-08 on the deterministic, task-routed
fixture provider: a static A→(B‖C)→D→E graph is committed atomically, executed by the
Frontier / Allocator under the concurrency bound, artifacts flow downstream, a losing
candidate is superseded, the Mission is judged on the integrated tree and stops are
explainable and cascade correctly (BLOCKED tasks end with the Mission, never CANCELLED)."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
from fixtures_provider import graph_proposal_step

from agent_orchestrator.artifacts.workspace import sha256_file
from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    DEMO_DAG_SPEC,
    DEMO_DAG_TASKS,
    TEXTKIT_SEED,
    demo_static_dag_provider,
    demo_static_dag_scripts,
)

# model calls per Worker script (one per script step); an Attempt that ran once made exactly these
CALLS = {key: len(steps) for key, steps in demo_static_dag_scripts().items()}
CALLS_C_REPAIR = len(demo_static_dag_scripts(c_first_wrong=True)["C"])


def spec(key: str, **overrides) -> MissionSpec:
    base = dict(
        goal=DEMO_DAG_SPEC["goal"],
        success_criteria=tuple(DEMO_DAG_SPEC["success_criteria"]),
        tenant_id="tenant-3",
        idempotency_key=key,
        allowed_tools=tuple(DEMO_DAG_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=200_000, max_attempts=12),
        workspace_seed=TEXTKIT_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


def config(tmp_path, **overrides) -> OrchestratorConfig:
    base = dict(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=2, test_timeout_seconds=60
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def tasks_by_key(store, mission_id):
    """graph key → Task (ids follow the deterministic topological order A,B,C,D,E)."""

    tasks = store.list_tasks(mission_id)
    return dict(zip("ABCDE", tasks, strict=False))


async def wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


def event_seq(store, mission_id, event_type, **match):
    """Sequence number of the first event of ``event_type`` whose fields match."""

    for event in store.list_events(mission_id):
        if event.type == event_type and all(
            getattr(event, k, event.payload.get(k)) == v for k, v in match.items()
        ):
            return event.seq
    return None


# --------------------------------------------------------------------------- S3-01
def test_s3_01_static_dag_runs_in_parallel_and_flows_artifacts_downstream(tmp_path):
    hold_b, hold_c = asyncio.Event(), asyncio.Event()
    provider = demo_static_dag_provider(holds={"B": [hold_b], "C": [hold_c]})

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s3-01"))
            runner = asyncio.create_task(orchestrator.run())
            store = orchestrator.store
            # B and C are both in flight at the same time (two RUNNING Attempts)
            await wait_until(lambda: len(provider.inflight) == 2)
            by_key = tasks_by_key(store, mission.id)
            running = {t: [a.status for a in store.list_attempts(by_key[t].id)] for t in "ABCDE"}
            assert running["B"] == [AttemptStatus.RUNNING]
            assert running["C"] == [AttemptStatus.RUNNING]
            assert running["D"] == [] and running["E"] == []  # D waits for both
            assert by_key["D"].status is TaskStatus.BLOCKED
            hold_b.set()
            hold_c.set()
            await runner
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            assert final.stop_reason == "verification_passed"
            by_key = tasks_by_key(store, mission.id)
            assert all(t.status is TaskStatus.COMPLETED for t in by_key.values())
            assert provider.calls_by_key == CALLS  # each Worker script ran exactly once
            assert provider.max_inflight == 2
            # D's Attempt was created only after B and C were both COMPLETED
            d_attempt = store.list_attempts(by_key["D"].id)[0]
            created_d = event_seq(store, mission.id, "AttemptCreated", attempt_id=d_attempt.id)
            done_b = event_seq(store, mission.id, "TaskCompleted", task_id=by_key["B"].id)
            done_c = event_seq(store, mission.id, "TaskCompleted", task_id=by_key["C"].id)
            assert created_d > done_b and created_d > done_c
            # D's workspace holds B's and C's accepted artifacts with identical hashes
            workspaces = orchestrator.assembled.workspaces.root
            for key, path in (("B", "textkit/slug.py"), ("C", "textkit/count.py")):
                accepted = [store.get_artifact(a) for a in by_key[key].accepted_artifacts]
                artifact = next(a for a in accepted if a.path == path)
                assert sha256_file(workspaces / d_attempt.id / path) == artifact.content_hash
                # the upstream input was frozen into D's dispatch intent
                intent = store.get_intent_for_subject(d_attempt.id)
                assert {
                    "task_id": by_key[key].id,
                    "path": path,
                    "content_hash": artifact.content_hash,
                    "artifact_id": artifact.id,
                } in intent.config["inputs"]
            # artifact versions follow the (mission, path) lineage: A's stub is v1, B's implementation v2
            versions = sorted(
                (a.version, a.task_id)
                for a in store.list_mission_artifacts(mission.id)
                if a.path == "textkit/slug.py"
            )
            assert versions[0] == (1, by_key["A"].id) and versions[1] == (2, by_key["B"].id)
            # the Mission report covers every Task and the judgment ran on the integrated tree
            report = final.final_report
            assert [t["task_id"] for t in report["tasks"]] == [t.id for t in by_key.values()]
            assert [j["met"] for j in report["success_criteria"]] == [True, True]
            assert report["graph_version"] == 1
            judged_tree = workspaces / f"{mission.id}-judge-{orchestrator.owner}-verify"
            assert (judged_tree / "DELIVERY.md").is_file()
            assert "return len(text.split())" in (judged_tree / "textkit/count.py").read_text()

    asyncio.run(case())


# --------------------------------------------------------------------------- S3-02
def test_s3_02_cyclic_graph_is_rejected_whole_and_the_planner_reproposes(tmp_path):
    cyclic = copy.deepcopy(DEMO_DAG_TASKS[:2])
    cyclic[0]["dependencies"] = ["B"]  # A→B→A
    provider = demo_static_dag_provider(
        planner_steps=[graph_proposal_step(cyclic), graph_proposal_step(DEMO_DAG_TASKS)]
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s3-02"))
            await orchestrator.run()
            store = orchestrator.store
            events = [e for e in store.list_events(mission.id) if e.type == "TaskGraphRejected"]
            assert len(events) == 1 and events[0].payload["reason"] == "cycle"
            assert "A" in events[0].payload["detail"] and "B" in events[0].payload["detail"]
            first_commit = event_seq(store, mission.id, "TaskGraphCommitted")
            assert first_commit > events[0].seq  # nothing was written before the second proposal
            assert provider.by_role["planner"] == 2
            # the second proposal package carried the rejection as feedback (D3-2')
            planner_requests = [
                r for r in provider.requests if "[role:planner]" in str(r.messages[0].content)
            ]
            second = str(planner_requests[1].messages[-1].content)
            assert "planning_rejected" in second and "cycle" in second
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            assert len(store.list_tasks(mission.id)) == 5

    asyncio.run(case())


# --------------------------------------------------------------------------- S3-03
def test_s3_03_failed_sibling_repairs_alone_and_the_join_waits(tmp_path):
    provider = demo_static_dag_provider(c_first_wrong=True)

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s3-03"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            by_key = tasks_by_key(store, mission.id)
            b_attempts = store.list_attempts(by_key["B"].id)
            c_attempts = store.list_attempts(by_key["C"].id)
            assert [a.status for a in b_attempts] == [AttemptStatus.COMPLETED]  # B not redone
            assert [a.status for a in c_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert c_attempts[1].retry_of == c_attempts[0].id
            assert c_attempts[1].feedback and "code_test" in c_attempts[1].feedback[0]
            assert provider.calls_by_key["B"] == CALLS["B"]  # B's script ran once
            assert provider.calls_by_key["C"] == CALLS_C_REPAIR  # bad + repair
            # D stayed BLOCKED until C's repair passed: its Attempt was created after that
            d_attempt = store.list_attempts(by_key["D"].id)[0]
            created_d = event_seq(store, mission.id, "AttemptCreated", attempt_id=d_attempt.id)
            c_done = event_seq(store, mission.id, "TaskCompleted", task_id=by_key["C"].id)
            c_failed = event_seq(store, mission.id, "VerificationFailed", task_id=by_key["C"].id)
            assert c_failed < c_done < created_d
            # the repair Attempt started from the upstream inputs again (A's contract intact)
            contract = next(
                store.get_artifact(a)
                for a in by_key["A"].accepted_artifacts
                if store.get_artifact(a).path == "textkit/__init__.py"
            )
            repaired = orchestrator.assembled.workspaces.root / c_attempts[1].id
            assert sha256_file(repaired / "textkit/__init__.py") == contract.content_hash

    asyncio.run(case())


# --------------------------------------------------------------------------- S3-05
def test_s3_05_two_candidates_first_pass_accepted_second_superseded(tmp_path):
    hold_second = asyncio.Event()
    single = [dict(DEMO_DAG_TASKS[0], dependencies=[])]  # one Task: A alone
    scripts_a = demo_static_dag_provider().worker_by_key["A"]
    provider = demo_static_dag_provider(
        planner_steps=[graph_proposal_step(single)],
        holds={
            "A": [None, hold_second]
        },  # the second candidate is held until the first is accepted
        extra_scripts={"A": list(scripts_a)},
    )

    async def case():
        async with Orchestrator(config(tmp_path, candidates_per_task=2), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s3-05", success_criteria=("file:textkit/__init__.py",))
            )
            runner = asyncio.create_task(orchestrator.run())
            store = orchestrator.store
            await wait_until(
                lambda: any(t.status is TaskStatus.COMPLETED for t in store.list_tasks(mission.id))
            )
            task = store.list_tasks(mission.id)[0]
            statuses = {a.id: a.status for a in store.list_attempts(task.id)}
            assert sorted(statuses.values()) == [AttemptStatus.COMPLETED, AttemptStatus.SUPERSEDED]
            hold_second.set()  # the loser now finishes its turn: a late result
            await runner
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            attempts = store.list_attempts(task.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.COMPLETED,
                AttemptStatus.SUPERSEDED,
            ]
            loser = attempts[1]
            assert (
                task.accepted_result_id == store.find_result_for_attempt(attempts[0].id).envelope.id
            )
            # both candidates counted against max_attempts; the loser was cancelled (receipt) and charged
            assert task.attempt_count == 2
            assert any(r["attempt_id"] == loser.id for r in orchestrator.cancel_receipts)
            with store.transaction():
                reservation = orchestrator.commit.ledger.reservation(loser.id)
            assert reservation["state"] == "SETTLED"
            events = {e.type for e in store.list_events(mission.id) if e.attempt_id == loser.id}
            assert "AttemptSuperseded" in events
            # the late result was not accepted: no second accepted result, history only
            late = [
                e
                for e in store.list_events(mission.id)
                if e.type == "ResultRejected" and e.attempt_id == loser.id
            ]
            assert store.find_result_for_attempt(loser.id) is None  # never a StoredResult
            assert store.count_events(mission.id, "TaskCompleted") == 1
            assert len(late) == 1 and late[0].payload["reason"] == "superseded"
            assert store.get_intent_for_subject(loser.id).state in {"SETTLED", "FAILED"}

    asyncio.run(case())


# --------------------------------------------------------------------------- S3-06
def test_s3_06_all_tasks_pass_but_the_mission_criterion_is_unmet(tmp_path):
    provider = demo_static_dag_provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s3-06", success_criteria=("pytest:tests", "file:CHANGELOG.md"))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert all(t.status is TaskStatus.COMPLETED for t in store.list_tasks(mission.id))
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason == "mission_criteria_unmet"
            judged = {j["criterion"]: j for j in final.final_report["success_criteria"]}
            assert judged["pytest:tests"]["met"] is True
            assert judged["file:CHANGELOG.md"]["met"] is False
            assert store.count_events(mission.id, "MissionSuccessJudged") == 1

    asyncio.run(case())


# --------------------------------------------------------------------------- S3-08
def test_s3_08a_graph_over_budget_is_rejected_with_the_dimension(tmp_path):
    provider = demo_static_dag_provider(
        planner_steps=[graph_proposal_step(DEMO_DAG_TASKS), graph_proposal_step(DEMO_DAG_TASKS)]
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s3-08a", budget=Budget(max_tokens=100_000, max_attempts=12))  # Σ = 120k
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "planning_failed"
            rejected = [e for e in store.list_events(mission.id) if e.type == "TaskGraphRejected"]
            assert len(rejected) == 2
            assert rejected[0].payload["reason"] == "budget"
            assert "dimension=max_tokens" in rejected[0].payload["detail"]
            assert "remaining=100000" in rejected[0].payload["detail"]
            assert store.list_tasks(mission.id) == []
            assert provider.by_role["planner"] == 2  # max_planning_attempts

    asyncio.run(case())


def test_s3_08b_task_reservation_failure_stops_the_task_and_leaves_blocked_tasks_alone(tmp_path):
    from fixtures_provider import envelope_step

    # A's first submission references an artifact that does not exist → RETRY_WAIT; the
    # repair Attempt cannot be reserved: A's own token account is nearly exhausted
    bad_a = [
        ("workspace_list", {}),
        envelope_step(summary="假装写了", artifacts=["textkit/missing.py"], claims=["x"]),
    ]
    provider = demo_static_dag_provider()
    provider.worker_by_key["A"] = bad_a + provider.worker_by_key["A"]
    # step 4 (D4-21): a repair reserves what the Task still has, so the exhaustion must be
    # real — the bad Attempt's two calls settle more than A's whole 20k account
    provider.usage_tokens = 7_000

    async def case():
        async with Orchestrator(
            config(tmp_path, attempt_reserve_tokens=20_000), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s3-08b"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            by_key = tasks_by_key(store, mission.id)
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason == "budget_exhausted", orchestrator.progress_log
            assert by_key["A"].status is TaskStatus.FAILED
            assert by_key["A"].failure_reason == "budget_exhausted"
            detail = final.final_report["detail"]
            assert detail["dimension"] == "tokens" and detail["remaining"] < detail["requested"]
            assert detail["account"] == f"budget:{by_key['A'].id}"
            assert final.final_report["failed_task_id"] == by_key["A"].id
            # D3-13': the BLOCKED tasks were not touched (no BLOCKED→CANCELLED edge)
            assert [by_key[k].status for k in "BCDE"] == [TaskStatus.BLOCKED] * 4
            assert provider.calls_by_key == {"A": 2}  # list + envelope of the bad Attempt only

    asyncio.run(case())


def test_s3_08c_mission_pool_exhaustion_blames_no_task_and_cascades(tmp_path):
    provider = demo_static_dag_provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            # three attempts for the whole Mission: A, B, C use them up; D cannot be reserved
            mission = await orchestrator.submit_mission(
                spec("s3-08c", budget=Budget(max_tokens=200_000, max_attempts=3))
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            by_key = tasks_by_key(store, mission.id)
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason == "budget_exhausted"  # D3-12': the pool's own reason
            assert final.final_report["detail"]["dimension"] == "attempts"
            assert final.final_report["detail"]["account"] == f"budget:{mission.id}"
            assert "failed_task_id" not in final.final_report
            assert [by_key[k].status for k in "ABC"] == [TaskStatus.COMPLETED] * 3
            assert by_key["D"].status is TaskStatus.CANCELLED  # READY → CANCELLED by the cascade
            assert by_key["E"].status is TaskStatus.BLOCKED  # untouched
            assert not any(t.status is TaskStatus.FAILED for t in by_key.values())

    asyncio.run(case())


# ------------------------------------------------------- review round 1 regressions
def test_r1_runtime_artifact_conflict_stops_the_task_cleanly(tmp_path):
    """P0-1: B and C both write an undeclared path with different content; when D is
    allocated the merge conflict stops the Task (READY→ACTIVE→FAILED) and the Mission
    with `artifact_conflict` instead of crashing the loop."""

    from fixtures_provider import envelope_step

    provider = demo_static_dag_provider()
    for key, text in (("B", "b notes"), ("C", "c notes")):
        steps = provider.worker_by_key[key]
        steps.insert(1, ("workspace_write_file", {"path": "NOTES.md", "content": text}))
        steps[-1] = envelope_step(
            summary=f"{key} done",
            artifacts=[f"textkit/{'slug' if key == 'B' else 'count'}.py", "NOTES.md"],
            claims=[f"{key} tests pass"],
        )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("r1-conflict"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            by_key = tasks_by_key(store, mission.id)
            assert final.status is MissionStatus.FAILED, orchestrator.progress_log
            assert final.stop_reason == "artifact_conflict"
            assert (
                by_key["B"].status is TaskStatus.COMPLETED
                and by_key["C"].status is TaskStatus.COMPLETED
            )
            assert (
                by_key["D"].status is TaskStatus.FAILED
                and by_key["D"].failure_reason == "artifact_conflict"
            )
            assert by_key["E"].status is TaskStatus.BLOCKED
            assert "NOTES.md" in final.final_report["detail"]["error"]
            assert store.list_attempts(by_key["D"].id) == []  # nothing was dispatched for D

    asyncio.run(case())


def test_r1_protected_upstream_path_listed_as_artifact_is_rejected(tmp_path):
    """P1-6: D rewrites A's contract (an undeclared upstream path) and lists it as its
    artifact; the submission is rejected before any verification policy runs."""

    from fixtures_provider import envelope_step

    provider = demo_static_dag_provider()
    d = provider.worker_by_key["D"]
    d.insert(
        1,
        (
            "workspace_write_file",
            {
                "path": "textkit/__init__.py",
                "content": "from textkit.slug import slugify\nfrom textkit.count import word_count\nCHEAT = 1\n",
            },
        ),
    )
    d[-1] = envelope_step(
        summary="cheated", artifacts=["textkit/__init__.py"], claims=["integration"]
    )
    provider.worker_by_key["D"] = d + demo_static_dag_scripts()["D"]  # the repair behaves

    async def case():
        async with Orchestrator(
            config(tmp_path, attempt_reserve_tokens=8_000), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("r1-cheat"))
            await orchestrator.run()
            store = orchestrator.store
            by_key = tasks_by_key(store, mission.id)
            d_attempts = store.list_attempts(by_key["D"].id)
            assert [a.status for a in d_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert d_attempts[0].failure["reason"] == "protected_path_rewritten"
            assert d_attempts[0].failure["paths"] == ["textkit/__init__.py"]
            assert store.find_result_for_attempt(d_attempts[0].id) is None  # never registered
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            contract = next(
                store.get_artifact(a)
                for a in by_key["A"].accepted_artifacts
                if store.get_artifact(a).path == "textkit/__init__.py"
            )
            judged = (
                orchestrator.assembled.workspaces.root
                / f"{mission.id}-judge-{orchestrator.owner}-verify"
            )
            assert sha256_file(judged / "textkit/__init__.py") == contract.content_hash

    asyncio.run(case())


def test_r1_fault_points_accept_transaction_and_after_task_completed(tmp_path):
    """P1-10: a crash inside the accept transaction rolls everything back (no accept, no
    unblock) and the replay accepts the same result; a crash right after the accept
    committed leaves the dependents READY and the restart never re-runs A."""

    import pytest

    from agent_orchestrator.storage.store import InjectedCrash

    provider = demo_static_dag_provider()

    async def case():
        cfg = config(tmp_path)
        async with Orchestrator(cfg, provider, owner="orch-1") as first:
            mission = await first.submit_mission(spec("r1-faults"))
            first.arm_fault("after_accept_before_supersede", kind="attempt")
            with pytest.raises(InjectedCrash):
                await first.run()
            store = first.store
            by_key = tasks_by_key(store, mission.id)
            assert by_key["A"].status is TaskStatus.VERIFYING  # nothing of the accept survived
            assert store.count_events(mission.id, "TaskCompleted") == 0
            assert store.count_events(mission.id, "VerificationPassed") == 0
            assert (
                by_key["B"].status is TaskStatus.BLOCKED
                and by_key["C"].status is TaskStatus.BLOCKED
            )
            result = store.find_result_for_attempt(store.list_attempts(by_key["A"].id)[0].id)
            assert result.verification_state == "RUNNING"
            calls_a = provider.calls_by_key["A"]
        async with Orchestrator(cfg, provider, owner="orch-1") as second:
            second.arm_fault("after_task_completed", kind="attempt")
            with pytest.raises(InjectedCrash):
                await second.run()
            store = second.store
            by_key = tasks_by_key(store, mission.id)
            assert by_key["A"].status is TaskStatus.COMPLETED
            assert store.count_events(mission.id, "TaskCompleted") == 1
            assert {by_key["B"].status, by_key["C"].status} == {
                TaskStatus.READY
            }  # unblocked atomically
            assert (
                provider.calls_by_key["A"] == calls_a == CALLS["A"]
            )  # A was verified again, never re-run
        async with Orchestrator(cfg, provider, owner="orch-1") as third:
            await third.run()
            store = third.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, third.progress_log
            assert store.count_events(mission.id, "TaskCompleted") == 5
            assert provider.calls_by_key == CALLS

    asyncio.run(case())


def test_r1_mission_wide_concurrency_is_enforced_in_the_commit(tmp_path):
    """P1-11: the Mission-wide open-Attempt bound is checked inside `create_attempt`."""

    from agent_orchestrator.graph.task_graph import TaskGraphProposal
    from agent_orchestrator.orchestrator.commit_service import (
        CommitRejected,
        CommitService,
        Reservation,
    )
    from agent_orchestrator.storage.store import Store

    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(spec("r1-conc"))
    planning = service.begin_planning(mission.id)
    tasks, _ = service.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json({"tasks": DEMO_DAG_TASKS}),
        base_version=planning.version,
        source={},
    )
    a = tasks[0]
    kwargs = dict(
        role="worker",
        model="agent-model",
        prompt_version="w",
        context_version="c",
        reservation=Reservation(tokens=1000, cost_micros=0),
        intent_config={"agent_config": {}, "message": {}},
        input_hash="h",
        candidates_per_task=3,
    )
    service.create_attempt(a.id, max_open_attempts=1, **kwargs)
    with pytest.raises(CommitRejected) as exc:
        service.create_attempt(a.id, max_open_attempts=1, **kwargs)
    assert "max_concurrency" in str(exc.value)
    attempt2, intent2 = service.create_attempt(a.id, max_open_attempts=2, **kwargs)
    assert intent2.config["attempt_id"] == attempt2.id  # P1-7: authoritative id in the intent


def test_r1_stale_owner_cannot_commit_a_verdict(tmp_path):
    """P1-3: accept/fail are refused for an owner whose lease lapsed and was taken over."""

    from agent_orchestrator.contracts import ResultEnvelope
    from agent_orchestrator.graph.task_graph import TaskGraphProposal
    from agent_orchestrator.orchestrator.commit_service import (
        CommitRejected,
        CommitService,
        Reservation,
    )
    from agent_orchestrator.storage.store import Store

    clock = {"now": 1000.0}
    service = CommitService(Store.open(tmp_path / "orchestrator.db", clock=lambda: clock["now"]))
    mission, _ = service.create_mission(spec("r1-stale"))
    planning = service.begin_planning(mission.id)
    tasks, _ = service.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json({"tasks": DEMO_DAG_TASKS}),
        base_version=planning.version,
        source={},
    )
    a = tasks[0]
    attempt, intent = service.create_attempt(
        a.id,
        role="worker",
        model="agent-model",
        prompt_version="w",
        context_version="c",
        reservation=Reservation(tokens=1000, cost_micros=0),
        intent_config={"agent_config": {}, "message": {}},
        input_hash="h",
    )
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=10)
    service.record_agent_created(intent.intent_id, agent_id="agent-x", expected_turn_id="turn-x")
    service.record_submitted(intent.intent_id, receipt={"turn_id": "turn-x"})
    envelope = ResultEnvelope.from_json(
        {
            "id": "result-1",
            "task_id": a.id,
            "attempt_id": attempt.id,
            "mission_id": mission.id,
            "outcome": "candidate",
            "summary": "s",
            "claims": [{"content": "c", "confidence": 0.5}],
            "evidence": ["x"],
            "artifacts": [],
            "proposed_tasks": [],
            "used_knowledge": [],
            "risks": [],
            "cost": {},
        }
    )
    service.record_result(
        attempt.id, envelope=envelope, turn_id="turn-x", artifacts=[], usage_refs=()
    )
    service.start_verification("result-1")
    clock["now"] += 20  # orch-1's lease lapsed
    service.renew_lease(attempt.id, owner="orch-2", lease_seconds=10, liveness={})  # takeover
    with pytest.raises(CommitRejected):
        service.accept_result("result-1", verifier_results=[], owner="orch-1")
    with pytest.raises(CommitRejected):
        service.fail_result("result-1", failures=[], owner="orch-1")
    assert (
        service.accept_result("result-1", verifier_results=[], owner="orch-2").status
        is TaskStatus.COMPLETED
    )
