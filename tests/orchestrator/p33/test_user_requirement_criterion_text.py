# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 真机文档任务：分层任务的成功条件存的是需求编号 c-user-N，文档核验曾把它当准则原文，
工人只看到"c-user-1"，任何结论都无法与之逐字绑定，文档 Mission 永远交付不了。"""
from dataclasses import replace

from graph_helpers7 import drive_to_running, graph_service, node

from agent_orchestrator.context.context_builder import build_worker_package
from agent_orchestrator.governance.domains import DOC_PROFILE
from agent_orchestrator.verification.assessments import task_criterion_text

STATEMENT = "summary.md 列出三条要点并注明来自哪个来源"


def test_user_requirement_ids_resolve_to_the_original_statement():
    texts = [STATEMENT, "第二条"]
    assert task_criterion_text("c-user-1", texts) == STATEMENT
    assert task_criterion_text("c-user-2", texts) == "第二条"
    # Anything else is kept exactly: out of range, derived ids, prefixed criteria.
    for kept in ("c-user-3", "c-user-0", "c-user-1-summary-written", "file:a.md", "cite:sources/a.md"):
        assert task_criterion_text(kept, texts) == kept


def test_worker_sees_the_statement_for_a_requirement_id(tmp_path):
    service, mission, tasks = graph_service(
        tmp_path,
        domain=DOC_PROFILE.id,
        success_criteria=(STATEMENT,),
        nodes=[node("A", verification_policy=["format_check", "rule_check", "critic_review"])],
    )
    try:
        task = replace(tasks["A"], success_criteria=("c-user-1",))
        attempt = drive_to_running(service, tasks["A"])
        package = build_worker_package(
            mission, task, attempt, domain=DOC_PROFILE, workspace_files=[],
            previous_attempts=[], verifier_feedback=[],
            source_versions={"sources/a.md": "a" * 64},
        ).package
        [criterion] = package["doc_assessment"]["criteria"]
        assert criterion["text"] == STATEMENT
        assert package["task_contract"]["success_criteria"] == ["c-user-1"]
    finally:
        service.store.close()


def test_verifier_and_worker_agree_on_the_criterion_id(tmp_path):
    from agent_orchestrator.contracts.models import ResultEnvelope
    from agent_orchestrator.verification.assessments import (
        AssessmentBindingV1,
        mission_criterion_catalog,
    )

    service, mission, tasks = graph_service(
        tmp_path,
        domain=DOC_PROFILE.id,
        success_criteria=(STATEMENT,),
        nodes=[node("A", verification_policy=["format_check", "rule_check", "critic_review"])],
    )
    try:
        task = replace(tasks["A"], success_criteria=("c-user-1",))
        attempt = drive_to_running(service, tasks["A"])
        package = build_worker_package(
            mission, task, attempt, domain=DOC_PROFILE, workspace_files=[],
            previous_attempts=[], verifier_feedback=[],
            source_versions={"sources/a.md": "a" * 64},
        ).package
        envelope = ResultEnvelope.from_json({
            "id": "r", "task_id": task.id, "attempt_id": attempt.id, "mission_id": mission.id,
            "outcome": "candidate", "summary": "s", "artifacts": [], "claims": [],
            "evidence": [], "risks": [], "proposed_tasks": [], "cost": {}, "used_knowledge": [],
        })
        binding = AssessmentBindingV1(
            tenant_id=mission.tenant_id, mission_id=mission.id, task_id=task.id,
            attempt_id=attempt.id, result_id="r", task_contract=package["task_contract"],
            output_hash="0" * 64, source_versions={}, source_roots=("sources",),
            claim_revisions={}, envelope=envelope,
            mission_criteria=mission_criterion_catalog(mission),
        )
        [verifier] = binding.criteria
        [worker] = package["doc_assessment"]["criteria"]
        assert (verifier["id"], verifier["text"]) == (worker["criterion_id"], worker["text"])
        assert verifier["kind"] == "free"
    finally:
        service.store.close()
