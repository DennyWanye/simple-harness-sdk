# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1: where a new method comes from, and what it has to pass to be used.

The scripted blocks in ``fixtures/htn/proposals`` stand in for the
``method_proposal`` tag block of §18.5 C8 — model output, decoded by the real
codec, so no test here depends on a live model.

What is pinned:

* an empty library answers a goal with a :class:`MethodProposalRequest`, not with
  a guess;
* a well-formed proposal walks ``DRAFT → STRUCTURALLY_VALID → TRIAL_ADMITTED`` and
  stops there — ``promote`` answers ``PROMOTION_NOT_AVAILABLE``, because
  ``EVALUATED → ADMITTED`` needs the offline evaluation P8 delivers (§7.3);
* a proposal that names an operator nobody registered is ``REJECTED`` with the
  missing capability spelled out;
* a model-authored payload that writes its own ``registry_status`` is refused
  before any check runs, and leaves no definition behind;
* a ``SUSPENDED`` method keeps its history and stops being retrievable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))

from htn_world import (  # noqa: E402
    BUDGET,
    Env,
    atom,
    ledger_for,
    load_proposal,
    method,
    param,
    ref,
    root_network,
    seed_env,
    step,
    task_binding,
)

from agent_orchestrator.contracts.evidence_state import TruthValue  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    MethodRegistryStatus,
    RegistryAuthor,
    TaskForm,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.planning.htn.refinement import (  # noqa: E402
    RefinementOutcome,
    planning_frontier,
    refine,
)
from agent_orchestrator.planning.htn.registry import (  # noqa: E402
    AdmissionStepId,
    AdmissionVerdict,
    MethodProposal,
    RejectionCode,
    StepOutcome,
    SuggestionReason,
    method_is_recursive,
    statement_similarity,
)


def code_env() -> Env:
    """The seed library, minus every method: the registry starts empty."""

    env = seed_env()
    env.registry.__init__()  # noqa: PLC2801 - a fresh registry over the same catalogue
    return env


def proposal(name: str) -> MethodProposal:
    return MethodProposal.from_json(load_proposal(name))


def admitted(env: Env, name: str):
    submission = proposal(name)
    return env.registry.admit(submission, author=submission.author, policy=env.policy())


def code_root(env: Env):
    return task_binding(
        env,
        "code.fix-failing-test",
        parameters={"repository": "repo-1", "failing_test": "test_alpha"},
    )


def observed_world(env: Env) -> Env:
    env.say("code.repo-checked-out", {"repository": "repo-1"}, TruthValue.TRUE)
    env.say("code.test-is-failing", {"test": "test_alpha"}, TruthValue.TRUE)
    env.say("code.working-tree-clean", {"repository": "repo-1"}, TruthValue.TRUE)
    return env


# ============================================================ an empty library


def test_an_empty_library_asks_for_a_method() -> None:
    env = observed_world(code_env())
    binding = code_root(env)
    network = root_network(env, binding)
    decision = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger_for(binding, mission=env.mission),
        budget=BUDGET,
    ).decisions[0]
    assert decision.outcome is RefinementOutcome.NO_APPLICABLE_METHOD
    assert decision.proposal_request is not None


def test_the_request_names_the_goal_type_a_method_would_have_to_target() -> None:
    env = observed_world(code_env())
    binding = code_root(env)
    network = root_network(env, binding)
    decision = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger_for(binding, mission=env.mission),
        budget=BUDGET,
    ).decisions[0]
    request = decision.proposal_request
    assert request is not None
    assert request.goal_type_ref is not None
    assert request.goal_type_ref.id == "code.fix-failing-test"


def test_the_request_is_json_serialisable_for_a_synthesiser() -> None:
    env = observed_world(code_env())
    binding = code_root(env)
    network = root_network(env, binding)
    decision = refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger_for(binding, mission=env.mission),
        budget=BUDGET,
    ).decisions[0]
    assert decision.proposal_request is not None
    payload = decision.proposal_request.to_json()
    assert payload["goal_signature"]["signature_id"] == "code.fix-failing-test"


def test_no_fuel_is_spent_when_there_is_nothing_to_expand() -> None:
    env = observed_world(code_env())
    binding = code_root(env)
    network = root_network(env, binding)
    ledger = ledger_for(binding, mission=env.mission)
    refine(
        planning_frontier(network),
        network=network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert ledger.remaining_fuel(binding.obligation_id) == 3


# ================================================================ the happy path


def test_a_scripted_proposal_reaches_trial_admission() -> None:
    receipt = admitted(code_env(), "valid")
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED


def test_the_status_chain_is_draft_then_valid_then_trial() -> None:
    receipt = admitted(code_env(), "valid")
    assert receipt.transitions == (
        MethodRegistryStatus.DRAFT,
        MethodRegistryStatus.STRUCTURALLY_VALID,
        MethodRegistryStatus.TRIAL_ADMITTED,
    )


def test_the_trial_is_scoped_to_one_mission() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    assert receipt.registration is not None
    assert receipt.registration.trial_scope_mission == env.mission


def test_the_registration_is_written_by_the_registry_service() -> None:
    receipt = admitted(code_env(), "valid")
    assert receipt.registration is not None
    assert receipt.registration.author is RegistryAuthor.SYSTEM
    assert receipt.author is RegistryAuthor.MODEL


def test_the_registration_points_at_its_admission_receipt() -> None:
    receipt = admitted(code_env(), "valid")
    assert receipt.registration is not None
    assert receipt.registration.admission_receipt_ref == receipt.receipt_ref()


def test_the_receipt_reference_is_content_addressed() -> None:
    first = admitted(code_env(), "valid").receipt_ref()
    second = admitted(code_env(), "valid").receipt_ref()
    assert first == second


def test_the_first_three_steps_pass() -> None:
    receipt = admitted(code_env(), "valid")
    outcomes = {record.step: record.outcome for record in receipt.steps}
    assert outcomes[AdmissionStepId.STRUCTURE_AND_TYPES] is StepOutcome.PASSED
    assert outcomes[AdmissionStepId.REGISTRY_TYPE_CHECK] is StepOutcome.PASSED
    assert outcomes[AdmissionStepId.STRUCTURAL_CHECKS] is StepOutcome.PASSED


def test_the_review_and_formal_steps_are_deferred_not_passed() -> None:
    """A receipt must not claim a check nobody ran (§7.3 steps 4 and 5)."""

    receipt = admitted(code_env(), "valid")
    outcomes = {record.step: record.outcome for record in receipt.steps}
    assert outcomes[AdmissionStepId.INDEPENDENT_PLAN_REVIEW] is StepOutcome.DEFERRED
    assert outcomes[AdmissionStepId.FORMAL_MODELING] is StepOutcome.DEFERRED


def test_an_admitted_method_is_retrievable_in_its_mission() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    candidates = env.registry.candidates_for(ref("code.fix-failing-test"), mission_id=env.mission)
    assert [item.method_ref for item in candidates] == [receipt.method_ref]


def test_a_human_submission_takes_the_same_path() -> None:
    receipt = admitted(code_env(), "human_draft")
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED
    assert receipt.author is RegistryAuthor.SYSTEM


def test_resubmitting_the_same_bytes_is_idempotent() -> None:
    env = code_env()
    first = admitted(env, "valid")
    second = admitted(env, "valid")
    assert first.method_ref == second.method_ref
    assert len(env.registry.method_refs()) == 1


# ==================================================================== refusals


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("unknown_operator", RejectionCode.UNKNOWN_OPERATOR),
        ("missing_capability", RejectionCode.UNKNOWN_CAPABILITY),
        ("unknown_predicate", RejectionCode.UNKNOWN_PREDICATE),
        ("coverage_gap", RejectionCode.ROOT_COVERAGE_GAP),
        ("ordering_cycle", RejectionCode.ORDERING_CYCLE),
        ("unguarded_recursion", RejectionCode.UNBOUNDED_RECURSION),
        ("parameter_typo", RejectionCode.PREDICATE_TYPE_ERROR),
        ("claims_admitted", RejectionCode.MODEL_CLAIMED_STATUS),
        ("claims_trial", RejectionCode.MODEL_CLAIMED_STATUS),
    ],
)
def test_each_defect_has_its_own_rejection_code(fixture: str, code: RejectionCode) -> None:
    receipt = admitted(code_env(), fixture)
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert code in receipt.codes()


def test_a_missing_operator_is_named_in_the_receipt() -> None:
    receipt = admitted(code_env(), "unknown_operator")
    assert receipt.missing_operators == ("code.apply-hotfix",)


def test_a_missing_operator_explains_that_the_deployment_cannot_run_it() -> None:
    receipt = admitted(code_env(), "unknown_operator")
    detail = next(
        problem.detail
        for problem in receipt.problems
        if problem.code is RejectionCode.UNKNOWN_OPERATOR
    )
    assert "cannot execute" in detail


def test_a_missing_capability_is_named_in_the_receipt() -> None:
    receipt = admitted(code_env(), "missing_capability")
    assert "repo.rewrite-history" in receipt.missing_capabilities


def test_a_coverage_gap_names_the_uncovered_criterion() -> None:
    receipt = admitted(code_env(), "coverage_gap")
    detail = next(
        problem.detail
        for problem in receipt.problems
        if problem.code is RejectionCode.ROOT_COVERAGE_GAP
    )
    assert "c-change-explained" in detail


def test_a_rejected_method_is_not_retrievable() -> None:
    env = code_env()
    receipt = admitted(env, "unknown_operator")
    assert not env.registry.retrievable(receipt.method_ref, mission_id=env.mission)


def test_a_rejected_method_keeps_a_registration_that_says_so() -> None:
    env = code_env()
    receipt = admitted(env, "unknown_operator")
    registration = env.registry.registration(receipt.method_ref)
    assert registration is not None
    assert registration.status is MethodRegistryStatus.REJECTED


def test_a_rejected_submission_leaves_no_usable_definition() -> None:
    env = code_env()
    receipt = admitted(env, "unknown_operator")
    assert env.registry.definition(receipt.method_ref) is None


def test_steps_after_the_failing_one_are_recorded_as_not_reached() -> None:
    receipt = admitted(code_env(), "unknown_operator")
    outcomes = {record.step: record.outcome for record in receipt.steps}
    assert outcomes[AdmissionStepId.STRUCTURE_AND_TYPES] is StepOutcome.FAILED
    assert outcomes[AdmissionStepId.STRUCTURAL_CHECKS] is StepOutcome.NOT_REACHED


def test_a_model_claiming_a_status_is_refused_before_any_check() -> None:
    receipt = admitted(code_env(), "claims_admitted")
    assert receipt.steps == ()
    assert receipt.codes() == {RejectionCode.MODEL_CLAIMED_STATUS}


def test_a_model_claiming_trial_admission_is_refused_too() -> None:
    receipt = admitted(code_env(), "claims_trial")
    assert receipt.verdict is AdmissionVerdict.REJECTED


def test_the_claimed_status_is_quoted_back() -> None:
    receipt = admitted(code_env(), "claims_admitted")
    assert "ADMITTED" in receipt.problems[0].detail


def test_the_contract_itself_refuses_a_model_authored_admission() -> None:
    from agent_orchestrator.contracts.htn import MethodRef, MethodRegistration

    with pytest.raises(ContractError, match="DRAFT"):
        MethodRegistration(
            method_ref=MethodRef(method_id="m", version=1, content_hash="a" * 64),
            status=MethodRegistryStatus.ADMITTED,
            author=RegistryAuthor.MODEL,
        )


def test_an_author_that_disagrees_with_the_submission_is_refused() -> None:
    env = code_env()
    submission = proposal("valid")
    receipt = env.registry.admit(submission, author=RegistryAuthor.SYSTEM, policy=env.policy())
    assert receipt.verdict is AdmissionVerdict.REJECTED


def test_a_method_with_no_steps_refines_nothing() -> None:
    env = code_env()
    contract = method(
        "code.empty",
        "code.fix-failing-test",
        parameter_schema="code.repo-params",
        output_schema="code.outputs",
        applicable=(atom("code.repo-checked-out", {"repository": param("repository")}),),
        steps=(),
        links=(("c-test-passes", None, "c-green"), ("c-change-explained", None, "c-x")),
    )
    receipt = env.admit(contract)
    assert RejectionCode.MALFORMED_DEFINITION in receipt.codes()


def test_a_method_written_for_a_primitive_type_is_refused() -> None:
    env = code_env()
    contract = method(
        "code.refines-a-primitive",
        "code.verify-tests",
        parameter_schema="code.repo-params",
        output_schema="code.outputs",
        steps=(step("verify", "code.verify-tests", TaskForm.PRIMITIVE, {}),),
        links=(("c-green", "verify", "c-green"),),
        finalizer="verify",
    )
    receipt = env.admit(contract)
    assert RejectionCode.FORM_MISMATCH in receipt.codes()


def test_a_step_whose_form_disagrees_with_its_type_is_refused() -> None:
    env = code_env()
    contract = method(
        "code.form-mismatch",
        "code.fix-failing-test",
        parameter_schema="code.repo-params",
        output_schema="code.outputs",
        applicable=(atom("code.repo-checked-out", {"repository": param("repository")}),),
        steps=(step("verify", "code.verify-tests", TaskForm.COMPOUND, {}),),
        links=(("c-test-passes", "verify", "c-green"), ("c-change-explained", "verify", "c-x")),
        finalizer="verify",
    )
    receipt = env.admit(contract)
    assert RejectionCode.FORM_MISMATCH in receipt.codes()


def test_a_criterion_link_naming_an_unknown_step_is_refused() -> None:
    env = code_env()
    contract = method(
        "code.bad-link",
        "code.fix-failing-test",
        parameter_schema="code.repo-params",
        output_schema="code.outputs",
        applicable=(atom("code.repo-checked-out", {"repository": param("repository")}),),
        steps=(step("verify", "code.verify-tests", TaskForm.PRIMITIVE, {}),),
        links=(
            ("c-test-passes", "verify", "c-green"),
            ("c-change-explained", "nowhere", "c-x"),
        ),
        finalizer="verify",
    )
    receipt = env.admit(contract)
    assert RejectionCode.MALFORMED_DEFINITION in receipt.codes()


def test_a_policy_step_bound_refuses_an_oversized_method() -> None:
    env = code_env()
    contract = method(
        "code.too-many-steps",
        "code.fix-failing-test",
        parameter_schema="code.repo-params",
        output_schema="code.outputs",
        applicable=(atom("code.repo-checked-out", {"repository": param("repository")}),),
        steps=(
            step("verify", "code.verify-tests", TaskForm.PRIMITIVE, {}),
            step("verify2", "code.verify-tests", TaskForm.PRIMITIVE, {}),
        ),
        links=(("c-test-passes", "verify", "c-green"), ("c-change-explained", "verify2", "c-x")),
        finalizer="verify",
    )
    receipt = env.admit(contract, policy=env.policy(max_steps=1))
    assert RejectionCode.SIZE_BOUND in receipt.codes()


# ================================================================== the lifecycle


@pytest.mark.parametrize(
    "target",
    [MethodRegistryStatus.EVALUATED, MethodRegistryStatus.ADMITTED],
)
def test_promotion_beyond_the_trial_is_not_available(target) -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    promoted = env.registry.promote(receipt.method_ref, target, policy=env.policy())
    assert promoted.verdict is AdmissionVerdict.PROMOTION_NOT_AVAILABLE


def test_promotion_explains_that_the_offline_evaluation_is_missing() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    promoted = env.registry.promote(
        receipt.method_ref, MethodRegistryStatus.ADMITTED, policy=env.policy()
    )
    assert "P8" in promoted.steps[0].detail


def test_promotion_does_not_change_the_registration() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.promote(receipt.method_ref, MethodRegistryStatus.ADMITTED, policy=env.policy())
    registration = env.registry.registration(receipt.method_ref)
    assert registration is not None
    assert registration.status is MethodRegistryStatus.TRIAL_ADMITTED


def test_a_suspended_method_is_not_retrieved() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.suspend(receipt.method_ref, reason="a counter-example was recorded")
    assert env.registry.candidates_for(ref("code.fix-failing-test"), mission_id=env.mission) == ()


def test_a_suspended_method_keeps_its_definition() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.suspend(receipt.method_ref, reason="a counter-example was recorded")
    assert env.registry.definition(receipt.method_ref) is not None


def test_a_suspended_method_is_offered_as_a_suggestion_with_its_status() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.suspend(receipt.method_ref, reason="a counter-example was recorded")
    suggestions = env.registry.suggest_for(ref("code.fix-failing-test"), mission_id=env.mission)
    assert [item.reason for item in suggestions] == [SuggestionReason.NOT_RETRIEVABLE_HERE]


def test_a_reinstated_method_is_retrievable_again() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.suspend(receipt.method_ref, reason="counter-example")
    env.registry.reinstate(receipt.method_ref, mission_id=env.mission, reason="reviewed")
    assert (
        len(env.registry.candidates_for(ref("code.fix-failing-test"), mission_id=env.mission)) == 1
    )


def test_reinstating_a_method_that_is_not_suspended_is_refused() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    with pytest.raises(ContractError, match="SUSPENDED"):
        env.registry.reinstate(receipt.method_ref, mission_id=env.mission, reason="x")


def test_a_retired_method_is_not_retrieved() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.retire(receipt.method_ref, reason="superseded")
    assert env.registry.candidates_for(ref("code.fix-failing-test"), mission_id=env.mission) == ()


def test_a_trial_admitted_method_is_invisible_to_another_mission() -> None:
    env = code_env()
    admitted(env, "valid")
    assert env.registry.candidates_for(ref("code.fix-failing-test"), mission_id="mission-2") == ()


def test_a_trial_admitted_method_is_suggested_to_another_mission_with_a_reason() -> None:
    env = code_env()
    admitted(env, "valid")
    suggestions = env.registry.suggest_for(ref("code.fix-failing-test"), mission_id="mission-2")
    assert suggestions and suggestions[0].reason is SuggestionReason.NOT_RETRIEVABLE_HERE


def test_trial_use_is_counted_per_mission() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.note_trial_use(receipt.method_ref, mission_id=env.mission)
    env.registry.note_trial_use(receipt.method_ref, mission_id=env.mission)
    env.registry.note_trial_use(receipt.method_ref, mission_id="mission-2")
    assert env.registry.trial_uses(receipt.method_ref, mission_id=env.mission) == 2
    assert env.registry.trial_uses(receipt.method_ref, mission_id="mission-2") == 1


def test_the_candidate_carries_its_trial_count() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    env.registry.note_trial_use(receipt.method_ref, mission_id=env.mission)
    candidate = env.registry.candidates_for(ref("code.fix-failing-test"), mission_id=env.mission)[0]
    assert candidate.trial_uses == 1
    assert candidate.trial_scoped


def test_suspending_an_unregistered_method_is_refused() -> None:
    env = code_env()
    from agent_orchestrator.contracts.htn import MethodRef

    with pytest.raises(ContractError, match="not registered"):
        env.registry.suspend(
            MethodRef(method_id="nope", version=1, content_hash="a" * 64), reason="x"
        )


# =================================================================== retrieval


def test_a_method_for_another_goal_version_is_only_a_suggestion() -> None:
    env = code_env()
    admitted(env, "valid")
    suggestions = env.registry.suggest_for(ref("code.fix-failing-test", 2), mission_id=env.mission)
    assert [item.reason for item in suggestions] == [
        SuggestionReason.OTHER_VERSION_OF_SAME_GOAL_TYPE
    ]


def test_a_suggestion_is_never_a_candidate() -> None:
    env = code_env()
    admitted(env, "valid")
    assert (
        env.registry.candidates_for(ref("code.fix-failing-test", 2), mission_id=env.mission) == ()
    )


def test_every_suggestion_is_marked_advisory() -> None:
    env = code_env()
    admitted(env, "valid")
    suggestions = env.registry.suggest_for(ref("code.fix-failing-test", 2), mission_id=env.mission)
    assert all(item.advisory_only for item in suggestions)


def test_similarity_is_symmetric_and_bounded() -> None:
    assert statement_similarity("fix failing test", "fix failing test") == 1.0
    assert statement_similarity("fix failing test", "") == 0.0
    assert 0.0 < statement_similarity("fix failing test", "fix the test") < 1.0


def test_a_recursive_method_is_recognised_and_still_admitted() -> None:
    env = seed_env()
    recursive = next(
        contract
        for domain in env.domains
        for contract in domain.methods
        if method_is_recursive(contract)
    )
    assert env.registry.retrievable(recursive.method_ref(), mission_id=env.mission)
    receipt = env.registry.receipt(recursive.method_ref())
    assert receipt is not None and receipt.recursive


def test_an_unguarded_recursive_method_explains_why_it_was_refused() -> None:
    receipt = admitted(code_env(), "unguarded_recursion")
    detail = next(
        problem.detail
        for problem in receipt.problems
        if problem.code is RejectionCode.UNBOUNDED_RECURSION
    )
    assert "termination guard" in detail or "no expansion makes progress" in detail


def test_the_registry_refuses_two_definitions_at_one_content_hash() -> None:
    env = code_env()
    receipt = admitted(env, "valid")
    original = env.registry.definition(receipt.method_ref)
    assert original is not None
    env.registry._definitions[  # noqa: SLF001 - simulate a corrupted row
        (receipt.method_ref.method_id, receipt.method_ref.version, receipt.method_ref.content_hash)
    ] = next(
        contract
        for domain in seed_env().domains
        for contract in domain.methods
        if contract.method_id != original.method_id
    )
    again = admitted(env, "valid")
    assert RejectionCode.ALREADY_REGISTERED in again.codes()
