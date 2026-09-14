# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""K01--K03: a passing test is not proof of an arbitrary semantic claim."""

from __future__ import annotations

from hashlib import sha256

import pytest
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import ClaimStatus
from agent_orchestrator.verification.deterministic_checks import code_test


def _accept(
    tmp_path, evidence, content="All backups remain recoverable after any hardware failure"
):
    service, mission, (task, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task)
    result = submit(service, attempt, envelope(attempt, claims=[claim(content, evidence=evidence)]))
    service.accept_result(
        result.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    return service, mission, service.store.list_claims(result.envelope.id)[0]


def test_k01_unrelated_passing_test_cannot_verify_arbitrary_claim(tmp_path):
    service, mission, stored_claim = _accept(tmp_path, ["pytest:tests/probe/test_impl_a.py"])
    assert stored_claim.status is not ClaimStatus.VERIFIED
    assert service.store.get_knowledge(stored_claim.id) is None


@pytest.mark.parametrize("reference", ["tool-run:missing", "knowledge:missing"])
def test_k02_dangling_reference_does_not_support_a_claim(tmp_path, reference):
    _, _, stored_claim = _accept(tmp_path, [reference])
    assert stored_claim.status is ClaimStatus.UNDER_REVIEW


def test_new_missions_freeze_strict_code_grading(tmp_path):
    service, mission, _ = two_branch_service(tmp_path)
    assert service.domain_for(mission.id).version == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "hash", "result", "attempt", "unrecorded", "receipt"])
async def test_k03_only_current_recorded_execution_produces_scoped_knowledge(tmp_path, mutation):
    service, mission, (task, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task)
    source = "def test_addition():\n    assert 2 + 2 == 4\n"
    path = "tests/probe/test_impl_a.py"
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[
                claim(
                    "pytest proves every backup will always be recoverable",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
        hashes={path: sha256(source.encode()).hexdigest()},
    )
    workspace = Workspace(tmp_path / "verify", attempt.id, True)
    workspace.root.mkdir()
    workspace.write_text(path, source)
    artifact = service.store.get_artifact(stored.artifacts[0])
    layer = await code_test(
        task,
        verification_copy=workspace,
        timeout=30,
        result_id=stored.envelope.id,
        artifacts=[artifact],
    )
    assert layer.status == "PASS"
    detail = dict(layer.detail)
    scope = dict(detail["observation_scope"])
    if mutation == "hash":
        scope["artifact_hashes"] = {path: "0" * 64}
    elif mutation in {"result", "attempt"}:
        scope[mutation + "_id"] = "old-version"
    elif mutation == "receipt":
        detail["runs"] = [
            {k: v for k, v in run.items() if k != "receipt"} for run in detail["runs"]
        ]
    detail["observation_scope"] = scope
    if mutation != "unrecorded":
        service.record_verification_layer(
            stored.envelope.id, layer="code_test", status="PASS", detail=detail
        )
    service.accept_result(stored.envelope.id, verifier_results=[layer.to_json()])
    knowledge = service.store.list_knowledge(mission.id)
    semantic = [
        c for c in service.store.list_claims(stored.envelope.id) if c.proposed_by == "agent-1"
    ]
    assert semantic[0].status is not ClaimStatus.VERIFIED
    if mutation is None:
        assert len(knowledge) == 1
        assert knowledge[0].type == "test_observation"
        assert knowledge[0].verifier["scope"]["artifact_hashes"] == {path: artifact.content_hash}
        assert "only that test outcome" in knowledge[0].content
        service.accept_result(stored.envelope.id, verifier_results=[layer.to_json()])
        assert len(service.store.list_knowledge(mission.id)) == 1
        assert service.store.count_events(mission.id, "KnowledgeCommitted") == 1
    else:
        assert knowledge == []
        assert semantic[0].status is ClaimStatus.UNDER_REVIEW


def test_missing_domain_binding_keeps_legacy_semantics():
    from agent_orchestrator.governance.domains import DomainProfileV1, resolve_domain

    legacy = resolve_domain(None)
    assert legacy.version == "1"
    assert DomainProfileV1.from_json(legacy.to_json()) == legacy


@pytest.mark.parametrize("scope", ["same", "other-mission", "other-attempt", "failed"])
def test_k02_tool_reference_must_resolve_to_success_in_this_attempt(tmp_path, scope):
    service, mission, (task, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task)
    ref = "run-1:call-1"
    service.store.record_tool_call(
        call_key=ref,
        subject_id=attempt.id if scope != "other-attempt" else "another-attempt",
        mission_id=mission.id if scope != "other-mission" else "another-mission",
        tool="workspace_read_file",
        outcome="failed" if scope == "failed" else "succeeded",
    )
    stored = submit(
        service,
        attempt,
        envelope(attempt, claims=[claim("The file was read", evidence=["tool-run:" + ref])]),
    )
    service.accept_result(stored.envelope.id, verifier_results=passed_layers())
    result = service.store.list_claims(stored.envelope.id)[0]
    assert result.status is (ClaimStatus.SUPPORTED if scope == "same" else ClaimStatus.UNDER_REVIEW)
    assert service.store.list_knowledge(mission.id) == []


def test_code_v2_keeps_accepted_verification_immutable(tmp_path):
    from agent_orchestrator.orchestrator.commit_service import CommitRejected

    service, _, claim = _accept(tmp_path, ["tool-run:missing"])
    with pytest.raises(CommitRejected, match="immutable"):
        service.record_verification_layer(
            claim.result_id, layer="code_test", status="PASS", detail={}
        )


def test_code_v2_prompts_describe_scoped_observations():
    from agent_orchestrator.governance.domains import CODE_PROFILE
    from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain

    for role in CODE_PROFILE.role_templates:
        template = template_for_domain(ROLES[role], CODE_PROFILE, {})
        assert "test_observation" in template.instructions
        assert "才可能被判 VERIFIED" not in template.instructions
