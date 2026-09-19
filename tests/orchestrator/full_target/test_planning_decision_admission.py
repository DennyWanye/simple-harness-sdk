# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-F red tests: deterministic admission of a decoded PlanningDecision (V2 §43).

Admission is the step between "the model's reply decoded" and "the system may act
on it".  It is a *pure* function: it opens no store, imports no storage reader,
compiles nothing, commits nothing and emits no event.  Everything it needs is a
read-only :class:`AdmissionContext` the caller assembled (the request record, the
current revisions, the method library view, the predicate registry, the active
method instances, the structural pre-checks).

The tests pin the §43 order — request identity → package/prompt binding → plan
revision → subject → visible refs → phase enable → budget/bound → payload →
method/applicability/evidence/auth/capability → operation gate → typed command —
and the "first failure wins" rule: two stages violated at once reports only the
earlier stage, and the same input always yields the same rejection.

The golden fixtures drive most of the table: every ``invalid/*`` file whose
expectation says ``"checked_in": "H1-F"`` must yield the code its ``.expect.json``
declares.  The list is walked, never transcribed:

* an admission-stage fixture is decoded and handed to admission together with a
  context whose one knob makes that code the first failure;
* a codec-stage fixture is a descriptor (its ``model_reply`` is a scenario note,
  V2 addendum §四), so the reply it describes is *built* from the valid fixtures
  and driven through ``parse_planning_decision``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts.evidence_state import TruthValue
from agent_orchestrator.contracts.htn import MethodRegistryStatus
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    H1_DECISION_ENABLEMENT,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionRejectionCode,
    PlanningDecisionStatus,
    PlanningDecisionType,
    PlanningFeedbackV1,
    PlanningRefV1,
    PlanningRequestBinding,
    PlanningRetryBudgetView,
    canonical_decision_hash,
)
from agent_orchestrator.planning.decision_admission import (
    AdmissionContext,
    AdmittedPlanningDecision,
    AuthorizationView,
    BudgetView,
    CapabilityView,
    MethodInstanceView,
    MethodView,
    OperationStateView,
    PlanShapeView,
    admit_planning_decision,
)
from agent_orchestrator.planning.decision_codec import (
    PlanningDecisionCodecError,
    parse_planning_decision,
    serialize_planning_decision,
)
from agent_orchestrator.planning.htn.planner_package import _feedback_json

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "planning_decision_v1"
VALID_DIR = FIXTURE_ROOT / "valid"
INVALID_DIR = FIXTURE_ROOT / "invalid"

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

REJECTION = PlanningDecisionRejectionCode

#: The codes of §33 whose *decision* belongs to the strict codec (H1-C) or to the
#: contract layer (H1-A2b).  They are never admission's answer; admission is
#: reached only by a shape the codec already accepted.
CODEC_LAYER_CODES = frozenset(
    {
        "DECISION_BLOCK_MISSING",
        "MULTIPLE_DECISIONS",
        "MIXED_PROTOCOL_BLOCKS",
        "UNKNOWN_FIELD",
        "MALFORMED_DECISION",
        "MODEL_SET_SYSTEM_FIELD",
        "DECISION_TYPE_UNKNOWN",
        "STRUCTURE_INVALID",
    }
)

#: §12 H1: the decision kinds the package advertises as executable.  The phase
#: gate reads this set, so a decode-only kind (REQUEST_HUMAN, ...) is refused.
H1_ENABLED = frozenset(
    name for name, enablement in H1_DECISION_ENABLEMENT.items() if enablement.executable
)

#: Every rejection code this slice must be able to produce, mapped to the test
#: that produces it.  The journal carries the same table; a code that §33 lists
#: and neither set mentions is a red test.
ADMISSION_CODE_CASES: dict[str, str] = {
    "SUBJECT_NOT_IN_REQUEST": "test_every_h1f_fixture_yields_its_expected_code",
    "REF_OUTSIDE_CONTEXT": "test_a_one_character_difference_in_any_quadruple_component_is_refused",
    "REQUEST_BINDING_STALE": "test_plan_revision_mismatch_is_request_binding_stale",
    "PACKAGE_HASH_MISMATCH": "test_a_package_or_prompt_binding_mismatch_is_refused",
    "DECISION_NOT_ENABLED_IN_PHASE": "test_a_decode_only_type_is_phase_refused",
    "METHOD_NOT_FOUND": "test_a_method_the_library_does_not_hold_is_refused",
    "METHOD_STALE": "test_a_method_behind_the_library_revision_is_refused",
    "METHOD_RETIRED": "test_a_retired_method_is_refused",
    "METHOD_REJECTED": "test_a_rejected_method_is_refused",
    "METHOD_INAPPLICABLE": "test_a_method_a_false_precondition_blocks_is_refused",
    "METHOD_NOT_AUTHORIZED": "test_a_method_without_its_grant_is_refused",
    "AUTHORIZATION_REQUIRED": "test_a_withheld_approval_is_refused",
    "PARAMETER_INVALID": "test_bindings_that_do_not_fill_the_parameters_are_refused",
    "EVIDENCE_REQUIRED": "test_an_unknown_premise_is_evidence_required",
    "EVIDENCE_CONFLICT": "test_a_conflicting_premise_is_evidence_conflict",
    "CAPABILITY_MISSING": "test_a_missing_capability_is_refused",
    "BUDGET_INSUFFICIENT": "test_exhausted_budget_is_refused",
    "PLANNING_BOUND_REACHED": "test_the_planning_bound_is_refused",
    "OPERATION_UNRESOLVED": "test_an_unresolved_operation_blocks_refining",
    "RUNNING_WORK_NOT_RECONCILED": "test_running_work_on_the_retired_instance_is_refused",
    "REPAIR_NOT_ALLOWED": "test_a_phase_that_does_not_allow_repair_is_refused",
    "OBLIGATION_NOT_OPEN": "test_a_successor_on_a_closed_obligation_is_refused",
    "REUSE_NOT_ALLOWED": "test_a_resolution_that_is_not_current_cannot_be_reused",
    "REFINEMENT_CYCLE": "test_a_successor_that_would_refine_itself_is_refused",
    "ORDER_CYCLE": "test_a_refinement_that_closes_an_order_cycle_is_refused",
    "DATA_UNBOUND": "test_a_data_edge_without_a_producer_is_refused",
    "COVERAGE_GAP": "test_a_refinement_that_leaves_an_obligation_uncovered_is_refused",
    "INTERNAL_CONTRACT_ERROR": "test_a_non_envelope_decision_is_an_internal_contract_error",
}

#: The decision the golden fixture describes.  Codes whose fixture is a scenario
#: descriptor rather than a wire object are absent on purpose.
TEXT_LEVEL_CODES = frozenset(
    {"DECISION_BLOCK_MISSING", "MULTIPLE_DECISIONS", "MIXED_PROTOCOL_BLOCKS"}
)


# --------------------------------------------------------------------------------------
# Fixture walking: never transcribe a case by hand.
# --------------------------------------------------------------------------------------


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_paths() -> list[Path]:
    return sorted(VALID_DIR.glob("*.json"))


def _h1f_cases() -> list[tuple[str, Any, dict[str, Any]]]:
    """Every ``invalid/*`` fixture this slice owns, with its expectation file."""

    cases: list[tuple[str, Any, dict[str, Any]]] = []
    for path in sorted(INVALID_DIR.glob("*.json")):
        if path.name.endswith(".expect.json"):
            continue
        expect = _read(path.with_name(path.stem + ".expect.json"))
        if expect["checked_in"] != "H1-F":
            continue
        cases.append((path.stem, _read(path), expect))
    return cases


def _h1f_ids() -> list[str]:
    return [name for name, _, _ in _h1f_cases()]


def _ref_shaped(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {
        "kind",
        "id",
        "semantic_revision",
        "content_hash",
    }


def _walk_refs(value: Any) -> Iterator[PlanningRefV1]:
    if _ref_shaped(value):
        yield PlanningRefV1.from_json(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_refs(item)


def _valid_refs() -> tuple[PlanningRefV1, ...]:
    seen: dict[tuple[str, str, int, str], PlanningRefV1] = {}
    for path in _valid_paths():
        for ref in _walk_refs(_read(path)):
            seen.setdefault(
                (str(ref.kind), ref.id, ref.semantic_revision, ref.content_hash), ref
            )
    return tuple(seen[key] for key in sorted(seen))


def _valid_envelope(name: str) -> PlanningDecisionEnvelopeV1:
    return PlanningDecisionEnvelopeV1.from_json(_read(VALID_DIR / f"{name}.json"))


def _binding(**overrides: Any) -> PlanningRequestBinding:
    fields: dict[str, Any] = {
        "request_id": "request-1",
        "mission_id": "mission-1",
        "protocol_version": "planning-decision-v1",
        "package_version": 4,
        "package_hash": HASH_A,
        "base_plan_revision": 7,
        "requirements_revision": 11,
        "scope_epoch_digest": HASH_B,
        "subject_bindings_hash": HASH_C,
        "visible_refs_digest": HASH_A,
        "prompt_version": "planner-hierarchical-v8",
        "prompt_hash": HASH_B,
        "created_at": 10.0,
        "intent_id": "intent-1",
    }
    fields.update(overrides)
    return PlanningRequestBinding(**fields)


def _ref(
    kind: str = "method",
    ident: str = "code.fix-by-patch",
    revision: int = 2,
    digest: str = HASH_A,
) -> PlanningRefV1:
    return PlanningRefV1(kind=kind, id=ident, semantic_revision=revision, content_hash=digest)


def _method(
    *,
    method_id: str = "code.fix-by-patch",
    version: int = 2,
    content_hash: str = HASH_A,
    status: MethodRegistryStatus = MethodRegistryStatus.ADMITTED,
    applies_to: tuple[str, ...] = ("subject-root",),
    required_parameters: tuple[str, ...] = ("target",),
    predicate_keys: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    requires_authorization: bool = False,
    authorization_granted: bool = True,
    precondition_truth: TruthValue = TruthValue.TRUE,
) -> MethodView:
    return MethodView(
        method_id=method_id,
        version=version,
        content_hash=content_hash,
        status=status,
        applies_to=applies_to,
        required_parameters=required_parameters,
        predicate_keys=predicate_keys,
        required_capabilities=required_capabilities,
        requires_authorization=requires_authorization,
        authorization_granted=authorization_granted,
        precondition_truth=precondition_truth,
    )


def _instance(
    *,
    subject_key: str = "subject-root",
    instance_id: str = "mi-1",
    semantic_revision: int = 3,
    content_hash: str = HASH_B,
    active: bool = True,
    running_work: bool = False,
) -> MethodInstanceView:
    return MethodInstanceView(
        subject_key=subject_key,
        instance_id=instance_id,
        semantic_revision=semantic_revision,
        content_hash=content_hash,
        active=active,
        running_work=running_work,
    )


def _method_views() -> tuple[MethodView, ...]:
    return (
        _method(),
        _method(
            method_id="code.alt-fix",
            version=1,
            content_hash=HASH_C,
            required_parameters=(),
        ),
    )


def _instances() -> tuple[MethodInstanceView, ...]:
    return (
        _instance(),
        _instance(instance_id="mi-2", semantic_revision=1, content_hash=HASH_A),
    )


def _budgets() -> PlanningRetryBudgetView:
    return PlanningRetryBudgetView(
        same_request_format_retries_remaining=1,
        planning_rounds_remaining=2,
        synthesis_asks_remaining=2,
        root_review_repairs_remaining=1,
        repeated_failure_before_escalation_remaining=3,
    )


def _context(**overrides: Any) -> AdmissionContext:
    """A context under which every executable valid fixture is admitted."""

    fields: dict[str, Any] = {
        "binding": _binding(),
        "decision_id": "pd-" + "1" * 24,
        "planning_subjects": (
            {
                "subject_key": "subject-root",
                "occurrence_id": "occ-1",
                "task_id": "t-1",
                "obligation_id": "o-1",
                "contract_revision": 4,
            },
        ),
        "visible_refs": _valid_refs(),
        "plan_revision": 7,
        "requirements_revision": 11,
        "scope_epoch_digest": HASH_B,
        "package_version": 4,
        "package_hash": HASH_A,
        "prompt_version": "planner-hierarchical-v8",
        "prompt_hash": HASH_B,
        "enabled_decision_types": H1_ENABLED,
        "repair_allowed": True,
        "methods": _method_views(),
        "predicates": frozenset(),
        "active_method_instances": _instances(),
        "open_obligations": (_ref("obligation", "o-1", 2, HASH_C),),
        "current_resolutions": (_ref("resolution", "r-1", 1, HASH_C),),
        "shareable_goals": (_ref("task", "g-1", 1, HASH_B),),
        "authorization": AuthorizationView(approval_granted=True),
        "capabilities": CapabilityView(available=frozenset()),
        "budget": BudgetView(
            planning_rounds_remaining=2, bound_remaining=128, budget_available=True
        ),
        "operations": OperationStateView(),
        "plan_shape": PlanShapeView(),
        "retry_budgets": _budgets(),
    }
    fields.update(overrides)
    return AdmissionContext(**fields)


def _with_method(view: MethodView, method_id: str) -> tuple[MethodView, ...]:
    return tuple(method_id == item.method_id and view or item for item in _method_views())


#: One knob per admission rejection code.  The fixture supplies the decision; the
#: context supplies the single fact that makes that code the first failure.
def _context_for(code: str, raw: Any) -> AdmissionContext:
    """The context that makes ``code`` the *first* failure for the fixture ``raw``.

    Every stage after the visible-ref check needs the decision's own references to
    be ones the request exposed, otherwise the earlier check speaks first.  The
    one code that must keep the citation outside the list is REF_OUTSIDE_CONTEXT
    itself.
    """

    base = _context_for_knob(code)
    if code == "REF_OUTSIDE_CONTEXT":
        return base
    exposed = {(_key(ref)): ref for ref in base.visible_refs}
    for ref in _walk_refs(raw):
        exposed.setdefault(_key(ref), ref)
    return replace(base, visible_refs=tuple(exposed[key] for key in sorted(exposed)))


def _key(ref: PlanningRefV1) -> tuple[str, str, int, str]:
    return (str(ref.kind), ref.id, ref.semantic_revision, ref.content_hash)


def _context_for_knob(code: str) -> AdmissionContext:
    if code == "AUTHORIZATION_REQUIRED":
        return _context(
            authorization=AuthorizationView(
                approval_granted=False, required_approvals=("approval-1",)
            )
        )
    if code == "BUDGET_INSUFFICIENT":
        return _context(
            budget=BudgetView(
                planning_rounds_remaining=0, bound_remaining=4, budget_available=False
            )
        )
    if code == "PLANNING_BOUND_REACHED":
        return _context(
            budget=BudgetView(
                planning_rounds_remaining=2, bound_remaining=0, budget_available=True
            )
        )
    if code == "CAPABILITY_MISSING":
        return _context(
            methods=_with_method(
                _method(required_capabilities=("cap.write",)), "code.fix-by-patch"
            )
        )
    if code == "EVIDENCE_REQUIRED":
        return _context(
            methods=_with_method(
                _method(precondition_truth=TruthValue.UNKNOWN, predicate_keys=("pred.x",)),
                "code.fix-by-patch",
            ),
            predicates=frozenset({"pred.x"}),
        )
    if code == "EVIDENCE_CONFLICT":
        return _context(
            methods=_with_method(
                _method(precondition_truth=TruthValue.CONFLICT), "code.fix-by-patch"
            )
        )
    if code == "METHOD_INAPPLICABLE":
        return _context(
            methods=_with_method(_method(precondition_truth=TruthValue.FALSE), "code.fix-by-patch")
        )
    if code == "METHOD_NOT_AUTHORIZED":
        return _context(
            methods=_with_method(
                _method(requires_authorization=True, authorization_granted=False),
                "code.fix-by-patch",
            )
        )
    if code in {"METHOD_REJECTED", "METHOD_RETIRED"}:
        status = (
            MethodRegistryStatus.REJECTED
            if code == "METHOD_REJECTED"
            else MethodRegistryStatus.RETIRED
        )
        return _context(methods=_with_method(_method(status=status), "code.fix-by-patch"))
    if code == "PARAMETER_INVALID":
        return _context(
            methods=_with_method(
                _method(required_parameters=("target", "mode")), "code.fix-by-patch"
            )
        )
    if code == "REQUEST_BINDING_STALE":
        return _context(plan_revision=8)
    if code == "PACKAGE_HASH_MISMATCH":
        return _context(package_hash=HASH_C)
    if code == "OPERATION_UNRESOLVED":
        return _context(operations=OperationStateView(unresolved_operations=("op-1",)))
    if code == "RUNNING_WORK_NOT_RECONCILED":
        other = _instance(instance_id="mi-2", semantic_revision=1, content_hash=HASH_A)
        return _context(active_method_instances=(_instance(running_work=True), other))
    if code == "REPAIR_NOT_ALLOWED":
        return _context(repair_allowed=False)
    if code == "OBLIGATION_NOT_OPEN":
        return _context(open_obligations=())
    if code == "REUSE_NOT_ALLOWED":
        return _context(current_resolutions=())
    if code == "REFINEMENT_CYCLE":
        return _context(
            plan_shape=PlanShapeView(refinement_cycle=("t-1 is an ancestor of goal.type",))
        )
    if code == "ORDER_CYCLE":
        return _context(
            plan_shape=PlanShapeView(order_cycle=("step-a before step-b before step-a",))
        )
    if code == "DATA_UNBOUND":
        return _context(plan_shape=PlanShapeView(data_unbound=("edge d-1 has no producer",)))
    if code == "COVERAGE_GAP":
        return _context(
            plan_shape=PlanShapeView(coverage_gap=("obligation o-9 has no covering step",))
        )
    return _context()


def _decision_for(raw: Any, code: str) -> Any:
    if code == "INTERNAL_CONTRACT_ERROR":
        # A decoded envelope is the module's precondition; something else is the
        # internal invariant this code names.
        return {"not": "an envelope"}
    return PlanningDecisionEnvelopeV1.from_json(raw)


def _admit(decision: Any, context: AdmissionContext) -> Any:
    return admit_planning_decision(decision, context=context)


def _reject(decision: Any, context: AdmissionContext) -> PlanningFeedbackV1:
    outcome = _admit(decision, context)
    assert isinstance(outcome, PlanningFeedbackV1), outcome
    return outcome


def _codes(feedback: PlanningFeedbackV1) -> list[str]:
    return [str(code) for code in feedback.rejection_codes]


def _codec_reply(code: str, raw: Any) -> str:
    """The model reply the codec-stage descriptor describes, built from the fixtures."""

    decision_block = serialize_planning_decision(_valid_envelope("refine"))
    if code == "DECISION_BLOCK_MISSING":
        return str(raw["model_reply"])
    if code == "MULTIPLE_DECISIONS":
        return decision_block + decision_block
    if code == "MIXED_PROTOCOL_BLOCKS":
        return decision_block + "<plan_revision_proposal>{}</plan_revision_proposal>"
    raise AssertionError(f"{code} is not a codec-stage code")  # pragma: no cover


# --------------------------------------------------------------------------------------
# The trailer for the golden fixtures (§46): every H1-F case yields its own code.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "raw", "expect"), _h1f_cases(), ids=_h1f_ids())
def test_every_h1f_fixture_yields_its_expected_code(
    name: str, raw: Any, expect: dict[str, Any]
) -> None:
    code = expect["expected_code"]
    if code in TEXT_LEVEL_CODES:
        assert expect["expected_stage"] == "codec", name
        with pytest.raises(PlanningDecisionCodecError) as caught:
            parse_planning_decision(_codec_reply(code, raw))
        assert str(caught.value.code) == code, name
        return
    assert expect["expected_stage"] == "admission", name
    feedback = _reject(_decision_for(raw, code), _context_for(code, raw))
    assert _codes(feedback) == [code], name


def test_the_h1f_case_list_is_not_empty_and_every_case_owns_its_code() -> None:
    # ``_h1f_cases`` is the *only* source of the case list: it is walked from the
    # expectation files, never transcribed.  Every ``H1-F`` case must name a code
    # this slice (or the codec layer it builds on) knows, and the vocabulary it is
    # drawn from is the closed §33 enum.
    cases = _h1f_cases()
    assert cases, "no H1-F fixture was found"
    owned = set(ADMISSION_CODE_CASES) | CODEC_LAYER_CODES
    for name, _raw, expect in cases:
        assert expect["checked_in"] == "H1-F", name
        assert expect["expected_code"] in owned, name
        assert expect["expected_code"] in {member.value for member in REJECTION}, name
    # Every admission code the slice lists is exercised by a real fixture *or* by
    # the named unit test in this module — a code with neither is a red test.
    fixture_codes = {expect["expected_code"] for _, _, expect in cases}
    module = Path(__file__).read_text(encoding="utf-8")
    for code, test_name in ADMISSION_CODE_CASES.items():
        assert code in fixture_codes or f"def {test_name}(" in module, code


def test_every_admission_rejection_code_has_a_case() -> None:
    listed = {member.value for member in REJECTION}
    declared = set(ADMISSION_CODE_CASES) | CODEC_LAYER_CODES
    assert declared == listed
    assert not (set(ADMISSION_CODE_CASES) & CODEC_LAYER_CODES)


# --------------------------------------------------------------------------------------
# The happy path: a decoded, in-scope decision becomes a typed admitted command.
# --------------------------------------------------------------------------------------

EXECUTABLE_VALID = (
    "refine",
    "repair-replace-method",
    "repair-propose-successor",
    "bind-existing-goal-reuse",
    "bind-existing-goal-share",
    "declare-blocked",
    "no-change",
    "wait",
)

DECODE_ONLY_VALID = ("request-evidence", "request-human", "propose-method")


@pytest.mark.parametrize("name", EXECUTABLE_VALID)
def test_every_executable_valid_fixture_is_admitted(name: str) -> None:
    outcome = _admit(_valid_envelope(name), _context())
    assert isinstance(outcome, AdmittedPlanningDecision), outcome
    assert outcome.decision is not None


@pytest.mark.parametrize("name", DECODE_ONLY_VALID)
def test_every_decode_only_valid_fixture_is_phase_refused(name: str) -> None:
    feedback = _reject(_valid_envelope(name), _context())
    assert _codes(feedback) == ["DECISION_NOT_ENABLED_IN_PHASE"]


def test_the_admitted_command_carries_the_resolved_subject_methods_and_hash() -> None:
    decision = _valid_envelope("refine")
    outcome = _admit(decision, _context())
    assert isinstance(outcome, AdmittedPlanningDecision)
    assert outcome.decision is decision
    assert outcome.canonical_hash == canonical_decision_hash(decision)
    assert outcome.subject["subject_key"] == "subject-root"
    assert [str(ref.method_id) for ref in outcome.method_refs] == ["code.fix-by-patch"]
    assert outcome.method_refs[0].content_hash == HASH_A


def test_an_admitted_command_is_frozen() -> None:
    outcome = _admit(_valid_envelope("no-change"), _context())
    assert isinstance(outcome, AdmittedPlanningDecision)
    with pytest.raises(Exception):
        outcome.canonical_hash = HASH_B  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Order: first failure wins, and the same input always yields the same answer.
# --------------------------------------------------------------------------------------


def test_plan_revision_mismatch_is_request_binding_stale() -> None:
    feedback = _reject(_valid_envelope("refine"), _context(plan_revision=8))
    assert _codes(feedback) == ["REQUEST_BINDING_STALE"]
    assert feedback.status is PlanningDecisionStatus.REJECTED


def test_a_package_or_prompt_binding_mismatch_is_refused() -> None:
    for overrides in (
        {"package_hash": HASH_C},
        {"package_version": 5},
        {"prompt_hash": HASH_C},
        {"prompt_version": "planner-hierarchical-v7"},
    ):
        feedback = _reject(_valid_envelope("refine"), _context(**overrides))
        assert _codes(feedback) == ["PACKAGE_HASH_MISMATCH"], overrides


def test_a_stale_binding_outranks_a_bad_subject() -> None:
    raw = dict(_read(VALID_DIR / "refine.json"))
    raw["subject_key"] = "subject-missing"
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context(plan_revision=8))
    assert _codes(feedback) == ["REQUEST_BINDING_STALE"]
    assert feedback.problems[0].field_path == "/request_binding/plan"


def test_a_bad_subject_outranks_a_ref_outside_context() -> None:
    raw = dict(_read(VALID_DIR / "refine.json"))
    raw["subject_key"] = "subject-missing"
    raw["reason_refs"] = [
        {
            "kind": "observation",
            "id": "obs-hidden",
            "semantic_revision": 1,
            "content_hash": HASH_B,
        }
    ]
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context())
    assert _codes(feedback) == ["SUBJECT_NOT_IN_REQUEST"]


def test_a_phase_refusal_outranks_a_payload_error() -> None:
    decision = _valid_envelope("request-human")
    feedback = _reject(decision, _context())
    assert _codes(feedback) == ["DECISION_NOT_ENABLED_IN_PHASE"]


def test_a_package_mismatch_outranks_a_stale_binding() -> None:
    decision = _valid_envelope("refine")
    feedback = _reject(decision, _context(plan_revision=8, package_hash=HASH_C))
    assert _codes(feedback) == ["PACKAGE_HASH_MISMATCH"]


def test_the_binding_revision_outranks_the_phase_gate() -> None:
    decision = _valid_envelope("request-human")
    feedback = _reject(decision, _context(plan_revision=8))
    assert _codes(feedback) == ["REQUEST_BINDING_STALE"]


def test_a_repair_payload_error_outranks_the_method_library() -> None:
    # OBLIGATION_NOT_OPEN is a payload check (§26); METHOD_NOT_FOUND belongs to the
    # method stage that follows it.  A successor whose obligation is closed is
    # refused for the obligation, never for a method it does not name.
    feedback = _reject(
        _valid_envelope("repair-propose-successor"),
        _context(open_obligations=(), methods=()),
    )
    assert _codes(feedback) == ["OBLIGATION_NOT_OPEN"]


def test_a_payload_error_outranks_an_unresolved_operation() -> None:
    feedback = _reject(
        _valid_envelope("repair-replace-method"),
        _context(
            active_method_instances=(),
            operations=OperationStateView(unresolved_operations=("op-1",)),
        ),
    )
    assert _codes(feedback) == ["METHOD_RETIRED"]


def test_a_one_stage_refusal_reports_one_problem_per_finding() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(plan_shape=PlanShapeView(order_cycle=("a", "b", "c"))),
    )
    assert _codes(feedback) == ["ORDER_CYCLE"]
    assert [problem.detail for problem in feedback.problems] == ["a", "b", "c"]
    assert all(problem.code is REJECTION.ORDER_CYCLE for problem in feedback.problems)


def test_a_refusal_never_marks_a_reference_as_changed() -> None:
    feedback = _reject(_valid_envelope("refine"), _context(plan_revision=8))
    assert feedback.changed_refs == ()
    assert PlanningFeedbackV1.from_json(feedback.to_json()).changed_refs == ()


def test_the_same_input_yields_the_same_rejection_twice() -> None:
    decision = _valid_envelope("refine")
    context = _context(operations=OperationStateView(unresolved_operations=("op-1",)))
    first = _reject(decision, context)
    second = _reject(decision, context)
    assert first == second
    assert _codes(first) == ["OPERATION_UNRESOLVED"]


# --------------------------------------------------------------------------------------
# Visible refs: byte-for-byte, all four components (§17, §18).
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    (
        {"kind": "task"},
        {"id": "code.fix-by-patchX"},
        {"semantic_revision": 3},
        {"content_hash": HASH_B},
    ),
    ids=("kind", "id", "revision", "hash"),
)
def test_a_one_character_difference_in_any_quadruple_component_is_refused(
    change: dict[str, Any],
) -> None:
    raw = json.loads(json.dumps(_read(VALID_DIR / "refine.json")))
    raw["payload"]["method_ref"].update(change)
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context())
    assert _codes(feedback) == ["REF_OUTSIDE_CONTEXT"]
    assert feedback.problems[0].field_path == "/payload/method_ref"


def test_a_reason_ref_absent_from_the_saved_list_is_refused() -> None:
    raw = json.loads(json.dumps(_read(VALID_DIR / "refine.json")))
    raw["reason_refs"] = [
        {
            "kind": "observation",
            "id": "obs-hidden",
            "semantic_revision": 1,
            "content_hash": HASH_B,
        }
    ]
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context())
    assert _codes(feedback) == ["REF_OUTSIDE_CONTEXT"]
    assert feedback.problems[0].field_path == "/reason_refs/0"


def test_a_ref_in_the_saved_list_but_not_exposed_by_the_package_is_irrelevant() -> None:
    # The addendum's 2026-09-19 ruling: the request record's list is authoritative;
    # admission never recomputes it from the package.
    decision = _valid_envelope("refine")
    context = _context(visible_refs=(_ref(),))
    outcome = _admit(decision, context)
    assert isinstance(outcome, AdmittedPlanningDecision)


# --------------------------------------------------------------------------------------
# Methods: existence, revision, lifecycle, applicability, parameters, evidence.
# --------------------------------------------------------------------------------------


def test_a_method_the_library_does_not_hold_is_refused() -> None:
    raw = _read(INVALID_DIR / "method-not-found.json")
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context_for("METHOD_NOT_FOUND", raw))
    assert _codes(feedback) == ["METHOD_NOT_FOUND"]
    assert feedback.problems[0].field_path == "/payload/method_ref"


def test_a_method_behind_the_library_revision_is_refused() -> None:
    raw = _read(INVALID_DIR / "method-stale.json")
    decision = PlanningDecisionEnvelopeV1.from_json(raw)
    feedback = _reject(decision, _context_for("METHOD_STALE", raw))
    assert _codes(feedback) == ["METHOD_STALE"]
    assert feedback.problems[0].expected == "2"
    assert feedback.problems[0].observed == "1"


def test_a_retired_method_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(status=MethodRegistryStatus.RETIRED), "code.fix-by-patch"
            )
        ),
    )
    assert _codes(feedback) == ["METHOD_RETIRED"]


def test_a_rejected_method_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(status=MethodRegistryStatus.REJECTED), "code.fix-by-patch"
            )
        ),
    )
    assert _codes(feedback) == ["METHOD_REJECTED"]


def test_a_method_a_false_precondition_blocks_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(_method(precondition_truth=TruthValue.FALSE), "code.fix-by-patch")
        ),
    )
    assert _codes(feedback) == ["METHOD_INAPPLICABLE"]


def test_a_method_that_does_not_apply_to_this_subject_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(methods=_with_method(_method(applies_to=("subject-other",)), "code.fix-by-patch")),
    )
    assert _codes(feedback) == ["METHOD_INAPPLICABLE"]


def test_bindings_that_do_not_fill_the_parameters_are_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(required_parameters=("target", "mode")), "code.fix-by-patch"
            )
        ),
    )
    assert _codes(feedback) == ["PARAMETER_INVALID"]
    assert feedback.problems[0].field_path == "/payload/bindings"


def test_an_unknown_premise_is_evidence_required() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(precondition_truth=TruthValue.UNKNOWN), "code.fix-by-patch"
            )
        ),
    )
    assert _codes(feedback) == ["EVIDENCE_REQUIRED"]


def test_an_unregistered_predicate_is_evidence_required() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(predicate_keys=("pred.missing",)), "code.fix-by-patch"
            ),
            predicates=frozenset(),
        ),
    )
    assert _codes(feedback) == ["EVIDENCE_REQUIRED"]


def test_a_conflicting_premise_is_evidence_conflict() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(precondition_truth=TruthValue.CONFLICT), "code.fix-by-patch"
            )
        ),
    )
    assert _codes(feedback) == ["EVIDENCE_CONFLICT"]


def test_a_missing_capability_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(required_capabilities=("cap.write",)), "code.fix-by-patch"
            ),
            capabilities=CapabilityView(available=frozenset({"cap.read"})),
        ),
    )
    assert _codes(feedback) == ["CAPABILITY_MISSING"]
    assert feedback.problems[0].observed == "cap.write"


def test_a_method_without_its_grant_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(requires_authorization=True, authorization_granted=False),
                "code.fix-by-patch",
            )
        ),
    )
    assert _codes(feedback) == ["METHOD_NOT_AUTHORIZED"]


def test_a_withheld_approval_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            authorization=AuthorizationView(
                approval_granted=False, required_approvals=("approval-1",)
            )
        ),
    )
    assert _codes(feedback) == ["AUTHORIZATION_REQUIRED"]


def test_exhausted_budget_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            budget=BudgetView(
                planning_rounds_remaining=0, bound_remaining=4, budget_available=False
            )
        ),
    )
    assert _codes(feedback) == ["BUDGET_INSUFFICIENT"]


def test_a_budget_account_that_is_not_available_is_refused_even_with_rounds_left() -> None:
    # The round counter and the budget account are two different facts: a mission
    # with a round left but an exhausted account must still not start new work.
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            budget=BudgetView(
                planning_rounds_remaining=2, bound_remaining=8, budget_available=False
            )
        ),
    )
    assert _codes(feedback) == ["BUDGET_INSUFFICIENT"]


def test_the_planning_bound_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            budget=BudgetView(
                planning_rounds_remaining=2, bound_remaining=0, budget_available=True
            )
        ),
    )
    assert _codes(feedback) == ["PLANNING_BOUND_REACHED"]


def test_the_bound_outranks_an_exhausted_budget() -> None:
    decision = _valid_envelope("refine")
    feedback = _reject(
        decision,
        _context(
            budget=BudgetView(
                planning_rounds_remaining=0, bound_remaining=0, budget_available=False
            )
        ),
    )
    assert _codes(feedback) == ["PLANNING_BOUND_REACHED"]


def test_a_decision_that_changes_nothing_is_not_budget_gated() -> None:
    # WAIT / NO_CHANGE / DECLARE_BLOCKED are the escape hatches an out-of-budget
    # planner must still be able to say.
    for name in ("no-change", "wait", "declare-blocked"):
        outcome = _admit(
            _valid_envelope(name),
            _context(
                budget=BudgetView(
                    planning_rounds_remaining=0, bound_remaining=0, budget_available=False
                )
            ),
        )
        assert isinstance(outcome, AdmittedPlanningDecision), name


# --------------------------------------------------------------------------------------
# Repair: the target instance, running work, and the phase's repair permit.
# --------------------------------------------------------------------------------------


def test_a_phase_that_does_not_allow_repair_is_refused() -> None:
    feedback = _reject(_valid_envelope("repair-replace-method"), _context(repair_allowed=False))
    assert _codes(feedback) == ["REPAIR_NOT_ALLOWED"]


def test_running_work_on_the_retired_instance_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("repair-replace-method"),
        _context(
            active_method_instances=(
                _instance(running_work=True),
                _instance(instance_id="mi-2", semantic_revision=1, content_hash=HASH_A),
            )
        ),
    )
    assert _codes(feedback) == ["RUNNING_WORK_NOT_RECONCILED"]


def test_an_instance_that_is_not_active_on_the_subject_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("repair-replace-method"),
        _context(
            active_method_instances=(
                _instance(active=False),
                _instance(instance_id="mi-2", semantic_revision=1, content_hash=HASH_A),
            )
        ),
    )
    assert _codes(feedback) == ["METHOD_RETIRED"]
    assert feedback.problems[0].field_path == "/payload/rejected_method_instance"


def test_an_instance_belonging_to_another_subject_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("repair-replace-method"),
        _context(
            active_method_instances=(
                _instance(subject_key="subject-other"),
                _instance(instance_id="mi-2", semantic_revision=1, content_hash=HASH_A),
            )
        ),
    )
    assert _codes(feedback) == ["METHOD_RETIRED"]


def test_a_successor_on_a_closed_obligation_is_refused() -> None:
    feedback = _reject(_valid_envelope("repair-propose-successor"), _context(open_obligations=()))
    assert _codes(feedback) == ["OBLIGATION_NOT_OPEN"]
    assert feedback.problems[0].field_path == "/payload/obligation_ref"


def test_a_successor_that_would_refine_itself_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("repair-propose-successor"),
        _context(plan_shape=PlanShapeView(refinement_cycle=("t-1 is an ancestor of goal.type",))),
    )
    assert _codes(feedback) == ["REFINEMENT_CYCLE"]


def test_a_refinement_that_closes_an_order_cycle_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(plan_shape=PlanShapeView(order_cycle=("a before b before a",))),
    )
    assert _codes(feedback) == ["ORDER_CYCLE"]


def test_a_data_edge_without_a_producer_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(plan_shape=PlanShapeView(data_unbound=("edge d-1 has no producer",))),
    )
    assert _codes(feedback) == ["DATA_UNBOUND"]


def test_a_refinement_that_leaves_an_obligation_uncovered_is_refused() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(plan_shape=PlanShapeView(coverage_gap=("obligation o-9 is uncovered",))),
    )
    assert _codes(feedback) == ["COVERAGE_GAP"]


# --------------------------------------------------------------------------------------
# Sharing: only a CURRENT resolution may be reused (§27).
# --------------------------------------------------------------------------------------


def test_a_resolution_that_is_not_current_cannot_be_reused() -> None:
    feedback = _reject(
        _valid_envelope("bind-existing-goal-reuse"), _context(current_resolutions=())
    )
    assert _codes(feedback) == ["REUSE_NOT_ALLOWED"]
    assert feedback.problems[0].field_path == "/payload/resolution_ref"


def test_a_goal_that_is_no_longer_demanded_cannot_be_shared() -> None:
    feedback = _reject(_valid_envelope("bind-existing-goal-share"), _context(shareable_goals=()))
    assert _codes(feedback) == ["REUSE_NOT_ALLOWED"]
    assert feedback.problems[0].field_path == "/payload/goal_ref"


# --------------------------------------------------------------------------------------
# The operation gate and the internal contract guard.
# --------------------------------------------------------------------------------------


def test_an_unresolved_operation_blocks_refining() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(operations=OperationStateView(unresolved_operations=("op-1",))),
    )
    assert _codes(feedback) == ["OPERATION_UNRESOLVED"]


def test_a_non_envelope_decision_is_an_internal_contract_error() -> None:
    feedback = _reject({"not": "an envelope"}, _context())
    assert _codes(feedback) == ["INTERNAL_CONTRACT_ERROR"]
    assert isinstance(feedback.problems[0].detail, str)


def test_a_caller_that_passes_no_usable_context_raises_rather_than_half_returns() -> None:
    # Without a context there are no §39 budgets to report, so the calling error is
    # raised instead of returned as feedback.  With a context it becomes the code.
    with pytest.raises(ContractError):
        admit_planning_decision(  # type: ignore[arg-type]
            _valid_envelope("refine"), context="not-a-context"
        )


# --------------------------------------------------------------------------------------
# The feedback is model-facing: §39 shape, JSON pointers, no internal fields.
# --------------------------------------------------------------------------------------


def test_the_feedback_round_trips_as_previous_feedback() -> None:
    feedback = _reject(_valid_envelope("refine"), _context(plan_revision=8))
    assert PlanningFeedbackV1.from_json(feedback.to_json()) == feedback
    assert _feedback_json(feedback) == feedback.to_json()
    assert feedback.previous_decision_id == "pd-" + "1" * 24
    assert feedback.budgets == _budgets()


def test_every_problem_is_located_by_a_json_pointer() -> None:
    feedback = _reject(
        _valid_envelope("refine"),
        _context(
            methods=_with_method(
                _method(required_parameters=("target", "mode")), "code.fix-by-patch"
            )
        ),
    )
    for problem in feedback.problems:
        assert problem.field_path is not None
        assert problem.field_path.startswith("/")
        assert problem.code is REJECTION.PARAMETER_INVALID
        assert problem.detail


#: V2 §32's structural system fields, transcribed as the codec transcribes them.
#: A refusal must not put one of these names (or the caller's internal ids) into
#: the feedback the model reads back.
SECTION_32_SYSTEM_FIELDS = frozenset(
    {
        "mission_id",
        "tenant_id",
        "principal",
        "principal_id",
        "scope",
        "scope_id",
        "manager_epoch",
        "budget_account",
        "budget_grant_revision",
        "registry_status",
        "opened_by",
        "authorization_ref",
        "grant_ref",
        "provenance",
        "authored_by",
        "dispatch_generation",
        "plan_revision",
        "expected_plan_revision",
        "operation_id",
        "acceptance_id",
        "approval_id",
        "decision_id",
        "request_id",
    }
)


@pytest.mark.parametrize("code", sorted(ADMISSION_CODE_CASES))
def test_a_refusal_never_mentions_a_system_field_or_an_internal_id(code: str) -> None:
    # §39: the feedback goes straight to the model.  A §32 name (or an internal
    # operation / approval / mission id) in it would either teach the model to
    # write a system field or leak the system's private vocabulary.
    cases = {name: (raw, expect) for name, raw, expect in _h1f_cases()}
    name = next(
        key for key, (_, expect) in cases.items() if expect["expected_code"] == code
    )
    raw = cases[name][0]
    feedback = _reject(_decision_for(raw, code), _context_for(code, raw))
    # §39 fixes the *key names* (including ``previous_decision_id``), so the scan is
    # over the human-facing values: what the model reads as prose, not the shape.
    values: list[str] = []
    for problem in feedback.problems:
        values.extend(
            item
            for item in (problem.field_path, problem.detail, problem.expected, problem.observed)
            if item
        )
        if problem.subject_ref is not None:
            values.append(problem.subject_ref.id)
    # Token equality, not substring: ``scope_epoch_digest`` is one identifier and
    # is *not* the §32 field ``scope``; ``plan_revision`` is.
    tokens = set()
    for value in values:
        tokens.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value))
    assert sorted(tokens & SECTION_32_SYSTEM_FIELDS) == [], code
    blob = json.dumps(values, ensure_ascii=False)
    for internal in ("mission-1", "request-1", "op-1", "approval-1", "intent-1"):
        assert internal not in blob, (code, internal)


def test_the_feedback_carries_no_internal_fields() -> None:
    feedback = _reject(_valid_envelope("refine"), _context(plan_revision=8))
    payload = feedback.to_json()
    assert set(payload) == {
        "previous_decision_id",
        "status",
        "rejection_codes",
        "problems",
        "changed_refs",
        "budgets",
    }
    for problem in payload["problems"]:
        assert set(problem) <= {
            "code",
            "subject_ref",
            "field_path",
            "detail",
            "expected",
            "observed",
        }


def test_the_admitted_decision_is_not_a_commit() -> None:
    # Admission returns a value; it writes nothing, so a second identical call is
    # still an admission rather than a replay.
    context = _context()
    decision = _valid_envelope("refine")
    first = _admit(decision, context)
    second = _admit(decision, context)
    assert isinstance(first, AdmittedPlanningDecision)
    assert isinstance(second, AdmittedPlanningDecision)
    assert first == second


def test_the_admitted_command_records_resolved_method_instances() -> None:
    outcome = _admit(_valid_envelope("repair-replace-method"), _context())
    assert isinstance(outcome, AdmittedPlanningDecision)
    assert [ref.id for ref in outcome.method_instances] == ["mi-1"]
    assert [ref.method_id for ref in outcome.method_refs] == ["code.alt-fix"]


def test_repair_kind_is_gated_by_the_enabled_set() -> None:
    context = _context(enabled_decision_types=H1_ENABLED - {"REPAIR/REPLACE_METHOD"})
    feedback = _reject(_valid_envelope("repair-replace-method"), context)
    assert _codes(feedback) == ["DECISION_NOT_ENABLED_IN_PHASE"]


def test_the_repair_kind_of_the_payload_names_the_enabled_key() -> None:
    # §12 names the repair rows by sub-kind; a request that enabled the successor
    # row must not have its REPLACE_METHOD payload admitted.
    context = _context(enabled_decision_types=H1_ENABLED - {"REPAIR/REPLACE_METHOD"})
    feedback = _reject(_valid_envelope("repair-replace-method"), context)
    assert _codes(feedback) == ["DECISION_NOT_ENABLED_IN_PHASE"]


def test_only_the_admission_codes_are_listed_as_this_slice_owner() -> None:
    assert PlanningDecisionType.REFINE in PlanningDecisionType
