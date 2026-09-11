# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.1 遗留修复（plans/2026-09-12-phase3/p31-fixes, plan v2）· FX-1..FX-5.

F-ORCH-1: a Task budget below what its first Attempt and that Attempt's Critic can reserve
— ``k × (base + critic)`` with k candidates per Task, the critic part only when the policy
names critic_review — is refused by the Graph Manager (``task_budget_below_floor``) and the
proposer is told why; nothing invents a number for the model.  The floor is a necessary
condition only: it never promises that repairs will be affordable.  System tasks
(synthesis / conflict) are not proposals and are not bound by it.  Without ``task_floor``
the gates behave as before (only the Orchestrator injects one).
F-ORCH-3: the artifacts of an accepted result become VERIFIED and those of a failed result
REJECTED, each in its own commit transaction; superseded candidates stay UNVERIFIED.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_orchestrator.context.context_builder import build_manager_package
from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimProposal,
    Mission,
    MissionStatus,
    ResultEnvelope,
    ids,
)
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.graph.changes import ChangeLimits, GraphChangeRejected, validate_change
from agent_orchestrator.graph.task_graph import (
    GraphRejected,
    TaskBudgetFloor,
    TaskGraphProposal,
    validate_graph,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec, Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.storage.store import InjectedCrash, Store
from agent_orchestrator.testing.fixtures import (
    RECORDER_SEED,
    RECORDER_SPEC,
    RoleScriptedProvider,
    critic_step,
    demo_dynamic_dag_provider,
    envelope_step,
    graph_change_step,
    graph_proposal_step,
    package_of,
    recorder_manager_change,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "step05"))
from graph_helpers import HASH, add, change, drive_to_running, graph_service  # noqa: E402
from graph_helpers import node as graph_node  # noqa: E402

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
        budget=Budget(max_attempts=3),  # max_tokens None: the "left blank" case of the native run
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
def test_the_floor_formula():
    assert FLOOR.floor_for(NO_CRITIC) == 4096
    assert FLOOR.floor_for(WITH_CRITIC) == 10_096
    assert FLOOR.floor_for(WITH_CRITIC, candidates=2) == 20_192
    assert TaskBudgetFloor(base=0, critic=6000).floor_for(WITH_CRITIC, candidates=3) == 0


@pytest.mark.parametrize(
    ("tokens", "policy", "floor", "candidates", "refused"),
    [
        (800, WITH_CRITIC, FLOOR, 1, True),  # the native run: 800 with critic_review
        (800, NO_CRITIC, FLOOR, 1, True),  # below one turn even without a Critic
        (4095, NO_CRITIC, FLOOR, 1, True),
        (4096, NO_CRITIC, FLOOR, 1, False),  # exactly the floor passes
        (10_095, WITH_CRITIC, FLOOR, 1, True),
        (10_096, WITH_CRITIC, FLOOR, 1, False),  # base + critic share
        (10_096, WITH_CRITIC, FLOOR, 2, True),  # two candidates each reserve a turn and a Critic
        (20_192, WITH_CRITIC, FLOOR, 2, False),
        (800, WITH_CRITIC, TaskBudgetFloor(base=0, critic=6000), 1, False),  # 0 switches it off
    ],
)
def test_the_graph_gate_refuses_a_task_budget_below_the_floor(
    tokens, policy, floor, candidates, refused
):
    graph = proposal(node("A", tokens=tokens, policy=policy))
    if refused:
        with pytest.raises(GraphRejected) as rejected:
            validate_graph(mission(), graph, task_floor=floor, candidates=candidates)
        assert rejected.value.reason == "budget"
        text = str(rejected.value)
        assert "task_budget_below_floor" in text
        assert str(floor.floor_for(policy, candidates=candidates)) in text  # what must be met
    else:
        validate_graph(mission(), graph, task_floor=floor, candidates=candidates)


def test_a_pool_share_below_the_floor_is_refused_too():
    # the Mission bounds tokens, the Planner leaves them blank: 3 × (20000 // 3) < 10096
    bounded = mission(budget=Budget(max_tokens=20_000, max_attempts=3))
    with pytest.raises(GraphRejected) as rejected:
        validate_graph(
            bounded,
            proposal(*(node(k, tokens=None, policy=WITH_CRITIC) for k in "ABC")),
            task_floor=FLOOR,
        )
    assert "task_budget_below_floor" in str(rejected.value)
    # the same shares without a Critic clear the base floor (6666 ≥ 4096)
    validate_graph(
        bounded,
        proposal(*(node(k, tokens=None, policy=NO_CRITIC) for k in "ABC")),
        task_floor=FLOOR,
    )


def test_no_floor_argument_keeps_the_old_behaviour():
    validate_graph(mission(), proposal(node("A", tokens=800, policy=WITH_CRITIC)))


def test_a_synthesis_template_is_not_bound_by_the_floor():
    template = {
        "goal": "汇总各部分",
        "success_criteria": ["file:SUMMARY.md"],
        "budget": {"max_tokens": 1000, "max_attempts": 1},
    }
    bounded = mission(
        budget=Budget(max_tokens=100_000, max_attempts=6), final_report={"synthesis": template}
    )
    validate_graph(
        bounded, proposal(node("A", tokens=30_000, policy=WITH_CRITIC)), task_floor=FLOOR
    )


# ------------------------------------------------------------------ FX-2 the change gate
def _validate(current, tasks, proposed, floor=FLOOR):
    return validate_change(
        current,
        tasks,
        proposed,
        limits=ChangeLimits(),
        proposals_by_attempt={},
        committed_tokens_by_task={},
        task_floor=floor,
    )


def test_a_manager_add_task_below_the_floor_is_refused(tmp_path):
    service, current, t = graph_service(tmp_path)
    tasks = service.store.list_tasks(current.id)
    low = change(
        1,
        [
            add(
                "E",
                [t["C"].id],
                budget={"max_tokens": 800, "max_attempts": 2},
                parent_task_ids=[t["C"].id],
            )
        ],
    )
    with pytest.raises(GraphChangeRejected) as rejected:
        _validate(current, tasks, low)
    assert rejected.value.reason == "budget" and "task_budget_below_floor" in str(rejected.value)
    enough = change(
        1,
        [
            add(
                "E",
                [t["C"].id],
                budget={"max_tokens": 5000, "max_attempts": 2},
                parent_task_ids=[t["C"].id],
            )
        ],
    )
    _validate(current, tasks, enough)
    _validate(current, tasks, low, floor=None)  # without a floor the old behaviour stays


def test_a_default_share_below_the_floor_is_refused(tmp_path):
    # DIAMOND commits 4 × 20000; a pool of 82000 leaves 2000 for a new node without a budget
    service, current, t = graph_service(tmp_path, budget=Budget(max_tokens=82_000, max_attempts=12))
    tasks = service.store.list_tasks(current.id)
    blank = change(
        1, [add("E", [t["C"].id], budget={"max_attempts": 2}, parent_task_ids=[t["C"].id])]
    )
    with pytest.raises(GraphChangeRejected) as rejected:
        _validate(current, tasks, blank)
    assert "task_budget_below_floor" in str(rejected.value)


def test_the_manager_package_names_the_floor(tmp_path):
    service, current, t = graph_service(tmp_path)
    package = build_manager_package(
        current,
        t["B"],
        trigger={"kind": "test"},
        verifier_feedback=[],
        subgraph=[],
        graph_version=1,
        limits={},
        knowledge=None,
        budget_floor={"min_task_tokens": 4096, "min_task_tokens_with_critic_review": 10_096},
    )
    assert package.package["budget_floor"] == {
        "min_task_tokens": 4096,
        "min_task_tokens_with_critic_review": 10_096,
    }


# ------------------------------------------------------------------ FX-3 / FX-4 end to end
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


def _passes(n=4):
    return [critic_step(verdict="PASS", criteria_met=True) for _ in range(n)]


def test_a_refused_budget_reaches_the_planner_and_the_replan_completes(tmp_path):
    seen: list[dict] = []
    provider = RoleScriptedProvider(
        {
            # exactly max_planning_attempts (2) steps: an exhausted script would hang
            "planner": [
                _capturing(graph_proposal_step([_task(800)]), seen),
                _capturing(graph_proposal_step([_task(30_000)]), seen),
            ],
            "worker": _worker(),
            "critic": _passes(),
        }
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            created = await orchestrator.submit_mission(_spec("fx3"))
            await asyncio.wait_for(orchestrator.run(), timeout=120)
            store = orchestrator.store
            return (
                store.get_mission(created.id),
                store.list_events(created.id),
                store.list_tasks(created.id),
            )

    current, events, tasks = asyncio.run(run())
    assert str(current.status) == "COMPLETED"
    rejected = [e for e in events if e.type == "TaskGraphRejected"]
    assert rejected and "task_budget_below_floor" in json.dumps(
        rejected[0].payload, ensure_ascii=False
    )
    assert [t.budget.max_tokens for t in tasks] == [30_000]
    assert len(seen) == 2
    for package in seen:
        assert package["budget_for_tasks"]["min_task_tokens"] == 4096
        assert package["budget_for_tasks"]["min_task_tokens_with_critic_review"] == 10_096
    assert "task_budget_below_floor" in json.dumps(seen[1]["planning_rejected"], ensure_ascii=False)


def test_a_pool_that_cannot_hold_one_floor_ends_as_planning_failed(tmp_path):
    provider = RoleScriptedProvider(
        {"planner": [graph_proposal_step([_task(5_000)]), graph_proposal_step([_task(5_000)])]}
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            created = await orchestrator.submit_mission(
                _spec("fx4", budget=Budget(max_tokens=9_000, max_attempts=6))
            )
            await asyncio.wait_for(orchestrator.run(), timeout=120)
            return orchestrator.store.get_mission(created.id)

    current = asyncio.run(run())
    assert str(current.status) == "FAILED" and current.stop_reason == "planning_failed"
    assert "task_budget_below_floor" in json.dumps(
        current.final_report["planning_failure"], ensure_ascii=False
    )


def test_min_task_tokens_zero_switches_the_floor_off(tmp_path):
    provider = RoleScriptedProvider(
        {"planner": [graph_proposal_step([_task(800)])], "worker": _worker(), "critic": _passes()}
    )

    async def run():
        async with Orchestrator(_config(tmp_path, min_task_tokens=0), provider) as orchestrator:
            created = await orchestrator.submit_mission(_spec("fx3-off"))
            await asyncio.wait_for(orchestrator.run(), timeout=120)
            return orchestrator.store.list_events(created.id), orchestrator.store.list_tasks(
                created.id
            )

    events, tasks = asyncio.run(run())
    assert not [e for e in events if e.type == "TaskGraphRejected"]
    assert [t.budget.max_tokens for t in tasks] == [800]


# ------------------------------------------------------------------ FX-5 artifact status
def _statuses(cfg):
    store = Store.open_readonly(cfg.orchestrator_db)  # a fresh connection: what is on disk
    try:
        missions = store.list_missions()
        artifacts = [a for m in missions for a in store.list_mission_artifacts(m.id)]
        tasks = [t for m in missions for t in store.list_tasks(m.id)]
        return artifacts, tasks
    finally:
        store.close()


def test_accepted_artifacts_are_verified_and_failed_ones_rejected(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task(60_000, attempts=3)])],
            "worker": _worker() + _worker(),
            "critic": [critic_step(verdict="FAIL", criteria_met=False, blocker="要点不够具体")]
            + _passes(),
        }
    )
    cfg = _config(tmp_path)

    async def run():
        async with Orchestrator(cfg, provider) as orchestrator:
            created = await orchestrator.submit_mission(
                _spec("fx5", budget=Budget(max_tokens=300_000, max_attempts=6))
            )
            await asyncio.wait_for(orchestrator.run(), timeout=120)
            return orchestrator.store.get_mission(created.id)

    assert str(asyncio.run(run()).status) == "COMPLETED"
    artifacts, tasks = _statuses(cfg)
    accepted = set(tasks[0].accepted_artifacts)
    assert accepted, "the completed Task names the artifacts it accepted"
    by_status = {a.id: a.verification_status for a in artifacts}
    assert all(by_status[a] == "VERIFIED" for a in accepted)
    rejected = [a for a in artifacts if a.id not in accepted]
    assert rejected, "the failed first Attempt's artifact is kept as history"
    assert all(a.verification_status == "REJECTED" for a in rejected)


def test_a_crash_inside_the_accept_transaction_leaves_the_artifact_unverified(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([_task(60_000)])],
            "worker": _worker(),
            "critic": _passes(),
        }
    )
    cfg = _config(tmp_path)

    async def run():
        async with Orchestrator(cfg, provider) as orchestrator:
            await orchestrator.submit_mission(
                _spec("fx5-crash", budget=Budget(max_tokens=300_000, max_attempts=6))
            )
            orchestrator.arm_fault("after_accept_before_supersede", kind="attempt")
            with pytest.raises(InjectedCrash):
                await asyncio.wait_for(orchestrator.run(), timeout=120)

    asyncio.run(run())
    artifacts, _tasks = _statuses(cfg)
    assert artifacts and all(a.verification_status == "UNVERIFIED" for a in artifacts)


# ------------------------------------------------------------------ code review round 1
def _recorder_spec(key):
    return MissionSpec(
        goal=RECORDER_SPEC["goal"],
        success_criteria=tuple(RECORDER_SPEC["success_criteria"]),
        tenant_id="tenant-floor-manager",
        idempotency_key=key,
        allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=300_000, max_attempts=16),
        workspace_seed=RECORDER_SEED,
    )


def test_a_manager_below_the_floor_is_refused_and_told_why(tmp_path):
    """Review P1-2: the Orchestrator's injection on the Manager path, end to end — the
    first change asks 800 tokens for the new Task E (format + rule, floor 4096) and is
    refused; the Manager is asked again, reads why, and the legal change completes."""

    seen: list[dict] = []

    def too_small(package):
        return [
            dict(op, budget={"max_tokens": 800, "max_attempts": 2}) if op.get("key") == "E" else op
            for op in recorder_manager_change(package)
        ]

    provider = demo_dynamic_dag_provider(
        manager_steps=[
            _capturing(graph_change_step(too_small), seen),
            _capturing(graph_change_step(recorder_manager_change), seen),
        ]
    )
    cfg = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )

    async def run():
        async with Orchestrator(cfg, provider) as orchestrator:
            created = await orchestrator.submit_mission(_recorder_spec("fx3-manager"))
            await asyncio.wait_for(orchestrator.run(), timeout=240)
            store = orchestrator.store
            return store.get_mission(created.id), store.list_events(created.id)

    current, events = asyncio.run(run())
    rejected = [e for e in events if e.type == "TaskGraphChangeRejected"]
    assert rejected and "task_budget_below_floor" in json.dumps(
        rejected[0].payload, ensure_ascii=False
    )
    assert len(seen) == 2
    for package in seen:
        assert package["budget_floor"] == {
            "min_task_tokens": 4096,
            "min_task_tokens_with_critic_review": 10_096,
        }
    assert "task_budget_below_floor" in json.dumps(seen[1]["rejections"], ensure_ascii=False)
    assert str(current.status) == "COMPLETED"


@pytest.mark.parametrize(
    ("overrides", "profile_output", "expected"),
    [
        ({}, None, (4096, 10_096)),
        ({"candidates_per_task": 2}, None, (8192, 20_192)),  # k from the bound policy
        ({}, 8192, (8192, 14_192)),  # a profile's larger output cap raises the base
        ({"min_task_tokens": 5000}, None, (5000, 11_000)),  # an explicit base
        ({"min_task_tokens": 0}, None, (0, 0)),  # switched off
    ],
    ids=["default", "two-candidates", "profile-8192", "explicit-5000", "off"],
)
def test_the_floor_is_wired_from_config_profiles_and_the_bound_policy(
    tmp_path, overrides, profile_output, expected
):
    """Review P2-1: ``_budget_floor_rule`` and ``_candidates_for`` without running a turn."""

    provider = RoleScriptedProvider({})
    profiles = (
        None
        if profile_output is None
        else {
            "default": RuntimeProfile(
                "default", provider, "agent-model", default_max_output_tokens=profile_output
            )
        }
    )

    async def run():
        async with Orchestrator(
            _config(tmp_path, **overrides), None if profiles else provider, profiles=profiles
        ) as orchestrator:
            created = await orchestrator.submit_mission(_spec("wiring"))
            return orchestrator._budget_floor(created.id)

    floor = asyncio.run(run())
    assert (floor["min_task_tokens"], floor["min_task_tokens_with_critic_review"]) == expected


@pytest.mark.parametrize("bad", [-1, True])
def test_a_negative_or_boolean_min_task_tokens_is_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="min_task_tokens"):
        _config(tmp_path, min_task_tokens=bad)


def test_a_proposal_that_meets_the_floor_but_not_the_pool_names_the_pool(tmp_path):
    """Review P2-3: a proposal at the floor (10096 with critic_review) in a pool of 9000 is
    refused for exceeding the Mission budget — the stop reason names the pool, not the
    floor.  (Refusing such a Mission before planning is a follow-up, see journal §4.)"""

    provider = RoleScriptedProvider(
        {"planner": [graph_proposal_step([_task(10_096)]), graph_proposal_step([_task(10_096)])]}
    )

    async def run():
        async with Orchestrator(_config(tmp_path), provider) as orchestrator:
            created = await orchestrator.submit_mission(
                _spec("fx4-pool", budget=Budget(max_tokens=9_000, max_attempts=6))
            )
            await asyncio.wait_for(orchestrator.run(), timeout=120)
            return orchestrator.store.get_mission(created.id)

    current = asyncio.run(run())
    assert str(current.status) == "FAILED" and current.stop_reason == "planning_failed"
    text = json.dumps(current.final_report["planning_failure"], ensure_ascii=False)
    assert "exceeds the Mission budget" in text and "task_budget_below_floor" not in text


def _submit_candidate(service, task, attempt, *, agent):
    """Record one candidate's result with its artifact (as ``graph_helpers.complete`` does)."""

    path = task.outputs[0]
    envelope = ResultEnvelope(
        id=f"result-{attempt.id}",
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="done",
        claims=(ClaimProposal(content="done", confidence=0.9),),
        evidence=(path,),
        artifacts=(path,),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, path, HASH),
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=path,
        version=1,
        content_hash=HASH,
        size_bytes=3,
        produced_by=agent,
    )
    return service.record_result(
        attempt.id, envelope=envelope, turn_id=f"turn-{agent}", artifacts=[artifact], usage_refs=()
    )


def _drive_second_candidate(service, task, *, agent, turn):
    """``graph_helpers.drive_to_running`` for a second, concurrent candidate (k = 2)."""

    attempt, intent = service.create_attempt(
        task.id,
        role="worker",
        model="agent-model",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"agent_config": {}, "message": "do"},
        input_hash="h",
        candidates_per_task=2,
    )
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id=agent, expected_turn_id=turn)
    service.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    return service.store.get_attempt(attempt.id)


def test_a_superseded_candidate_keeps_its_artifact_unverified(tmp_path):
    """Review P1-1: two candidates of one Task both submit; the first is accepted and the
    second is superseded — read from the file, the winner's artifact is VERIFIED and the
    loser's stays UNVERIFIED (never judged, so neither VERIFIED nor REJECTED)."""

    service, _current, t = graph_service(tmp_path, nodes=[graph_node("A")])
    task = t["A"]
    first = drive_to_running(service, task, agent="agent-1", turn="turn-agent-1")
    second = _drive_second_candidate(service, task, agent="agent-2", turn="turn-agent-2")
    winner = _submit_candidate(service, task, first, agent="agent-1")
    _submit_candidate(service, task, second, agent="agent-2")
    service.start_verification(winner.envelope.id)
    service.accept_result(
        winner.envelope.id, verifier_results=[{"layer": "rule_check", "status": "PASS"}]
    )
    assert service.store.get_attempt(second.id).status is AttemptStatus.SUPERSEDED
    store = Store.open_readonly(tmp_path / "orchestrator.db")  # a fresh connection
    try:
        assert [a.verification_status for a in store.list_artifacts(first.id)] == ["VERIFIED"]
        assert [a.verification_status for a in store.list_artifacts(second.id)] == ["UNVERIFIED"]
    finally:
        store.close()
