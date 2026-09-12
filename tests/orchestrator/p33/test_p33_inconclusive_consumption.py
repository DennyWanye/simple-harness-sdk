"""D01/D03/D09: uncertainty survives grading and only real v2 proof can promote."""

from dataclasses import replace

import pytest
from graph_helpers7 import drive_to_running, graph_service
from test_p33_document_grading import assessment, citation, grade

from agent_orchestrator.context.context_builder import build_worker_package
from agent_orchestrator.contracts import ClaimProposal, ClaimStatus, CriterionAssessmentV1
from agent_orchestrator.governance.domains import DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.memory.claims import system_attribution


def v2(proposal, *, verdict="PASS", **overrides):
    body = assessment(proposal, verdict=verdict).to_json()
    body.pop("receipt_id")
    body["version"] = "2"
    body["provenance"]["producer"] = "citation_integrity@v2"
    body.update(overrides)
    return CriterionAssessmentV1.create(**body)


@pytest.mark.parametrize("extra_pass", [False, True])
def test_inconclusive_cannot_be_hidden_by_a_pass_for_the_same_claim(extra_pass):
    c = citation()
    proposal = ClaimProposal(c.quote, 1.0, type="attribution", citations=(c,))
    rows = [v2(proposal, verdict="INCONCLUSIVE")]
    if extra_pass:
        rows.append(v2(proposal, criterion_id="other-criterion"))
    outcome = grade(proposal, rows)
    assert outcome.status is ClaimStatus.UNDER_REVIEW
    assert outcome.basis["grade"] == "insufficient_evidence"
    assert outcome.basis["assessment_receipts"] == sorted(r.receipt_id for r in rows)
    assert outcome.evidence_trust == ("untrusted_external",)
    assert system_attribution(outcome.basis) is None


def test_valid_v2_literal_assessment_retains_scoped_attribution():
    c = citation()
    proposal = ClaimProposal(c.quote, 0.5, citations=(c,))
    outcome = grade(proposal, [v2(proposal)])
    assert outcome.status is ClaimStatus.VERIFIED
    assert outcome.basis["adapter"] == "citation_integrity@v2"
    assert system_attribution(outcome.basis)["source_trust"] == "untrusted_external"


def test_an_unrelated_claims_inconclusive_record_is_not_uncertainty_evidence():
    c = citation()
    proposal = ClaimProposal(c.quote, 0.5, citations=(c,))
    other = replace(proposal, content="不同的主张")
    outcome = grade(proposal, [v2(other, verdict="INCONCLUSIVE")])
    assert outcome.status is ClaimStatus.UNDER_REVIEW
    assert outcome.basis["grade"] == "unsupported"


def test_new_doc_package_explains_candidates_and_original_mission_catalog(tmp_path):
    service, mission, tasks = graph_service(tmp_path, domain=DOC_DOMAIN)
    task = tasks["A"]
    attempt = drive_to_running(service, task)
    package = build_worker_package(
        mission,
        task,
        attempt,
        previous_attempts=[],
        verifier_feedback=[],
        workspace_files=[],
        domain=DOC_PROFILE,
        source_versions={},
    )
    detail = package.package["doc_assessment"]
    assert detail["version"] == "doc-assessment-v2"
    assert detail["candidate_fields"] == ["criterion_ids", "mission_criterion_ids"]
    assert detail["limitations_fields"] == ["criterion_id", "claim_id", "missing"]
    assert [c["text"] for c in detail["mission_criteria"]] == list(mission.success_criteria)
    assert "claim:1" in detail["limitation_rule"]
    assert "candidate" in detail["criterion_rule"]
    assert detail["mission_contract_revision"]
    assert detail["check_spec_ids"] == ["citation_integrity@v2", "source_coverage@v1"]
