"""H1-H new-protocol actions may not bypass the operation-link handoff gate."""
# ruff: noqa: E402 -- shared step07 fixture path is installed before imports.

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest

_STEP07 = Path(__file__).resolve().parent.parent / "step07"
if str(_STEP07) not in sys.path:
    sys.path.insert(0, str(_STEP07))

from helpers_step07 import ENABLED, candidate, ledger_service
from test_htn_store import envelope
from operation_completion.operation_runtime_fixture import materialized_file_publish

from agent_orchestrator.contracts.planning_decisions import PLANNING_DECISION_V1
from agent_orchestrator.orchestrator.commit_service import mission_account
from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol
from agent_orchestrator.runtime.actions import ActionExecutor
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from simple_harness.contracts import canonical_json


def _propose_read(service, mission, task, connectors):
    return service.propose_action(
        candidate(operation="read"),
        mission_id=mission.id,
        task_id=task.id,
        result_id="result-h1h-handoff",
        attempt_id=f"{task.id}:attempt-h1h-handoff",
        artifact_id="artifact-h1h-handoff",
        artifact_hash="a" * 64,
        connectors=connectors,
        deployment=ENABLED,
    )


def _handoff(service, action, connectors):
    return service.begin_handoff(
        action["action_key"],
        owner="h1h-handoff-owner",
        lease_seconds=30.0,
        connectors=connectors,
        deployment=ENABLED,
    )


def _assert_not_handed_off(service, mission_id: str, action_key: str) -> None:
    stored = service.store.get_action(action_key)
    assert stored is not None
    assert stored["state"] == "PROPOSED"
    assert stored["handoffs"] == 0
    assert service.ledger.reservation(f"action:{action_key}") is None
    assert not [
        event
        for event in service.store.list_events(mission_id)
        if event.type == "ActionHandedOff" and event.payload["action_key"] == action_key
    ]


def _store_link(service, mission, action, *, defect: str) -> None:
    task = service.store.get_task(action["task_id"])
    assert task is not None
    frozen = dataclasses.replace(envelope(), mission_id=mission.id, scope_id="mission")
    HtnStore(service.store).bind_operation(frozen, principal_id="origin-principal")
    binding = PlanningAdmissionStore(service.store).get_operation_binding(
        str(frozen.operation_occurrence_id)
    )
    assert binding is not None
    link = {
        "operation_id": binding["operation_id"],
        "request_hash": binding["request_hash"],
        "operation_occurrence_id": binding["operation_occurrence_id"],
        "mission_id": binding["mission_id"],
        "envelope_hash": binding["envelope_hash"],
        "principal_id": binding["principal_id"],
        "scope_id": binding["scope_id"],
        "obligation_id": binding["obligation_id"],
        "producer_task_id": task.id,
        "producer_htn_occurrence_id": "occ-h1h-handoff",
        "producer_contract_revision": 1,
        "producer_plan_revision": 1,
        "action_key": action["action_key"],
        "action_id": action["action_id"],
        "action_version": action["version"],
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "provenance_receipt_id": "receipt-h1h-handoff",
        "link_hash": hashlib.sha256(b"h1h-handoff-link").hexdigest(),
        "link_json": "{}",
    }
    if defect == "action_id":
        link["action_id"] = "wrong-action-id"
    elif defect == "envelope_hash":
        link["envelope_hash"] = "f" * 64
    link["link_json"] = canonical_json(link)
    admission = PlanningAdmissionStore(service.store)
    admission.put_operation_action_link(link)
    if defect == "mission":
        # The real FK correctly forbids a foreign Mission in the typed column.
        # Simulate legacy/corrupt JSON only, which is what the Store reader parses.
        corrupted = {**link, "mission_id": "foreign-mission"}
        corrupted["link_json"] = canonical_json(corrupted)
        with service.store.transaction() as connection:
            connection.execute(
                "UPDATE planning_operation_action_links SET link_json=? WHERE operation_id=?",
                (corrupted["link_json"], link["operation_id"]),
            )


def test_new_protocol_missing_marker_and_link_refuses_without_handoff_side_effect(tmp_path) -> None:
    service, mission, tasks, config, connectors, _ = ledger_service(tmp_path)
    bind_planning_protocol(service.store, mission.id, PLANNING_DECISION_V1)
    action = _propose_read(service, mission, tasks["A"], connectors)

    handed, reason = _handoff(service, action, connectors)

    assert handed is None
    assert reason == "operation_link_missing"
    assert config.state()["applied_count"] == 0
    _assert_not_handed_off(service, mission.id, action["action_key"])


def test_legacy_unlinked_action_keeps_existing_handoff_behavior(tmp_path) -> None:
    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    action = _propose_read(service, mission, tasks["A"], connectors)

    handed, reason = _handoff(service, action, connectors)

    assert reason is None
    assert handed is not None and handed["state"] == "HANDED_OFF"


@pytest.mark.parametrize("marker", (None, {"operation_id": "forged"}))
def test_new_protocol_marker_deletion_or_tampering_cannot_bypass_link(tmp_path, marker) -> None:
    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    bind_planning_protocol(service.store, mission.id, PLANNING_DECISION_V1)
    action = _propose_read(service, mission, tasks["A"], connectors)
    stored = service.store.get_action(action["action_key"])
    assert stored is not None
    stored.pop("planning_origin", None)
    if marker is not None:
        stored["planning_origin"] = marker
    service.store.put_action(stored)

    handed, reason = _handoff(service, action, connectors)

    assert handed is None
    assert reason == "operation_link_missing"
    _assert_not_handed_off(service, mission.id, action["action_key"])


@pytest.mark.parametrize("defect", ("mission", "action_id", "envelope_hash"))
def test_new_protocol_bad_bridge_identity_refuses_handoff(tmp_path, defect: str) -> None:
    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    bind_planning_protocol(service.store, mission.id, PLANNING_DECISION_V1)
    action = _propose_read(service, mission, tasks["A"], connectors)
    _store_link(service, mission, action, defect=defect)

    handed, reason = _handoff(service, action, connectors)

    assert handed is None
    assert reason in {"operation_link_missing", "operation_link_mismatch"}
    _assert_not_handed_off(service, mission.id, action["action_key"])


def test_o06_executor_weak_reconcile_rehandoff_cannot_call_connector(tmp_path) -> None:
    fixture = materialized_file_publish(tmp_path)
    service, mission = fixture.world.service, fixture.world.mission
    action, connectors = fixture.action, fixture.connectors
    stored = service.store.get_action(action["action_key"])
    assert stored is not None
    stored.update(
        state="UNKNOWN",
        handoffs=1,
        reconcile="CONFIRMED_NOT_STARTED",
        reconciliation_proof={
            "action_key": action["action_key"],
            "idempotency_key": action["idempotency_key"],
            "covered_handoffs": [1],
            "authoritative_not_applied": False,
            "all_handoffs_covered": False,
            "no_late_apply_proven": False,
        },
    )
    service.store.put_action(stored)
    subject_id = f"action:{action['action_key']}"
    with service.store.transaction():
        service.ledger.reserve(
            account_id=mission_account(mission.id),
            subject_id=subject_id,
            tokens=0,
            cost_micros=int(connectors["file_publish"].operations["publish"].cost_micros_ceiling or 0),
            tool_calls=1,
            counts_attempt=False,
            mission_id=mission.id,
        )
    before_reservation = service.ledger.reservation(subject_id)
    assert before_reservation is not None and before_reservation["state"] == "RESERVED"
    executor = ActionExecutor(service, connectors, fixture.deployment, owner="h1h-o06")

    assert asyncio.run(executor.hand_off(action["action_key"], rehandoff=True)) is None
    assert (
        executor.last_refusal[action["action_key"]]
        == "rehandoff_needs_authoritative_not_applied_proof"
    )
    after = service.store.get_action(action["action_key"])
    assert after is not None and after["handoffs"] == 1
    assert after["state"] == "UNKNOWN"
    after_reservation = service.ledger.reservation(subject_id)
    assert after_reservation == before_reservation
    assert after_reservation is not None and after_reservation["state"] == "RESERVED"
    assert not fixture.publish.ledger_path.exists()
    assert not list(fixture.publish.root.rglob("*"))


def test_o06_executor_empty_lookup_rehandoff_keeps_new_protocol_hold(tmp_path) -> None:
    fixture = materialized_file_publish(tmp_path)
    service, mission = fixture.world.service, fixture.world.mission
    action, connectors = fixture.action, fixture.connectors
    stored = service.store.get_action(action["action_key"])
    assert stored is not None
    stored.update(state="UNKNOWN", handoffs=1)
    stored.pop("reconcile", None)
    stored.pop("reconciliation_proof", None)
    service.store.put_action(stored)

    subject_id = f"action:{action['action_key']}"
    with service.store.transaction():
        service.ledger.reserve(
            account_id=mission_account(mission.id),
            subject_id=subject_id,
            tokens=0,
            cost_micros=int(connectors["file_publish"].operations["publish"].cost_micros_ceiling or 0),
            tool_calls=1,
            counts_attempt=False,
            mission_id=mission.id,
        )
    before_reservation = service.ledger.reservation(subject_id)
    assert before_reservation is not None and before_reservation["state"] == "RESERVED"
    assert not fixture.publish.ledger_path.exists()

    executor = ActionExecutor(
        service, connectors, fixture.deployment, owner="h1h-o06-empty"
    )
    reconciled = asyncio.run(executor.reconcile(mission.id))

    assert [(item["state"], item["handoffs"]) for item in reconciled] == [("UNKNOWN", 1)]
    after = service.store.get_action(action["action_key"])
    assert after is not None
    assert after["reconcile"] == "STILL_UNKNOWN"
    assert "reconciliation_proof" not in after
    assert action["action_key"] not in executor.last_refusal  # no attempted rehandoff

    assert service.ledger.reservation(subject_id) == before_reservation
    assert service.ledger.reservation(subject_id)["state"] == "RESERVED"
    assert not fixture.publish.ledger_path.exists()
    assert not list(fixture.publish.root.rglob("*"))

    from agent_orchestrator.runtime.planning_operations import (
        OperationEffect,
        SourceUnavailable,
        StoreOperationReader,
        build_operation_snapshot,
        operation_gate,
    )

    snapshot = build_operation_snapshot(mission.id, reader=StoreOperationReader(service.store))
    assert snapshot.effects == ((fixture.action["planning_origin"]["operation_id"], OperationEffect.UNRESOLVED),)
    with pytest.raises(SourceUnavailable, match="operation_unresolved"):
        operation_gate(snapshot)
