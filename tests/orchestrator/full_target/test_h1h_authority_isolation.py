"""A03 real Store/API regressions for issuer and tenant isolation."""

from __future__ import annotations

import pytest
from test_h1h_planning_authorization import _mission, _request

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import Store, StoreConflict


@pytest.fixture
def authority_store(tmp_path):
    store = Store.open(tmp_path / "authority-isolation.db")
    store.insert_mission(_mission(), spec_hash="f" * 64)
    store.connection.execute(
        "INSERT INTO mission_planning_protocols VALUES (?,?,?,?,?,?)",
        ("m-auth", "planning-decision-v1", 1, "p1", "g" * 64, 2.0),
    )
    PlanningDecisionStore(store).insert_planning_request(_request())
    return store


def _api(store, *, tenant: str = "tenant-a", principal: str = "host"):
    return PlanningAuthorizationApi(store, tenant_id=tenant, principal=Principal(principal))


def test_a03_foreign_issuer_cannot_replay_issue_receipt_by_naming_original_grantee(
    authority_store,
) -> None:
    original = _api(authority_store).issue(
        "m-auth",
        command_id="issue-a03",
        planner_principal_id="host",
    )
    before = authority_store.connection.total_changes

    with pytest.raises(StoreConflict) as refused:
        _api(authority_store, principal="foreign-host").issue(
            "m-auth",
            command_id="issue-a03",
            planner_principal_id="host",
        )

    assert original.grant_id not in str(refused.value)
    assert original.issuer_receipt_hash not in str(refused.value)
    assert authority_store.connection.total_changes == before


def test_a03_cross_tenant_caller_cannot_replay_revoke_receipt(authority_store) -> None:
    issuer = _api(authority_store)
    grant = issuer.issue("m-auth", command_id="issue-for-revoke")
    receipt = issuer.revoke(
        grant.grant_id,
        expected_revision=grant.revision,
        command_id="revoke-a03",
        reason="stop",
    )
    before = authority_store.connection.total_changes

    with pytest.raises(StoreConflict) as refused:
        _api(authority_store, tenant="tenant-foreign", principal="foreign-host").revoke(
            grant.grant_id,
            expected_revision=grant.revision,
            command_id="revoke-a03",
            reason="stop",
        )

    assert receipt.grant_id not in str(refused.value)
    assert receipt.issuer_receipt_hash not in str(refused.value)
    assert authority_store.connection.total_changes == before


def test_a03_foreign_principal_cannot_bind_another_issuers_grant(authority_store) -> None:
    grant = _api(authority_store).issue("m-auth", command_id="issue-for-binding")
    before = authority_store.connection.total_changes

    with pytest.raises(StoreConflict) as refused:
        _api(authority_store, principal="foreign-host").bind_request(
            "req-auth", grant_id=grant.grant_id
        )

    assert grant.grant_id not in str(refused.value)
    assert grant.issuer_receipt_hash not in str(refused.value)
    assert authority_store.connection.total_changes == before
    assert (
        authority_store.connection.execute(
            "SELECT 1 FROM planning_request_authority_bindings WHERE request_id = ?",
            ("req-auth",),
        ).fetchone()
        is None
    )


def test_a03_original_issuer_can_delegate_bind_and_replay_without_extending_ttl(authority_store):
    issuer = _api(authority_store)
    grant = issuer.issue(
        "m-auth", command_id="delegate-a03", planner_principal_id="planner-service"
    )
    issuer.bind_request("req-auth", grant_id=grant.grant_id)
    before = authority_store.connection.total_changes
    assert (
        issuer.issue("m-auth", command_id="delegate-a03", planner_principal_id="planner-service")
        == grant
    )
    assert authority_store.connection.total_changes == before
    assert (
        authority_store.connection.execute(
            "SELECT planner_principal_id FROM planning_request_authority_bindings "
            "WHERE request_id=?",
            ("req-auth",),
        ).fetchone()[0]
        == "planner-service"
    )
    renewed = issuer.renew(grant.grant_id, expected_revision=1, command_id="renew-a03")
    before = authority_store.connection.total_changes
    assert issuer.renew(grant.grant_id, expected_revision=1, command_id="renew-a03") == renewed
    assert authority_store.connection.total_changes == before


def test_a03_transaction_replay_rechecks_issuer_after_stale_api_lookup(
    authority_store, monkeypatch
):
    from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore

    original = _api(authority_store).issue(
        "m-auth", command_id="concurrent-issue", planner_principal_id="planner-service"
    )
    # Emulate a competing issue arriving after both pre-transaction lookups.
    # The final put_grant transaction still reads the real durable row.
    monkeypatch.setattr(PlanningAdmissionStore, "get_grant_by_command", lambda *a: None)
    monkeypatch.setattr(PlanningAdmissionStore, "get_grant", lambda *a: None)
    before = authority_store.connection.total_changes
    with pytest.raises(StoreConflict) as refused:
        _api(authority_store, principal="foreign-host").issue(
            "m-auth", command_id="concurrent-issue", planner_principal_id="planner-service"
        )
    assert original.grant_id not in str(refused.value)
    assert original.issuer_receipt_hash not in str(refused.value)
    assert authority_store.connection.total_changes == before


@pytest.mark.parametrize("foreign_tenant", ("tenant-a", "tenant-foreign"))
@pytest.mark.parametrize("entry", ("issue", "bind"))
def test_a03_foreign_mission_request_cannot_receive_this_missions_grant(
    authority_store, foreign_tenant, entry
):
    from dataclasses import replace

    other = replace(
        _mission(), id="other-mission", idempotency_key="other", tenant_id=foreign_tenant
    )
    authority_store.insert_mission(other, spec_hash="b" * 64)
    authority_store.connection.execute(
        "INSERT INTO mission_planning_protocols VALUES (?,?,?,?,?,?)",
        (other.id, "planning-decision-v1", 1, "p1", "g" * 64, 2.0),
    )
    PlanningDecisionStore(authority_store).insert_planning_request(
        replace(
            _request(), request_id="other-request", intent_id="other-intent", mission_id=other.id
        )
    )
    api = _api(authority_store)
    grant = api.issue("m-auth", command_id="valid-grant") if entry == "bind" else None
    before = authority_store.connection.total_changes
    with pytest.raises(StoreConflict) as refused:
        if entry == "issue":
            api.issue("m-auth", command_id="cross-mission", request_id="other-request")
        else:
            api.bind_request("other-request", grant_id=grant.grant_id)
    assert "other-mission" not in str(refused.value)
    assert authority_store.connection.total_changes == before
    assert (
        authority_store.connection.execute(
            "SELECT 1 FROM planning_request_authority_bindings WHERE request_id=?",
            ("other-request",),
        ).fetchone()
        is None
    )
