# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Actual selection/fragment records must replay; dropped provenance cannot pass."""
import asyncio

from test_candidate_selection_runtime import setup, until
from test_fragment_scope import scene  # noqa: F401

from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)


def audit(store, mission_id, *, drop=None):
    with store.read_view():
        events = events_from_store(store, mission_id)
        snapshot = store.snapshot(mission_id)
    if drop:
        events = [event for event in events if event["type"] != drop]
    projection = Projection().feed(events)
    projection.check_structure()
    report = compare(projection.formal(), formal_from_snapshot(snapshot))
    return projection, report


def test_actual_compare_snapshot_and_dropped_round_events(tmp_path):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(orch, lambda: orch.store.get_task(task.id).status is TaskStatus.COMPLETED)
            projection, report = audit(orch.store, task.mission_id)
            assert not projection.unknown and not projection.gaps
            assert not report["mismatches"] and not report["not_covered"]
            assert projection.objects["selection_round"]
            assert projection.objects["selection_candidate"]
            assert projection.objects["selection_decision"]
            _, missing_decision = audit(
                orch.store, task.mission_id, drop="SelectionDecisionRecorded"
            )
            assert any(
                row["object"] == "selection_decision"
                for row in missing_decision["not_covered"]
            )
            _, dropped = audit(orch.store, task.mission_id, drop="SelectionRoundUpdated")
            assert any(row["object"] == "selection_round" for row in dropped["not_covered"])
        finally:
            await orch.__aexit__(None, None, None)
    asyncio.run(run())


def test_actual_fragment_projection_and_missing_receipt_event(scene):  # noqa: F811
    receipt = scene.commit.commit_fragment_validation(
        scene.proposal, command_id="replay-fragment", base_graph_version=1,
        source={"manager": "replay-oracle"},
    )
    projection, report = audit(scene.store, scene.mission.id)
    assert not projection.unknown and not report["mismatches"] and not report["not_covered"]
    assert receipt["fragment_id"] in projection.objects["fragment_validation"]
    _, dropped = audit(scene.store, scene.mission.id, drop="FragmentValidationCommitted")
    assert any(row["object"] == "fragment_validation" for row in dropped["not_covered"])
