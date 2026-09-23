from __future__ import annotations

import pytest
from test_htn_end_to_end import build_world

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.htn.repair_adapter import RepairEventAdapter
from agent_orchestrator.planning.htn.repair_decision import (
    HumanRequestV1,
    RepairAction,
    RepairActionType,
    RepairCause,
    RepairDecisionStatus,
    RepairTriggerSource,
)


def test_worker_event_enters_auditable_retry_path() -> None:
    result = RepairEventAdapter.dispatch(
        {"type": "WorkerRejected", "payload": {"mission_id": "m1"}, "trigger_refs": ["task-a"]},
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
        all_items=("task-a", "task-b"),
        reverse_data={"task-a": ("task-b",)},
        mission_id="m1",
        plan_revision=4,
        diagnosis=RepairCause.IMPLEMENTATION_DEFECT,
    )
    assert result.request.trigger_source is RepairTriggerSource.WORKER_REJECT
    assert result.request.request_id == result.request.idempotency_key
    assert result.status is RepairDecisionStatus.ADMITTED
    assert result.audit_json()["impact"]["revalidate"] == ["task-a", "task-b"]


@pytest.mark.parametrize(
    ("event_type", "action"),
    [
        ("VerifierAcceptanceRejected", RepairActionType.REFINE_DEEPER),
        ("EvidenceInvalidated", RepairActionType.REQUEST_EVIDENCE),
        ("RuntimeUnavailable", RepairActionType.DECLARE_RUNTIME_BLOCKED),
        ("RequirementsUpdated", RepairActionType.REPLACE_METHOD),
    ],
)
def test_all_other_trigger_sources_use_the_same_dispatch_contract(
    event_type: str, action: RepairActionType
) -> None:
    result = RepairEventAdapter.dispatch(
        {"type": event_type, "trigger_refs": ["task-a"]},
        actions=(RepairAction(action),),
        all_items=("task-a",),
    )
    assert result.request.trigger_source in set(RepairTriggerSource)
    assert result.decision.request.request_id == result.request.request_id


def test_unknown_operation_is_deferred_before_commit_boundary() -> None:
    result = RepairEventAdapter.dispatch(
        {"type": "WorkerRejected", "trigger_refs": ["task-a"]},
        actions=(RepairAction(RepairActionType.REPLACE_METHOD),),
        all_items=("task-a",),
        operation_states={"operation-1": "UNKNOWN"},
    )
    assert result.status is RepairDecisionStatus.DEFERRED
    assert result.impact.unresolved_operations == ("operation-1",)


def test_request_human_stays_blocked_without_authority() -> None:
    result = RepairEventAdapter.dispatch(
        {"type": "RuntimeUnavailable", "trigger_refs": ["task-a"]},
        actions=(RepairAction(RepairActionType.ESCALATE),),
        all_items=("task-a",),
        human_request=HumanRequestV1("Choose a runtime", options=("local", "remote")),
        human_authorized=True,
    )
    assert result.status is RepairDecisionStatus.DEFERRED


def test_unknown_event_is_fail_closed() -> None:
    with pytest.raises(ContractError):
        RepairEventAdapter.dispatch(
            {"type": "TaskCompleted", "trigger_refs": ["task-a"]},
            actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
        )


def test_named_dispatch_helpers_cover_core_repair_paths() -> None:
    event = {"type": "WorkerRejected", "trigger_refs": ["task-a"]}
    assert (
        RepairEventAdapter.retry_same_method(event, all_items=("task-a",))
        .decision.actions[0]
        .action_type
        is RepairActionType.RETRY_SAME_METHOD
    )
    assert (
        RepairEventAdapter.refine_deeper(event, all_items=("task-a",))
        .decision.actions[0]
        .action_type
        is RepairActionType.REFINE_DEEPER
    )
    assert (
        RepairEventAdapter.replace_method(event, all_items=("task-a",))
        .decision.actions[0]
        .action_type
        is RepairActionType.REPLACE_METHOD
    )
    blocked = RepairEventAdapter.declare_runtime_blocked(
        {"type": "RuntimeUnavailable", "trigger_refs": ["task-a"]}, all_items=("task-a",)
    )
    assert blocked.decision.actions[0].action_type is RepairActionType.DECLARE_RUNTIME_BLOCKED


def test_hierarchical_dispatch_exposes_the_h4_event_boundary(tmp_path) -> None:
    world = build_world(tmp_path, key="h4-dispatch-entry")
    result = world.dispatch.dispatch_repair_event(
        world.mission.id,
        {"type": "WorkerRejected", "trigger_refs": ["task-a"]},
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
    )
    assert result.request.mission_id == world.mission.id
    assert result.decision.actions[0].action_type is RepairActionType.RETRY_SAME_METHOD


def test_event_handler_runtime_trigger_persists_h4_audit(tmp_path) -> None:
    from agent_orchestrator.contracts.planning_decisions import PLANNING_DECISION_V1
    from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol

    world = build_world(tmp_path, key="h4-event-handler-runtime")
    bind_planning_protocol(world.store, world.mission.id, PLANNING_DECISION_V1)
    orchestrator = object.__new__(Orchestrator)
    orchestrator._store = world.store
    orchestrator._new_mode = lambda mission: world.dispatch
    orchestrator._note = lambda message: None
    orchestrator._dispatch_h4_repair_trigger(
        world.mission,
        event_type="RuntimeUnavailable",
        trigger_ref=world.mission.id,
        detail={"reason": "provider_outcome_unknown"},
    )
    rows = world.events("PlanningRepairRequested")
    assert len(rows) == 1
    assert rows[0].payload["request"]["trigger_source"] == "RUNTIME_UNAVAILABLE"
    assert "decision" not in rows[0].payload
    assert not world.events("RepairDecisionDispatched")
    # Replayed transport callbacks cannot create another request or choose an action.
    orchestrator._dispatch_h4_repair_trigger(
        world.mission, event_type="RuntimeUnavailable", trigger_ref=world.mission.id,
        detail={"reason": "provider_outcome_unknown"},
    )
    assert len(world.events("PlanningRepairRequested")) == 1
