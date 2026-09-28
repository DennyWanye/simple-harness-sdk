"""2026-09-28 真机：有调用在跑时后台 CPU 持续约 100%——编排循环每轮在多处把同一计划版本
（连同全部祖先）完整校验一遍。校验过的结果只在"库里什么都没变"时复用：本连接任何写入、
另一个连接的提交、写事务之内，都重新校验。"""

import asyncio
import sqlite3

from production_fixture import enabled_world
from agent_orchestrator.storage.taskgraph_store import TaskGraphStore


def test_a_verified_revision_is_reused_only_while_nothing_changed(tmp_path):
    async def case():
        async with enabled_world(tmp_path, key='tg-revision-cache') as world:
            await world.commit_seed()
            store, mission = world.loop.store, world.mission.id
            reader = TaskGraphStore(store)
            verified = []
            original = reader._read_revision_verified

            def counting(mission_id, revision):
                verified.append(revision)
                return original(mission_id, revision)

            reader._read_revision_verified = counting
            first = reader.read_revision(mission, 1)
            assert reader.read_revision(mission, 1) is first
            assert len(verified) == 1

            # 本连接的任何写入 → 重新校验
            with store.transaction() as connection:
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS probe(x)")
                connection.execute("INSERT INTO probe VALUES (1)")
            reader.read_revision(mission, 1)
            assert len(verified) == 2

            # 另一个连接提交 → 重新校验
            other = sqlite3.connect(store.path)
            other.execute("CREATE TABLE IF NOT EXISTS probe_other(x)")
            other.execute("INSERT INTO probe_other VALUES (1)")
            other.commit()
            other.close()
            reader.read_revision(mission, 1)
            assert len(verified) == 3
            assert reader.read_revision(mission, 1).record.manifest_hash == first.record.manifest_hash
            assert len(verified) == 3

            # 写事务之内（未提交的行可能回滚）→ 不用、也不存缓存
            with store.transaction():
                reader.read_revision(mission, 1)
                reader.read_revision(mission, 1)
            assert len(verified) == 5
    asyncio.run(case())
