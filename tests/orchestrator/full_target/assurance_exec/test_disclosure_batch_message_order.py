# SPDX-License-Identifier: Apache-2.0
"""A disclosure batch over several tool-result messages binds to its delivery event.

Host real-model run 14 (2026-09-23, Grok lane): the reviewer's tool reads arrived
in two tool-result messages whose request order was not lexical. DisclosureBatch
stores visible_message_ids as a sorted set while the delivery event carried the
request order, so the store's event/batch binding refused the batch
(DISCLOSURE_INPUT_BINDING) and the review was classified EXPOSURE_UNAVAILABLE.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_orchestrator.assurance.evidence import build_catalogue
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.storage.assurance_reads import AssuranceReader
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.verification.reviewer_evidence_tools import record_disclosure_batch

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))


def test_unsorted_message_ids_record_and_replay(tmp_path):
    from _assured_fixture import build_world

    world, task, stored, artifact, scope_ref = build_world(tmp_path / "w")
    commit, mission = world.service, world.mission
    reader = AssuranceReader(commit.store, tenant_id=mission.tenant_id, mission_id=mission.id)
    # Only the event/batch binding is under test; the review binding row the
    # foreign key names is produced by the full review path, not by this unit.
    commit.store.connection.execute("PRAGMA foreign_keys=OFF")
    review_key = "assurance-content:" + "c" * 64
    entries = build_catalogue(review_key, (
        AssuranceRef("task", Pin("task-a", 1, "1" * 64)),
        AssuranceRef("result", Pin("result-b", 0, "2" * 64)),
    ))
    turn_ref = AssuranceRef("agent_turn_receipt", Pin("turn-1", 0, "3" * 64))
    manifest = {"provider_input_hash": "4" * 64}
    kwargs = dict(review_key=review_key, turn_ref=turn_ref, agent_id="agent-r",
                  manifest=manifest, entries=entries, message_ids=("msg-z", "msg-a"))
    record_disclosure_batch(commit, reader, **kwargs)
    chain = AssuranceStore(commit.store).disclosure_chain(mission.id, review_key)
    assert len(chain) == 1 and chain[0].visible_message_ids == ("msg-a", "msg-z")
    # Same body replay (either order) adds nothing and does not refuse.
    record_disclosure_batch(commit, reader, **{**kwargs, "message_ids": ("msg-a", "msg-z")})
    assert len(AssuranceStore(commit.store).disclosure_chain(mission.id, review_key)) == 1
