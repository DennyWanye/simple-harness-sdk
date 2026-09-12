"""A large failed citation record must not consume the next required context."""

from dataclasses import replace

from graph_helpers7 import drive_to_running, graph_service, node

from agent_orchestrator.context.context_builder import build_worker_package
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.domains import CODE_PROFILE, DOC_PROFILE, DOC_PROFILE_V5
from agent_orchestrator.runtime.model_router import classify_turn_error


def test_retry_keeps_actionable_ids_without_reinlining_all_source_blocks(tmp_path):
    service, mission, tasks = graph_service(
        tmp_path,
        domain=DOC_PROFILE.id,
        success_criteria=("file:a.md",),
        nodes=[node("A", verification_policy=["format_check", "rule_check", "critic_review"])],
    )
    try:
        task = tasks["A"]
        original = drive_to_running(service, task)
        prose = "OLD_SOURCE_MUST_NOT_REENTER_REQUIRED_CONTEXT " * 5000
        pairs = [
            {"claim_id": f"r:claim-{n}", "criterion_id": "criterion-needed"} for n in range(20)
        ]
        detail = {
            "layer": "rule_check",
            "status": "FAIL",
            "result_id": "r",
            "detail": {
                "assessment_binding": {"envelope": {"summary": prose}},
                "criterion_assessments": [{"evidence_refs": [{"display_block": prose}]}],
                "hard_failures": ["missing_limitations"],
                "limitations_check": {"required": pairs, "missing": pairs},
            },
        }
        attempt = replace(original, feedback=(prose[:1000],))
        previous = replace(original, failure=detail)
        kwargs = dict(
            previous_attempts=[previous],
            verifier_feedback=[detail],
            workspace_files=[],
            source_versions={"sources/a.md": "a" * 64},
        )
        current = build_worker_package(mission, task, attempt, domain=DOC_PROFILE, **kwargs)
        raw = build_worker_package(mission, task, attempt, domain=DOC_PROFILE_V5, **kwargs)
        assert len(raw.text.encode()) > 400_000
        assert len(current.text.encode()) < 16_384
        assert prose[:40] not in current.text
        assert current.package["task_contract"]["success_criteria"] == list(task.success_criteria)
        assert current.package["mission_root_goal"] == mission.goal
        feedback = current.package["verifier_feedback"][0]
        assert feedback["record_sha256"] == sha256_hex(detail)
        assert feedback["diagnostic"]["detail"]["limitations_check"]["missing"] == pairs
        assert "missing_limitations" in current.text
        assert current.package["source_versions"] == kwargs["source_versions"]
        for old in (CODE_PROFILE, DOC_PROFILE_V5):
            package = build_worker_package(mission, task, attempt, domain=old, **kwargs).package
            assert package["verifier_feedback"] == [detail]
            assert package["feedback"] == [prose[:1000]]
    finally:
        service.store.close()


def test_required_context_failure_is_not_provider_health_or_model_quality():
    assert (
        classify_turn_error({"error": {"error_code": "context_required_content_too_large"}})
        == "runtime_context_limit"
    )
