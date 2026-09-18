# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-A1 red tests: the planning-decision contract core (V2 plan §11–§17, §33–§40).

This slice nails only the protocol *core*: the limit constants, the five enums,
the H1 phase-enablement table, the strict ``to_json``/``from_json`` dataclasses
that do not depend on the (still-unratified) envelope/payload shapes, and the
deterministic ``decision_id`` derivation of §35.

The envelope, the payload variants, the JSON Schema file and the golden fixture
directories are deliberately out of scope here; they wait on the plan author.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    H1_DECISION_ENABLEMENT,
    LEGACY_PLANNING_PROTOCOL,
    MAX_PD_ALTERNATIVES,
    MAX_PD_ARGUMENTS,
    MAX_PD_ASSUMPTIONS,
    MAX_PD_BINDINGS,
    MAX_PD_BLOCKERS,
    MAX_PD_HUMAN_OPTIONS,
    MAX_PD_RATIONALE_CHARS,
    MAX_PD_REASON_REFS,
    MAX_PD_REPLAN_TRIGGERS,
    MAX_PD_UNCERTAINTIES,
    MAX_PD_WAIT_REFS,
    MAX_PLANNING_REF_ID,
    PLANNING_DECISION_CODEC_VERSION,
    PLANNING_DECISION_SCHEMA_VERSION,
    PLANNING_DECISION_V1,
    AssumptionRisk,
    DecisionEnablement,
    PlanningDecisionRejectionCode,
    PlanningDecisionStatus,
    PlanningDecisionType,
    PlanningFeedbackV1,
    PlanningProblemDetailV1,
    PlanningRefKind,
    PlanningRefV1,
    PlanningRequestBinding,
    PlanningRetryBudgetView,
    compute_decision_id,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


# --------------------------------------------------------------------------------------
# Enums: every member name and value pinned as a literal list (§11, §17, §33, §36)
# --------------------------------------------------------------------------------------


def test_planning_decision_type_members_are_pinned() -> None:
    assert [(member.name, member.value) for member in PlanningDecisionType] == [
        ("REFINE", "REFINE"),
        ("PROPOSE_METHOD", "PROPOSE_METHOD"),
        ("REQUEST_EVIDENCE", "REQUEST_EVIDENCE"),
        ("REPAIR", "REPAIR"),
        ("BIND_EXISTING_GOAL", "BIND_EXISTING_GOAL"),
        ("DECLARE_BLOCKED", "DECLARE_BLOCKED"),
        ("REQUEST_HUMAN", "REQUEST_HUMAN"),
        ("WAIT", "WAIT"),
        ("NO_CHANGE", "NO_CHANGE"),
    ]
    assert len(PlanningDecisionType) == 9


def test_planning_ref_kind_members_are_pinned() -> None:
    # V2 addendum (BL-1): exactly sixteen kinds.  "method_instance" is the
    # sixteenth, so a REPLACE_METHOD payload can name the instance it retires.
    assert [(member.name, member.value) for member in PlanningRefKind] == [
        ("TASK", "task"),
        ("OBLIGATION", "obligation"),
        ("METHOD", "method"),
        ("REQUIREMENTS", "requirements"),
        ("ARTIFACT", "artifact"),
        ("SOURCE", "source"),
        ("OBSERVATION", "observation"),
        ("REVIEW", "review"),
        ("ACCEPTANCE", "acceptance"),
        ("RESOLUTION", "resolution"),
        ("OPERATION", "operation"),
        ("TOOL_RECEIPT", "tool_receipt"),
        ("KNOWLEDGE", "knowledge"),
        ("AUTHORITY", "authority"),
        ("CAPABILITY", "capability"),
        ("METHOD_INSTANCE", "method_instance"),
    ]
    assert len(PlanningRefKind) == 16
    assert "fact" not in {member.value for member in PlanningRefKind}
    assert "method_instance" in {member.value for member in PlanningRefKind}


def test_planning_decision_rejection_code_members_are_pinned() -> None:
    assert [(member.name, member.value) for member in PlanningDecisionRejectionCode] == [
        ("DECISION_BLOCK_MISSING", "DECISION_BLOCK_MISSING"),
        ("MULTIPLE_DECISIONS", "MULTIPLE_DECISIONS"),
        ("MIXED_PROTOCOL_BLOCKS", "MIXED_PROTOCOL_BLOCKS"),
        ("MALFORMED_DECISION", "MALFORMED_DECISION"),
        ("UNKNOWN_FIELD", "UNKNOWN_FIELD"),
        ("MODEL_SET_SYSTEM_FIELD", "MODEL_SET_SYSTEM_FIELD"),
        ("DECISION_TYPE_UNKNOWN", "DECISION_TYPE_UNKNOWN"),
        ("DECISION_NOT_ENABLED_IN_PHASE", "DECISION_NOT_ENABLED_IN_PHASE"),
        ("SUBJECT_NOT_IN_REQUEST", "SUBJECT_NOT_IN_REQUEST"),
        ("REF_OUTSIDE_CONTEXT", "REF_OUTSIDE_CONTEXT"),
        ("REQUEST_BINDING_STALE", "REQUEST_BINDING_STALE"),
        ("PACKAGE_HASH_MISMATCH", "PACKAGE_HASH_MISMATCH"),
        ("METHOD_NOT_FOUND", "METHOD_NOT_FOUND"),
        ("METHOD_STALE", "METHOD_STALE"),
        ("METHOD_RETIRED", "METHOD_RETIRED"),
        ("METHOD_REJECTED", "METHOD_REJECTED"),
        ("METHOD_INAPPLICABLE", "METHOD_INAPPLICABLE"),
        ("METHOD_NOT_AUTHORIZED", "METHOD_NOT_AUTHORIZED"),
        ("PARAMETER_INVALID", "PARAMETER_INVALID"),
        ("EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED"),
        ("EVIDENCE_CONFLICT", "EVIDENCE_CONFLICT"),
        ("CAPABILITY_MISSING", "CAPABILITY_MISSING"),
        ("DATA_UNBOUND", "DATA_UNBOUND"),
        ("STRUCTURE_INVALID", "STRUCTURE_INVALID"),
        ("ORDER_CYCLE", "ORDER_CYCLE"),
        ("REFINEMENT_CYCLE", "REFINEMENT_CYCLE"),
        ("COVERAGE_GAP", "COVERAGE_GAP"),
        ("BUDGET_INSUFFICIENT", "BUDGET_INSUFFICIENT"),
        ("OBLIGATION_NOT_OPEN", "OBLIGATION_NOT_OPEN"),
        ("AUTHORIZATION_REQUIRED", "AUTHORIZATION_REQUIRED"),
        ("OPERATION_UNRESOLVED", "OPERATION_UNRESOLVED"),
        ("RUNNING_WORK_NOT_RECONCILED", "RUNNING_WORK_NOT_RECONCILED"),
        ("REPAIR_NOT_ALLOWED", "REPAIR_NOT_ALLOWED"),
        ("REUSE_NOT_ALLOWED", "REUSE_NOT_ALLOWED"),
        ("PLANNING_BOUND_REACHED", "PLANNING_BOUND_REACHED"),
        ("INTERNAL_CONTRACT_ERROR", "INTERNAL_CONTRACT_ERROR"),
    ]
    assert len(PlanningDecisionRejectionCode) == 36


def test_planning_decision_status_members_are_pinned() -> None:
    assert [(member.name, member.value) for member in PlanningDecisionStatus] == [
        ("UNREADABLE", "UNREADABLE"),
        ("DECODED", "DECODED"),
        ("REJECTED", "REJECTED"),
        ("ADMITTED", "ADMITTED"),
        ("COMPILED", "COMPILED"),
        ("COMMIT_REJECTED", "COMMIT_REJECTED"),
        ("COMMITTED", "COMMITTED"),
        ("NO_STATE_CHANGE", "NO_STATE_CHANGE"),
    ]
    assert len(PlanningDecisionStatus) == 8


def test_assumption_risk_members_are_pinned() -> None:
    assert [(member.name, member.value) for member in AssumptionRisk] == [
        ("LOW", "LOW"),
        ("MEDIUM", "MEDIUM"),
        ("HIGH", "HIGH"),
    ]
    assert len(AssumptionRisk) == 3


# --------------------------------------------------------------------------------------
# Limit constants (§16)
# --------------------------------------------------------------------------------------


def test_limit_constants_are_pinned() -> None:
    assert MAX_PD_RATIONALE_CHARS == 4_000
    assert MAX_PD_REASON_REFS == 32
    assert MAX_PD_ASSUMPTIONS == 16
    assert MAX_PD_ALTERNATIVES == 8
    assert MAX_PD_UNCERTAINTIES == 16
    assert MAX_PD_REPLAN_TRIGGERS == 16
    assert MAX_PD_BINDINGS == 64
    assert MAX_PD_WAIT_REFS == 32
    assert MAX_PD_BLOCKERS == 16
    assert MAX_PD_HUMAN_OPTIONS == 12
    assert MAX_PD_ARGUMENTS == 32


def test_wire_identity_constants_are_pinned() -> None:
    # §13/§14/§35: these are the on-the-wire strings and the envelope schema
    # version.  They are exported constants, so a silent edit would change the
    # protocol identity without touching any codec; pin every one literally.
    assert PLANNING_DECISION_SCHEMA_VERSION == 1
    assert PLANNING_DECISION_V1 == "planning-decision-v1"
    assert LEGACY_PLANNING_PROTOCOL == "legacy-plan-proposal-v1"
    assert PLANNING_DECISION_CODEC_VERSION == "planning-decision-codec-v1"


def test_max_planning_ref_id_is_pinned_with_boundaries() -> None:
    # §17: the id is "non-empty and <= 256".  Pin the concrete limit and both
    # sides of it so a silent widening or narrowing is caught.
    assert MAX_PLANNING_REF_ID == 256
    accepted = PlanningRefV1(
        kind=PlanningRefKind.TASK,
        id="t" * 256,
        semantic_revision=1,
        content_hash=HASH_A,
    )
    assert accepted.id == "t" * 256
    with pytest.raises(ContractError):
        PlanningRefV1(
            kind=PlanningRefKind.TASK,
            id="t" * 257,
            semantic_revision=1,
            content_hash=HASH_A,
        )


# --------------------------------------------------------------------------------------
# H1 phase enablement table (§12, H1 column)
# --------------------------------------------------------------------------------------


def test_h1_decision_enablement_matches_section_12() -> None:
    executable = DecisionEnablement(decodable=True, admissible=True, executable=True)
    decode_only = DecisionEnablement(decodable=True, admissible=False, executable=False)
    assert dict(H1_DECISION_ENABLEMENT) == {
        "REFINE": executable,
        "REPAIR/REPLACE_METHOD": executable,
        "REPAIR/PROPOSE_SUCCESSOR": executable,
        "BIND_EXISTING_GOAL": executable,
        "DECLARE_BLOCKED": executable,
        "WAIT": executable,
        "NO_CHANGE": executable,
        "REQUEST_EVIDENCE": decode_only,
        "REQUEST_HUMAN": decode_only,
        "PROPOSE_METHOD": decode_only,
    }
    assert len(H1_DECISION_ENABLEMENT) == 10


# --------------------------------------------------------------------------------------
# Round-trips and strict rejection (unknown / missing / wrong type)
# --------------------------------------------------------------------------------------


def _planning_ref() -> PlanningRefV1:
    return PlanningRefV1(
        kind=PlanningRefKind.METHOD,
        id="code.fix-by-patch",
        semantic_revision=2,
        content_hash=HASH_A,
    )


def _request_binding() -> PlanningRequestBinding:
    return PlanningRequestBinding(
        request_id="req-0001",
        mission_id="mission-1",
        protocol_version="planning-decision-v1",
        package_version=4,
        package_hash=HASH_A,
        base_plan_revision=3,
        requirements_revision=2,
        scope_epoch_digest=HASH_A,
        subject_bindings_hash=HASH_B,
        visible_refs_digest=HASH_A,
        prompt_version="planner-decision-v1",
        prompt_hash=HASH_B,
        created_at=1_700_000_000.5,
        intent_id="intent-refine-subject-root",
    )


def _problem_detail() -> PlanningProblemDetailV1:
    return PlanningProblemDetailV1(
        code=PlanningDecisionRejectionCode.METHOD_NOT_FOUND,
        subject_ref=_planning_ref(),
        field_path="payload.method_ref.id",
        detail="the referenced method is not registered",
        expected="a registered method id",
        observed="code.missing",
    )


def _retry_budget() -> PlanningRetryBudgetView:
    return PlanningRetryBudgetView(
        same_request_format_retries_remaining=1,
        planning_rounds_remaining=4,
        synthesis_asks_remaining=2,
        root_review_repairs_remaining=3,
        repeated_failure_before_escalation_remaining=None,
    )


def _feedback() -> PlanningFeedbackV1:
    return PlanningFeedbackV1(
        previous_decision_id="pd-0123456789abcdef01234567",
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=(PlanningDecisionRejectionCode.METHOD_NOT_FOUND,),
        problems=(_problem_detail(),),
        changed_refs=(_planning_ref(),),
        budgets=_retry_budget(),
    )


def _cases() -> dict[str, tuple[type[Any], Any]]:
    return {
        "planning_ref": (PlanningRefV1, _planning_ref()),
        "request_binding": (PlanningRequestBinding, _request_binding()),
        "problem_detail": (PlanningProblemDetailV1, _problem_detail()),
        "retry_budget": (PlanningRetryBudgetView, _retry_budget()),
        "feedback": (PlanningFeedbackV1, _feedback()),
    }


CASES = _cases()


@pytest.mark.parametrize("case", list(CASES))
def test_dataclass_round_trips_through_json(case: str) -> None:
    cls, instance = CASES[case]
    assert cls.from_json(instance.to_json()) == instance


@pytest.mark.parametrize("case", list(CASES))
def test_unknown_field_is_rejected(case: str) -> None:
    cls, instance = CASES[case]
    payload = dict(instance.to_json())
    payload["unexpected_authority_override"] = True
    with pytest.raises(ContractError):
        cls.from_json(payload)


@pytest.mark.parametrize("case", list(CASES))
def test_missing_required_field_is_rejected(case: str) -> None:
    cls, instance = CASES[case]
    payload = dict(instance.to_json())
    payload.pop(next(iter(payload)))
    with pytest.raises(ContractError):
        cls.from_json(payload)


@pytest.mark.parametrize(
    ("case", "field", "wrong"),
    [
        ("planning_ref", "semantic_revision", "1"),
        ("request_binding", "package_version", "4"),
        ("problem_detail", "detail", 123),
        ("retry_budget", "planning_rounds_remaining", "4"),
        ("feedback", "previous_decision_id", 123),
    ],
)
def test_wrong_type_is_rejected(case: str, field: str, wrong: object) -> None:
    cls, instance = CASES[case]
    payload = dict(instance.to_json())
    payload[field] = wrong
    with pytest.raises(ContractError):
        cls.from_json(payload)


def test_non_object_is_rejected_for_every_dataclass() -> None:
    for cls, _ in CASES.values():
        with pytest.raises(ContractError):
            cls.from_json(["not", "an", "object"])


def test_request_binding_package_version_lower_bound_is_pinned() -> None:
    # §9 ships H1 with package_version 4 and the counterpart source is "ints
    # >= 1"; pin both sides so a silent 0 is refused and 1 still decodes.
    payload = _request_binding().to_json()
    payload["package_version"] = 0
    with pytest.raises(ContractError):
        PlanningRequestBinding.from_json(payload)
    payload["package_version"] = 1
    assert PlanningRequestBinding.from_json(payload).package_version == 1


def test_feedback_status_closed_set_is_enforced() -> None:
    # §36: status is one of the eight PlanningDecisionStatus values.  A bare
    # string that is not a member must be refused, not silently persisted.
    with pytest.raises(ContractError):
        PlanningFeedbackV1(
            previous_decision_id="pd-0123456789abcdef01234567",
            status="GARBAGE",
            rejection_codes=(),
            problems=(),
            changed_refs=(),
            budgets=_retry_budget(),
        )


def test_feedback_rejection_codes_element_closed_set_is_enforced() -> None:
    # §33: every element of rejection_codes is a PlanningDecisionRejectionCode.
    with pytest.raises(ContractError):
        PlanningFeedbackV1(
            previous_decision_id="pd-0123456789abcdef01234567",
            status=PlanningDecisionStatus.REJECTED,
            rejection_codes=("NOPE",),
            problems=(),
            changed_refs=(),
            budgets=_retry_budget(),
        )


def test_problem_detail_code_closed_set_is_enforced() -> None:
    # §40: code is a PlanningDecisionRejectionCode, not an arbitrary string.
    with pytest.raises(ContractError):
        PlanningProblemDetailV1(
            code="NOPE",
            subject_ref=None,
            field_path=None,
            detail="a detail",
        )


def test_problem_detail_text_fields_reject_wrong_types() -> None:
    # §40: field_path / expected / observed are strings (or null); a non-string
    # must raise rather than being coerced by str().
    for field, wrong in (
        ("field_path", 123),
        ("expected", 123),
        ("observed", [1, 2]),
    ):
        kwargs = {"field_path": None, "expected": None, "observed": None}
        kwargs[field] = wrong
        with pytest.raises(ContractError):
            PlanningProblemDetailV1(
                code=PlanningDecisionRejectionCode.METHOD_NOT_FOUND,
                subject_ref=None,
                detail="a detail",
                **kwargs,
            )


def test_request_binding_intent_id_and_prompt_hash_are_strict() -> None:
    # BL-7 adds intent_id, and prompt_hash must stay a lowercase SHA-256 digest;
    # neither may be coerced from a non-string or accept a malformed hash.
    base = _request_binding().to_json()
    bad_intent = dict(base)
    bad_intent["intent_id"] = 123
    with pytest.raises(ContractError):
        PlanningRequestBinding.from_json(bad_intent)
    bad_hash = dict(base)
    bad_hash["prompt_hash"] = "not-a-hash"
    with pytest.raises(ContractError):
        PlanningRequestBinding.from_json(bad_hash)


def test_request_binding_base_plan_revision_lower_bound_is_pinned() -> None:
    # §34: base_plan_revision is a non-negative counter; 0 is legal, -1 is not.
    base = _request_binding().to_json()
    base["base_plan_revision"] = -1
    with pytest.raises(ContractError):
        PlanningRequestBinding.from_json(base)
    base["base_plan_revision"] = 0
    assert PlanningRequestBinding.from_json(base).base_plan_revision == 0


def test_retry_budget_counters_lower_bound_is_pinned() -> None:
    # §39: remaining counters may be zero; a negative is a contract error.
    with pytest.raises(ContractError):
        PlanningRetryBudgetView(
            same_request_format_retries_remaining=-1,
            planning_rounds_remaining=0,
            synthesis_asks_remaining=0,
            root_review_repairs_remaining=0,
            repeated_failure_before_escalation_remaining=None,
        )
    zeroed = PlanningRetryBudgetView(
        same_request_format_retries_remaining=0,
        planning_rounds_remaining=0,
        synthesis_asks_remaining=0,
        root_review_repairs_remaining=0,
        repeated_failure_before_escalation_remaining=None,
    )
    assert zeroed.same_request_format_retries_remaining == 0


# --------------------------------------------------------------------------------------
# PlanningRefV1 negative inputs (§17)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # unknown kind: the closed enum, including the forbidden ``fact`` alias
        {"kind": "fact", "id": "x", "semantic_revision": 1, "content_hash": HASH_A},
        # blank id
        {"kind": "task", "id": "   ", "semantic_revision": 1, "content_hash": HASH_A},
        # revision of exactly 0 violates the ">= 1" lower bound
        {"kind": "task", "id": "task-1", "semantic_revision": 0, "content_hash": HASH_A},
        # revision must be an integer >= 1 and never a bool
        {"kind": "task", "id": "task-1", "semantic_revision": True, "content_hash": HASH_A},
        # content hash must be a full lowercase SHA-256 hex digest
        {"kind": "task", "id": "task-1", "semantic_revision": 1, "content_hash": HASH_A.upper()},
        # content hash is required
        {"kind": "task", "id": "task-1", "semantic_revision": 1},
        # id longer than 256 characters
        {"kind": "task", "id": "t" * 257, "semantic_revision": 1, "content_hash": HASH_A},
    ],
)
def test_planning_ref_illegal_inputs_are_rejected(value: dict[str, object]) -> None:
    with pytest.raises(ContractError):
        PlanningRefV1.from_json(value)


def test_planning_ref_all_fields_are_required_by_construction() -> None:
    # §17 "all fields required": even the direct-construction path (used by
    # system code, not only the model wire) must refuse a missing content_hash.
    with pytest.raises(TypeError):
        PlanningRefV1(  # type: ignore[call-arg]
            kind=PlanningRefKind.TASK,
            id="task-1",
            semantic_revision=1,
        )


# --------------------------------------------------------------------------------------
# decision_id (§35): fixed literal, idempotence, and input sensitivity
# --------------------------------------------------------------------------------------

FIXED_REQUEST_ID = "req-0001"
FIXED_ATTEMPT = 0
FIXED_RAW_HASH = HASH_A
# Independently recomputed here with hashlib (not by calling the module) and then
# written down as a literal so a wire change cannot slide past the suite.
FIXED_DECISION_ID = "pd-6ddace2ec77d6efa64fde25e"


def _independent_decision_id(request_id: str, attempt_ordinal: int, raw_output_hash: str) -> str:
    material = (
        request_id
        + "\x1f"
        + str(attempt_ordinal)
        + "\x1f"
        + raw_output_hash
        + "\x1f"
        + "planning-decision-codec-v1"
    )
    return "pd-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def test_decision_id_matches_the_independent_literal() -> None:
    assert (
        _independent_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT, FIXED_RAW_HASH)
        == FIXED_DECISION_ID
    )
    assert compute_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT, FIXED_RAW_HASH) == FIXED_DECISION_ID
    assert FIXED_DECISION_ID.startswith("pd-")
    assert len(FIXED_DECISION_ID) == 27


def test_decision_id_is_stable_and_input_sensitive() -> None:
    baseline = compute_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT, FIXED_RAW_HASH)
    assert compute_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT, FIXED_RAW_HASH) == baseline
    assert compute_decision_id("req-0002", FIXED_ATTEMPT, FIXED_RAW_HASH) != baseline
    assert compute_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT + 1, FIXED_RAW_HASH) != baseline
    assert compute_decision_id(FIXED_REQUEST_ID, FIXED_ATTEMPT, HASH_B) != baseline


@pytest.mark.parametrize("ordinal", [-1, True, "0", 1.0])
def test_decision_id_rejects_invalid_attempt_ordinal(ordinal: object) -> None:
    with pytest.raises(ContractError):
        compute_decision_id(FIXED_REQUEST_ID, ordinal, FIXED_RAW_HASH)  # type: ignore[arg-type]
