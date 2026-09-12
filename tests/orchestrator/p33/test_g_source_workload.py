# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest
from test_p33_g1_create_sources import PERSON, command

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.context.context_builder import ContextRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer


@pytest.mark.parametrize("corrupt", [False, True])
def test_planner_freezes_verified_source_scale_without_inlining_source_body(tmp_path, corrupt):
    async def case():
        provider = RoleScriptedProvider({})
        config = OrchestratorConfig(evidence_root=tmp_path / "evidence")
        profile = RuntimeProfile(
            "default",
            provider,
            config.model,
            context_policy=ContextPolicy(),
            tokenizer=UpperBoundTokenizer(),
        )
        async with Orchestrator(config, profiles={"default": profile}) as orch:
            api = MissionControlV1(orch, tenant_id="tenant", principal=PERSON)
            value = command()
            receipt = api.create_with_sources(value)
            mid = receipt["mission_id"]
            orch.commit.begin_planning(mid)
            if corrupt:
                digest = receipt["source_versions"]["sources/a.md"]
                original = orch.assembled.workspaces.artifact_store.path_for(digest)
                original.chmod(0o600)
                original.write_bytes(b"changed")
                with pytest.raises(ContextRejected, match="workload"):
                    await orch._create_planner_intent(mid, ordinal=1)
                assert not orch.store.list_intents("PENDING", "CLAIMED", "SUBMITTED")
                return
            intent = await orch._create_planner_intent(mid, ordinal=1)
            content = intent.config["message"]["content"]
            assert "source-workload-v1" in content
            assert "permitted_ceiling_not_expected_spend" in content
            assert "unallocated tokens cannot be borrowed" in content
            assert "source text only; excludes prompts" in content
            assert '"total_bytes":27' in content  # actual UTF-8 bytes, including CRLF/LF
            for source in value["sources"]:
                assert source["content"].strip() not in content
                assert receipt["source_versions"][source["path"]] in content
            assert "file:REPORT.md" in content and value["mission"]["goal"] in content
            assert "完整小目标" in intent.config["agent_config"]["instructions"]

    asyncio.run(case())
