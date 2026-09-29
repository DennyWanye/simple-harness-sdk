# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""架构方案 B（2026-09-30）：资料换版本 / 撤销 → 重新规划；修复请求由系统消费。

此前 ``SourceSuperseded`` / ``SourceRevoked`` 编排器里无人听：换了资料，已排好的计划不动；
用到旧版的步骤要等跑到验收才失败。现在：换版本后，只对**有证据**的受影响对象（正在跑、
冻结了旧版的尝试；引用了旧版的已通过结果）发一条"证据失效"修复请求；新登记资料不发请求；
影响范围里的叶子步骤都在请求之后重新验收通过时，系统记"已处理"，不再逼规划器开新轮。

真实编排器、真实严格执行图、真实执行者派发；只有模型回复是脚本。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[3]
for _extra in (_TESTS / "orchestrator" / "full_target", _TESTS / "orchestrator" / "full_target" / "taskgraph_exec"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from production_fixture import enabled_world  # noqa: E402
from test_execution_view import _run_worker  # noqa: E402

from agent_orchestrator.api.facade import MissionControlV1  # noqa: E402
from agent_orchestrator.contracts.state_machines import TERMINAL_ATTEMPT  # noqa: E402
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.orchestrator.planning_repair_requests import (  # noqa: E402
    ADDRESSED,
    REQUESTED,
    SOURCE_CHANGE_ASSESSED,
    collect_triggers,
)
from agent_orchestrator.testing.fixtures import envelope_step  # noqa: E402

STEPS = (
    ("workspace_write_file", {"path": "facts.md", "content": "Repository facts."}),
    envelope_step(summary="Recorded repository facts.", artifacts=["facts.md"], claims=[],
                  override=lambda value: {**value, "outputs": {"facts": "facts.md"}}),
)
PATH = "sources/data.csv"


def _events(loop, mission_id: str, event_type: str):  # type: ignore[no-untyped-def]
    return [e for e in loop.store.list_events(mission_id) if e.type == event_type]


def _source_requests(loop, mission_id: str):  # type: ignore[no-untyped-def]
    """Repair requests opened by a source change (the scripted Worker's own
    ``VerificationFailed`` — it submits no claims — opens its ordinary request too)."""
    return [e for e in _events(loop, mission_id, REQUESTED) if str(e.payload["source_key"]).startswith("source:")]


async def _until_attempt_exists(world) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(30):
        while world.loop.store.connection.execute(
                "SELECT 1 FROM attempts WHERE mission_id=?", (world.mission.id,)).fetchone() is None:
            await world.loop._cycle()
            await asyncio.sleep(.01)


def test_a_superseded_source_reopens_planning_for_the_running_attempt_and_the_system_settles_it_after_reacceptance(tmp_path):
    async def case():  # type: ignore[no-untyped-def]
        async with enabled_world(tmp_path, key="tg-source-replan", worker_steps=STEPS) as world:
            loop, mission = world.loop, world.mission
            control = MissionControlV1(loop, tenant_id=mission.tenant_id, principal=Principal(loop._owner))
            first = control.register_source({"mission_id": mission.id, "path": PATH, "content": "region,amount\n华东,1\n",
                                             "kind": "text", "idempotency_key": "reg-data-1"})
            await world.commit_seed()
            await _until_attempt_exists(world)
            [attempt] = [a for t in loop.store.list_tasks(mission.id) for a in loop.store.list_attempts(t.id)]
            # 派发时冻结的就是第 1 版
            assert loop._frozen_source_binding(attempt)["source_versions"][PATH] == first["version_hash"]
            assert collect_triggers(loop, mission) is False and not _events(loop, mission.id, REQUESTED)

            # 换版本（经一次批准）。尝试还在跑：**先不评估**（2026-09-30 真机：当轮发请求，规划器只会
            # 回 WAIT 等它跑完，修复轮又拒绝 WAIT，白花两轮；跑完后验收本来就对照当前资料）。
            proposal = control.supersede_source({"mission_id": mission.id, "path": PATH, "content": "region,amount\n华东,2\n",
                                                 "kind": "text", "idempotency_key": "sup-data-1",
                                                 "expected_version_hash": first["version_hash"]})
            control.decide(proposal["request_id"], "approve", nonce="approve-sup-1")
            assert _events(loop, mission.id, "SourceSuperseded")
            assert collect_triggers(loop, mission) is False
            assert not _source_requests(loop, mission.id) and not _events(loop, mission.id, SOURCE_CHANGE_ASSESSED)
            # 新登记的资料不发请求
            control.register_source({"mission_id": mission.id, "path": "sources/extra.md", "content": "extra",
                                     "kind": "markdown", "idempotency_key": "reg-extra-1"})
            collect_triggers(loop, mission)
            assert not _source_requests(loop, mission.id)

            # 执行者跑完（这个夹具的脚本执行者不交 claim，验收判 FAIL，走普通的验收失败请求）
            await _run_worker(world)
            assert loop.store.get_attempt(attempt.id).status in TERMINAL_ATTEMPT
            collect_triggers(loop, mission)
            # 现在评估：拿着旧版跑过的尝试记在案，通过的结果没有引用它 → 只记评估、不发资料请求
            [assessed] = _events(loop, mission.id, SOURCE_CHANGE_ASSESSED)
            assert assessed.payload["reason"] == "source_superseded"
            assert assessed.payload["old_version"] == first["version_hash"]
            assert assessed.payload["affected"] == {"attempts_on_old_version": [attempt.id], "accepted_results": []}
            assert not _source_requests(loop, mission.id)
            assert [e.payload["source_key"] for e in _events(loop, mission.id, REQUESTED)] == [
                "event:" + next(e.idempotency_key for e in _events(loop, mission.id, "VerificationFailed"))]
            assert not _events(loop, mission.id, ADDRESSED)
            collect_triggers(loop, mission)
            assert len(_events(loop, mission.id, SOURCE_CHANGE_ASSESSED)) == 1

            # 跑完后再换一次版本：没有拿着这一版跑过的尝试 → 评估记录为空
            second = control.supersede_source({"mission_id": mission.id, "path": PATH, "content": "region,amount\n华东,3\n",
                                               "kind": "text", "idempotency_key": "sup-data-2",
                                               "expected_version_hash": proposal["version_hash"]})
            control.decide(second["request_id"], "approve", nonce="approve-sup-2")
            collect_triggers(loop, mission)
            assert not _source_requests(loop, mission.id)
            assert [e.payload["affected"] for e in _events(loop, mission.id, SOURCE_CHANGE_ASSESSED)][-1] == {
                "attempts_on_old_version": [], "accepted_results": []}
            assert len(_events(loop, mission.id, SOURCE_CHANGE_ASSESSED)) == 2

    asyncio.run(case())
