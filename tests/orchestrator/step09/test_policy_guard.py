# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · slice E (plan D9-8' / D9-9 / D9-9' / D9-10'; S9-06, S9-08): an Agent can never
change a core rule — a smuggled change is refused on record and nothing changes; only a
person proposes parameters, within the whitelist; every promotion is bounded (step,
cooldown, backpressure) while a rollback stays fast; under load the concurrency and the
budgets stay inside the deployment's limits under every version."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step

from agent_orchestrator.api.policies import PolicyApi, PolicyRequestError
from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.governance.promotion import (
    DEPLOYMENT_TIMELINE,
    PROMOTABLE,
    code_versions,
    resolve_params,
)
from agent_orchestrator.orchestrator.commit_service import (
    CommitService,
    MissionSpec,
    mission_account,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.policy_commits import PolicyCommitError
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.scheduling.backpressure import Observation
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import (
    RECORDER_SEED,
    RECORDER_SPEC,
    RECORDER_TASKS,
    demo_dynamic_dag_provider,
    demo_multi_mission_profiles,
    graph_change_step,
    outcome_step,
)

ALICE = Principal("alice")
TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")


def _config(tmp_path, **overrides):
    values = {"max_concurrency": 1, "test_timeout_seconds": 60, **overrides}
    return OrchestratorConfig(evidence_root=Path(tmp_path) / "evidence", **values)


def _recorder(key, **overrides):
    base = dict(
        goal=RECORDER_SPEC["goal"],
        success_criteria=("file:DOCS.md",),
        tenant_id="tenant-9",
        idempotency_key=key,
        allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=300_000, max_attempts=16),
        workspace_seed=RECORDER_SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


# ------------------------------------------------------------------ S9-06
def test_s9_06_a_manager_that_smuggles_a_safety_change_is_refused_and_nothing_changes(tmp_path):
    only_a = [t for t in RECORDER_TASKS if t["key"] == "A"]
    smuggle = graph_change_step(
        [
            {"op": "set_config", "key": "hard_cap_micros", "value": 10**12},
            {"op": "set_policy", "allocator_weights": {"mission_importance": 0.5}},
        ],
        rationale="放宽预算上限以便继续",
    )
    provider = demo_dynamic_dag_provider(
        tasks=only_a,
        scripts={"A": [outcome_step(outcome="blocked", summary="预算不够")] * 8},
        manager_steps=[smuggle] + [graph_change_step([])] * 8,
    )

    async def case():
        async with Orchestrator(_config(tmp_path, no_progress_limit=2), provider) as orch:
            store = orch.store
            before = store.active_policy()
            mission = await orch.submit_mission(
                _recorder(
                    "smuggle",
                    success_criteria=("file:analysis.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=6),
                )
            )
            await orch.run()
            refused = [
                e for e in store.iter_events(mission.id) if e.type == "PolicySuggestionRefused"
            ]
            assert refused, orch.progress_log
            payload = refused[0].payload
            assert payload["source"] == "manager"
            assert {"hard_cap_micros", "allocator_weights"} <= set(payload["keys"])
            assert "hard_cap_micros" in payload["core_keys"]
            after = store.active_policy()
            assert (
                after["version_id"] == before["version_id"] and after["params"] == before["params"]
            )
            assert store.list_policy_proposals() == []
            assert not [
                e
                for e in store.iter_events(DEPLOYMENT_TIMELINE)
                if e.type in {"PolicyProposed", "PolicyPromoted"}
            ]

    asyncio.run(case())


def test_s9_06_a_worker_that_writes_policy_files_changes_nothing(tmp_path):
    task = {
        "key": "A",
        "goal": "写报告（A）",
        "rationale": "服务根目标：交付报告",
        "dependencies": [],
        "success_criteria": ["file:REPORT.md"],
        "verification_policy": ["format_check", "rule_check"],
        "outputs": ["REPORT.md", "policy/raise.json"],
        "allowed_tools": list(TOOLS),
        "budget": {"max_tokens": 30_000, "max_attempts": 2},
        "priority": 1.0,
    }
    provider = RoleScriptedProvider(
        {
            "planner": [graph_proposal_step([task])],
            "worker": [
                ("workspace_write_file", {"path": "REPORT.md", "content": "# 报告\n"}),
                (
                    "workspace_write_file",
                    {
                        "path": "policy/raise.json",
                        "content": json.dumps({"hard_cap_micros": 1, "candidates_per_task": 3}),
                    },
                ),
                envelope_step(
                    summary="写好了",
                    artifacts=["REPORT.md", "policy/raise.json"],
                    claims=["报告已写好"],
                ),
            ],
        }
    )

    async def case():
        async with Orchestrator(_config(tmp_path), provider) as orch:
            store = orch.store
            before = store.active_policy()["version_id"]
            mission = await orch.submit_mission(
                MissionSpec(
                    goal="写报告",
                    success_criteria=("file:REPORT.md",),
                    tenant_id="tenant-9",
                    idempotency_key="worker-policy",
                    allowed_tools=TOOLS,
                    budget=Budget(max_tokens=100_000, max_attempts=2),
                )
            )
            await orch.run()
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orch.progress_log
            )
            [refused] = [
                e for e in store.iter_events(mission.id) if e.type == "PolicySuggestionRefused"
            ]
            assert (
                refused.payload["source"] == "worker"
                and refused.payload["path"] == "policy/raise.json"
            )
            assert refused.payload["keys"] == ["candidates_per_task", "hard_cap_micros"]
            assert refused.payload["core_keys"] == ["hard_cap_micros"]
            assert (
                store.active_policy()["version_id"] == before
                and store.list_policy_proposals() == []
            )

    asyncio.run(case())


def test_s9_06_only_a_person_proposes_and_only_within_the_whitelist(tmp_path):
    config = _config(tmp_path, max_concurrency=2)
    store = Store.open(config.orchestrator_db)
    commit = CommitService(store)
    commit.seed_policy(resolve_params(config))
    with pytest.raises(PolicyRequestError, match="Principal"):
        PolicyApi(commit, "worker-7")  # an Agent id is not an authenticated person
    api = PolicyApi(commit, ALICE, max_concurrency=2)
    with pytest.raises(PolicyRequestError, match="not promotable"):
        api.propose({"hard_cap_micros": 1})
    with pytest.raises(PolicyRequestError, match="outside"):
        api.propose({"candidates_per_task": 9})
    proposal = api.propose({"candidates_per_task": 2}, note="多给一个候选")
    stored = store.get_policy_proposal(proposal["proposal_id"])
    assert (
        stored["source"] == "human:alice" and stored["manifest"]["note"] == "人工参数，非规则改进"
    )
    with pytest.raises(PolicyCommitError, match="APPROVED"):
        api.promote(
            proposal["proposal_id"]
        )  # a person's parameters still need evaluation + approval
    store.close()


# ------------------------------------------------------------------ S9-08
def test_s9_08_cooldown_and_backpressure_bound_changes_while_a_rollback_stays_fast(tmp_path):
    config = _config(tmp_path, max_concurrency=2)
    store = Store.open(config.orchestrator_db)
    commit = CommitService(store)
    commit.seed_policy(resolve_params(config))
    api = PolicyApi(
        commit, ALICE, deployment=DeploymentPolicy(policy_cooldown_seconds=3600), max_concurrency=2
    )

    def ready(partial, n):
        proposal = api.propose(partial)
        commit.record_policy_evaluation(
            proposal["proposal_id"],
            verdict="PASSED",
            reasons=[],
            report_hash=f"report-{n}",
            baseline_version_id=store.active_policy()["version_id"],
            code_versions=code_versions(),
            evidence_kind="real",
        )
        api.approve(proposal["proposal_id"])
        return proposal

    first = ready({"no_progress_limit": 3}, 1)
    api.promote(first["proposal_id"])
    second = ready({"no_progress_limit": 3, "max_manager_rounds": 5}, 2)
    with pytest.raises(PolicyCommitError, match="cooldown"):
        api.promote(second["proposal_id"])  # frequent changes are bounded
    back = api.rollback(reason="异常：失败率上升")  # a rollback is never held by the cooldown
    assert back["action"] == "rollback"
    commit.record_backpressure(  # the deployment is under load, recorded as the orchestrator does
        Observation(
            running_attempts=99, pending_dispatch=0, pending_verifications=0, observed_at=store.now
        ),
        limits=config.backpressure_limits(),
        mission_ids=[],
    )
    assert commit.backpressure_state().is_raised
    wider = ready({"candidates_per_task": 2}, 3)
    unhurried = PolicyApi(
        commit, ALICE, deployment=DeploymentPolicy(policy_cooldown_seconds=0), max_concurrency=2
    )
    with pytest.raises(PolicyCommitError, match="backpressure"):
        unhurried.promote(wider["proposal_id"])
    for version in store.list_policy_versions():  # no version can carry a safety or budget item
        assert set(version["params"]) == set(PROMOTABLE)
    store.close()


def test_s9_08_under_load_concurrency_and_budgets_stay_inside_the_deployment_limits(tmp_path):
    profiles, rules = demo_multi_mission_profiles(missions=3, critic_delay_seconds=0.05)
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence",
        model=profiles["small"].model,
        max_concurrency=2,
        max_running_attempts=1,  # the drill: keep the deployment under backpressure
        verifier_workers=1,
        max_pending_verifications=2,
        global_budget=Budget(max_tokens=900_000, max_attempts=48),
        test_timeout_seconds=60,
    )

    async def case():
        async with Orchestrator(config, profiles=profiles, routing=rules) as orch:
            store = orch.store
            missions = [await orch.submit_mission(_recorder(f"load-{n}")) for n in range(3)]
            await orch.run()
            raised = [
                e
                for m in missions
                for e in store.iter_events(m.id)
                if e.type == "BackpressureRaised"
            ]
            assert raised, "the drill has to load the deployment"
            limits = []
            for mission in missions:
                assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                    orch.progress_log
                )
                for task in store.list_tasks(mission.id):
                    for attempt in store.list_attempts(task.id):
                        intent = store.get_intent_for_subject(attempt.id)
                        allocation = dict(intent.config.get("allocation") or {})
                        if allocation:
                            limits.append(int(allocation["concurrency_limit"]))
                            if allocation.get("pressure") == "RAISED":  # §18.5: halved
                                assert int(allocation["concurrency_limit"]) <= 1
                            assert int(allocation["candidates_per_task"]) <= 3
                with store.transaction():
                    remaining = orch.commit.ledger.account(
                        mission_account(mission.id)
                    ).remaining_tokens()
                assert remaining is None or remaining >= 0  # no Mission overspent
            assert limits and max(limits) <= config.max_concurrency
            peaks = dict((store.get_scheduler_state("backpressure") or {}).get("peaks") or {})
            assert (
                0 < int(peaks.get("running_attempts", 0)) <= config.max_concurrency * len(missions)
            )

    asyncio.run(case())
