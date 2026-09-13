# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Doc6 successor oracle; legacy hashes captured before production edits.

Real parser/CAS/locator checks plus real Worker/Critic proof across reopen.
The scripted Provider proves execution/binding, not model report quality.
"""

import asyncio
import json
import re
from dataclasses import replace

import pytest
from test_g_doc5_accept_critic import _prepared, _provider
from test_p33_evidence_resolver import SourceRepository

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import ClaimProposal, TaskStatus
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance import domains
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import ROLES, TEMPLATE_VERSIONS, template_for_domain
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver

RESULT_ROLES = (
    "worker", "arbiter", "synthesizer", "explorer", "exploiter", "simplifier",
    "connector", "failure_analyst",
)


def test_published_profiles_and_prompt_bytes_are_unchanged():
    assert sha256_hex(domains.DOC_PROFILE_V5.to_json()) == (
        "a2c0bdc7d58fdd4e95d760819f49e494e918e84f87a3b439beaa404cbf298597"
    )
    assert sha256_hex(domains.CODE_PROFILE.to_json()) == (
        "fbcefdba9801b4ec5dbd62adaafbd8490a63fe1240617c5271b1d7a73b901061"
    )
    for version, digest in (
        (1, "8460551cd829aa1cb6819a2f05d978dd53f5e727d60c37696c749759168eec5b"),
        (2, "0d06df7c7c87ec424602ab888e50a6fa1a9e2351f6223a9b62a983255f70f35e"),
    ):
        rows = {}
        for role in domains.DOC_PROFILE_V5.role_templates:
            template = TEMPLATE_VERSIONS[role][f"{role}-doc-research-v{version}"]
            rows[role] = {"instructions": template.instructions, "tools": list(template.tool_names)}
        assert sha256_hex(rows) == digest
    assert sha256_hex({
        role: {"instructions": frozen.instructions, "version": frozen.prompt_version,
               "tools": list(frozen.tool_names)}
        for role, current in ROLES.items()
        for frozen in [TEMPLATE_VERSIONS["manager"]["manager-v2"] if role == "manager" else current]
    }) == "933152b55f45fedd091514b55fad338e2316bcd05cd874c66ca048a15286a397"


@pytest.mark.parametrize("role", RESULT_ROLES)
def test_result_roles_offer_real_statement_support_and_page_range(role):
    selected = template_for_domain(ROLES[role], domains.DOC_PROFILE, {})
    assert selected.prompt_version == f"{role}-doc-research-v5"
    old = TEMPLATE_VERSIONS[role][f"{role}-doc-research-v2"]
    assert selected.instructions.startswith(old.instructions)
    assert selected.tool_names == old.tool_names
    match = re.search(
        r"<statement_claim_example>(.*?)</statement_claim_example>", selected.instructions, re.S
    )
    assert match
    proposal = ClaimProposal.from_json(json.loads(match[1]))
    assert proposal.type == "statement" and proposal.citations
    assert proposal.content != proposal.citations[0].quote
    assert proposal.evidence == ()
    assert "自身生成的 notes/报告不能代替来源 citations" in selected.instructions
    assert "可以使用覆盖引文的整页 start_line/end_line" in selected.instructions
    assert "全文唯一" in selected.instructions and "完整" in selected.instructions
    assert "最高 SUPPORTED" in selected.instructions
    assert "不能删除必要的准则关联" in selected.instructions


@pytest.mark.parametrize("role", ("planner", "manager"))
def test_doc6_planning_prefers_complete_goals_and_accounts_for_split_cost(role):
    selected = template_for_domain(ROLES[role], domains.DOC_PROFILE_V6, {})
    previous = TEMPLATE_VERSIONS[role][f"{role}-doc-research-v2"]
    assert selected.prompt_version == f"{role}-doc-research-v3"
    assert selected.instructions.startswith(previous.instructions)
    assert selected.tool_names == previous.tool_names
    guidance = selected.instructions[len(previous.instructions):]
    for requirement in (
        "小而完整的目标优先由一个 Task 完成",
        "保留原 Mission 的全部 success_criteria",
        "每个文档 Task 的 verification_policy 必须包含 critic_review",
        "独立验收", "工作集收益", "不能仅按文件数量拆节点",
        "Worker", "Critic", "重复输入", "必要的最终合成",
        "rationale", "budget_for_tasks", "不是实际总成本保证",
        "未知", "不得编造", "不能以删减准则",
    ):
        assert requirement in guidance
    assert "400k" not in guidance and "N1" not in guidance
    assert template_for_domain(ROLES[role], domains.DOC_PROFILE_V5, {}) == previous


def test_doc6_keeps_all_capabilities_and_changes_only_successor_role_bindings():
    old, new = domains.DOC_PROFILE_V5, domains.DOC_PROFILE_V6
    assert new.version == "6" and domains.resolve_domain(domains.DOC_DOMAIN).version == "9"
    expected = old.to_json()
    expected["version"] = "6"
    expected["role_templates"].update({
        r: f"{r}-doc-research-v3" for r in (*RESULT_ROLES, "planner", "manager")
    })
    assert new.to_json() == expected
    assert domains.DomainProfileV1.from_json(new.to_json()).to_json() == expected
    for profile in (old, new):
        assert domains.supports_document_assessments(profile)
        assert domains.requires_mission_source_binding(profile)
        assert domains.requires_document_critic_proof(profile)
        for policy in ((), ("format_check", "rule_check")):
            assert domains.check_against_domain(
                profile, key="A", success_criteria=("file:report.md",),
                verification_policy=policy,
            )
        assert not domains.check_against_domain(
            profile, key="A", success_criteria=("file:report.md",),
            verification_policy=("format_check", "rule_check", "critic_review"),
        )
    for profile in (domains.CODE_PROFILE, domains.DOC_PROFILE_V3, domains.DOC_PROFILE_V4):
        assert not domains.requires_document_critic_proof(profile)


def test_statement_example_page_range_resolves_but_wrong_line_and_partial_quote_do_not(tmp_path):
    prompt = template_for_domain(ROLES["worker"], domains.DOC_PROFILE, {}).instructions
    match = re.search(r"<statement_claim_example>(.*?)</statement_claim_example>", prompt, re.S)
    assert match
    claim = ClaimProposal.from_json(json.loads(match[1]))
    cas = ArtifactStore(tmp_path / "cas")
    version = cas.put_bytes("# 来源条件\n方案 A 不支持离线。\n本段未说明断网恢复能力。\n".encode())
    citation = replace(claim.citations[0], version=version)
    assert (citation.start_line, citation.end_line) == (1, 3)
    repository = SourceRepository()
    repository.rows[("mission", citation.path, version)] = {
        "mission_id": "mission", "tenant_id": "tenant", "path": citation.path,
        "version_hash": version, "kind": "text/markdown", "trust": "untrusted_external",
    }
    resolver = EvidenceResolver(repository, cas)
    options = dict(tenant_id="tenant", mission_id="mission",
                   source_roots=("sources/",), source_versions={citation.path: version})
    actual = resolver.resolve(citation, **options)
    assert actual.status == "resolved"
    assert actual.locator.to_json() == {"start_line": 2, "end_line": 2}
    assert resolver.resolve(replace(citation, start_line=1, end_line=1), **options).status == (
        "quote_mismatch"
    )
    assert resolver.resolve(replace(citation, quote="支持离线。"), **options).status == (
        "quote_not_whole_unit"
    )


@pytest.mark.parametrize("version", ["5", "6", "7", "8"])
@pytest.mark.parametrize("human", [False, True])
def test_actual_worker_and_critic_intents_keep_frozen_versions_across_reopen(
    tmp_path, monkeypatch, version, human
):
    async def run():
        profile = getattr(domains, f"DOC_PROFILE_V{version}")
        config = OrchestratorConfig(evidence_root=tmp_path)
        provider = _provider(human=human)
        async with Orchestrator(config, provider, owner="doc6-proof") as first:
            mission, _, stored, _, critic = await _prepared(
                first, monkeypatch, profile=profile, human=human,
            )
            worker = first.store.get_intent_for_subject(stored.envelope.attempt_id)
            assert worker.config["prompt_version"] == (
                "worker-doc-research-v2" if version == "5" else (
                    "worker-doc-research-v4" if version == "8" else "worker-doc-research-v3"
                )
            )
            assert critic.config["prompt_version"] == (
                "critic-doc-research-v3" if version == "8" else "critic-doc-research-v2"
            )
            intents = [worker.to_json(), critic.to_json()]
            proof = first.store.get_receipt("critic-verdict:" + critic.intent_id)
            assert proof is not None
            calls = dict(provider.by_role)
        async with Orchestrator(config, provider, owner="doc6-proof") as second:
            assert second.commit.domain_for(mission.id).to_json() == profile.to_json()
            for original in intents:
                assert second.store.get_intent(original["intent_id"]).to_json() == original
            assert second.store.get_receipt("critic-verdict:" + critic.intent_id) == proof
            assert second.commit.accept_result(
                stored.envelope.id, verifier_results=[], owner="doc6-proof",
            ).status is TaskStatus.COMPLETED
            assert provider.by_role == calls

    asyncio.run(run())


@pytest.mark.parametrize("version", ["5", "6", "7", "8"])
def test_actual_failed_critic_cannot_be_replaced_by_caller_pass(tmp_path, monkeypatch, version):
    async def run():
        profile = getattr(domains, f"DOC_PROFILE_V{version}")
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), _provider(critic_fail=True),
        ) as orch:
            mission, _, stored, row, intent = await _prepared(
                orch, monkeypatch, profile=profile, critic_fail=True,
            )
            proof = orch.store.get_receipt("critic-verdict:" + intent.intent_id)
            assert proof is not None and proof["verdict"]["verdict"] == "FAIL"
            orch.commit.record_verification_layer(
                stored.envelope.id, layer="critic_review", status="PASS",
                detail={**row["detail"], "verdict": "PASS", "findings": [], "needs_human": False},
            )
            before = orch.store.snapshot(mission.id)
            with pytest.raises(CommitRejected, match="[Cc]ritic"):
                orch.commit.accept_result(
                    stored.envelope.id, verifier_results=[], owner=orch._owner,
                )
            assert orch.store.snapshot(mission.id) == before
            assert orch.store.get_receipt("critic-verdict:" + intent.intent_id) == proof

    asyncio.run(run())
