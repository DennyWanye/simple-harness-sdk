# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 desktop run (ledger.py + pytest + README): the reviewer turn's input
manifest was 230 KB, under the 256 KB JSON limit, but read_exact_metadata also
serialised the whole row — which carries the same body again as an escaped string —
into lifecycle_json (275 KB), so every review import raised JSON_BYTES_LIMIT, the
recheck budget ran out and the leaf went to manual resolution.  The lifecycle part
now excludes the body column; the body is still checked against its hash on its own.
"""

from __future__ import annotations

from agent_orchestrator.assurance.codec import canonical, decode, fingerprint
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts.models import Budget, Mission, MissionStatus
from agent_orchestrator.storage.assurance_reads import AssuranceReader
from agent_orchestrator.storage.store import Store

TENANT = "tenant-large"
MISSION = "mission-large"


def test_a_large_manifest_body_under_the_limit_reads_exactly(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    try:
        store.insert_mission(Mission(MISSION, "goal", ("c",), (), (), "low", Budget(), TENANT,
                                     MissionStatus.CREATED, 1.0, 1, "key-large"), spec_hash="b" * 64)
        # Many small quoted fields, like a real provider request: escaping the body as a
        # string inside the row grows it by ~20 %, past the limit.
        body = {"messages": [{"role": "tool", "name": f"t{i}", "content": f"line {i}"} for i in range(4200)]}
        size = len(canonical(body).encode("utf-8"))
        assert 200 * 1024 < size < 256 * 1024  # the body alone is within the limit
        digest = fingerprint(body)
        store.connection.execute(
            "INSERT INTO input_manifests(manifest_hash, origin_mission_id, manifest_json, created_at) VALUES (?,?,?,?)",
            (digest, MISSION, canonical(body), 1.0))
        reader = AssuranceReader(store, tenant_id=TENANT, mission_id=MISSION)
        metadata = reader.read_exact_metadata(AssuranceRef("input_manifest", Pin(digest, 0, digest)))
        assert decode(metadata.body_json) == body
        lifecycle = decode(metadata.lifecycle_json)
        assert "manifest_json" not in lifecycle  # the body is never carried twice
        assert lifecycle["manifest_hash"] == digest and lifecycle["origin_mission_id"] == MISSION
    finally:
        store.close()
