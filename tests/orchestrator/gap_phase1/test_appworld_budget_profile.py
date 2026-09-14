"""The actual AppWorld driver must plan against its 256K execution profile."""

import re
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldConfig, AppWorldEpisode
from agent_orchestrator.evaluation.appworld_arms import TOOLS, ArmRuntime, execute_arm
from agent_orchestrator.evaluation.experiment import ExperimentBudget
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    package_of,
)
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer


class Counter(UpperBoundTokenizer):
    bound_protocol = "fixture-text-only-v1"
    requires_prior_output_reserve = True

    def estimate_input_tokens(self, request):
        return 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["D", "F"])
async def test_undersized_plan_is_repaired_before_worker_and_critic_run(tmp_path, arm, monkeypatch):
    # AppWorld prepends domain instructions before the ordinary fixture role marker.
    def role_of(request):
        return re.search(r"\[role:([a-z_]+)\]", request.messages[0].content).group(1)

    monkeypatch.setattr("agent_orchestrator.testing.fixtures.role_of", role_of)

    def proposal(tokens):
        return graph_proposal_step([{
            "key": "A", "goal": "Complete the public operation and report it",
            "rationale": "One task covers the whole request", "dependencies": [],
            "success_criteria": ["file:REPORT.md"],
            "verification_policy": ["format_check", "rule_check", "critic_review"],
            "allowed_tools": list(TOOLS), "outputs": ["REPORT.md"],
            "budget": {"max_tokens": tokens, "max_attempts": 1},
        }])

    provider = RoleScriptedProvider({
        "planner": [proposal(250_000), proposal(800_000)],
        "worker": [
            ("appworld_execute", {"code": "public_operation()"}),
            ("workspace_write_file", {"path": "REPORT.md", "content": "Observed completion"}),
            envelope_step(summary="Observed completion", artifacts=["REPORT.md"],
                          claims=["Observed completion"]),
        ],
        "critic": [critic_step(verdict="PASS", criteria_met=True)],
    })
    observed = []
    world = SimpleNamespace(
        task=SimpleNamespace(instruction="Complete the public operation and report it"),
        execute=lambda code: observed.append(code) or "Observed completion",
        save=lambda: None, close=lambda: None,
        evaluate=lambda: SimpleNamespace(to_dict=lambda: {"success": bool(observed)}),
    )
    root = tmp_path / arm
    with AppWorldEpisode(AppWorldConfig("task", arm), world_factory=lambda **_: world) as episode:
        result = await execute_arm(arm, episode, ArmRuntime(
            provider, "agent-model", Counter(),
            ContextPolicy(max_input_tokens=262_144, max_total_tokens=262_144,
                          output_reserve=32_768, safety_margin=1024, render_slack_tokens=0),
            ExperimentBudget(1_600_000, 131_072, 1_600_000, 60, 30),
        ), root)

    packages = [package_of(r) for r in provider.requests if role_of(r) == "planner"]
    assert packages[0]["budget_for_tasks"]["min_task_tokens_with_critic_review"] == 522_240
    assert len(packages) == 2
    assert "task_budget_below_floor" in str(packages[1]["planning_rejected"])
    assert result["mission_status"] == "COMPLETED"
    assert observed == ["public_operation()"]
    assert provider.by_role["critic"] == 1
    store = Store.open(root / "orchestrator" / "orchestrator.db")
    try:
        mission = store.get_mission(result["mission_id"])
        assert mission.final_report["runtime_profile_id"] == "default"
        [task] = store.list_tasks(mission.id)
        assert task.budget.max_tokens == 800_000
        [attempt] = store.list_attempts(task.id)
        intent = store.get_intent_for_subject(attempt.id)
        assert intent.config["first_critic_budget"]["minimum_tokens"] == 261_120
        assert mission.budget.max_tokens == 1_600_000
    finally:
        store.close()
