"""Actual ContextPort refusal stops an unchanged contract before any Provider call."""

import asyncio

from graph_helpers7 import node, spec

from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator, OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import MODEL, RoleScriptedProvider
from simple_harness.agents.context.budget import ContextPolicy


def test_actual_required_context_overflow_is_terminal_without_retry_or_provider_call(tmp_path):
    async def exercise():
        provider = RoleScriptedProvider({"worker": [("workspace_list", {})]})
        profile = RuntimeProfile(
            "default", provider, MODEL, context_policy=ContextPolicy(max_input_tokens=100)
        )
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), profiles={"default": profile}
        ) as orch:
            mission = await orch.submit_mission(spec(conflict_reserve_tokens=0))
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A")]}),
                base_version=planning.version,
                source={"planner": "valid graph fixture"},
            )
            for _ in range(500):
                await orch._cycle()
                task = orch.store.get_task(tasks[0].id)
                if task.status is TaskStatus.FAILED:
                    break
                await asyncio.sleep(0.002)
            else:
                raise AssertionError("actual context overflow did not stop the Task")
            attempts = orch.store.list_attempts(task.id)
            assert len(attempts) == 1 and provider.calls == 0
            assert (
                attempts[0].failure["error"]["error_code"] == "context_required_content_too_large"
            )
            assert attempts[0].failure["reason"] == "context_required_content_too_large"
            stopped = orch.store.get_mission(mission.id)
            assert stopped.status is MissionStatus.FAILED
            assert stopped.stop_reason == "runtime_unavailable"
            assert (
                stopped.final_report["detail"]["error"]["error_code"]
                == "context_required_content_too_large"
            )
            assert task.budget == tasks[0].budget
            before = len(orch.store.list_events(mission.id))
            for _ in range(5):
                await orch._cycle()
            assert len(orch.store.list_events(mission.id)) == before
            assert len(orch.store.list_attempts(task.id)) == 1 and provider.calls == 0
            assert orch.commit.unavailable_until() == {}

    asyncio.run(exercise())
