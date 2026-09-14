# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planner dispatch must explain lifetime budgets without requiring document sources."""

import asyncio
import json
import re

import pytest

from agent_orchestrator.contracts import Budget
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer


@pytest.mark.parametrize("domain", ["appworld-v1", "code-v1"])
def test_non_document_planner_dispatch_explains_cumulative_budget(tmp_path, domain):
    async def case():
        provider = RoleScriptedProvider({})
        config = OrchestratorConfig(
            evidence_root=tmp_path / "core",
            appworld_execute=lambda code: {"output": "unused: no Worker is dispatched"},
        )
        profile = RuntimeProfile(
            "default", provider, config.model,
            context_policy=ContextPolicy(
                max_input_tokens=262144, max_total_tokens=262144,
                output_reserve=32768, safety_margin=1024,
            ),
            tokenizer=UpperBoundTokenizer(),
            default_max_output_tokens=8192,
            max_output_tokens_ceiling=32768,
        )
        async with Orchestrator(config, profiles={"default": profile}) as orch:
            mission = await orch.submit_mission(MissionSpec(
                goal="Complete a multi-step application task",
                success_criteria=("file:REPORT.md",),
                tenant_id="budget-test", idempotency_key="budget-test",
                allowed_tools=("workspace_write_file", "workspace_read_file"),
                budget=Budget(max_tokens=4_000_000, max_attempts=12),
                domain=domain, runtime_profile_id="default",
            ))
            orch.commit.begin_planning(mission.id)
            intent = await orch._create_planner_intent(mission.id, ordinal=1)
            content = intent.config["message"]["content"]
            package = {
                header: json.loads(body)
                for header, body in re.findall(
                    r"## ([a-z_]+)\n(.*?)(?=\n## |\Z)", content, re.DOTALL
                )
                if header in {"budget_allocation_semantics", "budget_for_tasks",
                              "source_workload", "criterion_allocation_semantics"}
            }
            semantics = package["budget_allocation_semantics"]
            assert semantics["kind"] == "permitted_ceiling_not_expected_spend"
            assert "hard cumulative ceiling" in semantics["task_tokens"]
            assert "unallocated tokens cannot be borrowed" in semantics["task_tokens"]
            assert "not a recommended lifetime budget" in semantics["reservation_floor"]
            assert package["budget_for_tasks"]["min_task_tokens"] == 261120
            assert package["budget_for_tasks"]["max_tokens"] == 4_000_000
            assert "source_workload" not in package
            assert "criterion_allocation_semantics" not in package
            assert orch.store.get_mission(mission.id).budget.max_tokens == 4_000_000

    asyncio.run(case())
