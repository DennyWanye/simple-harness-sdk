# ruff: noqa: E402,I001
"""Focused H3 policy, evidence admission, and recovery contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from agent_orchestrator.contracts.htn import MethodRef, TaskForm
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    H1_DECISION_ENABLEMENT,
    H3_DECISION_ENABLEMENT,
    EvidenceQuestionV1,
    PLANNING_DECISION_V1,
    RequestEvidenceDecision,
)
from agent_orchestrator.contracts.semantic_base import VersionedRef
from agent_orchestrator.knowledge.predicates import PredicateRegistry, PredicateSignature
from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol
from agent_orchestrator.orchestrator.hierarchical_dispatch import METHOD_SELECTION_CALL_CLAIMED
from agent_orchestrator.planning.htn.applicability import ApplicabilityStatus
from agent_orchestrator.planning.htn.method_selection import (
    MethodSelectionCandidateV1,
    MethodSelectionPolicyV1,
    SelectionCallLedger,
    SelectionPolicyMode,
    SelectionRoute,
    select_method,
    selection_identity,
    validate_evidence_request,
)
from agent_orchestrator.planning.htn.planner_package import MethodApplicability

from test_htn_end_to_end import build_world  # noqa: E402


def _candidate(name: str, status: str = "APPLICABLE") -> MethodSelectionCandidateV1:
    return MethodSelectionCandidateV1(method_id=name, report=status)


def test_h3_enables_request_evidence_without_mutating_h1_matrix() -> None:
    assert H1_DECISION_ENABLEMENT["REQUEST_EVIDENCE"].executable is False
    assert H3_DECISION_ENABLEMENT["REQUEST_EVIDENCE"].executable is True
    assert H3_DECISION_ENABLEMENT["REFINE"] == H1_DECISION_ENABLEMENT["REFINE"]


def test_h3_policy_matrix_and_new_protocol_default() -> None:
    none = select_method(
        [_candidate("blocked", "NEEDS_EVIDENCE")], plan_revision=1, evidence_epoch=2
    )
    one = select_method([_candidate("one")], plan_revision=1, evidence_epoch=2)
    many = select_method([_candidate("b"), _candidate("a")], plan_revision=1, evidence_epoch=2)
    assert none.route is SelectionRoute.EVIDENCE_OR_SYNTHESIS
    assert one.route is SelectionRoute.DETERMINISTIC
    assert one.selected_method_id == "one"
    assert many.route is SelectionRoute.MODEL_REFINE

    deterministic = select_method(
        [_candidate("a"), _candidate("b")],
        plan_revision=1,
        evidence_epoch=2,
        policy=MethodSelectionPolicyV1(mode=SelectionPolicyMode.DETERMINISTIC),
    )
    always = select_method(
        [_candidate("a")],
        plan_revision=1,
        evidence_epoch=2,
        policy=MethodSelectionPolicyV1(mode=SelectionPolicyMode.ALWAYS_MODEL),
    )
    legacy = select_method(
        [_candidate("a"), _candidate("b")],
        plan_revision=1,
        evidence_epoch=2,
        policy=MethodSelectionPolicyV1(mode=SelectionPolicyMode.ALWAYS_MODEL),
        legacy=True,
    )
    assert deterministic.route is SelectionRoute.DETERMINISTIC
    assert always.route is SelectionRoute.MODEL_REFINE
    assert legacy.route is SelectionRoute.DETERMINISTIC


def test_h3_legacy_entry_keeps_cardinality_behavior_unchanged() -> None:
    """The production adapter's legacy fence ignores a new-policy override."""

    for candidates, expected in (
        ((), SelectionRoute.EVIDENCE_OR_SYNTHESIS),
        ((_candidate("one"),), SelectionRoute.DETERMINISTIC),
        ((_candidate("a"), _candidate("b")), SelectionRoute.DETERMINISTIC),
    ):
        result = select_method(
            candidates,
            plan_revision=1,
            evidence_epoch=2,
            policy=MethodSelectionPolicyV1.new_protocol(),
            legacy=True,
        )
        assert result.route is expected


def test_h3_selection_identity_is_idempotent_across_restart() -> None:
    candidates = [_candidate("b"), _candidate("a")]
    identity = selection_identity(3, 4, candidates)
    ledger = SelectionCallLedger()
    assert ledger.claim(identity, call_id="selection-1")
    assert not ledger.claim(identity, call_id="selection-2")
    restored = SelectionCallLedger.from_json(ledger.to_json())
    result = select_method(
        candidates,
        plan_revision=3,
        evidence_epoch=4,
        ledger=restored,
    )
    assert result.route is SelectionRoute.SELECTION_ALREADY_ATTEMPTED
    assert restored.call_id(identity) == "selection-1"
    changed = select_method(candidates, plan_revision=3, evidence_epoch=5, ledger=restored)
    assert changed.route is SelectionRoute.MODEL_REFINE


def test_h3_selection_claims_before_returning_a_model_call() -> None:
    ledger = SelectionCallLedger()
    first = select_method(
        [_candidate("a"), _candidate("b")],
        plan_revision=1,
        evidence_epoch=1,
        ledger=ledger,
    )
    second = select_method(
        [_candidate("a"), _candidate("b")],
        plan_revision=1,
        evidence_epoch=1,
        ledger=ledger,
    )
    assert first.route is SelectionRoute.MODEL_REFINE
    assert second.route is SelectionRoute.SELECTION_ALREADY_ATTEMPTED


def test_h3_production_adapter_persists_selection_claim_across_dispatch_restart(tmp_path) -> None:
    """The live dispatch seam rebuilds H3's ledger from Mission events."""

    world = build_world(tmp_path, key="h3-production-ledger")
    bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    occurrence_id = str(
        next(item for item in world.network().occurrences if item.form is TaskForm.COMPOUND)
        .occurrence_id
    )
    reports = (
        MethodApplicability(
            goal_occurrence_id=occurrence_id,
            goal_signature_id="plan.goal",
            method_ref=MethodRef("plan.outer", 1, "a" * 64),
            report=ApplicabilityStatus.APPLICABLE,
        ),
        MethodApplicability(
            goal_occurrence_id=occurrence_id,
            goal_signature_id="plan.goal",
            method_ref=MethodRef("plan.outer.alt", 1, "b" * 64),
            report=ApplicabilityStatus.APPLICABLE,
        ),
    )

    first = world.dispatch.select_method_candidates(world.mission.id, reports=reports)
    assert first[occurrence_id].route is SelectionRoute.MODEL_REFINE
    assert not world.events(METHOD_SELECTION_CALL_CLAIMED)
    # Prompt inspection is read-only; only the request owner reserves the call.
    claimed = world.dispatch.select_method_candidates(
        world.mission.id, reports=reports, persist_claims=True
    )
    assert claimed[occurrence_id].identity == first[occurrence_id].identity
    assert len(world.events(METHOD_SELECTION_CALL_CLAIMED)) == 1

    # A fresh HierarchicalDispatch represents a restarted process; it must observe
    # the durable claim instead of opening another model-selection call.
    restarted = world.reopen()
    second = restarted.dispatch.select_method_candidates(restarted.mission.id, reports=reports)
    assert second[occurrence_id].route is SelectionRoute.SELECTION_ALREADY_ATTEMPTED
    assert len(restarted.events(METHOD_SELECTION_CALL_CLAIMED)) == 1


def _evidence_request(predicate_key: str) -> RequestEvidenceDecision:
    return RequestEvidenceDecision(
        questions=(
            EvidenceQuestionV1(
                predicate_key=predicate_key,
                arguments={"subject": "workspace"},
                purpose="choose a method",
                blocking=True,
            ),
        )
    )


def _registry() -> PredicateRegistry:
    registry = PredicateRegistry()
    registry.register(
        PredicateSignature(
            predicate_ref=VersionedRef(id="workspace-readable", version=1, content_hash="a" * 64),
            observer_ids=("workspace-observer",),
            authority_scope="workspace-read",
        )
    )
    return registry


def test_h3_evidence_requires_registered_observer_and_authority() -> None:
    request = _evidence_request("workspace-readable@1")
    registry = _registry()
    assert validate_evidence_request(
        request,
        registry=registry,
        observers={"workspace-readable@1": True},
        authorized_scopes={"workspace-read"},
    ) == request
    with pytest.raises(ContractError, match="not registered"):
        validate_evidence_request(
            _evidence_request("unknown@1"),
            registry=registry,
            observers={"unknown@1": True},
            authorized_scopes={"workspace-read"},
        )
    with pytest.raises(ContractError, match="no observer"):
        validate_evidence_request(
            request,
            registry=registry,
            observers={},
            authorized_scopes={"workspace-read"},
        )
    with pytest.raises(ContractError, match="outside the caller authority"):
        validate_evidence_request(
            request,
            registry=registry,
            observers={"workspace-readable@1": True},
            authorized_scopes=set(),
        )


def test_h3_evidence_contract_caps_questions_and_rejects_duplicates() -> None:
    questions = tuple(
        EvidenceQuestionV1(
            predicate_key=f"workspace-readable@{index}",
            arguments={"subject": "workspace"},
            purpose="choose a method",
            blocking=True,
        )
        for index in range(1, 10)
    )
    with pytest.raises(ContractError, match="more than 8"):
        RequestEvidenceDecision(questions=questions)
    duplicate = RequestEvidenceDecision(
        questions=(
            EvidenceQuestionV1("workspace-readable@1", {"subject": "workspace"}, "x", True),
            EvidenceQuestionV1("workspace-readable@1", {"subject": "workspace"}, "y", True),
        )
    )
    with pytest.raises(ContractError, match="repeats"):
        validate_evidence_request(
            duplicate,
            registry=_registry(),
            observers={"workspace-readable@1": True},
            authorized_scopes={"workspace-read"},
        )


def test_h3_real_hierarchical_dispatch_routes_zero_one_and_many_candidates(
    tmp_path, monkeypatch
) -> None:
    """The production dispatch adapter feeds one frozen report set into H3 policy."""

    world = build_world(tmp_path, key="h3-selection-dispatch")
    bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    occurrence_id = str(world.network().occurrences[0].occurrence_id)
    method_a = MethodRef("h3.method-a", 1, "a" * 64)
    method_b = MethodRef("h3.method-b", 1, "b" * 64)

    def report(reference: MethodRef) -> MethodApplicability:
        return MethodApplicability(
            goal_occurrence_id=occurrence_id,
            goal_signature_id="plan.goal",
            method_ref=reference,
            report=ApplicabilityStatus.APPLICABLE,
        )

    from agent_orchestrator.planning.htn import method_selection as selection_module

    original = selection_module.select_method
    calls: list[tuple[object, ...]] = []

    def spy(candidates, **kwargs):
        calls.append(tuple(candidates))
        return original(candidates, **kwargs)

    monkeypatch.setattr(selection_module, "select_method", spy)

    zero = world.dispatch.select_method_candidates(world.mission.id, reports=())
    one = world.dispatch.select_method_candidates(world.mission.id, reports=(report(method_a),))
    many = world.dispatch.select_method_candidates(
        world.mission.id, reports=(report(method_b), report(method_a))
    )

    assert zero[occurrence_id].route is SelectionRoute.EVIDENCE_OR_SYNTHESIS
    assert one[occurrence_id].route is SelectionRoute.DETERMINISTIC
    assert one[occurrence_id].selected_method_id == "h3.method-a"
    assert many[occurrence_id].route is SelectionRoute.MODEL_REFINE
    assert len(calls) == 3
    assert [tuple(sorted(candidate.method_id for candidate in items)) for items in calls] == [
        (),
        ("h3.method-a",),
        ("h3.method-a", "h3.method-b"),
    ]
