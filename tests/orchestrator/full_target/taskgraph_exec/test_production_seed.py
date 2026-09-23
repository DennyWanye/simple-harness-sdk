# SPDX-License-Identifier: Apache-2.0
"""A01/C01 entry assertions; not the entire scenario acceptance matrix."""
import asyncio

from production_fixture import enabled_world
from agent_orchestrator.storage.taskgraph_store import TaskGraphStore


def test_explicit_enable_original_dispatch_and_revision_receipt(tmp_path):
    async def case():
        async with enabled_world(tmp_path, key='tg-production-seed') as world:
            await world.commit_seed()
            store, mission = world.loop.store, world.mission.id
            historical = TaskGraphStore(store).read_revision(mission, 1)
            assert historical.record.source_kind == 'SEED_COMMIT'
            before = store.last_event_seq(mission)
            snapshot = world.graph.reads.snapshot(mission)
            repeated = world.graph.reads.snapshot(mission)
            assert snapshot == repeated
            assert snapshot['read_token']['plan_revision'] == 1
            assert store.last_event_seq(mission) == before
            assert store.connection.execute(
                'SELECT COUNT(*) FROM taskgraph_revision_records WHERE mission_id=?', (mission,)).fetchone()[0] == 1
            assert store.connection.execute(
                'SELECT COUNT(*) FROM attempts WHERE mission_id=?', (mission,)).fetchone()[0] == 0
    asyncio.run(case())
