# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""NEXT-TG-1.0 第二批 B 验收补测（§7.3 第 3、6 条）。

* 第 3 条：明确的结构缺口（叶子超时 → 原修复触发）只产生一次原 Planner 请求；重复
  收集触发、反复 tick、冷重开都不重复产生请求或规划轮。世界复用
  ``test_h4_retry_runtime_entry`` 的 planning-decision-v1 夹具。
* 第 6 条：动作结果未知只核对不重发；把一笔未知费用按上限结清，不改变该动作 / 效果
  状态，也不触发重发。世界复用 ``assurance_exec/_operation_world.OperationWorld``。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE / "assurance_exec", _HERE / "operation_completion"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _operation_world import OperationWorld  # noqa: E402
from test_completion_contract import _effect_state  # noqa: E402
from test_h1i_production_entry import (  # noqa: E402
    _config,
    build_planning_world,
    code_observers,
    deployed_layers,
)
from test_h4_retry_runtime_entry import _owed_repair  # noqa: E402

from agent_orchestrator.governance.budgets import UsageFact  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.completion_status import (  # noqa: E402
    read_occurrence_completion,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.planning_repair_requests import (  # noqa: E402
    REQUESTED,
    collect_triggers,
    pending_requests,
)
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

OPEN = ("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")


# ======================================================================================
# §7.3 第 3 条：同一结构缺口只开一轮
# ======================================================================================


def _planner_intents(loop, mission_id: str) -> list:
    return [i for i in loop.store.list_intents(*OPEN, "SETTLED", "FAILED")
            if i.mission_id == mission_id and i.kind == "plan" and ":planner:" in i.subject_id]


def _ledger(loop, mission_id: str) -> dict[str, int]:
    events = list(loop.store.iter_events(mission_id))
    return {
        "requested": sum(1 for e in events if e.type == REQUESTED),
        "resumed": sum(1 for e in events if e.type == "PlanningServiceResumed"),
        "planner_intents": len(_planner_intents(loop, mission_id)),
    }


async def _slow_entrances(loop, mission) -> list[bool]:
    """§7.1 列出的每个"何时再规划"入口各敲一次（不跑执行器）。"""

    current = loop.store.get_mission(mission.id)
    return [
        collect_triggers(loop, current),
        loop._resume_planning_services(current),
        await loop._refine_open_compounds(current),
        await loop._retry_deferred_planning(),
        await loop._retry_deferred_repair(),
    ]


def test_one_structural_gap_opens_one_planner_round_across_repeated_ticks(tmp_path) -> None:
    """§7.3-3：同一修复触发反复收集、反复 tick，只有一条修复请求和一个规划轮。"""

    async def case():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _task_id = await _owed_repair(loop, tmp_path, "tg2b-one-gap")
            assert len(pending_requests(loop.store, mission.id)) == 1
            baseline = _ledger(loop, mission.id)
            # 同一事实再收集三次：不再记录新请求。
            assert [collect_triggers(loop, loop.store.get_mission(mission.id)) for _ in range(3)] == [False] * 3
            # 第一次恢复开出一轮；之后同一请求不再开第二轮。
            assert loop._resume_planning_services(loop.store.get_mission(mission.id)) is True
            opened = _ledger(loop, mission.id)
            assert opened["requested"] == baseline["requested"] == 1
            assert opened["planner_intents"] == baseline["planner_intents"] + 1
            assert opened["resumed"] == baseline["resumed"] + 1
            for _ in range(3):
                assert not any(await _slow_entrances(loop, mission))
            assert _ledger(loop, mission.id) == opened

    asyncio.run(case())


def test_a_cold_reopen_does_not_ask_the_planner_again_for_the_same_gap(tmp_path) -> None:
    """§7.3-3：进程重启（同一库冷重开、重新装配）后，同一缺口不再产生请求或规划轮。"""

    state: dict = {}

    async def first():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _task_id = await _owed_repair(loop, tmp_path, "tg2b-reopen")
            assert loop._resume_planning_services(loop.store.get_mission(mission.id)) is True
            state["mission_id"] = mission.id
            state["ledger"] = _ledger(loop, mission.id)

    async def second():
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission = loop.store.get_mission(state["mission_id"])
            env = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(tmp_path / "repo", allow_test_execution=True),
            )
            loop.install_hierarchical(planning=env)
            assert loop._new_mode(mission) is not None  # 真的装回了层次模式，入口不是空转
            assert len(pending_requests(loop.store, mission.id)) == 1
            for _ in range(3):
                assert not any(await _slow_entrances(loop, mission))
            assert _ledger(loop, mission.id) == state["ledger"]

    asyncio.run(first())
    asyncio.run(second())


# ======================================================================================
# §7.3 第 6 条：结果未知只核对；费用按上限结清不改变效果状态
# ======================================================================================


def _unknown_publish(tmp_path) -> tuple[OperationWorld, dict]:
    """服务端已发布但回执丢失：动作 UNKNOWN，效果待核对。"""

    w = OperationWorld(tmp_path)
    w.produce_and_accept()
    w.submit("deliver-report")
    w.materialize("deliver-report")
    real_execute = FilePublishConnector.execute

    def applied_but_lost(self, *args, **kwargs):
        real_execute(self, *args, **kwargs)
        raise RuntimeError("reply lost after the service applied it")

    with patch.object(FilePublishConnector, "execute", applied_but_lost):
        assert w.dispatch()
    action = w.store.get_action(w.actions["deliver-report"]["action_key"])
    assert action["state"] == "UNKNOWN" and not action.get("receipt")
    assert _effect_state(w) == "RECONCILIATION_REQUIRED"
    return w, action


def test_settling_an_unknown_charge_at_its_upper_bound_leaves_the_unknown_action_alone(tmp_path) -> None:
    """§7.3-6：未知费用按上限结清后，UNKNOWN 动作 / 待核对效果不变、不重发，之后仍只靠核对收敛。"""

    w, action = _unknown_publish(tmp_path)
    ledger = w.commit.ledger
    subject = "charge:" + str(action["action_key"])
    with w.store.transaction():
        ledger.reserve(account_id=mission_account(w.mission_id), subject_id=subject,
                       tokens=4_000, cost_micros=0, counts_attempt=False)
        ledger.import_usage(subject_id=subject, mission_id=w.mission_id, facts=[
            UsageFact("call-known", 1_000, 200, 0), UsageFact("call-lost", 0, 0, 0, unknown=True)])
    before = dict(w.store.get_action(action["action_key"]))
    with w.store.transaction():
        settled = ledger.settle_at_upper_bound(subject_id=subject)
    assert settled["state"] == "SETTLED" and settled["settled_tokens"] == 4_000
    assert ledger.usage_flags(w.mission_id)["usage_fully_known"] is False  # 费用事实仍是未知
    after = dict(w.store.get_action(action["action_key"]))
    assert after["state"] == "UNKNOWN" and not after.get("receipt")
    assert {k: after[k] for k in ("state", "receipt", "idempotency_key")} == {
        k: before[k] for k in ("state", "receipt", "idempotency_key")}
    assert _effect_state(w) == "RECONCILIATION_REQUIRED"
    assert not read_occurrence_completion(w.store, w.mission_id, w.occurrence).complete
    # 结清不是"已完成"也不是"未发生"：不重发，服务端只有那一次发布。
    assert not w.dispatch() and len(w.published_files()) == 1
    account = ledger.account(mission_account(w.mission_id))
    booked = (account.settled_tokens, account.reserved_tokens)
    # 之后唯一的收敛路径仍是核对：读到服务端事实后记为成功，且依旧没有第二次发送。
    assert asyncio.run(w.executor.reconcile(w.mission_id))
    assert w.store.get_action(action["action_key"])["state"] == "SUCCEEDED"
    assert len(w.published_files()) == 1
    # 核对成功也不回头改写已结清的费用（不退、不重复计）。
    account = ledger.account(mission_account(w.mission_id))
    assert (account.settled_tokens, account.reserved_tokens) == booked
    assert ledger.usage_flags(w.mission_id)["budget_conserved"] is True
