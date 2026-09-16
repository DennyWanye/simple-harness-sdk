# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1b red tests: the pure acceptance rules (AER §4.3, §5.3–5.4, §6.2, §7).

Rewritten from ``annex/aer-1.0/reference/protocol_rules.py`` and its thirty
reference unit tests against the *production* contracts, so the rules are pinned
where the SDK will actually read them.  Nothing here calls a model, reads a
store, or imports a commit path — the module under test is a pure function
library and one test asserts that literally.

The scenarios carry their AER ids: V01 (a missing root requirement), V02 (a hard
gate that an approved result OR must not cancel), V04 (a check that did not run
is not a PASS), V05 (independence), V08 (infrastructure error is not a semantic
negative).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_orchestrator.contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    ChildBinding,
    MethodInstanceId,
    ObligationId,
    OccurrenceId,
    Requiredness,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.resolution import (
    AllExpr,
    AnyExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequiredEvidencePolicy,
    RequirementClass,
    RequirementsRevision,
    RequirementsRevisionId,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
)
from agent_orchestrator.contracts.semantic_base import Provenance, TypedRef, TypedRefKind
from agent_orchestrator.verification import acceptance_rules
from agent_orchestrator.verification.acceptance_rules import (
    AcceptanceSubject,
    AcceptReason,
    CompoundFacts,
    ExecutionPosture,
    IndependenceFacts,
    IndependenceReason,
    ReceiptSource,
    RetryAction,
    RetryReason,
    ReviewAttempt,
    ReviewHistory,
    acceptable,
    evaluate_success_expression,
    hard_gate,
    independence_ok,
    outcomes_by_id,
    project_verdict,
    receipt_source,
    required_checks_complete,
    retry_policy,
    success_expression_digest,
)

PASS = CriterionVerdict.PASS
FAIL = CriterionVerdict.FAIL
UNKNOWN = CriterionVerdict.UNKNOWN

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

NOW_MS = 1_000
EPOCH = 7


# --------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------


def ref(
    kind: TypedRefKind,
    ident: str = "r1",
    digest: str = HASH_A,
    *,
    produced_by: Provenance | None = None,
) -> TypedRef:
    return TypedRef(kind=kind, id=ident, revision=0, content_hash=digest, produced_by=produced_by)


def tool_receipt(ident: str = "receipt-1") -> TypedRef:
    """A receipt kind with no attribution — the legacy, fallback-only shape."""

    return ref(TypedRefKind.TOOL_RECEIPT, ident)


def attributed_receipt(ident: str = "receipt-1") -> TypedRef:
    """A receipt the system attributed to a real dispatched tool."""

    return ref(TypedRefKind.TOOL_RECEIPT, ident, produced_by=Provenance.TOOL)


def forged_receipt(ident: str = "receipt-1") -> TypedRef:
    """Receipt *kind*, but the model attributed it to itself."""

    return ref(TypedRefKind.TOOL_RECEIPT, ident, produced_by=Provenance.MODEL)


def model_note(ident: str = "note-1") -> TypedRef:
    """A model-authored assertion: a knowledge ref is not a dispatcher receipt."""

    return ref(TypedRefKind.KNOWLEDGE, ident)


def criterion(
    criterion_id: str,
    *,
    requirement_class: RequirementClass = RequirementClass.REQUIRED_OUTCOME,
    evaluation_kind: EvaluationKind = EvaluationKind.SEMANTIC,
    checks: tuple[str, ...] = (),
    independence_required: bool = False,
) -> Criterion:
    return Criterion(
        criterion_id=criterion_id,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"{criterion_id} holds",
        requirement_class=requirement_class,
        evaluation_kind=evaluation_kind,
        required_evidence_policy=RequiredEvidencePolicy(
            required_check_ids=checks, independence_required=independence_required
        ),
    )


def outcome(
    criterion_id: str,
    verdict: CriterionVerdict,
    *,
    execution: CheckExecution = CheckExecution.SUCCEEDED,
    evidence: tuple[TypedRef, ...] = (),
) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_id=criterion_id,
        verdict=verdict,
        check_execution=execution,
        evidence_refs=evidence,
    )


def binding(*, requirements_revision: int = 3, mission_id: str = "mission-1") -> ReviewBinding:
    return ReviewBinding(
        mission_id=mission_id,
        obligation_id="ob-1",
        subject_ref=ref(TypedRefKind.ARTIFACT, "cand-1", HASH_B),
        requirements_revision=requirements_revision,
        input_manifest_hash=HASH_C,
        policy_ref=ref(TypedRefKind.SOURCE, "policy-1", HASH_A),
    )


def revision(
    criteria: tuple[Criterion, ...],
    expression: object,
    *,
    number: int = 3,
    mission_id: str = "mission-1",
) -> RequirementsRevision:
    return RequirementsRevision(
        revision_id=RequirementsRevisionId("req-1"),
        mission_id=mission_id,
        revision=number,
        criteria=criteria,
        success_expression=expression,
    )


def package(
    criteria: tuple[Criterion, ...],
    expression: object,
    *,
    purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT,
    review_binding: ReviewBinding | None = None,
    producer_agent_ids: tuple[str, ...] = (),
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_ONLY,
    requirements_content_hash: str | None = None,
) -> ReviewPackage:
    return ReviewPackage(
        package_id=ReviewPackageId("pkg-1"),
        purpose=purpose,
        binding=review_binding if review_binding is not None else binding(),
        criteria=criteria,
        success_expression=expression,
        candidate_refs=(ref(TypedRefKind.ARTIFACT, "cand-1", HASH_B),),
        producer_agent_ids=producer_agent_ids,
        reviewer_workspace_access=workspace_access,
        requirements_content_hash=requirements_content_hash,
    )


def record(
    outcomes: tuple[CriterionOutcome, ...],
    *,
    verdict: ReviewVerdict = ReviewVerdict.ACCEPT,
    purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT,
    reviewer: str = "agent-reviewer",
    review_binding: ReviewBinding | None = None,
    package_id: str = "pkg-1",
) -> ReviewRecord:
    return ReviewRecord(
        record_id=ReviewRecordId("rec-1"),
        package_id=ReviewPackageId(package_id),
        purpose=purpose,
        binding=review_binding if review_binding is not None else binding(),
        reviewer_agent_id=reviewer,
        reviewer_turn_id="turn-1",
        evidence_manifest_hash=HASH_A,
        criteria=outcomes,
        verdict=verdict,
    )


def witness(
    *,
    purpose: WitnessPurpose = WitnessPurpose.ACCEPT,
    truth: TruthValue = TruthValue.TRUE,
    freshness: Validity = Validity.CURRENT,
    availability: Availability = Availability.READABLE,
    decision: WitnessDecision = WitnessDecision.USABLE,
    scope_epoch: int = EPOCH,
    not_after_ms: int | None = 5_000,
) -> ValidityWitness:
    return ValidityWitness(
        witness_id="wit-1",
        consumer_ref=ref(TypedRefKind.TASK, "task-1"),
        purpose=purpose,
        truth=truth,
        freshness=freshness,
        availability=availability,
        decision=decision,
        scope_id="scope-1",
        scope_epoch=scope_epoch,
        support_revision=2,
        as_of_ms=100,
        not_after_ms=not_after_ms,
    )


def child(slot: str, occurrence: str, *, required: bool = True) -> ChildBinding:
    return ChildBinding(
        instance_id=MethodInstanceId("mi-1"),
        slot_key=slot,
        occurrence_id=OccurrenceId(occurrence),
        obligation_id=ObligationId(f"ob-{slot}"),
        requiredness=Requiredness.REQUIRED if required else Requiredness.OPTIONAL_AUTHORIZED,
    )


# A "windows AND linux" world reused by several acceptance scenarios (AER-V01).
WINDOWS = criterion("windows", checks=("win-suite",), evaluation_kind=EvaluationKind.DETERMINISTIC)
LINUX = criterion("linux", checks=("linux-suite",), evaluation_kind=EvaluationKind.DETERMINISTIC)
PRIVACY = criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT)
BOTH = AllExpr(children=(CriterionExpr("windows"), CriterionExpr("linux")))


def passing_outcome(criterion_id: str) -> CriterionOutcome:
    return outcome(criterion_id, PASS, evidence=(tool_receipt(f"{criterion_id}-receipt"),))


def happy_subject(**overrides: object) -> AcceptanceSubject:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("privacy-receipt"),)),
    )
    base: dict[str, object] = {
        "revision": revision(criteria, expression),
        "package": package(criteria, expression),
        "record": record(outcomes),
        "independence": IndependenceFacts(producer_agent_ids=("agent-worker",)),
        "posture": ExecutionPosture(),
        "semantic_review_required": True,
        "compound": None,
    }
    base.update(overrides)
    return AcceptanceSubject(**base)  # type: ignore[arg-type]


def decide(subject: AcceptanceSubject, **overrides: object):
    kwargs: dict[str, object] = {
        "now_ms": NOW_MS,
        "purpose": ReviewPurpose.TASK_CONTENT,
        "witness": witness(),
        "current_scope_epoch": EPOCH,
    }
    kwargs.update(overrides)
    return acceptable(subject, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# AER §4.3 — the three-valued success expression
# --------------------------------------------------------------------------------------

ALL_TABLE = {
    (PASS, PASS): PASS,
    (PASS, FAIL): FAIL,
    (PASS, UNKNOWN): UNKNOWN,
    (FAIL, PASS): FAIL,
    (FAIL, FAIL): FAIL,
    (FAIL, UNKNOWN): FAIL,
    (UNKNOWN, PASS): UNKNOWN,
    (UNKNOWN, FAIL): FAIL,
    (UNKNOWN, UNKNOWN): UNKNOWN,
}

ANY_TABLE = {
    (PASS, PASS): PASS,
    (PASS, FAIL): PASS,
    (PASS, UNKNOWN): PASS,
    (FAIL, PASS): PASS,
    (FAIL, FAIL): FAIL,
    (FAIL, UNKNOWN): UNKNOWN,
    (UNKNOWN, PASS): PASS,
    (UNKNOWN, FAIL): UNKNOWN,
    (UNKNOWN, UNKNOWN): UNKNOWN,
}


def two_outcomes(left: CriterionVerdict, right: CriterionVerdict):
    return outcomes_by_id(
        (outcome("left", left), outcome("right", right)),
    )


@pytest.mark.parametrize(("pair", "expected"), sorted(ALL_TABLE.items(), key=repr))
def test_all_truth_table(pair: tuple[CriterionVerdict, CriterionVerdict], expected) -> None:
    expression = AllExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    assert evaluate_success_expression(expression, two_outcomes(*pair)).verdict is expected


@pytest.mark.parametrize(("pair", "expected"), sorted(ANY_TABLE.items(), key=repr))
def test_any_truth_table(pair: tuple[CriterionVerdict, CriterionVerdict], expected) -> None:
    expression = AnyExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    assert evaluate_success_expression(expression, two_outcomes(*pair)).verdict is expected


def test_single_criterion_passes_through() -> None:
    result = evaluate_success_expression(
        CriterionExpr("left"), outcomes_by_id((outcome("left", PASS),))
    )
    assert result.verdict is PASS
    assert result.witness_path == ("left",)
    assert result.unevaluated_ids == ()


def test_nested_all_of_any_keeps_the_hard_side() -> None:
    expression = AllExpr(
        children=(
            AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample"))),
            CriterionExpr("privacy"),
        )
    )
    outcomes = outcomes_by_id(
        (
            outcome("proof", FAIL),
            outcome("counterexample", PASS),
            outcome("privacy", FAIL),
        )
    )
    result = evaluate_success_expression(expression, outcomes)
    assert result.verdict is FAIL
    assert result.witness_path == ("privacy",)


def test_nested_any_of_all_can_still_pass() -> None:
    expression = AnyExpr(
        children=(
            AllExpr(children=(CriterionExpr("a"), CriterionExpr("b"))),
            CriterionExpr("c"),
        )
    )
    outcomes = outcomes_by_id(
        (outcome("a", FAIL), outcome("b", PASS), outcome("c", PASS)),
    )
    result = evaluate_success_expression(expression, outcomes)
    assert result.verdict is PASS
    assert result.witness_path == ("c",)


def test_empty_all_is_rejected_by_the_contract() -> None:
    with pytest.raises(ContractError):
        AllExpr(children=())


def test_empty_any_is_rejected_by_the_contract() -> None:
    with pytest.raises(ContractError):
        AnyExpr(children=())


def test_missing_criterion_is_unknown_not_a_default_pass() -> None:
    expression = AllExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    result = evaluate_success_expression(expression, outcomes_by_id((outcome("left", PASS),)))
    assert result.verdict is UNKNOWN
    assert result.unevaluated_ids == ("right",)


def test_missing_criterion_does_not_hide_a_real_failure() -> None:
    expression = AllExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    result = evaluate_success_expression(expression, outcomes_by_id((outcome("left", FAIL),)))
    assert result.verdict is FAIL
    assert result.unevaluated_ids == ("right",)


@pytest.mark.parametrize(
    "execution",
    [
        CheckExecution.ERROR,
        CheckExecution.NOT_RUN,
        CheckExecution.CANCELLED,
        CheckExecution.RUNNING,
    ],
)
def test_execution_error_projects_to_unknown_not_fail(execution: CheckExecution) -> None:
    """AER-V08 / §4.3: infrastructure trouble is not evidence against the content."""

    assert project_verdict(outcome("left", FAIL, execution=execution)) is UNKNOWN


def test_succeeded_execution_keeps_the_reported_verdict() -> None:
    assert project_verdict(outcome("left", FAIL)) is FAIL
    assert project_verdict(outcome("left", PASS)) is PASS
    assert project_verdict(outcome("left", UNKNOWN)) is UNKNOWN


def test_running_check_reported_as_pass_is_still_unknown() -> None:
    running = outcome("left", PASS, execution=CheckExecution.RUNNING)
    expression = CriterionExpr("left")
    assert evaluate_success_expression(expression, outcomes_by_id((running,))).verdict is UNKNOWN


def test_contract_refuses_a_pass_that_never_ran() -> None:
    """Invariant I07, enforced upstream so this module never sees such an outcome."""

    with pytest.raises(ContractError):
        outcome("left", PASS, execution=CheckExecution.NOT_RUN)


def test_witness_path_names_the_failing_conjunct() -> None:
    expression = AllExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    result = evaluate_success_expression(expression, two_outcomes(PASS, FAIL))
    assert result.witness_path == ("right",)


def test_witness_path_names_the_passing_branch() -> None:
    expression = AnyExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    result = evaluate_success_expression(expression, two_outcomes(FAIL, PASS))
    assert result.witness_path == ("right",)


def test_unknown_all_witness_path_lists_only_the_undecided() -> None:
    expression = AllExpr(children=(CriterionExpr("left"), CriterionExpr("right")))
    result = evaluate_success_expression(expression, two_outcomes(PASS, UNKNOWN))
    assert result.verdict is UNKNOWN
    assert result.witness_path == ("right",)


# --------------------------------------------------------------------------------------
# AER §4.1 / AER-V02 — the hard gate is independent of the result OR
# --------------------------------------------------------------------------------------


def test_hard_gate_is_not_cancelled_by_a_passing_result_or() -> None:
    """AER-V02: the approved OR proved the result; privacy still failed."""

    criteria = (
        criterion("proof"),
        criterion("counterexample"),
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
    )
    expression = AllExpr(
        children=(
            AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample"))),
            CriterionExpr("privacy"),
        )
    )
    outcomes = outcomes_by_id(
        (
            outcome("proof", FAIL),
            outcome("counterexample", PASS),
            outcome("privacy", FAIL),
        )
    )
    gate = hard_gate(revision(criteria, expression), outcomes)
    assert not gate.passed
    assert gate.failed_ids == ("privacy",)
    assert (
        evaluate_success_expression(
            AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample"))), outcomes
        ).verdict
        is PASS
    )


def test_hard_gate_separates_fail_from_unknown() -> None:
    criteria = (
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("licence", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("feature"),
    )
    expression = AllExpr(
        children=(
            CriterionExpr("privacy"),
            CriterionExpr("licence"),
            CriterionExpr("feature"),
        )
    )
    outcomes = outcomes_by_id(
        (
            outcome("privacy", FAIL),
            outcome("licence", UNKNOWN),
            outcome("feature", PASS),
        )
    )
    gate = hard_gate(revision(criteria, expression), outcomes)
    assert gate.failed_ids == ("privacy",)
    assert gate.unknown_ids == ("licence",)
    assert gate.missing_ids == ()
    assert not gate.passed


def test_hard_gate_reports_a_missing_hard_outcome() -> None:
    criteria = (
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("feature"),
    )
    expression = AllExpr(children=(CriterionExpr("privacy"), CriterionExpr("feature")))
    gate = hard_gate(revision(criteria, expression), outcomes_by_id((outcome("feature", PASS),)))
    assert gate.missing_ids == ("privacy",)
    assert not gate.passed


def test_hard_gate_passes_when_every_hard_constraint_passes() -> None:
    criteria = (
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("feature"),
    )
    expression = AllExpr(children=(CriterionExpr("privacy"), CriterionExpr("feature")))
    gate = hard_gate(
        revision(criteria, expression),
        outcomes_by_id((outcome("privacy", PASS), outcome("feature", FAIL))),
    )
    assert gate.passed


def test_hard_gate_ignores_errored_execution_as_a_pass() -> None:
    criteria = (criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),)
    expression = CriterionExpr("privacy")
    gate = hard_gate(
        revision(criteria, expression),
        outcomes_by_id((outcome("privacy", FAIL, execution=CheckExecution.ERROR),)),
    )
    assert gate.unknown_ids == ("privacy",)
    assert not gate.passed


def test_contract_refuses_a_hard_constraint_inside_an_or() -> None:
    criteria = (
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
        criterion("feature"),
    )
    with pytest.raises(ContractError):
        revision(
            criteria,
            AnyExpr(children=(CriterionExpr("privacy"), CriterionExpr("feature"))),
        )


# --------------------------------------------------------------------------------------
# AER §5.4 / AER-V04 — required checks and the one-to-one catalogue match
# --------------------------------------------------------------------------------------


def test_unknown_criterion_id_is_rejected() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            passing_outcome("windows"),
            passing_outcome("linux"),
            outcome("invented", PASS, evidence=(tool_receipt("x"),)),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.match.unknown == ("invented",)
    assert not result.complete


def test_duplicate_criterion_id_is_rejected_by_the_contract() -> None:
    with pytest.raises(ContractError):
        record((passing_outcome("windows"), passing_outcome("windows")))


def test_missing_required_criterion_id_is_rejected() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    result = required_checks_complete(pkg, record((passing_outcome("linux"),)))
    assert result.match.missing_required == ("windows",)
    assert not result.complete


def test_explicitly_declined_or_branch_may_stay_unevaluated() -> None:
    criteria = (criterion("proof", checks=("prover",)), criterion("counterexample"))
    expression = AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample")))
    pkg = package(criteria, expression)
    rec = record((outcome("counterexample", PASS, evidence=(tool_receipt("repro"),)),))
    result = required_checks_complete(pkg, rec)
    assert result.match.matched
    assert result.complete
    assert result.declined_ids == ("proof",)


def test_check_that_did_not_run_is_not_a_pass() -> None:
    """AER-V04: the test tool was unavailable and the model still said PASS."""

    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            outcome("windows", UNKNOWN, execution=CheckExecution.NOT_RUN),
            passing_outcome("linux"),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.not_executed_ids == ("windows",)
    assert result.not_passed_ids == ("windows",)
    assert not result.complete


def test_errored_check_is_not_a_pass() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            outcome("windows", FAIL, execution=CheckExecution.ERROR),
            passing_outcome("linux"),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.not_executed_ids == ("windows",)
    assert not result.complete


def test_model_authored_evidence_is_not_a_dispatcher_receipt() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            outcome("windows", PASS, evidence=(model_note("i-ran-it"),)),
            passing_outcome("linux"),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.unsourced_ids == ("windows",)
    assert not result.complete


def test_gated_check_without_any_evidence_is_unsourced() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record((outcome("windows", PASS), passing_outcome("linux")))
    result = required_checks_complete(pkg, rec)
    assert result.unsourced_ids == ("windows",)


def test_execution_receipt_criterion_needs_a_receipt_even_without_named_checks() -> None:
    receipted = criterion("sent", evaluation_kind=EvaluationKind.EXECUTION_RECEIPT)
    pkg = package((receipted,), CriterionExpr("sent"))
    rec = record((outcome("sent", PASS, evidence=(model_note("claims-sent"),)),))
    assert required_checks_complete(pkg, rec).unsourced_ids == ("sent",)


def test_operation_receipt_counts_as_a_dispatcher_receipt() -> None:
    receipted = criterion("sent", evaluation_kind=EvaluationKind.EXECUTION_RECEIPT)
    pkg = package((receipted,), CriterionExpr("sent"))
    rec = record((outcome("sent", PASS, evidence=(ref(TypedRefKind.OPERATION, "op-1"),)),))
    assert required_checks_complete(pkg, rec).complete


def test_semantic_criterion_without_named_checks_needs_no_receipt() -> None:
    plain = criterion("reads_well")
    pkg = package((plain,), CriterionExpr("reads_well"))
    assert required_checks_complete(pkg, record((outcome("reads_well", PASS),))).complete


def test_completeness_surfaces_the_independence_requirement() -> None:
    gated = criterion("audited", checks=("audit",), independence_required=True)
    pkg = package((gated,), CriterionExpr("audited"))
    rec = record((outcome("audited", PASS, evidence=(tool_receipt("audit-1"),)),))
    assert required_checks_complete(pkg, rec).independence_required_ids == ("audited",)


# --------------------------------------------------------------------------------------
# AER §5.3 / AER-V05 — independence
# --------------------------------------------------------------------------------------


def test_self_review_is_not_independent() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),), reviewer="agent-worker")
    result = independence_ok(
        pkg, rec, facts=IndependenceFacts(producer_agent_ids=("agent-worker",))
    )
    assert not result.independent
    assert IndependenceReason.SELF_REVIEW in result.reasons


def test_write_access_to_the_candidate_breaks_independence() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),))
    result = independence_ok(
        pkg,
        rec,
        facts=IndependenceFacts(
            producer_agent_ids=("agent-worker",), reviewer_can_write_candidate=True
        ),
    )
    assert not result.independent
    assert IndependenceReason.WRITE_ACCESS_TO_CANDIDATE in result.reasons


def test_reviewing_a_version_the_reviewer_edited_is_not_independent() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),))
    result = independence_ok(
        pkg,
        rec,
        facts=IndependenceFacts(
            producer_agent_ids=("agent-worker",), reviewed_revision_authored_by_reviewer=True
        ),
    )
    assert not result.independent
    assert IndependenceReason.REVIEWED_OWN_EDIT in result.reasons


def test_a_different_model_is_not_by_itself_independent_evidence() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),), reviewer="agent-worker")
    result = independence_ok(
        pkg,
        rec,
        facts=IndependenceFacts(
            producer_agent_ids=("agent-worker",),
            reviewer_model_id="model-b",
            producer_model_id="model-a",
        ),
    )
    assert not result.independent
    assert IndependenceReason.SELF_REVIEW in result.reasons
    assert IndependenceReason.MODEL_DIVERSITY_IS_NOT_INDEPENDENCE in result.reasons


def test_independent_reviewer_passes() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),))
    result = independence_ok(
        pkg, rec, facts=IndependenceFacts(producer_agent_ids=("agent-worker",))
    )
    assert result.independent
    assert result.reasons == ()


def test_independence_reports_every_broken_requirement_at_once() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"))
    rec = record((passing_outcome("windows"),), reviewer="agent-worker")
    result = independence_ok(
        pkg,
        rec,
        facts=IndependenceFacts(
            producer_agent_ids=("agent-worker",),
            reviewer_can_write_candidate=True,
            reviewed_revision_authored_by_reviewer=True,
        ),
    )
    assert set(result.reasons) == {
        IndependenceReason.SELF_REVIEW,
        IndependenceReason.WRITE_ACCESS_TO_CANDIDATE,
        IndependenceReason.REVIEWED_OWN_EDIT,
        # The package says READ_ONLY (WRITE is refused at construction), so a
        # caller claiming the reviewer could edit the candidate also disagrees
        # with the frozen anchor.
        IndependenceReason.FACTS_CONTRADICT_PACKAGE,
    }


# --------------------------------------------------------------------------------------
# AER §6.2 — the formal acceptance formula
# --------------------------------------------------------------------------------------


def test_happy_path_is_acceptable() -> None:
    decision = decide(happy_subject())
    assert decision.acceptable
    assert decision.reasons == ()


def test_missing_root_requirement_is_not_acceptable() -> None:
    """AER-V01: the user asked for Windows AND Linux; only Linux was reviewed."""

    criteria = (WINDOWS, LINUX)
    subject = happy_subject(
        revision=revision(criteria, BOTH),
        package=package((LINUX,), CriterionExpr("linux")),
        record=record((passing_outcome("linux"),)),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.ROOT_CRITERION_MISSING in decision.reasons
    assert decision.expression.unevaluated_ids == ("windows",)


def test_hard_gate_failure_beats_a_passing_result_or() -> None:
    """AER-V02, end to end through the acceptance formula."""

    criteria = (
        criterion("proof"),
        criterion("counterexample"),
        criterion("privacy", requirement_class=RequirementClass.HARD_CONSTRAINT),
    )
    expression = AllExpr(
        children=(
            AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample"))),
            CriterionExpr("privacy"),
        )
    )
    outcomes = (
        outcome("proof", FAIL),
        outcome("counterexample", PASS),
        outcome("privacy", FAIL),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.HARD_CONSTRAINT_FAILED in decision.reasons


def test_unknown_success_expression_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        outcome("windows", UNKNOWN, execution=CheckExecution.ERROR),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.SUCCESS_EXPRESSION_NOT_PASS in decision.reasons
    assert AcceptReason.REQUIRED_CHECKS_INCOMPLETE in decision.reasons


def test_unsourced_required_check_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        outcome("windows", PASS, evidence=(model_note("trust-me"),)),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.REQUIRED_CHECKS_INCOMPLETE in decision.reasons


def test_self_reviewed_candidate_is_not_acceptable() -> None:
    subject = happy_subject(independence=IndependenceFacts(producer_agent_ids=("agent-reviewer",)))
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.INDEPENDENT_REVIEW_MISSING in decision.reasons


def test_expired_witness_is_not_acceptable() -> None:
    decision = decide(happy_subject(), witness=witness(not_after_ms=100))
    assert not decision.acceptable
    assert AcceptReason.WITNESS_STALE in decision.reasons


def test_witness_expiry_boundary_blocks() -> None:
    """The AER reference requires ``now < not_after``; equality is already stale."""

    decision = decide(happy_subject(), witness=witness(not_after_ms=NOW_MS))
    assert not decision.acceptable
    assert AcceptReason.WITNESS_STALE in decision.reasons


def test_witness_for_another_purpose_is_not_acceptable() -> None:
    decision = decide(happy_subject(), witness=witness(purpose=WitnessPurpose.PLAN))
    assert not decision.acceptable
    assert AcceptReason.WITNESS_PURPOSE_MISMATCH in decision.reasons


def test_witness_from_a_previous_scope_epoch_is_not_acceptable() -> None:
    decision = decide(happy_subject(), witness=witness(scope_epoch=EPOCH - 1))
    assert not decision.acceptable
    assert AcceptReason.WITNESS_STALE in decision.reasons


def test_witness_that_is_not_usable_is_not_acceptable() -> None:
    unusable = witness(truth=TruthValue.UNKNOWN, decision=WitnessDecision.NEEDS_REVIEW)
    decision = decide(happy_subject(), witness=unusable)
    assert not decision.acceptable
    assert AcceptReason.WITNESS_NOT_USABLE in decision.reasons


def test_redacted_witness_is_not_acceptable() -> None:
    hidden = witness(availability=Availability.REDACTED, decision=WitnessDecision.BLOCKED)
    decision = decide(happy_subject(), witness=hidden)
    assert not decision.acceptable
    assert AcceptReason.WITNESS_NOT_USABLE in decision.reasons


@pytest.mark.parametrize(
    "verdict", [ReviewVerdict.REWORK, ReviewVerdict.INCONCLUSIVE, ReviewVerdict.REJECTED]
)
def test_a_non_accept_review_verdict_is_not_acceptable(verdict: ReviewVerdict) -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    subject = happy_subject(record=record(outcomes, verdict=verdict))
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.REVIEW_VERDICT_NOT_ACCEPT in decision.reasons
    assert isinstance(expression, AllExpr) and len(criteria) == 3


def test_record_bound_to_another_package_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes, package_id="pkg-other"),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.IDENTITY_MISMATCH in decision.reasons


def test_record_bound_to_another_input_manifest_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    other = ReviewBinding(
        mission_id="mission-1",
        obligation_id="ob-1",
        subject_ref=ref(TypedRefKind.ARTIFACT, "cand-2", HASH_A),
        requirements_revision=3,
        input_manifest_hash=HASH_A,
        policy_ref=ref(TypedRefKind.SOURCE, "policy-1", HASH_A),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes, review_binding=other),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.IDENTITY_MISMATCH in decision.reasons


def test_package_bound_to_another_requirements_revision_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    stale = binding(requirements_revision=2)
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression, review_binding=stale),
        record=record(outcomes, review_binding=stale),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.REQUIREMENTS_REVISION_MISMATCH in decision.reasons


def test_review_for_another_purpose_is_not_acceptable() -> None:
    decision = decide(happy_subject(), purpose=ReviewPurpose.MISSION_FINAL)
    assert not decision.acceptable
    assert AcceptReason.PURPOSE_MISMATCH in decision.reasons


def test_unowned_critical_operation_blocks_acceptance() -> None:
    posture = ExecutionPosture(unowned_critical_operation_ids=("op-send-1",))
    decision = decide(happy_subject(posture=posture))
    assert not decision.acceptable
    assert AcceptReason.CRITICAL_OPERATION_UNOWNED in decision.reasons


def test_pending_cancellation_blocks_acceptance() -> None:
    decision = decide(happy_subject(posture=ExecutionPosture(cancellation_requested=True)))
    assert not decision.acceptable
    assert AcceptReason.CANCELLATION_PENDING in decision.reasons


def test_stale_method_adoption_blocks_acceptance() -> None:
    decision = decide(happy_subject(posture=ExecutionPosture(method_adoption_current=False)))
    assert not decision.acceptable
    assert AcceptReason.METHOD_ADOPTION_STALE in decision.reasons


def test_compound_happy_path_is_acceptable() -> None:
    compound = CompoundFacts(
        selected_method_legal=True,
        child_bindings=(child("report", "occ-1"), child("audit", "occ-2", required=False)),
        contributing_occurrence_ids=("occ-1",),
        composition_obligation_passed=True,
    )
    decision = decide(happy_subject(compound=compound))
    assert decision.acceptable


def test_compound_missing_required_occurrence_is_not_acceptable() -> None:
    compound = CompoundFacts(
        selected_method_legal=True,
        child_bindings=(child("report", "occ-1"), child("send", "occ-2")),
        contributing_occurrence_ids=("occ-1",),
        composition_obligation_passed=True,
    )
    decision = decide(happy_subject(compound=compound))
    assert not decision.acceptable
    assert AcceptReason.REQUIRED_OCCURRENCE_MISSING in decision.reasons
    assert decision.missing_occurrence_ids == ("occ-2",)


def test_compound_with_an_illegal_method_is_not_acceptable() -> None:
    compound = CompoundFacts(
        selected_method_legal=False,
        child_bindings=(child("report", "occ-1"),),
        contributing_occurrence_ids=("occ-1",),
        composition_obligation_passed=True,
    )
    decision = decide(happy_subject(compound=compound))
    assert not decision.acceptable
    assert AcceptReason.COMPOUND_METHOD_ILLEGAL in decision.reasons


def test_compound_failing_composition_obligation_is_not_acceptable() -> None:
    compound = CompoundFacts(
        selected_method_legal=True,
        child_bindings=(child("report", "occ-1"),),
        contributing_occurrence_ids=("occ-1",),
        composition_obligation_passed=False,
    )
    decision = decide(happy_subject(compound=compound))
    assert not decision.acceptable
    assert AcceptReason.COMPOSITION_OBLIGATION_FAILED in decision.reasons


def test_every_failing_conjunct_gets_its_own_reason_code() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        outcome("windows", UNKNOWN, execution=CheckExecution.ERROR),
        outcome("linux", FAIL, evidence=(tool_receipt("l"),)),
        outcome("privacy", FAIL),
    )
    subject = happy_subject(
        revision=revision(criteria, expression),
        package=package(criteria, expression),
        record=record(outcomes, verdict=ReviewVerdict.REWORK),
        independence=IndependenceFacts(producer_agent_ids=("agent-reviewer",)),
        posture=ExecutionPosture(cancellation_requested=True),
    )
    decision = decide(subject, witness=witness(not_after_ms=1))
    assert not decision.acceptable
    assert set(decision.reasons) >= {
        AcceptReason.HARD_CONSTRAINT_FAILED,
        AcceptReason.SUCCESS_EXPRESSION_NOT_PASS,
        AcceptReason.REQUIRED_CHECKS_INCOMPLETE,
        AcceptReason.INDEPENDENT_REVIEW_MISSING,
        AcceptReason.REVIEW_VERDICT_NOT_ACCEPT,
        AcceptReason.WITNESS_STALE,
        AcceptReason.CANCELLATION_PENDING,
    }
    assert len(set(decision.reasons)) == len(decision.reasons)


def test_acceptance_subject_demands_the_independence_and_posture_facts() -> None:
    """Neither may default: an unstated fact must not read as an all-clear."""

    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    with pytest.raises(TypeError):
        AcceptanceSubject(  # type: ignore[call-arg]
            revision=revision(criteria, expression),
            package=package(criteria, expression),
            record=record(outcomes),
        )
    with pytest.raises(TypeError):
        AcceptanceSubject(  # type: ignore[call-arg]
            revision=revision(criteria, expression),
            package=package(criteria, expression),
            record=record(outcomes),
            independence=IndependenceFacts(producer_agent_ids=("agent-worker",)),
        )


def test_stated_all_clear_facts_are_still_accepted() -> None:
    """Stating "nothing in flight, produced by someone else" is legitimate."""

    decision = decide(
        happy_subject(
            independence=IndependenceFacts(producer_agent_ids=("agent-worker",)),
            posture=ExecutionPosture(),
        )
    )
    assert decision.acceptable


def test_package_expression_must_match_the_requirements_revision() -> None:
    """A tampered package cannot turn a hard constraint into a declined OR branch."""

    criteria = (WINDOWS, LINUX, PRIVACY)
    stated = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    tampered = AnyExpr(children=(BOTH, CriterionExpr("privacy")))
    outcomes = (passing_outcome("windows"), passing_outcome("linux"))
    subject = happy_subject(
        revision=revision(criteria, stated),
        package=package(criteria, tampered),
        record=record(outcomes),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.SUCCESS_EXPRESSION_MISMATCH in decision.reasons
    assert AcceptReason.HARD_CONSTRAINT_STRUCTURE in decision.reasons
    assert decision.hard_constraint_structure_ids == ("privacy",)
    # Without these checks the hole would have looked clean: the tampered package
    # reports the absent hard constraint as a legitimately declined branch.
    assert decision.completeness.declined_ids == ("privacy",)
    assert decision.completeness.complete


def test_a_reshaped_but_equivalent_package_expression_is_still_a_mismatch() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    stated = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    reshaped = AllExpr(
        children=(CriterionExpr("windows"), CriterionExpr("linux"), CriterionExpr("privacy"))
    )
    outcomes = (
        passing_outcome("windows"),
        passing_outcome("linux"),
        outcome("privacy", PASS, evidence=(tool_receipt("p"),)),
    )
    subject = happy_subject(
        revision=revision(criteria, stated),
        package=package(criteria, reshaped),
        record=record(outcomes),
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert decision.reasons == (AcceptReason.SUCCESS_EXPRESSION_MISMATCH,)


def test_success_expression_digest_separates_the_operators() -> None:
    children = (CriterionExpr("windows"), CriterionExpr("linux"))
    assert success_expression_digest(AllExpr(children=children)) != success_expression_digest(
        AnyExpr(children=children)
    )
    assert success_expression_digest(AllExpr(children=children)) == success_expression_digest(
        AllExpr(children=children)
    )


def test_an_unparsed_expression_is_refused_not_walked() -> None:
    """Raw JSON is not a success AST; the rule refuses it instead of crashing."""

    raw = {"op": "all", "children": [{"op": "criterion", "criterion_id": "windows"}]}
    with pytest.raises(ContractError):
        evaluate_success_expression(raw, {})  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        success_expression_digest(raw)  # type: ignore[arg-type]


def test_an_illegal_nested_node_is_refused() -> None:
    expression = AllExpr(children=(CriterionExpr("windows"), "linux"))
    with pytest.raises(ContractError):
        evaluate_success_expression(expression, {})


# --------------------------------------------------------------------------------------
# Contract round 3 — helper reuse, package-declared facts, evidence provenance
# --------------------------------------------------------------------------------------


def test_declined_branches_come_from_the_contract_helper() -> None:
    """The private duplicate is gone; the package's own helper is the authority."""

    assert not hasattr(acceptance_rules, "_criteria_only_under_any")
    criteria = (criterion("proof", checks=("prover",)), criterion("counterexample"))
    expression = AnyExpr(children=(CriterionExpr("proof"), CriterionExpr("counterexample")))
    pkg = package(criteria, expression)
    rec = record((outcome("counterexample", PASS, evidence=(attributed_receipt("repro"),)),))
    result = required_checks_complete(pkg, rec)
    # ``declined_ids`` is exactly the package's declinable set minus what the
    # record actually reported — no second opinion about which branches are OR.
    declinable = pkg.criteria_only_under_any()
    assert declinable == frozenset({"proof", "counterexample"})
    reported = {item.criterion_id for item in rec.criteria}
    assert result.declined_ids == tuple(sorted(declinable - reported))
    assert result.declined_ids == ("proof",)


def test_package_refuses_a_reviewer_with_write_access() -> None:
    """AER §5.3, now enforced at construction rather than only at review time."""

    with pytest.raises(ContractError):
        package((WINDOWS,), CriterionExpr("windows"), workspace_access=WorkspaceAccess.WRITE)


def test_package_declared_producers_catch_a_self_review_the_facts_omitted() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"), producer_agent_ids=("agent-reviewer",))
    rec = record((passing_outcome("windows"),), reviewer="agent-reviewer")
    result = independence_ok(
        pkg, rec, facts=IndependenceFacts(producer_agent_ids=("agent-someone-else",))
    )
    assert not result.independent
    assert IndependenceReason.SELF_REVIEW in result.reasons
    assert IndependenceReason.FACTS_CONTRADICT_PACKAGE in result.reasons


def test_facts_that_disagree_with_the_package_producers_are_a_finding() -> None:
    pkg = package((WINDOWS,), CriterionExpr("windows"), producer_agent_ids=("agent-worker",))
    rec = record((passing_outcome("windows"),))
    result = independence_ok(pkg, rec, facts=IndependenceFacts(producer_agent_ids=("agent-other",)))
    assert not result.independent
    assert result.reasons == (IndependenceReason.FACTS_CONTRADICT_PACKAGE,)


def test_facts_claiming_write_access_contradict_a_read_only_package() -> None:
    pkg = package(
        (WINDOWS,),
        CriterionExpr("windows"),
        producer_agent_ids=("agent-worker",),
        workspace_access=WorkspaceAccess.READ_ONLY,
    )
    rec = record((passing_outcome("windows"),))
    result = independence_ok(
        pkg,
        rec,
        facts=IndependenceFacts(
            producer_agent_ids=("agent-worker",), reviewer_can_write_candidate=True
        ),
    )
    assert set(result.reasons) == {
        IndependenceReason.WRITE_ACCESS_TO_CANDIDATE,
        IndependenceReason.FACTS_CONTRADICT_PACKAGE,
    }


def test_facts_agreeing_with_the_package_are_independent() -> None:
    pkg = package(
        (WINDOWS,),
        CriterionExpr("windows"),
        producer_agent_ids=("agent-worker",),
        workspace_access=WorkspaceAccess.NONE,
    )
    rec = record((passing_outcome("windows"),))
    result = independence_ok(
        pkg, rec, facts=IndependenceFacts(producer_agent_ids=("agent-worker",))
    )
    assert result.independent


def declared_subject(**overrides: object) -> AcceptanceSubject:
    """A happy subject whose package declares the requirements hash it mirrors."""

    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    rev = revision(criteria, expression)
    outcomes = (
        outcome("windows", PASS, evidence=(attributed_receipt("w"),)),
        outcome("linux", PASS, evidence=(attributed_receipt("l"),)),
        outcome("privacy", PASS, evidence=(attributed_receipt("p"),)),
    )
    base: dict[str, object] = {
        "revision": rev,
        "package": package(criteria, expression, requirements_content_hash=rev.content_hash()),
        "record": record(outcomes),
        "independence": IndependenceFacts(producer_agent_ids=("agent-worker",)),
        "posture": ExecutionPosture(),
    }
    base.update(overrides)
    return AcceptanceSubject(**base)  # type: ignore[arg-type]


def test_a_declared_requirements_hash_that_matches_is_acceptable() -> None:
    decision = decide(declared_subject())
    assert decision.acceptable


def test_a_declared_requirements_hash_that_differs_is_not_acceptable() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    expression = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    subject = declared_subject(
        package=package(criteria, expression, requirements_content_hash=HASH_A)
    )
    decision = decide(subject)
    assert not decision.acceptable
    assert AcceptReason.REQUIREMENTS_CONTENT_MISMATCH in decision.reasons
    assert AcceptReason.SUCCESS_EXPRESSION_MISMATCH not in decision.reasons


def test_a_declared_package_may_not_reshape_a_hard_constraint_into_an_or() -> None:
    """With the hash declared the contract itself refuses the tampered shape."""

    criteria = (WINDOWS, LINUX, PRIVACY)
    tampered = AnyExpr(children=(BOTH, CriterionExpr("privacy")))
    with pytest.raises(ContractError):
        package(criteria, tampered, requirements_content_hash=HASH_A)


def test_hard_constraint_structure_is_reported_for_an_undeclared_package() -> None:
    criteria = (WINDOWS, LINUX, PRIVACY)
    stated = AllExpr(children=(BOTH, CriterionExpr("privacy")))
    tampered = AnyExpr(children=(BOTH, CriterionExpr("privacy")))
    subject = happy_subject(
        revision=revision(criteria, stated),
        package=package(criteria, tampered),
        record=record((passing_outcome("windows"), passing_outcome("linux"))),
    )
    decision = decide(subject)
    assert AcceptReason.HARD_CONSTRAINT_STRUCTURE in decision.reasons
    assert decision.hard_constraint_structure_ids == ("privacy",)


def test_receipt_source_prefers_attribution_over_kind() -> None:
    attributed = outcome(
        "windows", PASS, evidence=(ref(TypedRefKind.KNOWLEDGE, "k", produced_by=Provenance.TOOL),)
    )
    assert receipt_source(attributed) is ReceiptSource.DISPATCHER_ATTRIBUTED
    assert receipt_source(outcome("windows", PASS, evidence=(tool_receipt(),))) is (
        ReceiptSource.PROVENANCE_UNKNOWN
    )
    assert receipt_source(outcome("windows", PASS, evidence=(forged_receipt(),))) is (
        ReceiptSource.NOT_A_RECEIPT
    )
    assert receipt_source(outcome("windows", PASS)) is ReceiptSource.NOT_A_RECEIPT


def test_a_model_attributed_receipt_kind_is_still_not_a_receipt() -> None:
    """The forgery this rule exists for: ``kind`` is model-writable, attribution is not."""

    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record((outcome("windows", PASS, evidence=(forged_receipt(),)), passing_outcome("linux")))
    result = required_checks_complete(pkg, rec)
    assert result.unsourced_ids == ("windows",)
    assert not result.complete


def test_an_attributed_receipt_needs_no_fallback_flag() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            outcome("windows", PASS, evidence=(attributed_receipt("w"),)),
            outcome("linux", PASS, evidence=(attributed_receipt("l"),)),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.complete
    assert result.provenance_unknown_ids == ()


def test_an_unattributed_receipt_falls_back_to_kind_and_is_flagged() -> None:
    pkg = package((WINDOWS, LINUX), BOTH)
    rec = record(
        (
            passing_outcome("windows"),
            outcome("linux", PASS, evidence=(attributed_receipt("l"),)),
        )
    )
    result = required_checks_complete(pkg, rec)
    assert result.complete
    assert result.provenance_unknown_ids == ("windows",)


# --------------------------------------------------------------------------------------
# AER §5.4 — retry, escalation and arbitration
# --------------------------------------------------------------------------------------


def attempt(
    verdict: ReviewVerdict,
    *,
    reviewer: str = "agent-reviewer",
    subject_hash: str = HASH_B,
    infrastructure_error: bool = False,
    record_id: str = "rec-1",
) -> ReviewAttempt:
    return ReviewAttempt(
        record_id=ReviewRecordId(record_id),
        reviewer_agent_id=reviewer,
        verdict=verdict,
        subject_content_hash=subject_hash,
        infrastructure_error=infrastructure_error,
    )


def history(attempts: tuple[ReviewAttempt, ...], **overrides: object) -> ReviewHistory:
    base: dict[str, object] = {
        "attempts": attempts,
        "budget_remaining": 5,
        "attempt_limit": 4,
        "escalated_independent": False,
        "escalated_human": False,
        "candidate_changed_since_last_attempt": False,
        "policy_allows_human": True,
    }
    base.update(overrides)
    return ReviewHistory(**base)  # type: ignore[arg-type]


def test_first_attempt_is_a_plain_review() -> None:
    decision = retry_policy(history(()))
    assert decision.action is RetryAction.RETRY
    assert decision.reason is RetryReason.NO_ATTEMPT_YET
    assert decision.consumes_budget


def test_a_clean_accept_concludes_without_more_budget() -> None:
    decision = retry_policy(history((attempt(ReviewVerdict.ACCEPT),)))
    assert decision.action is RetryAction.CONCLUDE
    assert not decision.consumes_budget


def test_resampling_an_unchanged_candidate_until_pass_is_refused() -> None:
    decision = retry_policy(history((attempt(ReviewVerdict.REWORK),)))
    assert decision.action is RetryAction.ESCALATE_INDEPENDENT_REVIEWER
    assert decision.reason is RetryReason.RESAMPLING_NOT_PERMITTED


def test_a_changed_candidate_may_be_reviewed_again() -> None:
    decision = retry_policy(
        history((attempt(ReviewVerdict.REWORK),), candidate_changed_since_last_attempt=True)
    )
    assert decision.action is RetryAction.RETRY
    assert decision.reason is RetryReason.CANDIDATE_CHANGED


def test_infrastructure_error_may_be_retried_without_a_semantic_verdict() -> None:
    """AER-V08: the reviewer request failed; that is not REJECTED and not PASS."""

    decision = retry_policy(
        history((attempt(ReviewVerdict.INCONCLUSIVE, infrastructure_error=True),))
    )
    assert decision.action is RetryAction.RETRY
    assert decision.reason is RetryReason.INFRASTRUCTURE_ERROR


def test_conflicting_reviews_go_to_arbitration_not_a_vote() -> None:
    attempts = (
        attempt(ReviewVerdict.ACCEPT, reviewer="a", record_id="rec-1"),
        attempt(ReviewVerdict.REJECTED, reviewer="b", record_id="rec-2"),
    )
    decision = retry_policy(history(attempts))
    assert decision.action is RetryAction.ARBITRATE
    assert decision.reason is RetryReason.CONFLICTING_VERDICTS


def test_a_majority_does_not_override_a_conflict() -> None:
    attempts = (
        attempt(ReviewVerdict.ACCEPT, reviewer="a", record_id="rec-1"),
        attempt(ReviewVerdict.ACCEPT, reviewer="b", record_id="rec-2"),
        attempt(ReviewVerdict.ACCEPT, reviewer="c", record_id="rec-3"),
        attempt(ReviewVerdict.REWORK, reviewer="d", record_id="rec-4"),
    )
    decision = retry_policy(history(attempts))
    assert decision.action is RetryAction.ARBITRATE


def test_verdicts_on_different_candidate_versions_are_not_a_conflict() -> None:
    attempts = (
        attempt(ReviewVerdict.REWORK, reviewer="a", record_id="rec-1", subject_hash=HASH_A),
        attempt(ReviewVerdict.ACCEPT, reviewer="b", record_id="rec-2", subject_hash=HASH_B),
    )
    decision = retry_policy(history(attempts))
    assert decision.action is RetryAction.CONCLUDE


def test_exhausted_budget_stops_the_obligation() -> None:
    decision = retry_policy(history((attempt(ReviewVerdict.REWORK),), budget_remaining=0))
    assert decision.action is RetryAction.STOP
    assert decision.reason is RetryReason.BUDGET_EXHAUSTED
    assert not decision.consumes_budget


def test_escalation_ladder_reaches_a_human_then_stops() -> None:
    inconclusive = (attempt(ReviewVerdict.INCONCLUSIVE),)
    first = retry_policy(history(inconclusive))
    assert first.action is RetryAction.ESCALATE_INDEPENDENT_REVIEWER
    second = retry_policy(history(inconclusive, escalated_independent=True))
    assert second.action is RetryAction.ESCALATE_HUMAN
    third = retry_policy(history(inconclusive, escalated_independent=True, escalated_human=True))
    assert third.action is RetryAction.STOP
    assert third.reason is RetryReason.ESCALATION_EXHAUSTED


def test_a_policy_without_human_escalation_stops_instead() -> None:
    decision = retry_policy(
        history(
            (attempt(ReviewVerdict.INCONCLUSIVE),),
            escalated_independent=True,
            policy_allows_human=False,
        )
    )
    assert decision.action is RetryAction.STOP


def test_a_rejected_candidate_stops_without_resampling() -> None:
    decision = retry_policy(history((attempt(ReviewVerdict.REJECTED),)))
    assert decision.action is RetryAction.STOP
    assert decision.reason is RetryReason.CANDIDATE_REJECTED


def test_attempt_limit_escalates_even_when_the_candidate_changed() -> None:
    attempts = tuple(
        attempt(ReviewVerdict.REWORK, record_id=f"rec-{index}") for index in range(1, 5)
    )
    decision = retry_policy(
        history(attempts, attempt_limit=4, candidate_changed_since_last_attempt=True)
    )
    assert decision.action is RetryAction.ESCALATE_INDEPENDENT_REVIEWER
    assert decision.reason is RetryReason.ATTEMPT_LIMIT_REACHED


def test_every_escalating_action_draws_on_the_same_obligation_budget() -> None:
    inconclusive = (attempt(ReviewVerdict.INCONCLUSIVE),)
    for extra in ({}, {"escalated_independent": True}):
        decision = retry_policy(history(inconclusive, **extra))  # type: ignore[arg-type]
        assert decision.consumes_budget
    assert retry_policy(history((attempt(ReviewVerdict.ACCEPT),))).consumes_budget is False


# --------------------------------------------------------------------------------------
# Slice boundary
# --------------------------------------------------------------------------------------


def test_module_imports_no_store_commit_or_router() -> None:
    """P1.1b keeps zero store / commit / verifier_router imports."""

    from agent_orchestrator.verification import acceptance_rules

    source = Path(acceptance_rules.__file__).read_text(encoding="utf-8")
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) or line.lstrip().startswith(("import ", "from "))
    ]
    joined = "\n".join(import_lines)
    for banned in ("store", "commit", "verifier_router", "sqlite", "requests", "httpx"):
        assert banned not in joined, f"{banned} must not be imported by acceptance_rules"
