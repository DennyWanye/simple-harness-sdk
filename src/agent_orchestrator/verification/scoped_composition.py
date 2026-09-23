# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure acceptance formula for a frozen non-root compound projection."""

from __future__ import annotations

from ..contracts.evidence_state import ValidityWitness
from ..contracts.resolution import Criterion, ReviewPurpose, SuccessExpression
from .acceptance_rules import AcceptanceSubject, AcceptDecision, AcceptReason
from .scoped_acceptance import acceptable_scoped_task_content


def acceptable_scoped_composition(
    subject: AcceptanceSubject,
    *,
    projected_criteria: tuple[Criterion, ...],
    projected_expression: SuccessExpression,
    now_ms: int,
    witness: ValidityWitness,
    current_scope_epoch: int,
) -> AcceptDecision:
    """Reuse the scoped gates after binding the only allowed purpose/compound facts.

    The primitive helper intentionally rejects compounds and non-TASK_CONTENT
    purposes.  A local immutable view changes only those two type discriminators;
    all identities, outcomes, evidence, independence, witness and posture remain
    the caller's original records.
    """

    from dataclasses import replace

    if (
        subject.compound is None
        or subject.package.purpose is not ReviewPurpose.COMPOSITION
        or subject.record.purpose is not ReviewPurpose.COMPOSITION
    ):
        return acceptable_scoped_task_content(
            subject,
            projected_criteria=projected_criteria,
            projected_expression=projected_expression,
            now_ms=now_ms,
            witness=witness,
            current_scope_epoch=current_scope_epoch,
        )
    projected = replace(
        subject,
        package=replace(subject.package, purpose=ReviewPurpose.TASK_CONTENT),
        record=replace(subject.record, purpose=ReviewPurpose.TASK_CONTENT),
        compound=None,
    )
    decision = acceptable_scoped_task_content(
        projected,
        projected_criteria=projected_criteria,
        projected_expression=projected_expression,
        now_ms=now_ms,
        witness=witness,
        current_scope_epoch=current_scope_epoch,
    )
    compound = subject.compound
    assert compound is not None
    reasons = list(decision.reasons)
    missing = compound.missing_required_occurrences()
    if not compound.selected_method_legal:
        reasons.append(AcceptReason.COMPOUND_METHOD_ILLEGAL)
    if missing:
        reasons.append(AcceptReason.REQUIRED_OCCURRENCE_MISSING)
    if not compound.composition_obligation_passed:
        reasons.append(AcceptReason.COMPOSITION_OBLIGATION_FAILED)
    return replace(
        decision,
        reasons=tuple(reasons),
        missing_occurrence_ids=missing,
    )


__all__ = ("acceptable_scoped_composition",)
