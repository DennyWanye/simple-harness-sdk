"""Focused Store-backed coverage for H1-H Blocker O matrix gaps.

These cases intentionally exercise ``StoreOperationReader`` and the immutable
operation/action tables.  They do not replace the missing production caller
that supplies ``planning_origin`` to ``propose_action`` (O09).
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
from test_htn_store import envelope
from test_plan_commits import _world

from agent_orchestrator.runtime.planning_operations import (
    OperationEffect,
    SourceUnavailable,
    StoreOperationReader,
    build_operation_snapshot,
    operation_gate,
)
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bound_store_sources(tmp_path):
    """Use the real operation binding producer and Store-backed reader."""

    world = _world(tmp_path, key="h1h-operation-matrix")
    origin = dataclasses.replace(
        envelope(),
        mission_id=world.mission.id,
        scope_id=world.principal.scope_id,
    )
    world.semantics.bind_operation(origin, principal_id=world.principal.principal_id)
    binding = PlanningAdmissionStore(world.store).get_operation_binding(
        str(origin.operation_occurrence_id)
    )
    assert binding is not None

    action = {
        "mission_id": world.mission.id,
        "action_key": "operation-action:v1",
        "action_id": "operation-action",
        "version": 1,
        "params_hash": _digest("params"),
        "idempotency_key": "operation-action:v1",
        "state": "FAILED",
        "handoffs": 0,
        "history": [],
        "receipt": None,
    }
    world.store.put_action(action)
    link = {
        "operation_id": binding["operation_id"],
        "request_hash": binding["request_hash"],
        "operation_occurrence_id": binding["operation_occurrence_id"],
        "mission_id": binding["mission_id"],
        "envelope_hash": binding["envelope_hash"],
        "principal_id": binding["principal_id"],
        "scope_id": binding["scope_id"],
        "obligation_id": binding["obligation_id"],
        "producer_task_id": "task-origin",
        "producer_htn_occurrence_id": "htn-origin",
        "producer_contract_revision": 1,
        "producer_plan_revision": 1,
        "action_key": action["action_key"],
        "action_id": action["action_id"],
        "action_version": action["version"],
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "provenance_receipt_id": "receipt-origin",
        "link_hash": _digest("operation-link"),
        "link_json": "{}",
    }
    # Store the exact canonical link body because this is the persistence API
    # used by the real action writer once it has an authoritative origin.
    from simple_harness.contracts import canonical_json

    link["link_json"] = canonical_json(link)
    PlanningAdmissionStore(world.store).put_operation_action_link(link)
    return world, action, link


def test_o01_store_complete_empty_has_digest_and_read_error_is_not_empty(
    tmp_path, monkeypatch
) -> None:
    world = _world(tmp_path, key="h1h-o01")
    reader = StoreOperationReader(world.store)

    complete_empty = build_operation_snapshot(world.mission.id, reader=reader)
    assert complete_empty.complete_empty is True
    assert complete_empty.read_digest

    def broken_bindings(self, mission_id: str):
        del self, mission_id
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(PlanningAdmissionStore, "list_operation_bindings", broken_bindings)
    with pytest.raises(SourceUnavailable, match="operation_read_failed"):
        build_operation_snapshot(world.mission.id, reader=reader)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("action_key", "operation_mapping_incomplete"),
        ("action_version", "action_link_mismatch"),
        ("params_hash", "action_link_mismatch"),
        ("dangling_link", "operation_mapping_incomplete"),
    ),
)
def test_o04_store_identity_or_link_fault_is_source_unavailable(
    tmp_path, mutation: str, reason: str
) -> None:
    world, action, link = _bound_store_sources(tmp_path)
    adapter = PlanningAdmissionStore(world.store)
    from simple_harness.contracts import canonical_json

    if mutation == "dangling_link":
        # The production schema rightly prevents this state.  Temporarily disable
        # the SQLite constraint *before* the test transaction to emulate legacy
        # corruption, then restore it before the production reader is reopened.
        world.store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with world.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM actions WHERE action_key=?", (action["action_key"],)
                )
        finally:
            world.store.connection.execute("PRAGMA foreign_keys = ON")
    else:
        with world.store.transaction() as connection:
            if mutation == "action_key":
                broken_link = {**link, "action_key": "wrong-action:v1"}
            elif mutation == "action_version":
                broken_link = {**link, "action_version": 2}
            else:
                broken_link = {**link, "params_hash": _digest("wrong-params")}
            broken_link["link_json"] = canonical_json(broken_link)
            connection.execute(
                "UPDATE planning_operation_action_links SET link_json=? WHERE operation_id=?",
                (broken_link["link_json"], link["operation_id"]),
            )

    # The producer reads only real Store rows.  It must never repair any of
    # these bad identities by choosing a latest action or returning an empty set.
    with pytest.raises(SourceUnavailable, match=reason):
        build_operation_snapshot(world.mission.id, reader=StoreOperationReader(world.store))
    assert adapter.get_operation_binding(link["operation_occurrence_id"]) is not None


@pytest.mark.parametrize("now_ms", (99, 101))
def test_o05_store_handed_off_lease_never_becomes_not_applied(tmp_path, now_ms: int) -> None:
    world, action, _link = _bound_store_sources(tmp_path)
    handed_off = {
        **action,
        "state": "HANDED_OFF",
        "handoffs": 1,
        "lease_expires_at": 0.1,  # Store timestamps are seconds; now_ms brackets 100 ms.
    }
    world.store.put_action(handed_off)

    snapshot = build_operation_snapshot(
        world.mission.id,
        reader=StoreOperationReader(world.store),
        now_ms=now_ms,
    )

    assert snapshot.effects == (("operation-1", OperationEffect.IN_FLIGHT),)
    with pytest.raises(SourceUnavailable, match="operation_unresolved"):
        operation_gate(snapshot)
