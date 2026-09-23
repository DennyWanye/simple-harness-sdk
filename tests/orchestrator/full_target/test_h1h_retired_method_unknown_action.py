"""O03: retired method membership cannot hide an UNKNOWN operation action.

The setup deliberately uses the repair library's real retire+refine path.  The
old method instance is RETIRED but remains in the Store's historical membership
table, while the active network has adopted the replacement.  The operation
link is then attached to a task occurrence from that retired membership; it is
not an artificial empty-plan or operation-name-only fixture.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest

from simple_harness.contracts import canonical_json


_SDK = Path("/Users/denny/projects/simple-harness-sdk-h1h-impl")
_FULL_TARGET = _SDK / "tests" / "orchestrator" / "full_target"
if str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_htn_store import envelope  # noqa: E402
from test_root_review_repair_library import (  # noqa: E402
    _adopted_root,
    _alt_method,
    _rejected_open,
    _replacement,
)

from agent_orchestrator.runtime.planning_operations import (  # noqa: E402
    OperationEffect,
    SourceUnavailable,
    StoreOperationReader,
    build_operation_snapshot,
    operation_gate,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.planning_admission_store import (  # noqa: E402
    PlanningAdmissionStore,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_o03_retired_method_unknown_action_remains_visible_and_blocks(tmp_path) -> None:
    """A retired branch is historical work, not a reader exclusion filter."""

    world = _rejected_open(tmp_path, key="h1h-o03-retired-unknown", alt=True)
    htn = HtnStore(world.store)
    retired_instance_id = _adopted_root(world)
    retired_membership = htn.list_child_occurrences(world.mission.id, retired_instance_id)
    assert retired_membership, "the real adopted method must have persisted membership"

    # Retire+refine can deliberately retain an accepted read-only occurrence as
    # ``share_active``.  Keep the materialised task id for every old slot now and
    # select, after the real replacement, the old slot absent from the *adopted
    # execution projection*.  This avoids confusing the network's historical/shared
    # occurrence inventory with current method membership.
    before = world.network()
    old_tasks_by_occurrence = {
        str(item.occurrence_id): str(before.occurrence(item.occurrence_id).task_id)
        for item in retired_membership
    }

    outcome = world.plan(
        _replacement(
            world,
            _alt_method(),
            instance_id=retired_instance_id,
            revision=1,
        ),
        command_id="h1h-o03-retire-and-refine",
    )
    assert outcome.committed, outcome.last_reason

    active = world.network()
    assert retired_instance_id not in {str(item) for item in active.adopted_instance_ids}
    assert retired_instance_id in {
        str(item.instance_id)
        for item in htn.list_method_instances(world.mission.id, state="RETIRED")
    }
    # Membership survives for audit/reconciliation.  A network can retain an
    # occurrence in its historical/shared inventory, but only adopted method
    # membership reaches ``execution_projection``.  Choose an old slot that the
    # replacement no longer projects; a reader filtered to the active plan would
    # therefore miss its UNKNOWN action.
    old_membership_occurrences = {
        str(item.occurrence_id)
        for item in htn.list_child_occurrences(world.mission.id, retired_instance_id)
    }
    assert old_membership_occurrences == set(old_tasks_by_occurrence)
    projected_occurrences = {
        str(item) for item in active.execution_projection().projected_occurrences
    }
    retired_only = sorted(old_membership_occurrences - projected_occurrences)
    assert retired_only, "the replacement must leave at least one old slot inactive"
    old_occurrence_id = retired_only[0]
    retired_task_id = old_tasks_by_occurrence[old_occurrence_id]
    assert retired_task_id in {str(task.id) for task in world.store.list_tasks(world.mission.id)}
    assert old_occurrence_id not in projected_occurrences

    origin = dataclasses.replace(
        envelope(
            operation_id="h1h-o03-retired-operation",
            occurrence="h1h-o03-retired-operation-occurrence",
            request_hash=_sha("h1h-o03-retired-request"),
        ),
        mission_id=world.mission.id,
        scope_id=world.principal.scope_id,
    )
    htn.bind_operation(origin, principal_id=world.principal.principal_id)
    admission = PlanningAdmissionStore(world.store)
    binding = admission.get_operation_binding(str(origin.operation_occurrence_id))
    assert binding is not None

    action = {
        "mission_id": world.mission.id,
        "action_key": "h1h-o03-retired-action:v1",
        "action_id": "h1h-o03-retired-action",
        "version": 1,
        "params_hash": _sha("h1h-o03-retired-params"),
        "idempotency_key": "h1h-o03-retired-action:v1",
        "state": "UNKNOWN",
        "handoffs": 1,
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
        "producer_task_id": retired_task_id,
        "producer_htn_occurrence_id": old_occurrence_id,
        "producer_contract_revision": 1,
        "producer_plan_revision": 1,
        "action_key": action["action_key"],
        "action_id": action["action_id"],
        "action_version": action["version"],
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "provenance_receipt_id": "h1h-o03-retired-receipt",
        "link_hash": _sha("h1h-o03-retired-link"),
        "link_json": "{}",
    }
    link["link_json"] = canonical_json(link)
    admission.put_operation_action_link(link)

    snapshot = build_operation_snapshot(
        world.mission.id,
        reader=StoreOperationReader(world.store),
    )
    assert snapshot.effects == ((str(origin.operation_id), OperationEffect.UNRESOLVED),)
    stored_link = admission.list_operation_action_links(world.mission.id)[0]
    assert stored_link["producer_task_id"] == retired_task_id
    assert stored_link["producer_htn_occurrence_id"] == old_occurrence_id
    assert snapshot.actions[0].state == "UNKNOWN"
    with pytest.raises(SourceUnavailable, match="operation_unresolved"):
        operation_gate(snapshot)
