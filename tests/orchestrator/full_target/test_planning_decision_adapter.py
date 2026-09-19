# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-G red tests: adapt admitted decisions into the legacy proposal chain."""

from __future__ import annotations

import json
from typing import Any

import pytest

from simple_harness.contracts import canonical_json

from agent_orchestrator.contracts.htn import (
    EvidenceRef,
    ReadItem,
    ReadItemKind,
    RunningWorkPolicy,
)
from agent_orchestrator.contracts.planning_decisions import (
    BindExistingGoalDecision,
    BindExistingGoalMode,
    BlockedItemV1,
    BlockerCode,
    DeclareBlockedDecision,
    NoChangeDecision,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionType,
    PlanningRefKind,
    PlanningRefV1,
    RefineDecision,
    RepairKind,
    RepairProposeSuccessorDecision,
    RepairReplaceMethodDecision,
    ResumableIf,
    WaitDecision,
    VersionedTypeRefV1,
    canonical_decision_hash,
)
from agent_orchestrator.planning.decision_adapter import (
    AdapterContext,
    AdaptedPlanningOutcome,
    DurableOnly,
    adapt_admitted_decision,
)
from agent_orchestrator.planning.decision_admission import AdmittedPlanningDecision
from agent_orchestrator.planning.planner import parse_plan_proposal


HASH_A = "a" * 64
HASH_B = "b" * 64
MISSION = "mission-adapter"


def _ref(kind: PlanningRefKind | str, ref_id: str, revision: int = 1, digest: str = HASH_A):
    return PlanningRefV1(kind, ref_id, revision, digest)


def _envelope(decision_type: PlanningDecisionType, payload: object) -> PlanningDecisionEnvelopeV1:
    return PlanningDecisionEnvelopeV1(
        schema_version=1,
        decision_type=decision_type,
        subject_key="subject-root",
        rationale="采用当前请求中已准入的决定",
        reason_refs=(),
        assumptions=(),
        payload=payload,
        uncertainties=(),
        alternatives=(),
        replan_triggers=(),
    )


def _context(**overrides: Any) -> AdapterContext:
    fields: dict[str, Any] = {
        "mission_id": MISSION,
        "base_plan_revision": 7,
        "proposal_id": "pd-" + "1" * 24,
        "read_set": (
            ReadItem(ReadItemKind.TASK, "task-root", 1, HASH_A),
            ReadItem(ReadItemKind.OBLIGATION, "obl-root", 1, HASH_B),
            ReadItem(ReadItemKind.METHOD, "code.fix", 2, HASH_A),
        ),
        "trigger_refs": (
            EvidenceRef("observation", "obs-1", 1, HASH_B),
        ),
    }
    fields.update(overrides)
    return AdapterContext(**fields)


def _admitted(envelope: PlanningDecisionEnvelopeV1, **method_refs: Any) -> AdmittedPlanningDecision:
    return AdmittedPlanningDecision(
        decision=envelope,
        subject={
            "subject_key": "subject-root",
            "task_id": "task-root",
            "obligation_id": "obl-root",
        },
        method_refs=tuple(method_refs.values()),
        method_instances=(),
        canonical_hash=canonical_decision_hash(envelope),
    )


def _old_text(proposal_json: dict[str, Any]) -> str:
    return "<plan_revision_proposal>" + json.dumps(proposal_json) + "</plan_revision_proposal>"


def _assert_equivalent(admitted: AdmittedPlanningDecision, context: AdapterContext) -> None:
    outcome = adapt_admitted_decision(admitted, context=context)
    assert isinstance(outcome, AdaptedPlanningOutcome)
    assert outcome.proposal is not None
    old = parse_plan_proposal(
        _old_text(outcome.proposal.to_json()),
        mission_id=context.mission_id,
    )
    assert canonical_json(old.to_json()) == canonical_json(outcome.proposal.to_json())


def test_refine_is_byte_equivalent_to_the_existing_refine_proposal() -> None:
    method = _ref(PlanningRefKind.METHOD, "code.fix", 2, HASH_A)
    decision = _envelope(
        PlanningDecisionType.REFINE,
        RefineDecision(method_ref=method, bindings={"target": "README.md"}),
    )
    _assert_equivalent(_admitted(decision, method=method), _context())


def test_replace_method_is_retire_then_refine_and_requests_stop_then_reconcile() -> None:
    rejected = _ref(PlanningRefKind.METHOD_INSTANCE, "mi-old", 7, HASH_B)
    replacement = _ref(PlanningRefKind.METHOD, "code.alt-fix", 3, HASH_A)
    decision = _envelope(
        PlanningDecisionType.REPAIR,
        RepairReplaceMethodDecision(
            repair_kind=RepairKind.REPLACE_METHOD,
            rejected_method_instance=rejected,
            replacement_method_ref=replacement,
            bindings={"target": "README.md"},
        ),
    )
    outcome = adapt_admitted_decision(
        _admitted(decision, replacement=replacement), context=_context()
    )
    assert outcome.proposal is not None
    assert [item.to_json()["op"] for item in outcome.proposal.operations] == [
        "retire_method",
        "refine",
    ]
    assert outcome.proposal.running_work_policy is RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE
    _assert_equivalent(_admitted(decision, replacement=replacement), _context())


def test_successor_is_byte_equivalent_to_the_existing_successor_proposal() -> None:
    decision = _envelope(
        PlanningDecisionType.REPAIR,
        RepairProposeSuccessorDecision(
            repair_kind=RepairKind.PROPOSE_SUCCESSOR,
            old_task_ref=_ref(PlanningRefKind.TASK, "task-old", 4, HASH_A),
            obligation_ref=_ref(PlanningRefKind.OBLIGATION, "obl-root", 1, HASH_B),
            goal_type_ref=VersionedTypeRefV1("goal.next", 2, HASH_A),
            bindings={"target": "README.md"},
        ),
    )
    _assert_equivalent(_admitted(decision), _context())


@pytest.mark.parametrize(
    ("mode", "resolution"),
    [
        (BindExistingGoalMode.REUSE_ACCEPTED, _ref(PlanningRefKind.RESOLUTION, "resolution-1")),
        (BindExistingGoalMode.SHARE_ACTIVE, None),
    ],
)
def test_both_existing_goal_binding_modes_are_byte_equivalent(
    mode: BindExistingGoalMode, resolution: PlanningRefV1 | None
) -> None:
    decision = _envelope(
        PlanningDecisionType.BIND_EXISTING_GOAL,
        BindExistingGoalDecision(
            mode=mode,
            consumer_method_instance_ref=_ref(PlanningRefKind.METHOD_INSTANCE, "mi-consumer"),
            step="inspect",
            goal_ref=_ref(PlanningRefKind.TASK, "goal-shared"),
            resolution_ref=resolution,
        ),
    )
    _assert_equivalent(_admitted(decision), _context())


def test_wait_and_no_change_are_durable_only_and_never_proposals() -> None:
    wait = _envelope(
        PlanningDecisionType.WAIT,
        WaitDecision(wait_for=(_ref(PlanningRefKind.REVIEW, "review-1"),), reason="等待结果"),
    )
    no_change = _envelope(
        PlanningDecisionType.NO_CHANGE,
        NoChangeDecision(reason="当前计划仍然有效"),
    )
    for decision in (wait, no_change):
        outcome = adapt_admitted_decision(_admitted(decision), context=_context())
        assert outcome.proposal is None
        assert isinstance(outcome.durable_only, DurableOnly)


def test_declare_blocked_preserves_the_minimal_synthesis_stall_signal() -> None:
    decision = _envelope(
        PlanningDecisionType.DECLARE_BLOCKED,
        DeclareBlockedDecision(
            blockers=(BlockedItemV1(BlockerCode.NO_USABLE_METHOD, "没有可用方法"),),
            resumable_if=(ResumableIf.NEW_METHOD_ADMITTED,),
        ),
    )
    outcome = adapt_admitted_decision(_admitted(decision), context=_context())
    assert outcome.proposal is None
    assert outcome.durable_only is not None
    assert outcome.durable_only.blockers == decision.payload.blockers
    assert outcome.durable_only.resumable_if == decision.payload.resumable_if


def test_system_fields_come_from_context_not_model_payload_values() -> None:
    method = _ref(PlanningRefKind.METHOD, "code.fix", 2, HASH_A)
    decision = _envelope(
        PlanningDecisionType.REFINE,
        RefineDecision(
            method_ref=method,
            bindings={
                "mission_id": "model-mission",
                "expected_plan_revision": 999,
                "proposal_id": "model-proposal",
            },
        ),
    )
    outcome = adapt_admitted_decision(
        _admitted(decision, method=method), context=_context()
    )
    assert outcome.proposal is not None
    assert str(outcome.proposal.mission_id) == MISSION
    assert int(outcome.proposal.expected_plan_revision) == 7
    assert outcome.proposal.proposal_id == "pd-" + "1" * 24
    assert outcome.proposal.operations[0].bindings["mission_id"] == "model-mission"


def test_decode_only_decisions_are_programming_errors_at_the_adapter_boundary() -> None:
    decision = _envelope(
        PlanningDecisionType.REQUEST_HUMAN,
        {"question": "q", "options": [], "blocking": True},
    )
    with pytest.raises(Exception, match="not enabled"):
        adapt_admitted_decision(_admitted(decision), context=_context())
