"""Pure Task-content Scope acceptance keeps root identity without claiming root success."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from agent_orchestrator.contracts.resolution import (
    CriterionExpr,
    EvaluationKind,
    ReviewPurpose,
)
from agent_orchestrator.contracts.semantic_base import TypedRefKind
from agent_orchestrator.verification.acceptance_rules import (
    AcceptanceSubject,
    AcceptReason,
    CompoundFacts,
    ExecutionPosture,
    IndependenceFacts,
)
from agent_orchestrator.verification.scoped_acceptance import acceptable_scoped_task_content

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_acceptance_rules import (  # noqa: E402
    EPOCH,
    HASH_B,
    NOW_MS,
    PASS,
    attributed_receipt,
    binding,
    criterion,
    outcome,
    package,
    record,
    ref,
    revision,
    witness,
)


def _scoped_subject():
    root_criterion = criterion("root-delivery")
    root_revision = revision((root_criterion,), CriterionExpr("root-delivery"))
    local = criterion(
        "c-leaf-verified",
        checks=("leaf-verifier",),
        evaluation_kind=EvaluationKind.DETERMINISTIC,
    )
    projected = (local,)
    expression = CriterionExpr("c-leaf-verified")
    task_binding = dataclasses.replace(
        binding(),
        subject_ref=ref(TypedRefKind.TASK, "task-preparation", HASH_B),
    )
    review_package = package(
        projected,
        expression,
        review_binding=task_binding,
        requirements_content_hash=root_revision.content_hash(),
    )
    review_record = record(
        (
            outcome(
                "c-leaf-verified",
                PASS,
                evidence=(attributed_receipt("leaf-verifier-receipt"),),
            ),
        ),
        review_binding=task_binding,
    )
    subject = AcceptanceSubject(
        revision=root_revision,
        package=review_package,
        record=review_record,
        independence=IndependenceFacts(producer_agent_ids=("agent-worker",)),
        posture=ExecutionPosture(),
        semantic_review_required=True,
    )
    return subject, projected, expression


def _decide(subject, projected, expression):
    return acceptable_scoped_task_content(
        subject,
        projected_criteria=projected,
        projected_expression=expression,
        now_ms=NOW_MS,
        witness=witness(),
        current_scope_epoch=EPOCH,
    )


def test_scoped_content_accepts_local_projection_without_claiming_root_criterion() -> None:
    subject, projected, expression = _scoped_subject()

    decision = _decide(subject, projected, expression)

    assert decision.acceptable
    assert decision.expression.passed
    assert not decision.missing_root_ids
    assert "root-delivery" not in {item.criterion_id for item in subject.record.criteria}


def test_scoped_content_requires_task_subject_and_exact_root_revision_hash() -> None:
    subject, projected, expression = _scoped_subject()
    artifact_binding = dataclasses.replace(
        subject.package.binding,
        subject_ref=ref(TypedRefKind.ARTIFACT, "task-preparation", HASH_B),
    )
    changed = dataclasses.replace(
        subject,
        package=dataclasses.replace(subject.package, binding=artifact_binding),
        record=dataclasses.replace(subject.record, binding=artifact_binding),
    )
    decision = _decide(changed, projected, expression)
    assert AcceptReason.IDENTITY_MISMATCH in decision.reasons

    stale = dataclasses.replace(
        subject,
        package=dataclasses.replace(subject.package, requirements_content_hash="f" * 64),
    )
    decision = _decide(stale, projected, expression)
    assert AcceptReason.REQUIREMENTS_CONTENT_MISMATCH in decision.reasons


def test_scoped_content_rejects_projection_drift_and_missing_real_receipt() -> None:
    subject, projected, expression = _scoped_subject()
    other = criterion("other-local")

    decision = _decide(subject, (other,), CriterionExpr("other-local"))
    assert AcceptReason.CRITERIA_NOT_MATCHED in decision.reasons
    assert AcceptReason.SUCCESS_EXPRESSION_MISMATCH in decision.reasons

    unsourced = dataclasses.replace(
        subject,
        record=record(
            (outcome("c-leaf-verified", PASS),),
            review_binding=subject.package.binding,
        ),
    )
    decision = _decide(unsourced, projected, expression)
    assert AcceptReason.REQUIRED_CHECKS_INCOMPLETE in decision.reasons


def test_scoped_content_rejects_non_task_purpose() -> None:
    subject, projected, expression = _scoped_subject()
    changed = dataclasses.replace(
        subject,
        package=dataclasses.replace(subject.package, purpose=ReviewPurpose.COMPOSITION),
        record=dataclasses.replace(subject.record, purpose=ReviewPurpose.COMPOSITION),
    )
    decision = _decide(changed, projected, expression)
    assert AcceptReason.PURPOSE_MISMATCH in decision.reasons


def test_scoped_content_rejects_compound_subject() -> None:
    subject, projected, expression = _scoped_subject()
    changed = dataclasses.replace(
        subject,
        compound=CompoundFacts(selected_method_legal=True),
    )
    decision = _decide(changed, projected, expression)
    assert AcceptReason.COMPOUND_METHOD_ILLEGAL in decision.reasons
