# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure acceptance for a frozen Task-content completion Scope.

The root ``RequirementsRevision`` remains the immutable authority bound into the
review package.  ``projected_criteria`` and ``projected_expression`` describe only
the exact Task-content slice under review; accepting that slice never means the
root requirements or its Obligation are satisfied.
"""

from __future__ import annotations

from ..contracts.evidence_state import ValidityWitness
from ..contracts.resolution import (
    Criterion,
    CriterionOutcome,
    CriterionVerdict,
    RequirementClass,
    ReviewPurpose,
    ReviewVerdict,
    SuccessExpression,
    hard_constraints_not_independent,
)
from ..contracts.semantic_base import TypedRefKind
from .acceptance_rules import (
    AcceptanceSubject,
    AcceptDecision,
    AcceptReason,
    GateResult,
    _witness_reasons,
    evaluate_success_expression,
    independence_ok,
    outcomes_by_id,
    project_verdict,
    required_checks_complete,
    success_expression_digest,
)


def _projected_hard_gate(
    criteria: tuple[Criterion, ...], outcomes: dict[str, CriterionOutcome]
) -> GateResult:
    failed: list[str] = []
    unknown: list[str] = []
    missing: list[str] = []
    for criterion in criteria:
        if criterion.requirement_class is not RequirementClass.HARD_CONSTRAINT:
            continue
        outcome = outcomes.get(criterion.criterion_id)
        if outcome is None:
            missing.append(criterion.criterion_id)
            continue
        verdict = project_verdict(outcome)
        if verdict is CriterionVerdict.FAIL:
            failed.append(criterion.criterion_id)
        elif verdict is not CriterionVerdict.PASS:
            unknown.append(criterion.criterion_id)
    return GateResult(
        failed_ids=tuple(failed),
        unknown_ids=tuple(unknown),
        missing_ids=tuple(missing),
    )


def _acceptable_scoped(
    subject: AcceptanceSubject,
    *,
    projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression,
    now_ms: int,
    witness: ValidityWitness,
    current_scope_epoch: int,
    purpose: ReviewPurpose,
    subject_kinds: frozenset[TypedRefKind],
) -> AcceptDecision:
    """Evaluate one primitive Task's frozen content projection.

    The caller must reconstruct the projection from the persisted Scope plus the
    approved Task/Method/review-policy contracts.  This function performs no store
    lookup and never creates a replacement ``RequirementsRevision``.
    """

    revision = subject.revision
    package = subject.package
    record = subject.record
    reasons: list[AcceptReason] = []

    if subject.compound is not None:
        reasons.append(AcceptReason.COMPOUND_METHOD_ILLEGAL)
    if record.package_id != package.package_id or record.binding != package.binding:
        reasons.append(AcceptReason.IDENTITY_MISMATCH)
    if (
        package.purpose is not purpose
        or record.purpose is not purpose
    ):
        reasons.append(AcceptReason.PURPOSE_MISMATCH)
    if package.binding.subject_ref.kind not in subject_kinds:
        reasons.append(AcceptReason.IDENTITY_MISMATCH)
    if (
        package.binding.mission_id != revision.mission_id
        or package.binding.requirements_revision != revision.revision
    ):
        reasons.append(AcceptReason.REQUIREMENTS_REVISION_MISMATCH)
    if package.requirements_content_hash != revision.content_hash():
        reasons.append(AcceptReason.REQUIREMENTS_CONTENT_MISMATCH)

    criteria_match = package.criteria == projected_criteria
    if not criteria_match:
        reasons.append(AcceptReason.CRITERIA_NOT_MATCHED)
    expression_match = success_expression_digest(
        package.success_expression
    ) == success_expression_digest(projected_expression)
    if not expression_match:
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_MISMATCH)

    structure_violations = hard_constraints_not_independent(
        projected_expression, projected_criteria
    )
    if structure_violations:
        reasons.append(AcceptReason.HARD_CONSTRAINT_STRUCTURE)

    outcomes = outcomes_by_id(record.criteria)
    gate = _projected_hard_gate(projected_criteria, outcomes)
    if gate.failed_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_FAILED)
    if gate.unknown_ids or gate.missing_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_UNKNOWN)

    expression = evaluate_success_expression(projected_expression, outcomes)
    if not expression.passed:
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_NOT_PASS)

    completeness = required_checks_complete(package, record)
    if not completeness.match.matched and AcceptReason.CRITERIA_NOT_MATCHED not in reasons:
        reasons.append(AcceptReason.CRITERIA_NOT_MATCHED)
    if not completeness.complete:
        reasons.append(AcceptReason.REQUIRED_CHECKS_INCOMPLETE)

    if record.verdict is not ReviewVerdict.ACCEPT:
        reasons.append(AcceptReason.REVIEW_VERDICT_NOT_ACCEPT)
    independence = independence_ok(package, record, facts=subject.independence)
    independence_required = subject.semantic_review_required or bool(
        completeness.independence_required_ids
    )
    if independence_required and not independence.independent:
        reasons.append(AcceptReason.INDEPENDENT_REVIEW_MISSING)

    reasons.extend(
        _witness_reasons(witness, now_ms=now_ms, current_scope_epoch=current_scope_epoch)
    )
    if subject.posture.unowned_critical_operation_ids:
        reasons.append(AcceptReason.CRITICAL_OPERATION_UNOWNED)
    if subject.posture.cancellation_requested:
        reasons.append(AcceptReason.CANCELLATION_PENDING)
    if not subject.posture.method_adoption_current:
        reasons.append(AcceptReason.METHOD_ADOPTION_STALE)

    return AcceptDecision(
        reasons=tuple(reasons),
        expression=expression,
        gate=gate,
        completeness=completeness,
        independence=independence,
        hard_constraint_structure_ids=structure_violations,
    )


def acceptable_scoped_task_content(
    subject: AcceptanceSubject, *, projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression, now_ms: int, witness: ValidityWitness,
    current_scope_epoch: int,
) -> AcceptDecision:
    return _acceptable_scoped(
        subject, projected_criteria=projected_criteria, projected_expression=projected_expression,
        now_ms=now_ms, witness=witness, current_scope_epoch=current_scope_epoch,
        purpose=ReviewPurpose.TASK_CONTENT, subject_kinds=frozenset({TypedRefKind.TASK}),
    )


def acceptable_scoped_operation_outcome(
    subject: AcceptanceSubject, *, projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression, now_ms: int, witness: ValidityWitness,
    current_scope_epoch: int,
) -> AcceptDecision:
    return _acceptable_scoped(
        subject, projected_criteria=projected_criteria, projected_expression=projected_expression,
        now_ms=now_ms, witness=witness, current_scope_epoch=current_scope_epoch,
        purpose=ReviewPurpose.OPERATION_OUTCOME,
        subject_kinds=frozenset({TypedRefKind.OPERATION, TypedRefKind.TOOL_RECEIPT}),
    )


__all__ = ("acceptable_scoped_task_content", "acceptable_scoped_operation_outcome")
