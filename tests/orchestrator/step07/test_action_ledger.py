# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · slice A (D7-2 / D7-3 / D7-4 / D7-6): the action ledger, the risk policy, the
approvals bound to one action version, and the test configuration service."""

from __future__ import annotations

import pytest
from helpers_step07 import ALICE, BOB, ENABLED, candidate, ledger_service

from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy, action_decision
from agent_orchestrator.orchestrator.action_commits import ActionCommitError, CandidateRejected
from agent_orchestrator.runtime.connectors import (
    ConnectorRejected,
    ConnectorTransportError,
    OperationSpec,
    PaymentConnectorStub,
    TestConfigService,
    params_hash,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _propose(
    service, mission, task, connectors, deployment, cand, artifact_hash=HASH_A, result="result-1"
):
    return service.propose_action(
        cand,
        mission_id=mission.id,
        task_id=task.id,
        result_id=result,
        attempt_id=f"{task.id}:attempt-1",
        artifact_id=f"artifact-{artifact_hash[:8]}",
        artifact_hash=artifact_hash,
        connectors=connectors,
        deployment=deployment,
    )


# ------------------------------------------------------------------ D7-3 policy
def test_levels_follow_the_connector_declaration_and_only_rise_with_overrides(tmp_path):
    config = TestConfigService(tmp_path / "c.json")
    assert DeploymentPolicy().enabled_connectors == ()  # D7-3': the switch is off by default
    assert action_decision(DeploymentPolicy(), config, "set").refused == "connector_not_enabled"
    plain = ENABLED
    assert action_decision(plain, config, "read").to_json() == {
        "level": "L0",
        "required_approvals": 0,
        "refused": None,
    }
    assert action_decision(plain, config, "set").required_approvals == 1
    assert action_decision(plain, config, "delete").to_json() == {
        "level": "L3",
        "required_approvals": 2,
        "refused": None,
    }
    raised = DeploymentPolicy(
        enabled_connectors=("test_config",),
        level_overrides=(("test_config.set", "L3"), ("test_config.delete", "L0")),
    )
    assert action_decision(raised, config, "set").level == "L3"  # raised
    assert action_decision(raised, config, "delete").level == "L3"  # an override never lowers
    assert action_decision(plain, config, "rename").refused == "unknown_operation"
    assert action_decision(plain, PaymentConnectorStub(), "pay").refused == "connector_not_enabled"
    enabled = DeploymentPolicy(enabled_connectors=("test_config", "payment"))
    assert (
        action_decision(enabled, PaymentConnectorStub(), "pay").refused
        == "connector_without_idempotency_or_reconciliation"
    )
    capped = DeploymentPolicy(enabled_connectors=("test_config",), max_action_level="L2")
    assert action_decision(capped, config, "delete").refused == "above_deployment_ceiling:L2"


# ------------------------------------------------------------------ D7-6 test service
def test_the_test_service_applies_a_key_once_and_can_be_asked_what_happened(tmp_path):
    service = TestConfigService(tmp_path / "config.json")
    first = service.execute("set", "feature_flags.new_ui", {"value": "on"}, idempotency_key="k-1")
    again = service.execute("set", "feature_flags.new_ui", {"value": "on"}, idempotency_key="k-1")
    assert first.receipt_hash == again.receipt_hash and first.applied and first.after == "on"
    assert service.state()["applied_count"] == 1 and service.state()["config"] == {
        "feature_flags.new_ui": "on"
    }
    assert (
        service.lookup("k-1").receipt_hash == first.receipt_hash and service.lookup("k-2") is None
    )
    assert first.params_hash == params_hash({"value": "on"})
    service.lose_receipt_after_apply = 1
    with pytest.raises(ConnectorTransportError):
        service.execute("set", "feature_flags.dark", {"value": "on"}, idempotency_key="k-3")
    assert (
        service.lookup("k-3") is not None and service.state()["applied_count"] == 2
    )  # applied, receipt lost
    service.reject_next = 1
    with pytest.raises(ConnectorRejected):
        service.execute("set", "feature_flags.x", {"value": "on"}, idempotency_key="k-4")
    assert (
        service.lookup("k-4") is None and service.state()["applied_count"] == 2
    )  # refused: not applied
    read = service.execute("read", "feature_flags.new_ui", {}, idempotency_key="k-5")
    assert (
        read.applied is False and read.after == "on" and service.lookup("k-5") is None
    )  # reads are not ledgered


# ------------------------------------------------------------------ D7-2 ledger
def test_an_l2_candidate_waits_for_one_approval_bound_to_its_hashes(tmp_path):
    service, mission, t, config, connectors, deployment = ledger_service(tmp_path)
    action = _propose(service, mission, t["A"], connectors, deployment, candidate())
    assert (
        action["state"] == "AWAITING_APPROVAL"
        and action["level"] == "L2"
        and action["version"] == 1
    )
    assert action["idempotency_key"] == f"{action['action_id']}:v1"
    assert (
        action["params_hash"] == params_hash({"value": "on"}) and action["artifact_hash"] == HASH_A
    )
    request = service.store.get_approval(action["approval_request_id"])
    assert (
        request["state"] == "PENDING"
        and request["required_count"] == 1
        and request["kind"] == "action"
    )
    assert request["binding"] == {
        "mission_id": mission.id,
        "task_id": t["A"].id,
        "action_id": action["action_id"],
        "version": 1,
        "params_hash": action["params_hash"],
        "artifact_hash": HASH_A,
    }
    types = [e.type for e in service.store.list_events(mission.id)]
    assert "ActionProposed" in types and "ApprovalRequested" in types
    assert config.calls == []  # nothing touched the test service


def test_the_same_candidate_twice_is_one_version_and_a_changed_one_supersedes_it(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    first = _propose(service, mission, t["A"], connectors, deployment, candidate())
    again = _propose(service, mission, t["A"], connectors, deployment, candidate())
    assert again["action_key"] == first["action_key"]  # idempotent: same params, same artifact
    assert len(service.store.list_approvals(mission.id)) == 1
    changed = _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(value="beta"),
        artifact_hash=HASH_B,
        result="result-2",
    )
    assert (
        changed["action_id"] == first["action_id"] and changed["version"] == 2
    )  # same business action
    old = service.store.get_action(first["action_key"])
    assert old["state"] == "SUPERSEDED" and old["superseded_by"] == changed["action_key"]
    assert service.store.get_approval(first["approval_request_id"])["state"] == "SUPERSEDED"
    assert service.store.get_approval(changed["approval_request_id"])["state"] == "PENDING"
    assert service.store.count_events(mission.id, "ApprovalSuperseded") == 1


def test_l0_runs_without_approval_and_policy_refusals_never_enter_the_ledger(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    read = _propose(service, mission, t["A"], connectors, deployment, candidate(operation="read"))
    assert (
        read["state"] == "PROPOSED"
        and read["level"] == "L0"
        and read.get("approval_request_id") is None
    )
    pay = {
        "connector": "payment",
        "operation": "pay",
        "target": "acct-1",
        "params": {"amount": 5},
        "reason": "x",
    }
    with pytest.raises(CandidateRejected) as refused:
        _propose(service, mission, t["A"], connectors, deployment, pay)
    assert refused.value.reason == "connector_not_enabled"
    nowhere = {
        "connector": "nowhere",
        "operation": "set",
        "target": "x",
        "params": {},
        "reason": "x",
    }
    with pytest.raises(CandidateRejected) as missing:
        _propose(service, mission, t["A"], connectors, deployment, nowhere)
    assert missing.value.reason == "connector_not_enabled"
    assert [a["action_key"] for a in service.store.list_actions(mission.id)] == [read["action_key"]]


def test_scope_schema_and_operation_kind_are_checked_before_anything_is_written(tmp_path):
    service, mission, t, config, connectors, deployment = ledger_service(tmp_path)
    with pytest.raises(CandidateRejected) as scope:  # D7-3': outside the Mission's action criteria
        _propose(
            service,
            mission,
            t["A"],
            connectors,
            deployment,
            candidate(target="feature_flags.other"),
        )
    assert scope.value.reason == "action_out_of_scope"
    for smuggled in ({"approved": True}, {"level": "L0"}, {"idempotency_key": "k"}):  # S7-08
        with pytest.raises(CandidateRejected) as schema:
            _propose(service, mission, t["A"], connectors, deployment, {**candidate(), **smuggled})
        assert schema.value.reason == "invalid_candidate"
    spaced = _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(target=" feature_flags . new_ui "),
    )
    plain = _propose(service, mission, t["A"], connectors, deployment, candidate())
    assert (
        spaced["action_key"] == plain["action_key"] and spaced["target"] == "feature_flags.new_ui"
    )
    appender = TestConfigService(
        tmp_path / "e.json",
        operations={"append": OperationSpec("append", "L1", ("value",), kind="event")},
    )
    assert action_decision(ENABLED, appender, "append").refused == "event_operation_not_supported"
    enabled = DeploymentPolicy(enabled_connectors=("test_config", "payment"))
    assert (  # the payment stub stays refused even when a deployment lists it
        action_decision(enabled, PaymentConnectorStub(), "pay").refused
        == "connector_without_idempotency_or_reconciliation"
    )
    assert service.store.count_events(mission.id, "ActionRefused") == 0 and config.calls == []


def _force(service, action_key, state):
    action = service.store.get_action(action_key)
    action["state"] = state
    service.store.put_action(action)


def test_a_version_that_ran_or_is_running_is_never_superseded(tmp_path):
    """D7-2' / review P0-1: reality is not rewritten by the ledger."""

    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    v1 = _propose(service, mission, t["A"], connectors, deployment, candidate())
    _force(service, v1["action_key"], "HANDED_OFF")
    busy = _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(value="beta"),
        artifact_hash=HASH_B,
    )
    assert busy["state"] == "REFUSED" and busy["refused"] == "action_in_flight"
    assert service.store.get_action(v1["action_key"])["state"] == "HANDED_OFF"
    with pytest.raises(ActionCommitError):  # no revocation once handed off
        service.revoke_approval(v1["approval_request_id"], principal=BOB, reason="太晚了")
    _force(service, v1["action_key"], "SUCCEEDED")
    same = _propose(service, mission, t["A"], connectors, deployment, candidate())
    assert same["action_key"] == v1["action_key"]  # the same content: nothing new
    done = _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(value="gamma"),
        artifact_hash="c" * 64,
    )
    assert done["state"] == "REFUSED" and done["refused"] == "action_already_executed"
    assert service.store.get_action(v1["action_key"])["state"] == "SUCCEEDED"
    assert service.store.count_events(mission.id, "ActionRefused") == 2
    assert not any(
        a["state"] in {"AWAITING_APPROVAL", "APPROVED", "PROPOSED"}
        for a in service.store.list_actions(mission.id)
    )


def test_after_a_failure_a_changed_candidate_is_a_new_attempt_that_needs_new_approval(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    v1 = _propose(service, mission, t["A"], connectors, deployment, candidate())
    _force(service, v1["action_key"], "FAILED")
    v2 = _propose(
        service,
        mission,
        t["A"],
        connectors,
        deployment,
        candidate(value="beta"),
        artifact_hash=HASH_B,
    )
    assert v2["version"] == 2 and v2["state"] == "AWAITING_APPROVAL" and v2["after"] == "FAILED"
    assert v2["idempotency_key"] == f"{v1['action_id']}:v2"
    assert service.store.get_approval(v2["approval_request_id"])["state"] == "PENDING"


def test_a_decision_needs_an_active_mission_and_an_ending_mission_cancels_open_work(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    open_action = _propose(service, mission, t["A"], connectors, deployment, candidate(target="a"))
    ran = _propose(service, mission, t["A"], connectors, deployment, candidate(target="b"))
    service.decide_approval(
        ran["approval_request_id"],
        principal=ALICE,
        decision="grant",
        nonce="n-b",
        deployment=deployment,
    )
    _force(service, ran["action_key"], "UNKNOWN")
    cancelled = service.cancel_open_actions(mission.id, reason="mission_cancelled")
    assert [a["action_key"] for a in cancelled] == [open_action["action_key"]]
    assert service.store.get_approval(open_action["approval_request_id"])["state"] == "CANCELLED"
    assert service.store.get_action(ran["action_key"])["state"] == "UNKNOWN"  # never touched
    assert service.store.get_approval(ran["approval_request_id"])["state"] == "GRANTED"
    with pytest.raises(ActionCommitError):
        service.decide_approval(
            open_action["approval_request_id"],
            principal=ALICE,
            decision="grant",
            nonce="n-a",
            deployment=deployment,
        )
    late = _propose(service, mission, t["A"], connectors, deployment, candidate(target="c"))
    from agent_orchestrator.contracts import MissionStopReason

    service.fail_mission(
        mission.id, stop_reason=MissionStopReason.MISSION_CRITERIA_UNMET, detail={}
    )
    with pytest.raises(ActionCommitError):  # D7-4': no grant on a Mission that ended
        service.decide_approval(
            late["approval_request_id"],
            principal=ALICE,
            decision="grant",
            nonce="n-c",
            deployment=deployment,
        )
    with pytest.raises(CandidateRejected):
        _propose(
            service,
            mission,
            t["A"],
            connectors,
            deployment,
            candidate(target="a", value="x"),
            artifact_hash=HASH_B,
        )


# ------------------------------------------------------------------ D7-4 approvals
def test_a_grant_counts_once_per_nonce_and_only_a_human_principal_decides(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    action = _propose(service, mission, t["A"], connectors, deployment, candidate())
    request_id = action["approval_request_id"]
    with pytest.raises(ValueError):
        Principal("model", kind="agent")
    granted, receipt = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-1", deployment=deployment
    )
    replay, receipt_again = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-1", deployment=deployment
    )
    assert granted["state"] == "GRANTED" and receipt == receipt_again and len(receipt) == 64
    assert (
        len(service.store.list_decisions(request_id)) == 1
    )  # the replayed receipt is not counted twice
    assert service.store.get_action(action["action_key"])["state"] == "APPROVED"
    assert service.store.count_events(mission.id, "ApprovalGranted") == 1


def test_reject_revoke_and_expiry_each_close_the_request_without_success(tmp_path):
    clock = {"now": 1_000.0}
    deployment = DeploymentPolicy(enabled_connectors=("test_config",), approval_ttl_seconds=60.0)
    service, mission, t, _config, connectors, _ = ledger_service(
        tmp_path, deployment=deployment, clock=lambda: clock["now"]
    )
    a = _propose(service, mission, t["A"], connectors, deployment, candidate(target="a"))
    b = _propose(service, mission, t["A"], connectors, deployment, candidate(target="b"))
    c = _propose(service, mission, t["A"], connectors, deployment, candidate(target="c"))
    rejected, _ = service.decide_approval(
        a["approval_request_id"],
        principal=ALICE,
        decision="reject",
        nonce="n-a",
        deployment=deployment,
        reason="不同意",
    )
    assert (
        rejected["state"] == "REJECTED"
        and service.store.get_action(a["action_key"])["state"] == "REJECTED"
    )
    service.decide_approval(
        b["approval_request_id"],
        principal=ALICE,
        decision="grant",
        nonce="n-b",
        deployment=deployment,
    )
    revoked = service.revoke_approval(b["approval_request_id"], principal=BOB, reason="撤回")
    assert (
        revoked["state"] == "REVOKED"
        and service.store.get_action(b["action_key"])["state"] == "REVOKED"
    )
    clock["now"] += 61.0
    expired = service.expire_approvals(mission.id)
    assert [r["request_id"] for r in expired] == [c["approval_request_id"]]
    assert service.store.get_action(c["action_key"])["state"] == "EXPIRED"
    with pytest.raises(Exception):  # a closed request takes no more decisions
        service.decide_approval(
            a["approval_request_id"],
            principal=BOB,
            decision="grant",
            nonce="n-late",
            deployment=deployment,
        )
    types = [e.type for e in service.store.list_events(mission.id)]
    assert {"ApprovalRejected", "ApprovalRevoked", "ApprovalExpired"} <= set(types)


def test_l3_needs_two_independent_grants_from_different_people_by_default(tmp_path):
    service, mission, t, _config, connectors, deployment = ledger_service(tmp_path)
    action = _propose(
        service, mission, t["A"], connectors, deployment, candidate(operation="delete")
    )
    request_id = action["approval_request_id"]
    assert service.store.get_approval(request_id)["required_count"] == 2
    one, _ = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-1", deployment=deployment
    )
    assert (
        one["state"] == "PENDING"
        and service.store.get_action(action["action_key"])["state"] == "AWAITING_APPROVAL"
    )
    same_person, _ = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-2", deployment=deployment
    )
    assert (
        same_person["state"] == "PENDING" and same_person["grant_count"] == 1
    )  # same person does not count twice
    two, _ = service.decide_approval(
        request_id, principal=BOB, decision="grant", nonce="n-3", deployment=deployment
    )
    assert (
        two["state"] == "GRANTED"
        and service.store.get_action(action["action_key"])["state"] == "APPROVED"
    )


def test_without_the_distinct_people_rule_two_distinct_receipts_of_one_person_count(tmp_path):
    relaxed = DeploymentPolicy(enabled_connectors=("test_config",), l3_distinct_principals=False)
    service, mission, t, _config, connectors, _ = ledger_service(tmp_path, deployment=relaxed)
    action = _propose(service, mission, t["A"], connectors, relaxed, candidate(operation="delete"))
    request_id = action["approval_request_id"]
    service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-1", deployment=relaxed
    )
    replay, _ = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-1", deployment=relaxed
    )
    assert (
        replay["state"] == "PENDING" and replay["grant_count"] == 1
    )  # a replayed receipt never counts twice
    done, _ = service.decide_approval(
        request_id, principal=ALICE, decision="grant", nonce="n-2", deployment=relaxed
    )
    assert done["state"] == "GRANTED" and done["grant_count"] == 2
