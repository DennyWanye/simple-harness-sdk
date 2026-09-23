"""Scoped composition keeps root Requirements identity without claiming root success."""

from __future__ import annotations

import dataclasses

from test_scoped_acceptance_rules import EPOCH, NOW_MS, _scoped_subject, witness

from agent_orchestrator.contracts.resolution import ReviewPurpose
from agent_orchestrator.verification.acceptance_rules import AcceptReason, CompoundFacts
from agent_orchestrator.verification.scoped_composition import acceptable_scoped_composition


def _composition_subject(*, legal: bool = True, passed: bool = True):
    subject, criteria, expression = _scoped_subject()
    package = dataclasses.replace(subject.package, purpose=ReviewPurpose.COMPOSITION)
    record = dataclasses.replace(subject.record, purpose=ReviewPurpose.COMPOSITION)
    return (
        dataclasses.replace(
            subject,
            package=package,
            record=record,
            compound=CompoundFacts(
                selected_method_legal=legal,
                composition_obligation_passed=passed,
            ),
        ),
        criteria,
        expression,
    )


def _decide(subject, criteria, expression):
    return acceptable_scoped_composition(
        subject,
        projected_criteria=criteria,
        projected_expression=expression,
        now_ms=NOW_MS,
        witness=witness(),
        current_scope_epoch=EPOCH,
    )


def test_scoped_composition_accepts_exact_projection_without_rewriting_root_formula() -> None:
    subject, criteria, expression = _composition_subject()

    decision = _decide(subject, criteria, expression)

    assert decision.acceptable
    assert subject.revision.success_expression != expression
    assert "root-delivery" not in {item.criterion_id for item in subject.record.criteria}


def test_scoped_composition_keeps_method_and_composition_gates() -> None:
    subject, criteria, expression = _composition_subject(legal=False, passed=False)

    decision = _decide(subject, criteria, expression)

    assert AcceptReason.COMPOUND_METHOD_ILLEGAL in decision.reasons
    assert AcceptReason.COMPOSITION_OBLIGATION_FAILED in decision.reasons
