# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1 red tests: the AER annex contracts (§13, §14.3, AER §4–6, §12).

The eight annex fixtures, the operation payload conflict, the one-to-one match
between a review record and its package catalogue, and the invariants the codecs
themselves enforce (I07, I10, I18).
"""

from __future__ import annotations

from typing import Any

import pytest
from full_target_world import (
    FIXTURE_ROOT,
    HASH_A,
    HASH_B,
    HASH_C,
    load_aer_fixture,
    load_aer_fixture_index,
    tref,
)

from agent_orchestrator.contracts.evidence_state import (
    Availability,
    QueryCompleteness,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.resolution import (
    OPERATION_PAYLOAD_CONFLICT,
    AllExpr,
    AnyExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    DeliveryReceipt,
    DeliveryStage,
    EvaluationKind,
    OperationEnvelope,
    ReconciliationOutcome,
    ReconciliationResult,
    RequirementClass,
    RequirementsRevision,
    ReviewAccount,
    ReviewBinding,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewVerdict,
    WorkspaceAccess,
    account_for_purpose,
    criteria_only_under_any,
    envelope_conflict,
    hard_constraints_not_independent,
    match_review_criteria,
    may_rehandoff,
    parse_success_expression,
)
from agent_orchestrator.contracts.semantic_base import (
    MAX_ID,
    MAX_JSON_INT,
    MAX_REASON,
    Provenance,
    TypedRef,
    TypedRefKind,
)

CODECS: dict[str, Any] = {
    "review-record.schema.json": ReviewRecord.from_json,
    "validity-witness.schema.json": ValidityWitness.from_json,
    "operation-envelope.schema.json": OperationEnvelope.from_json,
    "reconciliation-result.schema.json": ReconciliationResult.from_json,
}


# --------------------------------------------------------------------------------------
# The eight annex fixtures
# --------------------------------------------------------------------------------------


def test_all_eight_aer_fixtures_land_on_the_expected_side() -> None:
    index = load_aer_fixture_index()
    assert len(index) == 8

    for entry in index:
        codec = CODECS[entry["schema"]]
        payload = load_aer_fixture(entry["fixture"])
        if entry["expect_valid"]:
            decoded = codec(payload)
            assert decoded.to_json() == payload, entry["fixture"]
        else:
            with pytest.raises(ContractError):
                codec(payload)


def test_every_negative_fixture_is_rejected_for_its_unknown_authority_field() -> None:
    """Each invalid sample smuggles ``model_says_approved`` past the contract."""

    index = load_aer_fixture_index()
    for entry in index:
        if entry["expect_valid"]:
            continue
        payload = load_aer_fixture(entry["fixture"])
        assert "model_says_approved" in payload
        with pytest.raises(ContractError, match="unknown fields"):
            CODECS[entry["schema"]](payload)


# --------------------------------------------------------------------------------------
# Operation identity (§14.3, AER §12.2)
# --------------------------------------------------------------------------------------


def envelope(
    *, operation_id: str = "operation-1", target: str = "recipient-1"
) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=operation_id,  # type: ignore[arg-type]
        operation_occurrence_id="send-approved-report-1",  # type: ignore[arg-type]
        mission_id="mission-1",
        obligation_id="obligation-send",
        scope_id="mission-1",
        connector_id="test-delivery",
        connector_version="1",
        operation_name="enqueue",
        operation_kind="EVENT_WRITE",  # type: ignore[arg-type]
        target_ref=target,
        expected_target_version=None,
        parameters_artifact_ref=tref(TypedRefKind.ARTIFACT, "parameters-1"),
        request_hash=HASH_C,
        requirements_revision=1,
        review_ref=tref(TypedRefKind.REVIEW, "review-1"),
        accepted_input_refs=(tref(TypedRefKind.ACCEPTANCE, "report-1"),),
        effect_contract_ref=tref(TypedRefKind.REQUIREMENTS, "delivery-contract-1"),
    )


def test_the_same_operation_id_with_a_different_payload_is_a_conflict() -> None:
    conflict = envelope_conflict(envelope(), envelope(target="recipient-2"))

    assert conflict is not None
    assert conflict.code == OPERATION_PAYLOAD_CONFLICT
    assert conflict.left_hash != conflict.right_hash


def test_the_same_operation_id_with_the_same_payload_is_a_retry_not_a_conflict() -> None:
    assert envelope_conflict(envelope(), envelope()) is None


def test_different_operation_ids_are_different_intents() -> None:
    assert envelope_conflict(envelope(), envelope(operation_id="operation-2")) is None


def test_an_operation_envelope_round_trips() -> None:
    original = envelope()
    assert OperationEnvelope.from_json(original.to_json()) == original


# --------------------------------------------------------------------------------------
# Reconciliation (invariant I10)
# --------------------------------------------------------------------------------------


def reconciliation(
    *,
    outcome: ReconciliationOutcome = ReconciliationOutcome.UNKNOWN,
    completeness: QueryCompleteness = QueryCompleteness.BEST_EFFORT,
    old_request_cannot_apply: bool = False,
    explanation: str = "nothing found, but the request may still be in flight",
) -> ReconciliationResult:
    return ReconciliationResult(
        record_id="reconciliation-1",
        operation_id="operation-1",  # type: ignore[arg-type]
        request_hash=HASH_C,
        connector_namespace="test-account/ledger-v1",
        outcome=outcome,
        observed_at_ms=1_500,
        evidence_refs=(),
        old_request_cannot_apply=old_request_cannot_apply,
        query_completeness=completeness,
        explanation=explanation,
    )


def test_an_empty_best_effort_lookup_does_not_license_a_resend() -> None:
    assert may_rehandoff(reconciliation()) is False


def test_a_final_negative_needs_an_authoritative_scoped_query() -> None:
    with pytest.raises(ContractError, match="authoritative, scoped query"):
        reconciliation(
            outcome=ReconciliationOutcome.NOT_APPLIED_FINAL,
            completeness=QueryCompleteness.BEST_EFFORT,
        )
    proven = reconciliation(
        outcome=ReconciliationOutcome.NOT_APPLIED_FINAL,
        completeness=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE,
    )
    assert may_rehandoff(proven) is True


def test_a_provably_unusable_old_request_may_be_re_handed_off() -> None:
    assert may_rehandoff(reconciliation(old_request_cannot_apply=True)) is True


# --------------------------------------------------------------------------------------
# Review: purposes, accounts, criterion matching
# --------------------------------------------------------------------------------------


def criterion(
    identifier: str,
    *,
    requirement_class: RequirementClass = RequirementClass.REQUIRED_OUTCOME,
) -> Criterion:
    return Criterion(
        criterion_id=identifier,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"criterion {identifier} is satisfied",
        requirement_class=requirement_class,
        evaluation_kind=EvaluationKind.SEMANTIC,
    )


def review_binding() -> ReviewBinding:
    return ReviewBinding(
        mission_id="mission-1",
        obligation_id="obligation-1",
        subject_ref=tref(TypedRefKind.TASK, "task-1"),
        requirements_revision=1,
        input_manifest_hash=HASH_B,
        policy_ref=tref(TypedRefKind.REQUIREMENTS, "policy-1"),
    )


def review_package(expression: Any, *criteria: Criterion) -> ReviewPackage:
    return ReviewPackage(
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        criteria=criteria,
        success_expression=expression,
    )


def outcome(identifier: str, verdict: CriterionVerdict = CriterionVerdict.PASS) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_id=identifier,
        verdict=verdict,
        check_execution=CheckExecution.SUCCEEDED,
    )


def review_record(*outcomes: CriterionOutcome) -> ReviewRecord:
    return ReviewRecord(
        record_id="review-1",  # type: ignore[arg-type]
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        reviewer_agent_id="independent-agent",
        reviewer_turn_id="turn-2",
        evidence_manifest_hash=HASH_A,
        criteria=outcomes,
        verdict=ReviewVerdict.ACCEPT,
    )


@pytest.mark.parametrize(
    "purpose,expected",
    [
        (ReviewPurpose.TASK_CONTENT, ReviewAccount.TASK),
        (ReviewPurpose.METHOD_PLAN, ReviewAccount.MISSION_PLANNING),
        (ReviewPurpose.COMPOSITION, ReviewAccount.PARENT_COMPOUND_TASK),
        (ReviewPurpose.ACTION_PROPOSAL, ReviewAccount.OPERATION_TASK),
        (ReviewPurpose.OPERATION_OUTCOME, ReviewAccount.OPERATION_TASK),
        (ReviewPurpose.MISSION_FINAL, ReviewAccount.MISSION),
    ],
)
def test_every_review_purpose_lands_on_its_account(
    purpose: ReviewPurpose, expected: ReviewAccount
) -> None:
    assert account_for_purpose(purpose) is expected


def test_the_account_map_covers_all_six_purposes() -> None:
    assert {account_for_purpose(item) for item in ReviewPurpose}
    assert len(list(ReviewPurpose)) == 6


def test_a_record_matching_its_catalogue_one_to_one() -> None:
    package = review_package(
        AllExpr((CriterionExpr("c-1"), CriterionExpr("c-2"))),
        criterion("c-1"),
        criterion("c-2"),
    )
    match = match_review_criteria(review_record(outcome("c-1"), outcome("c-2")), package)
    assert match.matched is True


def test_an_unknown_criterion_id_is_rejected() -> None:
    package = review_package(AllExpr((CriterionExpr("c-1"),)), criterion("c-1"))
    match = match_review_criteria(review_record(outcome("c-1"), outcome("c-9")), package)
    assert match.unknown == ("c-9",)
    assert match.matched is False


def test_a_repeated_criterion_id_is_rejected_by_the_codec_itself() -> None:
    with pytest.raises(ContractError, match="must not repeat a criterion_id"):
        review_record(outcome("c-1"), outcome("c-1"))


def test_a_missing_required_criterion_is_rejected() -> None:
    package = review_package(
        AllExpr((CriterionExpr("c-1"), CriterionExpr("c-2"))),
        criterion("c-1"),
        criterion("c-2"),
    )
    match = match_review_criteria(review_record(outcome("c-1")), package)
    assert match.missing_required == ("c-2",)
    assert match.matched is False


def test_an_unevaluated_or_branch_is_allowed_to_be_absent() -> None:
    package = review_package(
        AllExpr((CriterionExpr("c-1"), AnyExpr((CriterionExpr("c-2"), CriterionExpr("c-3"))))),
        criterion("c-1"),
        criterion("c-2"),
        criterion("c-3"),
    )
    match = match_review_criteria(review_record(outcome("c-1"), outcome("c-2")), package)
    assert match.matched is True


def test_a_check_that_never_ran_is_not_a_pass() -> None:
    """Invariant I07."""

    for execution in (CheckExecution.NOT_RUN, CheckExecution.ERROR, CheckExecution.CANCELLED):
        with pytest.raises(ContractError, match="did not run or errored is not a PASS"):
            CriterionOutcome(
                criterion_id="c-1", verdict=CriterionVerdict.PASS, check_execution=execution
            )


def test_an_execution_error_may_still_report_unknown() -> None:
    record = CriterionOutcome(
        criterion_id="c-1",
        verdict=CriterionVerdict.UNKNOWN,
        check_execution=CheckExecution.ERROR,
        limitations=("the isolated runner could not start",),
    )
    assert record.verdict is CriterionVerdict.UNKNOWN


def test_a_review_record_round_trips() -> None:
    record = review_record(outcome("c-1"))
    assert ReviewRecord.from_json(record.to_json()) == record
    assert record.account is ReviewAccount.TASK


# --------------------------------------------------------------------------------------
# Success expression: structure only (§25.1 decision 2)
# --------------------------------------------------------------------------------------


def test_the_success_expression_rejects_empty_children_and_unknown_operators() -> None:
    with pytest.raises(ContractError, match="at least 1 entries"):
        parse_success_expression({"op": "all", "children": []})
    with pytest.raises(ContractError, match="children must not be empty"):
        AllExpr(children=())
    with pytest.raises(ContractError, match="must be one of"):
        parse_success_expression({"op": "not", "children": [{"op": "criterion", "id": "c-1"}]})
    with pytest.raises(ContractError):
        parse_success_expression("c-1 and c-2")


def test_a_hard_constraint_may_not_hide_inside_an_or_branch() -> None:
    criteria = (
        criterion("c-safety", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("c-proof"),
        criterion("c-counterexample"),
    )
    traded_away = AnyExpr((CriterionExpr("c-safety"), CriterionExpr("c-proof")))
    assert hard_constraints_not_independent(traded_away, criteria) == ("c-safety",)

    independent = AllExpr(
        (
            CriterionExpr("c-safety"),
            AnyExpr((CriterionExpr("c-proof"), CriterionExpr("c-counterexample"))),
        )
    )
    assert hard_constraints_not_independent(independent, criteria) == ()


def test_a_requirements_revision_refuses_an_expression_that_drops_a_hard_constraint() -> None:
    criteria = (
        criterion("c-safety", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("c-report"),
    )
    with pytest.raises(ContractError, match="independent AND conjunct"):
        RequirementsRevision(
            revision_id="requirements-1",  # type: ignore[arg-type]
            mission_id="mission-1",
            revision=1,
            criteria=criteria,
            success_expression=AllExpr((CriterionExpr("c-report"),)),
        )


def test_a_requirements_revision_round_trips() -> None:
    original = RequirementsRevision(
        revision_id="requirements-1",  # type: ignore[arg-type]
        mission_id="mission-1",
        revision=1,
        criteria=(criterion("c-1"), criterion("c-2")),
        success_expression=AllExpr((CriterionExpr("c-1"), CriterionExpr("c-2"))),
    )
    restored = RequirementsRevision.from_json(original.to_json())
    assert restored.to_json() == original.to_json()
    assert restored.required_criterion_ids() == ("c-1", "c-2")


def test_a_hard_constraint_cannot_declare_itself_model_exemptible() -> None:
    from agent_orchestrator.contracts.resolution import AmendmentPolicy

    with pytest.raises(ContractError, match="model-exemptible"):
        Criterion(
            criterion_id="c-safety",
            revision=1,
            origin=CriterionOrigin.POLICY_REQUIRED,
            statement="never exfiltrate the workspace",
            requirement_class=RequirementClass.HARD_CONSTRAINT,
            evaluation_kind=EvaluationKind.DETERMINISTIC,
            amendment_policy=AmendmentPolicy(amendable_by=("owner",), model_exemptible=True),
        )


# --------------------------------------------------------------------------------------
# Validity witness and delivery (I18, I19, §6.1)
# --------------------------------------------------------------------------------------


def witness(*, truth: TruthValue = TruthValue.TRUE, epoch: int = 3) -> ValidityWitness:
    return ValidityWitness(
        witness_id="witness-1",
        consumer_ref=tref(TypedRefKind.TASK, "task-2"),
        purpose=WitnessPurpose.START,
        truth=truth,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=WitnessDecision.USABLE,
        scope_id="mission-1",
        scope_epoch=epoch,
        support_revision=4,
        as_of_ms=1_000,
        not_after_ms=2_000,
        support_refs=(tref(TypedRefKind.OBSERVATION, "observation-1"),),
    )


def test_a_usable_witness_requires_a_true() -> None:
    with pytest.raises(ContractError, match="USABLE requires truth TRUE"):
        witness(truth=TruthValue.UNKNOWN)


def test_a_witness_goes_unusable_once_the_scope_epoch_or_deadline_moves() -> None:
    current = witness(epoch=3)
    assert current.is_fresh_for(now_ms=1_500, current_scope_epoch=3) is True
    assert current.is_fresh_for(now_ms=1_500, current_scope_epoch=4) is False
    assert current.is_fresh_for(now_ms=2_500, current_scope_epoch=3) is False


def test_a_witness_round_trips() -> None:
    original = witness()
    assert ValidityWitness.from_json(original.to_json()) == original


def test_a_sent_delivery_must_name_the_operation_that_produced_it() -> None:
    with pytest.raises(ContractError, match="must name the operation"):
        DeliveryReceipt(
            receipt_id="receipt-1",
            mission_id="mission-1",
            acceptance_id="acceptance-1",  # type: ignore[arg-type]
            stage=DeliveryStage.SENT,
            observed_at_ms=2_000,
        )
    persisted = DeliveryReceipt(
        receipt_id="receipt-1",
        mission_id="mission-1",
        acceptance_id="acceptance-1",  # type: ignore[arg-type]
        stage=DeliveryStage.PERSISTED,
        observed_at_ms=2_000,
    )
    assert DeliveryReceipt.from_json(persisted.to_json()) == persisted


# --------------------------------------------------------------------------------------
# Schema boundaries on the AER side
# --------------------------------------------------------------------------------------


def _schema_values(node: Any, keyword: str) -> set[Any]:
    found: set[Any] = set()
    if isinstance(node, dict):
        for key, item in node.items():
            if key == keyword and not isinstance(item, (dict, list)):
                found.add(item)
            found |= _schema_values(item, keyword)
    elif isinstance(node, list):
        for item in node:
            found |= _schema_values(item, keyword)
    return found


def _all_aer_schemas() -> list[Any]:
    import json

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURE_ROOT / "aer").glob("*.schema.json"))
    ]


def test_the_aer_caps_are_exactly_the_ones_the_annex_schemas_declare() -> None:
    schemas = _all_aer_schemas()
    assert _schema_values(schemas, "maxLength") == {512, 10_000}
    assert _schema_values(schemas, "minLength") == {1}
    assert _schema_values(schemas, "minimum") == {0}
    assert _schema_values(schemas, "maximum") == {9_007_199_254_740_991}

    assert MAX_ID == 512
    assert MAX_REASON == 10_000
    assert MAX_JSON_INT == 9_007_199_254_740_991


def test_an_explanation_of_exactly_ten_thousand_characters_is_accepted() -> None:
    at_limit = "说" * MAX_REASON
    assert reconciliation(explanation=at_limit).explanation == at_limit
    with pytest.raises(ContractError, match=f"exceeds {MAX_REASON} characters"):
        reconciliation(explanation="说" * (MAX_REASON + 1))


def test_a_revision_at_the_safe_integer_ceiling_is_accepted_and_one_more_is_not() -> None:
    assert tref(TypedRefKind.OBSERVATION, "obs-1", revision=MAX_JSON_INT).revision == MAX_JSON_INT
    with pytest.raises(ContractError, match="safe JSON integer range"):
        tref(TypedRefKind.OBSERVATION, "obs-1", revision=MAX_JSON_INT + 1)


def test_a_review_record_must_report_at_least_one_criterion() -> None:
    payload = review_record(outcome("c-1")).to_json()
    payload["criteria"] = []
    with pytest.raises(ContractError, match="at least 1 entries"):
        ReviewRecord.from_json(payload)


def test_an_observation_must_arrive_with_at_least_one_piece_of_evidence() -> None:
    from agent_orchestrator.contracts.htn import FeedbackObservation

    with pytest.raises(ContractError, match="at least 1 entries"):
        FeedbackObservation.from_json(
            {"statement": "the source was unreachable", "evidence_refs": []}
        )


# --------------------------------------------------------------------------------------
# A usable witness is TRUE *and* current *and* readable (AER §8.2)
# --------------------------------------------------------------------------------------


def test_a_usable_witness_may_not_rest_on_revoked_support() -> None:
    with pytest.raises(ContractError, match="requires freshness CURRENT"):
        ValidityWitness(
            witness_id="witness-1",
            consumer_ref=tref(TypedRefKind.TASK, "task-2"),
            purpose=WitnessPurpose.START,
            truth=TruthValue.TRUE,
            freshness=Validity.REVOKED,
            availability=Availability.READABLE,
            decision=WitnessDecision.USABLE,
            scope_id="mission-1",
            scope_epoch=3,
            support_revision=4,
            as_of_ms=1_000,
        )


def test_a_usable_witness_may_not_rest_on_unreadable_support() -> None:
    with pytest.raises(ContractError, match="requires availability READABLE"):
        ValidityWitness(
            witness_id="witness-1",
            consumer_ref=tref(TypedRefKind.TASK, "task-2"),
            purpose=WitnessPurpose.START,
            truth=TruthValue.TRUE,
            freshness=Validity.CURRENT,
            availability=Availability.UNAVAILABLE,
            decision=WitnessDecision.USABLE,
            scope_id="mission-1",
            scope_epoch=3,
            support_revision=4,
            as_of_ms=1_000,
        )


def test_a_blocked_witness_may_record_any_combination() -> None:
    blocked = ValidityWitness(
        witness_id="witness-1",
        consumer_ref=tref(TypedRefKind.TASK, "task-2"),
        purpose=WitnessPurpose.START,
        truth=TruthValue.TRUE,
        freshness=Validity.STALE,
        availability=Availability.REDACTED,
        decision=WitnessDecision.BLOCKED,
        scope_id="mission-1",
        scope_epoch=3,
        support_revision=4,
        as_of_ms=1_000,
        reason_codes=("VALIDITY_RECHECK_PENDING",),
    )
    assert blocked.decision is WitnessDecision.BLOCKED


def test_the_not_after_deadline_is_exclusive() -> None:
    """The annex reference reads ``now_ms < not_after_ms``: at the instant it
    expires, it is expired."""

    current = witness(epoch=3)
    assert current.not_after_ms == 2_000
    assert current.is_fresh_for(now_ms=1_999, current_scope_epoch=3) is True
    assert current.is_fresh_for(now_ms=2_000, current_scope_epoch=3) is False


# --------------------------------------------------------------------------------------
# Contract round 3: review-package additions requested by P1.1b
# --------------------------------------------------------------------------------------


def test_a_package_records_who_produced_the_candidate() -> None:
    package = ReviewPackage(
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        criteria=(criterion("c-1"),),
        success_expression=AllExpr((CriterionExpr("c-1"),)),
        producer_agent_ids=("agent-worker-1", "agent-worker-2"),
    )

    assert package.produced_by("agent-worker-1") is True
    assert package.produced_by("agent-reviewer") is False
    assert ReviewPackage.from_json(package.to_json()) == package


def test_a_package_may_not_grant_the_reviewer_write_access() -> None:
    """AER §5.3: a reviewer that can edit the candidate cannot independently judge it."""

    with pytest.raises(ContractError, match="may not hold write access"):
        ReviewPackage(
            package_id="package-1",  # type: ignore[arg-type]
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=review_binding(),
            criteria=(criterion("c-1"),),
            success_expression=AllExpr((CriterionExpr("c-1"),)),
            reviewer_workspace_access=WorkspaceAccess.WRITE,
        )


def test_a_package_defaults_to_read_only_and_accepts_no_access() -> None:
    default = review_package(AllExpr((CriterionExpr("c-1"),)), criterion("c-1"))
    assert default.reviewer_workspace_access is WorkspaceAccess.READ_ONLY

    sealed = ReviewPackage(
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        criteria=(criterion("c-1"),),
        success_expression=AllExpr((CriterionExpr("c-1"),)),
        reviewer_workspace_access=WorkspaceAccess.NONE,
    )
    assert sealed.reviewer_workspace_access is WorkspaceAccess.NONE


def test_a_package_bound_to_a_requirements_revision_keeps_its_hard_constraints_independent() -> (
    None
):
    criteria = (
        criterion("c-safety", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("c-proof"),
        criterion("c-counterexample"),
    )
    sound = ReviewPackage(
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        criteria=criteria,
        success_expression=AllExpr(
            (
                CriterionExpr("c-safety"),
                AnyExpr((CriterionExpr("c-proof"), CriterionExpr("c-counterexample"))),
            )
        ),
        requirements_content_hash=HASH_C,
    )
    assert sound.hard_constraint_violations() == ()

    with pytest.raises(ContractError, match="independent AND conjunct"):
        ReviewPackage(
            package_id="package-1",  # type: ignore[arg-type]
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=review_binding(),
            criteria=criteria,
            success_expression=AnyExpr((CriterionExpr("c-safety"), CriterionExpr("c-proof"))),
            requirements_content_hash=HASH_C,
        )


def test_an_unbound_package_still_reports_its_hard_constraint_violations() -> None:
    """A tampered anchor that claims no requirements revision is still constructible.

    Catching it is the requirements-digest check's job (P1.1b), not the codec's —
    but the package says so itself, so that check does not have to re-derive it.
    """

    criteria = (
        criterion("c-safety", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("c-proof"),
    )
    tampered = ReviewPackage(
        package_id="package-1",  # type: ignore[arg-type]
        purpose=ReviewPurpose.TASK_CONTENT,
        binding=review_binding(),
        criteria=criteria,
        success_expression=AnyExpr((CriterionExpr("c-safety"), CriterionExpr("c-proof"))),
    )
    assert tampered.hard_constraint_violations() == ("c-safety",)
    assert tampered.requirements_content_hash is None


def test_criteria_only_under_any_is_public_and_ignores_criteria_seen_outside_an_any() -> None:
    package = review_package(
        AllExpr(
            (
                CriterionExpr("c-1"),
                AnyExpr((CriterionExpr("c-1"), CriterionExpr("c-2"))),
            )
        ),
        criterion("c-1"),
        criterion("c-2"),
    )

    assert criteria_only_under_any(package.success_expression) == frozenset({"c-2"})
    assert package.criteria_only_under_any() == frozenset({"c-2"})


def test_a_review_package_requirements_hash_must_be_a_digest() -> None:
    with pytest.raises(ContractError, match="SHA-256 hex digest"):
        ReviewPackage(
            package_id="package-1",  # type: ignore[arg-type]
            purpose=ReviewPurpose.TASK_CONTENT,
            binding=review_binding(),
            criteria=(criterion("c-1"),),
            success_expression=AllExpr((CriterionExpr("c-1"),)),
            requirements_content_hash="not-a-digest",
        )


def test_a_typed_ref_may_carry_an_attribution_but_a_model_may_not_claim_one() -> None:
    attributed = TypedRef(
        kind=TypedRefKind.TOOL_RECEIPT,
        id="receipt-1",
        revision=1,
        content_hash=HASH_A,
        produced_by=Provenance.TOOL,
    )
    assert TypedRef.from_json(attributed.to_json()) == attributed

    with pytest.raises(ContractError, match="may not claim 'tool'"):
        TypedRef.from_model_json(attributed.to_json())

    claimed_by_model = dict(attributed.to_json())
    claimed_by_model["produced_by"] = "model"
    assert TypedRef.from_model_json(claimed_by_model).produced_by is Provenance.MODEL


def test_an_unknown_attribution_is_refused() -> None:
    payload = {
        "kind": "tool_receipt",
        "id": "receipt-1",
        "revision": 1,
        "content_hash": HASH_A,
        "produced_by": "trusted",
    }
    with pytest.raises(ContractError, match="must be one of"):
        TypedRef.from_json(payload)
    with pytest.raises(ContractError, match="not a known attribution"):
        TypedRef.from_model_json(payload)
