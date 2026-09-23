from __future__ import annotations

import pytest

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.contracts.planning_decisions import PlanningRequestBinding
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.planning_authorization import (
    PlanPrincipal,
    StorePlanningAuthorityReader,
    build_planning_authorization,
    check_planning_authorization,
)
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import Store, StoreConflict


def _mission() -> Mission:
    return Mission(
        id="m-auth",
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(max_tokens=1000, max_attempts=2),
        tenant_id="tenant-a",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key="m-auth",
    )


def _request() -> PlanningRequestBinding:
    return PlanningRequestBinding(
        request_id="req-auth",
        mission_id="m-auth",
        protocol_version="planning-decision-v1",
        package_version=1,
        package_hash="a" * 64,
        base_plan_revision=0,
        requirements_revision=0,
        scope_epoch_digest="b" * 64,
        subject_bindings_hash="c" * 64,
        visible_refs_digest="d" * 64,
        prompt_version="p1",
        prompt_hash="e" * 64,
        created_at=2.0,
        intent_id="intent-auth",
    )


@pytest.fixture
def store(tmp_path):
    value = Store.open(tmp_path / "orch.db")
    value.insert_mission(_mission(), spec_hash="f" * 64)
    value.connection.execute(
        "INSERT INTO mission_planning_protocols VALUES (?,?,?,?,?,?)",
        ("m-auth", "planning-decision-v1", 1, "p1", "g" * 64, 2.0),
    )
    PlanningDecisionStore(value).insert_planning_request(_request())
    return value


def test_issue_bind_snapshot_revoke_and_renew_are_authoritative(store):
    api = PlanningAuthorizationApi(store, tenant_id="tenant-a", principal=Principal("host"))
    issued = api.issue("m-auth", command_id="cmd-1", request_id="req-auth")
    assert issued.revision == 1 and issued.active
    reader = StorePlanningAuthorityReader(PlanningAdmissionStore(store), store)
    snap = build_planning_authorization(
        "req-auth", read=reader, caller=PlanPrincipal("host"), policy=api.policy, now_ms=2_000
    )
    assert snap.grant_id == issued.grant_id
    assert (
        check_planning_authorization(snap, decision_key="REFINE", now_ms=snap.not_before_ms) is None
    )
    revoked = api.revoke(issued.grant_id, expected_revision=1, command_id="cmd-2", reason="stop")
    assert revoked.revision == 2 and not revoked.active
    stale = build_planning_authorization(
        "req-auth",
        read=reader,
        caller=PlanPrincipal("host"),
        policy=api.policy,
        now_ms=int(store.now * 1000),
    )
    assert (
        check_planning_authorization(stale, decision_key="REFINE", now_ms=int(store.now * 1000))
        == "REQUEST_BINDING_STALE"
    )


def test_issue_command_replay_and_changed_input_conflict(store):
    api = PlanningAuthorizationApi(store, tenant_id="tenant-a", principal=Principal("host"))
    first = api.issue("m-auth", command_id="cmd-1")
    replay = api.issue("m-auth", command_id="cmd-1")
    assert replay == first
    with pytest.raises(StoreConflict):
        api.issue("m-auth", command_id="cmd-1", scope_id="other")


def test_issue_command_replay_includes_request_binding_input(store):
    api = PlanningAuthorizationApi(store, tenant_id="tenant-a", principal=Principal("host"))
    api.issue("m-auth", command_id="cmd-request", request_id="req-auth")
    with pytest.raises(StoreConflict):
        api.issue("m-auth", command_id="cmd-request", request_id=None)


def test_revoke_command_replay_includes_human_reason(store):
    api = PlanningAuthorizationApi(store, tenant_id="tenant-a", principal=Principal("host"))
    issued = api.issue("m-auth", command_id="cmd-reason")
    api.revoke(issued.grant_id, expected_revision=1, command_id="cmd-revoke", reason="stop")
    with pytest.raises(StoreConflict):
        api.revoke(issued.grant_id, expected_revision=1, command_id="cmd-revoke", reason="retry")
