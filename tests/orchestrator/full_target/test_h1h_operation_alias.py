"""O02/O03 Store-backed regression draft; do not map this file to O09.

The tests call the trusted ``planning_origin`` seam directly because the real
candidate-result producer is still absent.  They cover ActionCommits and the
authoritative Store bridge, but do not prove producer wiring or crash recovery.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

_SDK_TESTS = Path(__file__).resolve().parent.parent
for _directory in (_SDK_TESTS / "full_target", _SDK_TESTS / "step07"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from helpers_step07 import ENABLED, candidate, ledger_service  # noqa: E402
from test_htn_store import HASH_B, envelope  # noqa: E402

from agent_orchestrator.orchestrator.action_commits import (  # noqa: E402
    ActionCommitError,
)
from agent_orchestrator.runtime.planning_operations import (  # noqa: E402
    BoundPlanningOperationOrigin,
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
from agent_orchestrator.storage.store import StoreConflict  # noqa: E402


def _origin(service, mission, task, *, operation_id: str, occurrence: str, request_hash=None):
    frozen = dataclasses.replace(
        envelope(
            operation_id=operation_id,
            occurrence=occurrence,
            **({} if request_hash is None else {"request_hash": request_hash}),
        ),
        mission_id=mission.id,
        scope_id="mission",
    )
    HtnStore(service.store).bind_operation(frozen, principal_id="origin-principal")
    binding = PlanningAdmissionStore(service.store).get_operation_binding(occurrence)
    assert binding is not None
    return BoundPlanningOperationOrigin(
        operation_id=binding["operation_id"],
        operation_occurrence_id=binding["operation_occurrence_id"],
        request_hash=binding["request_hash"],
        mission_id=binding["mission_id"],
        envelope_hash=binding["envelope_hash"],
        principal_id=binding["principal_id"],
        scope_id=binding["scope_id"],
        obligation_id=binding["obligation_id"],
        producer_task_id=task.id,
        producer_htn_occurrence_id=f"htn-{occurrence}",
        producer_contract_revision=1,
        producer_plan_revision=1,
        provenance_receipt_id=f"receipt-{occurrence}",
    )


def _propose(service, mission, task, connectors, value, origin, *, operation="set"):
    return service.propose_action(
        candidate(value=value, operation=operation),
        mission_id=mission.id,
        task_id=task.id,
        result_id=f"result-{origin.operation_occurrence_id}",
        attempt_id=f"attempt-{origin.operation_occurrence_id}",
        artifact_id=f"artifact-{origin.operation_occurrence_id}",
        artifact_hash=("a" if value == "on" else "b") * 64,
        connectors=connectors,
        deployment=ENABLED,
        planning_origin=origin,
    )


def test_o02_same_target_different_operation_does_not_join_latest(tmp_path):
    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    write_origin = _origin(
        service, mission, tasks["A"], operation_id="operation-write", occurrence="occ-write"
    )
    read_origin = _origin(
        service, mission, tasks["B"], operation_id="operation-read", occurrence="occ-read"
    )

    written = _propose(service, mission, tasks["A"], connectors, "on", write_origin)
    read = _propose(service, mission, tasks["B"], connectors, "off", read_origin, operation="read")

    assert written["target"] == read["target"] == "feature_flags.new_ui"
    assert written["operation"] == "set" and read["operation"] == "read"
    assert written["action_id"] != read["action_id"]
    assert written["action_key"] != read["action_key"]
    links = PlanningAdmissionStore(service.store).list_operation_action_links(mission.id)
    assert {item["operation_id"]: item["action_key"] for item in links} == {
        "operation-write": written["action_key"],
        "operation-read": read["action_key"],
    }


def test_o02_second_occurrence_cannot_alias_or_overwrite_original_action_link(tmp_path):
    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    first_origin = _origin(
        service, mission, tasks["A"], operation_id="operation-first", occurrence="occ-first"
    )
    second_origin = _origin(
        service, mission, tasks["B"], operation_id="operation-second", occurrence="occ-second"
    )
    first = _propose(service, mission, tasks["A"], connectors, "on", first_origin)
    adapter = PlanningAdmissionStore(service.store)
    original_link = adapter.get_operation_action_link("operation-first")
    original_action = service.store.get_action(first["action_key"])
    original_events = tuple(service.store.list_events(mission.id))

    with pytest.raises(ActionCommitError, match="operation_occurrence_alias_conflict"):
        _propose(service, mission, tasks["B"], connectors, "on", second_origin)

    assert adapter.get_operation_action_link("operation-first") == original_link
    assert adapter.get_operation_action_link("operation-second") is None
    assert service.store.get_action(first["action_key"]) == original_action
    assert tuple(service.store.list_events(mission.id)) == original_events


def test_o02_same_origin_same_action_replay_is_idempotent(tmp_path):
    """Replay compares the same persisted identity without rewriting the action."""

    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    origin = _origin(
        service, mission, tasks["A"], operation_id="operation-replay", occurrence="occ-replay"
    )
    first = _propose(service, mission, tasks["A"], connectors, "on", origin)
    adapter = PlanningAdmissionStore(service.store)
    before_link = adapter.get_operation_action_link(origin.operation_id)
    before_action = service.store.get_action(first["action_key"])
    before_events = tuple(service.store.list_events(mission.id))
    before_rows = tuple(service.store.list_actions(mission.id))
    before_changes = service.store.connection.total_changes

    replay = _propose(service, mission, tasks["A"], connectors, "on", origin)

    assert replay == first
    assert adapter.get_operation_action_link(origin.operation_id) == before_link
    assert service.store.get_action(first["action_key"]) == before_action
    assert tuple(service.store.list_actions(mission.id)) == before_rows
    assert tuple(service.store.list_events(mission.id)) == before_events
    assert service.store.connection.total_changes == before_changes


def test_o02_same_operation_id_with_different_request_hash_is_conflict(tmp_path):
    service, mission, tasks, _config, _connectors, _ = ledger_service(tmp_path)
    _origin(service, mission, tasks["A"], operation_id="operation-stable", occurrence="occ-a")

    with pytest.raises(StoreConflict, match="OPERATION_PAYLOAD_CONFLICT"):
        _origin(
            service,
            mission,
            tasks["B"],
            operation_id="operation-stable",
            occurrence="occ-b",
            request_hash=HASH_B,
        )

    bindings = PlanningAdmissionStore(service.store).list_operation_bindings(mission.id)
    assert [(item["operation_id"], item["operation_occurrence_id"]) for item in bindings] == [
        ("operation-stable", "occ-a")
    ]


def test_o03_unknown_action_outside_active_plan_membership_still_blocks(tmp_path):
    """Store mapper is Mission-wide; this is partial until a real method retirement drives it."""

    service, mission, tasks, _config, connectors, _ = ledger_service(tmp_path)
    origin = _origin(
        service, mission, tasks["A"], operation_id="operation-retired", occurrence="occ-retired"
    )
    action = _propose(service, mission, tasks["A"], connectors, "on", origin)
    stored = service.store.get_action(action["action_key"])
    assert stored is not None
    stored.update(state="UNKNOWN", handoffs=1)
    service.store.put_action(stored)

    # No active-plan filter is passed to the production Store reader.  The
    # authoritative Mission-wide rows must remain visible after their producer
    # method occurrence has left the active plan.
    snapshot = build_operation_snapshot(mission.id, reader=StoreOperationReader(service.store))
    assert snapshot.effects == (("operation-retired", OperationEffect.UNRESOLVED),)
    with pytest.raises(SourceUnavailable, match="operation_unresolved"):
        operation_gate(snapshot)
