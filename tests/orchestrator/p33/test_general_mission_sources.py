"""User decision 2026-09-26: one general task that may carry reference material.

A general (code-v1) Mission now accepts the same initial source batch a document
Mission does: the material is frozen per Attempt, mounted in the Worker's
workspace under ``sources/`` and protected from change.  Until now a general
Mission refused it with ``not_found`` (its profile had no source roots), so the
only way to hand an orchestration task a file was the strict citation domain.
"""

import asyncio

from fixtures_provider import RoleScriptedProvider
from graph_helpers7 import node

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.governance.domains import CODE_DOMAIN, CODE_PROFILE_V4
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig

NOTES = "# 周会纪要\n\n1. 登录页改版，下周三。\n"


def test_a_general_mission_carries_sources_into_the_worker_workspace(tmp_path):
    async def case():
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, RoleScriptedProvider({})) as orch:
            control = MissionControlV1(orch, tenant_id="tenant-5", principal=Principal("main-agent"))
            receipt = control.create_with_sources({
                "mission": {"goal": "根据纪要整理待办", "success_criteria": ["file:a.md"],
                            "idempotency_key": "general-with-source"},
                "sources": [{"path": "sources/纪要.md", "content": NOTES, "kind": "text/markdown"}],
            })
            mission = orch.store.get_mission(receipt["mission_id"])
            domain = orch.commit.domain_for(mission.id)
            assert (domain.id, domain.version) == (CODE_DOMAIN, "5")
            assert domain.source_roots == ("sources/",) and "source" in domain.allowed_evidence_kinds
            planning = orch.commit.begin_planning(mission.id)
            [task], _ = orch.commit.commit_task_graph(
                mission.id, TaskGraphProposal.from_json({"tasks": [node("A")]}),
                base_version=planning.version, source={"planner": "fixture"})
            mission = orch.store.get_mission(mission.id)
            assert await orch._next_attempt(mission, task, [])
            [attempt] = orch.store.list_attempts(task.id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert list(intent.config["source_versions"]) == ["sources/纪要.md"]
            orch._bind_workspace(attempt)
            assert orch._source_files(attempt) == {"sources/纪要.md": NOTES.encode()}
            assert "sources/纪要.md" in orch._protected_files(mission, task, attempt)

    asyncio.run(case())


def test_frozen_general_profiles_keep_no_source_roots():
    assert CODE_PROFILE_V4.source_roots == () and "source" not in CODE_PROFILE_V4.allowed_evidence_kinds
