"""N1 real-provider failure: document prompts must specify the accepted wire contract.

Old prompt bytes/profile snapshots remain available. The example is parsed by the
real ClaimProposal, not a permissive fixture; real model quality still needs UI.
"""

import asyncio
import hashlib
import json
import re

import pytest
from fixtures_provider import RoleScriptedProvider
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import ClaimProposal, ContractError
from agent_orchestrator.governance import domains
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import ROLES, TEMPLATE_VERSIONS, template_for_domain

OLD = {
    "worker": "ad12ce4a26e4029f54d3eea83151517955bf57e15b17c317adf8b9ba256c9df8",
    "planner": "123d0b299d3e58f221feb298ff5b1ae7d86933682484cc8997a5607698556194",
    "arbiter": "abdeee0163e22a9466634871801214c976b04fd125550912d4ff4451b79c78e6",
    "synthesizer": "5dc715184141a1a335be93504016e7b524dd1a1935499bab816ec677e337907c",
}


@pytest.mark.parametrize(
    "role",
    [
        "worker",
        "arbiter",
        "synthesizer",
        "explorer",
        "exploiter",
        "simplifier",
        "connector",
        "failure_analyst",
    ],
)
def test_current_document_prompt_example_is_a_real_valid_literal_claim(role):
    selected = template_for_domain(ROLES[role], domains.DOC_PROFILE, {})
    assert selected.prompt_version == f"{role}-doc-research-v4"
    match = re.search(r"<claim_example>\s*(.*?)\s*</claim_example>", selected.instructions, re.S)
    assert match, "the actual document prompt lacks an unambiguous claim wire example"
    raw = json.loads(match[1])
    claim = ClaimProposal.from_json(raw)
    assert claim.type == "attribution"
    assert claim.content == claim.citations[0].quote
    assert all(isinstance(item, str) for item in raw["evidence"])
    assert "不能把引用对象放进 evidence" in selected.instructions
    assert "不支持 #L、:行号、?lines=" in selected.instructions
    with pytest.raises(ContractError, match=r"claim.evidence\[\] must be a string"):
        ClaimProposal.from_json({**raw, "evidence": raw["citations"]})


def test_old_document_prompt_and_v4_snapshot_remain_exact():
    old = domains.DOC_PROFILE_V4
    restored = domains.DomainProfileV1.from_json(old.to_json())
    for role, digest in OLD.items():
        template = template_for_domain(ROLES[role], restored, {})
        assert hashlib.sha256(template.instructions.encode()).hexdigest() == digest
        assert template == TEMPLATE_VERSIONS[role][f"{role}-doc-research-v1"]
    assert domains.supports_document_assessments(restored)
    assert domains.requires_mission_source_binding(restored)


@pytest.mark.parametrize("version", ["5", "6"])
def test_new_document_dispatch_freezes_example_and_keeps_code_template(
    tmp_path, monkeypatch, version
):
    async def case():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), RoleScriptedProvider({})
        ) as orch:
            # Historical doc5/doc6 are frozen at real creation only; later
            # dispatch runs under today's registry.
            with monkeypatch.context() as patch:
                profile = domains.DOC_PROFILE_V5 if version == "5" else domains.DOC_PROFILE_V6
                patch.setattr(
                    domains,
                    "DOMAINS",
                    {**domains.DOMAINS, domains.DOC_DOMAIN: profile},
                )
                mission = await orch.submit_mission(spec(domain=domains.DOC_DOMAIN))
            assert domains.resolve_domain(domains.DOC_DOMAIN).version == "8"
            planning = orch.commit.begin_planning(mission.id)
            planner = await orch._create_planner_intent(mission.id, ordinal=1)
            assert planner.config["prompt_version"] == (
                "planner-doc-research-v2" if version == "5" else "planner-doc-research-v3"
            )
            assert "自然语言的报告质量要求" in planner.config["agent_config"]["instructions"]
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                verification_policy=["format_check", "rule_check", "critic_review"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            assert await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert "<claim_example>" in intent.config["agent_config"]["instructions"]
            assert (
                intent.config["prompt_version"]
                == attempt.prompt_version
                == ("worker-doc-research-v2" if version == "5" else "worker-doc-research-v3")
            )
            frozen = orch.commit.domain_for(mission.id)
            assert frozen.version == version
            for role in frozen.role_templates:
                prompt_version = "2" if version == "5" or role == "critic" else "3"
                assert frozen.role_templates[role] == f"{role}-doc-research-v{prompt_version}"
            assert domains.requires_mission_source_binding(frozen)
            code = await orch.submit_mission(spec(idempotency_key="code-control"))
            assert orch._template(ROLES["worker"], code.id) == ROLES["worker"]

    asyncio.run(case())


def test_current_document_critic_uses_exact_read_paths_without_result_authority():
    template = template_for_domain(ROLES["critic"], domains.DOC_PROFILE, {})
    assert template.prompt_version == "critic-doc-research-v3"
    assert "不支持 #L、:行号、?lines=" in template.instructions
    assert template.tool_names == ("workspace_read_file", "workspace_list")
    assert "<claim_example>" not in template.instructions
