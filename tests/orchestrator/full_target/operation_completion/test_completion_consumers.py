"""Actual core acceptance, cold recovery, and shared completion readers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_orchestrator.graph.eligibility import OccurrenceOutcome
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.orchestrator.completion_status import read_occurrence_completion
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.operation_completion import OperationCompletionError
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import Store

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from test_scoped_content_commit import _mixed_world  # noqa: E402


def test_prepared_mixed_is_data_readable_but_order_and_terminal_remain_closed(tmp_path):
    world, _, _, task, _, artifact = _mixed_world(tmp_path, with_output=True)
    dispatch = HierarchicalDispatch(world.store, world.service)
    network = dispatch.network(world.mission.id)
    occurrence = network.root_occurrence_ids[0]
    status = read_occurrence_completion(world.store, world.mission.id, str(occurrence))
    assert status.preparation_ready and status.content_ready
    assert not status.effects_ready and not status.complete
    assert (
        dispatch.occurrence_outcomes(world.mission.id, network)[occurrence]
        is OccurrenceOutcome.RUNNING
    )
    index = dispatch.accepted_outputs(world.mission.id, network)
    assert occurrence in index.completed_producers
    assert [item.artifact_id for item in index.outputs] == [artifact.id]
    assert not dispatch.terminal(world.mission.id)
    assert not dispatch.root_review_ready(world.mission.id)
    assert task.id not in dispatch.admissions(world.mission.id).readiness


def test_cold_store_accepts_original_result_without_handler_port_claim_memory(tmp_path):
    world, requirements, _, task, stored, artifact = _mixed_world(
        tmp_path, accept_result=False, with_output=True
    )
    path = world.store.path
    world.store.close()
    reopened = Store.open(path)
    try:
        service = CommitService(reopened)
        accepted = service.accept_result(stored.envelope.id, verifier_results=())
        assert accepted.status == "VERIFYING"
        assert accepted.accepted_result_id == stored.envelope.id
        assert HtnStore(reopened).latest_requirements_revision(world.mission.id) == requirements
        dispatch = HierarchicalDispatch(reopened, service)
        outputs = dispatch.accepted_outputs(world.mission.id, dispatch.network(world.mission.id))
        assert [item.artifact_id for item in outputs.outputs] == [artifact.id]
        before = reopened.connection.total_changes
        service.accept_result(stored.envelope.id, verifier_results=())
        assert reopened.connection.total_changes == before
    finally:
        reopened.close()


def test_cold_missing_result_claims_cannot_be_filled_from_current_ports(tmp_path):
    world, _, _, task, stored, _ = _mixed_world(tmp_path, accept_result=False, with_output=True)
    # Disk-corruption negative: never replace a missing worker claim by port order.
    world.store.connection.execute(
        "UPDATE events SET payload_json=json_remove(payload_json,'$.completion_port_claims') "
        "WHERE mission_id=? AND type='ResultSubmitted'",
        (world.mission.id,),
    )
    before = world.store.connection.total_changes
    with pytest.raises(OperationCompletionError, match="port claims"):
        world.service.accept_result(stored.envelope.id, verifier_results=())
    assert world.store.connection.total_changes == before
    assert world.store.get_task(task.id).accepted_result_id is None
