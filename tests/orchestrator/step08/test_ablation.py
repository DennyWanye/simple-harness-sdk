# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · slice C (plan D8-7'; S8-04): an ablation is an explicit change of the
effective policy — a closed vocabulary (critic, blackboard, graph_changes); safety
boundaries can never be removed; what was removed is on record, and nothing is presumed
about the direction of the effect."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fixtures_provider import RoleScriptedProvider, graph_proposal_step, proposal_step

from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.governance.policies import policy_snapshot, snapshot_diff
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import SAFETY_BOUNDARIES, OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    COMPARE_SEED,
    COMPARE_SPEC,
    COMPARE_TASKS,
    DEMO_GOOD,
    DEMO_PROPOSAL,
    DEMO_SEED,
    demo_knowledge_sharing_provider,
    demo_worker_script,
)

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")


def _config(tmp_path, name="evidence", **overrides):
    return OrchestratorConfig(
        evidence_root=Path(tmp_path) / name, max_concurrency=1, test_timeout_seconds=60, **overrides
    )


def _spec(key, criteria=("pytest:tests/test_parse_kv.py",)):
    return MissionSpec(
        goal="实现 parse_kv(text) -> dict 并通过 tests/test_parse_kv.py",
        success_criteria=tuple(criteria),
        tenant_id="tenant-8",
        idempotency_key=key,
        allowed_tools=TOOLS,
        budget=Budget(max_tokens=300_000, max_attempts=4),
        workspace_seed=DEMO_SEED,
    )


@pytest.mark.parametrize("name", [*SAFETY_BOUNDARIES, "critic_review", "allocator", "anything"])
def test_s8_04_only_the_closed_vocabulary_can_be_ablated(tmp_path, name):
    with pytest.raises(ValueError, match="not ablatable"):
        _config(tmp_path, ablations=(name,))


def test_s8_04_ablations_switch_their_layer_off_and_show_in_the_snapshot(tmp_path):
    plain = _config(tmp_path)
    ablated = _config(tmp_path, ablations=("graph_changes", "blackboard", "critic", "critic"))
    assert ablated.ablations == ("blackboard", "critic", "graph_changes")
    assert ablated.knowledge_sharing is False and ablated.dynamic_graph is False
    diff = {d["key"]: d for d in snapshot_diff(policy_snapshot(plain), policy_snapshot(ablated))}
    assert diff["config.ablations"]["b"] == ["blackboard", "critic", "graph_changes"]
    assert (
        diff["config.knowledge_sharing"]["b"] is False
        and diff["config.dynamic_graph"]["b"] is False
    )


def test_s8_04_without_the_critic_the_layer_says_it_was_removed_and_no_critic_runs(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(DEMO_PROPOSAL)],
            "worker": demo_worker_script(DEMO_GOOD),
        }  # no critic script at all
    )

    async def case():
        async with Orchestrator(_config(tmp_path, ablations=("critic",)), provider) as orchestrator:
            mission = await orchestrator.submit_mission(_spec("no-critic"))
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            [task] = store.list_tasks(mission.id)
            assert "critic_review" in task.verification_policy  # the Task still asks for it
            layers = {v["layer"]: v for v in store.list_verifications(task.accepted_result_id)}
            critic = layers["critic_review"]
            assert critic["status"] == "NOT_REQUIRED" and critic["detail"]["ablated"] is True
            assert (
                critic["detail"]["required_by_policy"] is True
                and "ablated" in critic["detail"]["summary"]
            )
            assert layers["code_test"]["status"] == "PASS"  # the deterministic layers stay

    asyncio.run(case())
    assert provider.by_role.get("critic", 0) == 0


def test_s8_04_a_free_text_criterion_says_its_judge_was_ablated(tmp_path):
    provider = RoleScriptedProvider(
        {"planner": [proposal_step(DEMO_PROPOSAL)], "worker": demo_worker_script(DEMO_GOOD)}
    )

    async def case():
        async with Orchestrator(_config(tmp_path, ablations=("critic",)), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                _spec("no-judge", criteria=("pytest:tests/test_parse_kv.py", "实现应处理空字符串"))
            )
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            judged = {j["criterion"]: j for j in final.final_report["success_criteria"]}
            free = judged["实现应处理空字符串"]
            assert (
                free["met"] is False
                and free["source"] == "ablated"
                and free["reason"] == "judge ablated in this run"
            )
            assert (
                final.status is MissionStatus.FAILED
                and final.stop_reason == "mission_criteria_unmet"
            )
            assert orchestrator.store.list_approvals(mission.id) == []  # no arbitration either

    asyncio.run(case())


def test_s8_04_without_the_blackboard_no_knowledge_is_retrieved_or_used(tmp_path):
    only_a = [t for t in COMPARE_TASKS if t["key"] == "A"]
    spec = MissionSpec(
        goal=COMPARE_SPEC["goal"],
        success_criteria=("file:contract/CONTRACT.md",),
        tenant_id="tenant-8",
        idempotency_key="no-blackboard",
        allowed_tools=tuple(COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=200_000, max_attempts=12),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
    )

    async def case():
        async with Orchestrator(
            _config(tmp_path, ablations=("blackboard",)),
            demo_knowledge_sharing_provider(tasks=only_a),
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            store = orchestrator.store
            [task] = store.list_tasks(mission.id)
            intent = store.get_intent_for_subject(store.list_attempts(task.id)[0].id)
            assert (
                intent.config["retrieval_status"] == "disabled" and intent.config["knowledge"] == []
            )
            assert store.count_events(mission.id, "KnowledgeUsed") == 0
            return store.get_mission(mission.id).status

    status = asyncio.run(case())
    assert status in {MissionStatus.COMPLETED, MissionStatus.FAILED}  # either way is an observation
    assert graph_proposal_step  # the graph proposal helper is the one the fixture uses
