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


def test_ended_mission_holds_are_rechecked_every_five_minutes(monkeypatch):
    """2026-09-28 第四轮：运行中每轮都有写入，已结束任务的旧预留仍每轮重核（约三成 CPU）。"""
    from types import SimpleNamespace
    from agent_orchestrator.orchestrator import taskgraph_action_settlement

    clock = [1000.0]
    generation = [1]
    checked = []
    recovered = []
    guard = accounting_recovery.ProviderBudgetGuard.__new__(accounting_recovery.ProviderBudgetGuard)
    monkeypatch.setattr(accounting_recovery.ProviderBudgetGuard, "recover",
                        lambda self, uow, missions=None: recovered.append(missions))
    pool = SimpleNamespace(bridge=SimpleNamespace(runtime=SimpleNamespace(
        ports=SimpleNamespace(provider_admission=guard), uow=None)))
    orch = SimpleNamespace(store=SimpleNamespace(has_table=lambda name: True),
                           assembled=SimpleNamespace(pools={"p": pool}), _note=lambda text: None)
    monkeypatch.setattr(accounting_recovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(accounting_recovery, "_holds_generation", lambda orch: (generation[0],))
    monkeypatch.setattr(accounting_recovery, "_open_holds",
                        lambda store: [("i-ended", "m-ended"), ("i-live", "m-live")])
    monkeypatch.setattr(accounting_recovery, "_live_missions", lambda store: frozenset({"m-live"}))
    monkeypatch.setattr(accounting_recovery, "_import_hold",
                        lambda orch, intent_id: checked.append(intent_id) or False)
    monkeypatch.setattr(taskgraph_action_settlement, "settle_resolved_actions", lambda orch: False)

    def run(*, write: bool, after: float = 0.0):
        clock[0] += after
        generation[0] += 1 if write else 0
        checked.clear()
        recovered.clear()
        accounting_recovery.import_late_accounting(orch)
        return list(checked), list(recovered)

    assert run(write=False) == (["i-ended", "i-live"], [None])  # 启动后第一轮：全部
    assert run(write=True, after=10) == (["i-live"], [frozenset({"m-live"})])  # 运行中：只核未结束的
    assert run(write=False, after=10) == ([], [frozenset({"m-live"})])  # 没有写入：跳过
    assert run(write=True, after=300) == (["i-ended", "i-live"], [None])  # 满 5 分钟：全部
    # 满 5 分钟但自上次全量以来没有任何写入：全量结果不变，跳过
    assert run(write=False, after=300) == ([], [None])
