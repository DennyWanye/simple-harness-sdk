"""2026-09-28 真机：几十个"用量未知、按上限预留"的旧调用每轮都被完整重读（约三成 CPU）。
编排库与各执行池库都没有写入时跳过重读；任何一边有写入，下一次照常重读。"""

import asyncio

from production_fixture import enabled_world
from agent_orchestrator.orchestrator import accounting_recovery


def test_open_holds_are_reread_only_after_a_write(tmp_path, monkeypatch):
    async def case():
        async with enabled_world(tmp_path, key='tg-late-accounting-quiet') as world:
            await world.commit_seed()
            orch = world.loop
            reads = []
            original = accounting_recovery._open_holds
            monkeypatch.setattr(accounting_recovery, "_open_holds",
                                lambda store: reads.append(1) or original(store))
            accounting_recovery.import_late_accounting(orch)
            accounting_recovery.import_late_accounting(orch)
            assert len(reads) == 1

            with orch.store.transaction() as connection:  # 编排库写入
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS probe(x)")
                connection.execute("INSERT INTO probe VALUES (1)")
            accounting_recovery.import_late_accounting(orch)
            assert len(reads) == 2
            accounting_recovery.import_late_accounting(orch)
            assert len(reads) == 2

            pool = next(iter(orch.assembled.pools.values()))  # 执行池库写入
            with pool.bridge.runtime.uow.database.transaction() as connection:
                connection.execute("CREATE TEMP TABLE IF NOT EXISTS probe_pool(x)")
                connection.execute("INSERT INTO probe_pool VALUES (1)")
            accounting_recovery.import_late_accounting(orch)
            assert len(reads) == 3
    asyncio.run(case())
