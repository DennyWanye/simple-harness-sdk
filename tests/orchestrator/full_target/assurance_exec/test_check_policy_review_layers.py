# SPDX-License-Identifier: Apache-2.0
"""A Task-derived criterion's checks never include the review layer itself.

Host native canary 4 (2026-09-24): a document Task declared
("format_check", "rule_check", "code_test", "critic_review"); its derived criteria
required ``critic_review`` as a registered check, which the Assurance review *is*,
so the check policy could never be projected (CHECK_POLICY_UNRESOLVED: critic_review)
and the Mission stalled.  Independent adjudication: drop only the review layer; a
criterion left with no checks is judged by the reviewer (SEMANTIC); human_review stays.
"""

from __future__ import annotations

from agent_orchestrator.contracts.resolution import (
    Criterion,
    CriterionOrigin,
    EvaluationKind,
    RequirementClass,
)
from agent_orchestrator.orchestrator.scoped_content_review import local_check_criterion

DERIVED = Criterion(
    criterion_id="c-notes-file-written", revision=1, origin=CriterionOrigin.DERIVED,
    statement="NOTES.md is written", requirement_class=RequirementClass.REQUIRED_OUTCOME,
    evaluation_kind=EvaluationKind.DETERMINISTIC,
)


def test_review_layer_is_dropped_and_real_checks_stay_in_order() -> None:
    got = local_check_criterion(DERIVED, ("format_check", "rule_check", "code_test", "critic_review"))
    assert got.required_evidence_policy.required_check_ids == ("format_check", "rule_check", "code_test")
    assert got.evaluation_kind is EvaluationKind.DETERMINISTIC


def test_review_only_policy_is_judged_by_the_reviewer() -> None:
    got = local_check_criterion(DERIVED, ("critic_review",))
    assert got.required_evidence_policy.required_check_ids == ()
    assert got.evaluation_kind is EvaluationKind.SEMANTIC


def test_other_unregistered_layers_are_kept_never_dropped() -> None:
    got = local_check_criterion(DERIVED, ("format_check", "human_review", "critic_review"))
    assert got.required_evidence_policy.required_check_ids == ("format_check", "human_review")
    empty = local_check_criterion(DERIVED, ())
    assert empty == DERIVED  # nothing declared: unchanged (and still unresolved downstream)
