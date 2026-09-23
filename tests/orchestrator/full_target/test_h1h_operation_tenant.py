"""Store-backed O04 mission isolation; it is not a tenant-authorized reader test.

``StoreOperationReader`` accepts only ``mission_id``.  These cases prove that its
three Store queries do not mix two missions in one database.  They intentionally
do not manufacture a tenant argument that the production reader does not have.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
from test_htn_store import envelope
from test_plan_commits import _spec, _world

from agent_orchestrator.runtime.planning_operations import (
    SourceUnavailable,
    StoreOperationReader,
    build_operation_snapshot,
)
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from simple_harness.contracts import canonical_json


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bind_action_and_link(world, mission, *, suffix: str) -> tuple[dict, dict]:
    """Create all three real Store source rows for exactly one Mission."""

    frozen = dataclasses.replace(
        envelope(operation_id=f"operation-{suffix}", occurrence=f"occurrence-{suffix}"),
        mission_id=mission.id,
        scope_id=world.principal.scope_id,
    )
    world.semantics.bind_operation(frozen, principal_id=world.principal.principal_id)
    binding = PlanningAdmissionStore(world.store).get_operation_binding(
        str(frozen.operation_occurrence_id)
    )
    assert binding is not None
    action = {
        "mission_id": mission.id,
        "action_key": f"operation-action-{suffix}:v1",
        "action_id": f"operation-action-{suffix}",
        "version": 1,
        "params_hash": _digest(f"params-{suffix}"),
        "idempotency_key": f"operation-action-{suffix}:v1",
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
        "producer_task_id": f"task-{suffix}",
        "producer_htn_occurrence_id": f"htn-{suffix}",
        "producer_contract_revision": 1,
        "producer_plan_revision": 1,
        "action_key": action["action_key"],
        "action_id": action["action_id"],
        "action_version": action["version"],
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "provenance_receipt_id": f"receipt-{suffix}",
        "link_hash": _digest(f"link-{suffix}"),
        "link_json": "{}",
    }
    link["link_json"] = canonical_json(link)
    PlanningAdmissionStore(world.store).put_operation_action_link(link)
    return action, link


def _two_tenant_missions(tmp_path):
    world = _world(tmp_path, key="h1h-tenant-a")
    mission_b, _ = world.service.create_mission(
        dataclasses.replace(_spec("h1h-tenant-b", mode="hierarchical"), tenant_id="tenant-b")
    )
    return world, mission_b


def test_o04_store_reader_mission_filters_isolate_tenant_a_and_b(tmp_path) -> None:
    world, mission_b = _two_tenant_missions(tmp_path)
    action_a, link_a = _bind_action_and_link(world, world.mission, suffix="tenant-a")
    action_b, link_b = _bind_action_and_link(world, mission_b, suffix="tenant-b")
    reader = StoreOperationReader(world.store)

    snapshot_a = build_operation_snapshot(world.mission.id, reader=reader)
    snapshot_b = build_operation_snapshot(mission_b.id, reader=reader)

    assert {row.mission_id for row in snapshot_a.bindings} == {world.mission.id}
    assert {row.mission_id for row in snapshot_a.links} == {world.mission.id}
    assert {row.mission_id for row in snapshot_a.actions} == {world.mission.id}
    assert {row.action_key for row in snapshot_a.actions} == {action_a["action_key"]}
    assert {row.operation_id for row in snapshot_a.links} == {link_a["operation_id"]}
    assert {row.mission_id for row in snapshot_b.bindings} == {mission_b.id}
    assert {row.mission_id for row in snapshot_b.links} == {mission_b.id}
    assert {row.mission_id for row in snapshot_b.actions} == {mission_b.id}
    assert {row.action_key for row in snapshot_b.actions} == {action_b["action_key"]}
    assert {row.operation_id for row in snapshot_b.links} == {link_b["operation_id"]}


def test_o04_cross_mission_link_json_is_source_unavailable_and_reader_writes_nothing(
    tmp_path,
) -> None:
    world, mission_b = _two_tenant_missions(tmp_path)
    action_a, link_a = _bind_action_and_link(world, world.mission, suffix="tenant-a")
    _bind_action_and_link(world, mission_b, suffix="tenant-b")

    # The typed FK-backed columns remain Mission A.  This simulates only a corrupt
    # legacy JSON body, which is the body the real reader decodes; it does not
    # invent a cross-tenant write that the Store schema might reject first.
    corrupted = {**link_a, "mission_id": mission_b.id}
    corrupted["link_json"] = canonical_json(corrupted)
    with world.store.transaction() as connection:
        connection.execute(
            "UPDATE planning_operation_action_links SET link_json=? WHERE operation_id=?",
            (corrupted["link_json"], link_a["operation_id"]),
        )
    before_link = PlanningAdmissionStore(world.store).get_operation_action_link(
        link_a["operation_id"]
    )
    before_action = world.store.get_action(action_a["action_key"])
    before_events = tuple(world.store.list_events(world.mission.id))

    with pytest.raises(SourceUnavailable, match="cross_mission_rows"):
        build_operation_snapshot(world.mission.id, reader=StoreOperationReader(world.store))

    assert (
        PlanningAdmissionStore(world.store).get_operation_action_link(link_a["operation_id"])
        == before_link
    )
    assert world.store.get_action(action_a["action_key"]) == before_action
    assert tuple(world.store.list_events(world.mission.id)) == before_events
