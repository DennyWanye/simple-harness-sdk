# SPDX-License-Identifier: Apache-2.0
"""The root review sees the root's own accepted operation effects.

Real run 2026-09-28 (mission-a3272b79f2d4d360): the publish ran, was read back
and its outcome accepted on the root Task, but the MISSION_FINAL materials were
only the child acceptances of the cut; the reviewer saw the candidate, not the
execution, answered UNKNOWN on the action criterion and the Mission failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "operation_completion"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts/assurance_seams"))

from _operation_world import OperationWorld  # noqa: E402

from agent_orchestrator.orchestrator.assurance_purpose_reviews import (  # noqa: E402
    _root_effect_materials,
)
from agent_orchestrator.storage.assurance_reads import AssuranceReader  # noqa: E402


def test_accepted_root_effect_and_its_readback_are_final_review_material(tmp_path):
    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    scope_id = w.store.connection.execute(
        "SELECT scope_id FROM operation_completion_scopes WHERE mission_id=? AND occurrence_id=?",
        (w.mission_id, w.occurrence),
    ).fetchone()[0]
    # Content acceptance alone: nothing effect-shaped to expose yet.
    assert _root_effect_materials(w.store, w.mission_id, scope_id) == set()
    prepared, _record, _receipt = w.complete_effect("deliver-report")
    refs = _root_effect_materials(w.store, w.mission_id, scope_id)
    by_kind = {}
    for ref in refs:
        by_kind.setdefault(ref.kind, set()).add(ref.pin.id)
    assert by_kind["acceptance"] == {"acc-" + prepared.binding_id}
    observations = {str(ref["id"]) for ref in prepared.binding.to_json()["source_receipt_refs"]}
    assert by_kind["commit_receipt"] == observations
    # Every pin is exact: the review preparation reads each one back unchanged.
    reader = AssuranceReader(w.store, tenant_id=w.world.mission.tenant_id, mission_id=w.mission_id)
    for ref in refs:
        reader.read_exact_metadata(ref)
