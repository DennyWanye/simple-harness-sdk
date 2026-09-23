# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure acceptance for a frozen Task-content completion Scope.

The root ``RequirementsRevision`` remains the immutable authority bound into the
review package.  ``projected_criteria`` and ``projected_expression`` describe only
the exact Task-content slice under review; accepting that slice never means the
root requirements or its Obligation are satisfied.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..assurance.checks import Formula, Grade
from ..contracts.evidence_state import ValidityWitness
from ..contracts.resolution import (
    AllExpr,
    AnyExpr,
    Criterion,
    CriterionExpr,
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
    CompletenessResult,
    ExpressionResult,
    GateResult,
    _witness_reasons,
    evaluate_success_expression,
    independence_ok,
    match_review_criteria,
    outcomes_by_id,
    project_verdict,
    required_checks_complete,
    success_expression_digest,
)


@dataclass(frozen=True, slots=True)
class AssuredAcceptance:
    """Current Assurance facts for the pure formula; decoding grants nothing.

    ``effective_grades`` are the per-criterion grades re-decided under the
    *current* typed check results by the validity evaluator, and
    ``licence_reasons`` is what that evaluator's committed certificate says
    about this use. The legacy ``CriterionOutcome`` projection of an assured
    record (SEMANTIC PASS shown as UNKNOWN/NOT_RUN) is never consulted here.
    """

    effective_grades: Mapping[str, str]
    gate_reasons: Mapping[str, str]
    licence_reasons: tuple[AcceptReason, ...] = ()


def _assurance_formula(expression: SuccessExpression) -> dict[str, Any]:
    if isinstance(expression, CriterionExpr):
        return {"criterion": str(expression.criterion_id)}
    if isinstance(expression, (AllExpr, AnyExpr)):
        return {
            "all" if isinstance(expression, AllExpr) else "any": [
                _assurance_formula(child) for child in expression.children
            ]
        }
    raise ValueError("illegal success expression node")


def _assured_grades(
    criteria: tuple[Criterion, ...], assured: AssuredAcceptance
) -> dict[str, Grade]:
    grades = {}
    for criterion in criteria:
        value = assured.effective_grades.get(criterion.criterion_id, "UNKNOWN")
        grades[criterion.criterion_id] = (
            Grade(value) if value in Grade.__members__ else Grade.UNKNOWN
        )
    return grades


def _assured_hard_gate(criteria: tuple[Criterion, ...], assured: AssuredAcceptance) -> GateResult:
    failed: list[str] = []
    unknown: list[str] = []
    missing: list[str] = []
    for criterion in criteria:
        if criterion.requirement_class is not RequirementClass.HARD_CONSTRAINT:
            continue
        value = assured.effective_grades.get(criterion.criterion_id)
        if value is None:
            missing.append(criterion.criterion_id)
        elif value == "FAIL":
            failed.append(criterion.criterion_id)
        elif value != "PASS":
            unknown.append(criterion.criterion_id)
    return GateResult(
        failed_ids=tuple(failed), unknown_ids=tuple(unknown), missing_ids=tuple(missing)
    )


def _assured_expression(
    criteria: tuple[Criterion, ...], expression: SuccessExpression, assured: AssuredAcceptance
) -> ExpressionResult:
    grades = _assured_grades(criteria, assured)
    formula = Formula.from_json(_assurance_formula(expression), frozenset(grades))
    verdict, witness = formula.evaluate(grades)
    return ExpressionResult(
        verdict=CriterionVerdict(verdict.value),
        witness_path=tuple(sorted(witness)),
        unevaluated_ids=tuple(
            sorted(
                criterion.criterion_id
                for criterion in criteria
                if criterion.criterion_id not in assured.effective_grades
            )
        ),
    )


def _assured_completeness(
    subject: AcceptanceSubject, assured: AssuredAcceptance
) -> CompletenessResult:
    """Required checks are complete when their *current* gate decided the grade."""
    package = subject.package
    match = match_review_criteria(subject.record, package, allow_unevaluated_or_branches=True)
    optional_ids = package.criteria_only_under_any()
    not_executed: list[str] = []
    not_passed: list[str] = []
    declined: list[str] = []
    independence: list[str] = []
    for criterion in package.criteria:
        if criterion.required_evidence_policy.independence_required:
            independence.append(criterion.criterion_id)
        grade = assured.effective_grades.get(criterion.criterion_id)
        if grade is None:
            if criterion.criterion_id in optional_ids:
                declined.append(criterion.criterion_id)
            continue
        if not criterion.is_required:
            continue
        if criterion.required_evidence_policy.required_check_ids:
            if assured.gate_reasons.get(criterion.criterion_id) == "CHECK_EVIDENCE_INCOMPLETE":
                not_executed.append(criterion.criterion_id)
            if grade != "PASS":
                not_passed.append(criterion.criterion_id)
    return CompletenessResult(
        match=match,
        not_executed_ids=tuple(not_executed),
        not_passed_ids=tuple(not_passed),
        declined_ids=tuple(sorted(declined)),
        independence_required_ids=tuple(independence),
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
    witness: ValidityWitness | None,
    current_scope_epoch: int,
    purpose: ReviewPurpose,
    subject_kinds: frozenset[TypedRefKind],
    assured: AssuredAcceptance | None = None,
) -> AcceptDecision:
    """Evaluate one primitive Task's frozen content projection.

    The caller must reconstruct the projection from the persisted Scope plus the
    approved Task/Method/review-policy contracts.  This function performs no store
    lookup and never creates a replacement ``RequirementsRevision``.

    Exactly one licence source is consulted: the legacy ``witness`` or, for an
    assured Mission, the current certificate facts in ``assured``.
    """

    if (witness is None) == (assured is None):
        raise ValueError("exactly one of witness or assured must be supplied")

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

    if assured is None:
        outcomes = outcomes_by_id(record.criteria)
        gate = _projected_hard_gate(projected_criteria, outcomes)
        expression = evaluate_success_expression(projected_expression, outcomes)
        completeness = required_checks_complete(package, record)
    else:
        gate = _assured_hard_gate(projected_criteria, assured)
        expression = _assured_expression(projected_criteria, projected_expression, assured)
        completeness = _assured_completeness(subject, assured)
    if gate.failed_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_FAILED)
    if gate.unknown_ids or gate.missing_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_UNKNOWN)
    if not expression.passed:
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_NOT_PASS)
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

    if witness is not None:
        reasons.extend(
            _witness_reasons(witness, now_ms=now_ms, current_scope_epoch=current_scope_epoch)
        )
    else:
        reasons.extend(assured.licence_reasons)
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


def acceptable_assured_root(
    subject: AcceptanceSubject,
    *,
    now_ms: int,
    purpose: ReviewPurpose,
    assured: AssuredAcceptance,
) -> AcceptDecision:
    """AER §6.2 for an assured Mission-root ``GoalResolution`` (handoff item 7).

    The same conjunction as ``acceptance_rules.acceptable`` — identity, root
    coverage, hard gate, success expression, completeness, independence, posture,
    compound facts — with one difference: every grade comes from the *current*
    re-decision of the bound review manifest (``assured.effective_grades``) and the
    licence is the committed UseCertificate (``assured.licence_reasons``), never the
    legacy ``CriterionOutcome`` projection or a self-issued witness.
    """

    if not isinstance(assured, AssuredAcceptance):
        raise ValueError("an assured root decision needs the current certificate facts")
    revision = subject.revision
    package = subject.package
    record = subject.record
    reasons: list[AcceptReason] = []

    if record.package_id != package.package_id or record.binding != package.binding:
        reasons.append(AcceptReason.IDENTITY_MISMATCH)
    if record.purpose is not package.purpose or record.purpose is not purpose:
        reasons.append(AcceptReason.PURPOSE_MISMATCH)
    if (
        package.binding.requirements_revision != revision.revision
        or package.binding.mission_id != revision.mission_id
    ):
        reasons.append(AcceptReason.REQUIREMENTS_REVISION_MISMATCH)
    if package.requirements_content_hash is not None:
        if package.requirements_content_hash != revision.content_hash():
            reasons.append(AcceptReason.REQUIREMENTS_CONTENT_MISMATCH)
    elif success_expression_digest(package.success_expression) != success_expression_digest(
        revision.success_expression
    ):
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_MISMATCH)
    structure_violations = package.hard_constraint_violations()
    if structure_violations:
        reasons.append(AcceptReason.HARD_CONSTRAINT_STRUCTURE)
    catalogue = set(package.criterion_catalogue())
    missing_root = tuple(
        criterion_id
        for criterion_id in revision.required_criterion_ids()
        if criterion_id not in catalogue
    )
    if missing_root:
        reasons.append(AcceptReason.ROOT_CRITERION_MISSING)

    gate = _assured_hard_gate(revision.criteria, assured)
    if gate.failed_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_FAILED)
    if gate.unknown_ids or gate.missing_ids:
        reasons.append(AcceptReason.HARD_CONSTRAINT_UNKNOWN)
    expression = _assured_expression(revision.criteria, revision.success_expression, assured)
    if not expression.passed:
        reasons.append(AcceptReason.SUCCESS_EXPRESSION_NOT_PASS)
    completeness = _assured_completeness(subject, assured)
    if not completeness.match.matched:
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
    reasons.extend(assured.licence_reasons)

    posture = subject.posture
    if posture.unowned_critical_operation_ids:
        reasons.append(AcceptReason.CRITICAL_OPERATION_UNOWNED)
    if posture.cancellation_requested:
        reasons.append(AcceptReason.CANCELLATION_PENDING)
    if not posture.method_adoption_current:
        reasons.append(AcceptReason.METHOD_ADOPTION_STALE)

    missing_occurrences: tuple[str, ...] = ()
    compound = subject.compound
    if compound is not None:
        if not compound.selected_method_legal:
            reasons.append(AcceptReason.COMPOUND_METHOD_ILLEGAL)
        missing_occurrences = compound.missing_required_occurrences()
        if missing_occurrences:
            reasons.append(AcceptReason.REQUIRED_OCCURRENCE_MISSING)
        if not compound.composition_obligation_passed:
            reasons.append(AcceptReason.COMPOSITION_OBLIGATION_FAILED)

    return AcceptDecision(
        reasons=tuple(reasons),
        expression=expression,
        gate=gate,
        completeness=completeness,
        independence=independence,
        missing_root_ids=missing_root,
        missing_occurrence_ids=missing_occurrences,
        hard_constraint_structure_ids=structure_violations,
    )


def acceptable_scoped_task_content(
    subject: AcceptanceSubject, *, projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression, now_ms: int, witness: ValidityWitness | None,
    current_scope_epoch: int, assured: AssuredAcceptance | None = None,
) -> AcceptDecision:
    return _acceptable_scoped(
        subject, projected_criteria=projected_criteria, projected_expression=projected_expression,
        now_ms=now_ms, witness=witness, current_scope_epoch=current_scope_epoch,
        purpose=ReviewPurpose.TASK_CONTENT, subject_kinds=frozenset({TypedRefKind.TASK}),
        assured=assured,
    )


def acceptable_scoped_operation_outcome(
    subject: AcceptanceSubject, *, projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression, now_ms: int, witness: ValidityWitness | None,
    current_scope_epoch: int, assured: AssuredAcceptance | None = None,
) -> AcceptDecision:
    return _acceptable_scoped(
        subject, projected_criteria=projected_criteria, projected_expression=projected_expression,
        now_ms=now_ms, witness=witness, current_scope_epoch=current_scope_epoch, assured=assured,
        purpose=ReviewPurpose.OPERATION_OUTCOME,
        subject_kinds=frozenset({TypedRefKind.OPERATION, TypedRefKind.TOOL_RECEIPT}),
    )


__all__ = (
    "AssuredAcceptance",
    "acceptable_assured_root",
    "acceptable_scoped_task_content",
    "acceptable_scoped_operation_outcome",
)
