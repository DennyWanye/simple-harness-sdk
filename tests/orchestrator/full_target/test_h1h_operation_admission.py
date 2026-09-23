from __future__ import annotations

import hashlib

import pytest

from agent_orchestrator.planning.plan_preview import (
    RuntimeWorkSnapshot as PreviewRuntimeWorkSnapshot,
)
from agent_orchestrator.runtime.planning_operations import (
    OperationEffect,
    SourceUnavailable,
    build_operation_snapshot,
    operation_gate,
)
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.store import StoreConflict


def _action(key: str = "action-1:v1", *, state: str = "FAILED", handoffs: int = 0) -> dict:
    return {
        "mission_id": "mission-1",
        "action_key": key,
        "action_id": key.split(":", 1)[0],
        "version": 1,
        "params_hash": hashlib.sha256(b"{}").hexdigest(),
        "idempotency_key": f"{key}:id",
        "state": state,
        "handoffs": handoffs,
        "history": [],
        "receipt": None,
    }


def _sources(action: dict | None = None) -> dict:
    return {
        "complete_read": True,
        "bindings": [
            {
                "mission_id": "mission-1",
                "operation_id": "operation-1",
                "operation_occurrence_id": "occ-1",
                "request_hash": "a" * 64,
                "envelope_hash": "b" * 64,
            }
        ],
        "links": [
            {
                "mission_id": "mission-1",
                "operation_id": "operation-1",
                "operation_occurrence_id": "occ-1",
                "request_hash": "a" * 64,
                "envelope_hash": "b" * 64,
                "action_key": "action-1:v1",
                "action_id": "action-1",
                "action_version": 1,
                "params_hash": hashlib.sha256(b"{}").hexdigest(),
                "idempotency_key": "action-1:v1:id",
            }
        ],
        "actions": [_action() if action is None else action],
    }


def test_operation_snapshot_uses_explicit_action_key_and_digest() -> None:
    snapshot = build_operation_snapshot("mission-1", reader=_sources())
    assert snapshot.effects == (("operation-1", OperationEffect.CONFIRMED_NOT_APPLIED),)
    assert snapshot.read_digest


def test_missing_bridge_is_source_unavailable_instead_of_latest_join() -> None:
    source = _sources()
    source["links"] = []
    with pytest.raises(SourceUnavailable, match="operation_mapping_incomplete"):
        build_operation_snapshot("mission-1", reader=source)


def test_lease_expiry_does_not_prove_not_applied() -> None:
    action = _action(state="HANDED_OFF", handoffs=1)
    snapshot = build_operation_snapshot("mission-1", reader=_sources(action))
    with pytest.raises(SourceUnavailable, match="operation_unresolved"):
        operation_gate(snapshot)
    assert snapshot.effects[0][1] is OperationEffect.IN_FLIGHT


def test_incomplete_reader_is_not_an_empty_snapshot() -> None:
    with pytest.raises(SourceUnavailable, match="operation_read_incomplete"):
        build_operation_snapshot("mission-1", reader={"complete_read": False})


def test_preview_reuses_runtime_work_snapshot_type() -> None:
    from agent_orchestrator.runtime.planning_operations import RuntimeWorkSnapshot

    assert PreviewRuntimeWorkSnapshot is RuntimeWorkSnapshot


def test_binding_json_cannot_shadow_authoritative_columns(tmp_path) -> None:
    from agent_orchestrator.storage.store import Store

    store = Store.open(tmp_path / "orch.db")
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?)",
            ("m", "tenant", "m-key", "ACTIVE", 1, "h", "{}", 0.0, 0.0),
        )
        connection.execute(
            "INSERT INTO planning_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "req",
                "m",
                "planning-decision-v1",
                1,
                "a" * 64,
                0,
                0,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "p",
                "e" * 64,
                "intent",
                0.0,
            ),
        )
        connection.execute(
            "INSERT INTO planning_lane_grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "grant",
                1,
                "m",
                "tenant",
                "scope",
                "planner",
                "issuer",
                "cmd",
                "f" * 64,
                1,
                '["REFINE"]',
                "1" * 64,
                0,
                100,
                "2" * 64,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO planning_request_authority_bindings VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "req",
                "m",
                "tenant",
                "scope",
                "planner",
                "grant",
                1,
                "2" * 64,
                "3" * 64,
                '{"grant_id":"attacker","mission_id":"other"}',
            ),
        )
    binding = PlanningAdmissionStore(store).get_request_binding("req")
    assert binding is not None
    assert binding["grant_id"] == "grant"
    assert binding["mission_id"] == "m"


def test_admission_check_same_id_different_payload_conflicts(tmp_path) -> None:
    from agent_orchestrator.storage.store import Store

    store = Store.open(tmp_path / "orch.db")
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?)",
            ("m", "tenant", "m-key", "ACTIVE", 1, "h", "{}", 0.0, 0.0),
        )
        connection.execute(
            "INSERT INTO planning_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "req",
                "m",
                "planning-decision-v1",
                1,
                "a" * 64,
                0,
                0,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "p",
                "e" * 64,
                "intent",
                0.0,
            ),
        )
    checks = PlanningAdmissionStore(store)
    base = {
        "check_id": "check",
        "request_id": "req",
        "decision_id": "decision",
        "phase": "PREFLIGHT",
        "snapshot_hash": "a" * 64,
        "decision_hash": "b" * 64,
        "authority_hash": None,
        "operations_hash": None,
        "delta_hash": None,
        "check_schema": "v1",
        "detail_json": "{}",
        "checked_at_ms": 1,
    }
    checks.put_admission_check(base)
    with pytest.raises(StoreConflict):
        checks.put_admission_check({**base, "detail_json": '{"changed":true}'})
