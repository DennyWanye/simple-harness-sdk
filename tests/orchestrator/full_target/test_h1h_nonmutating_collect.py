"""P08 production-collector regressions for all three non-mutating decisions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import test_h1i_production_entry as production_entry
from test_h1i_production_entry import (
    _config,
    _events,
    _open_planner_round,
    _seed_new_protocol,
)
from test_h1i_wait_lifecycle import _ensure_wait_task, _wait_reply

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import (
    PlanningDecisionEnvelopeV1,
    PlanningDecisionStatus,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.decision_codec import serialize_planning_decision
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

_FIXTURES = (
    Path(production_entry.__file__).resolve().parent / "fixtures" / "planning_decision_v1" / "valid"
)


def _state_free_reply(
    decision_type: str, package: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    if decision_type == "WAIT":
        return _wait_reply(package)
    body = json.loads(
        (_FIXTURES / f"{decision_type.lower().replace('_', '-')}.json").read_text(encoding="utf-8")
    )
    body["subject_key"] = package["planning_subjects"][0]["subject_key"]
    return serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(body)), None


@pytest.mark.parametrize("decision_type", ("WAIT", "NO_CHANGE", "DECLARE_BLOCKED"))
def test_p08_state_free_decisions_use_real_collector_without_shape_or_operation_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision_type: str
) -> None:
    async def case() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key=f"h1h-p08-{decision_type.lower()}"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id=f"grant-h1h-p08-{decision_type.lower()}",
                request_id=opener.intent_id,
            )
            reply, wait_ref = _state_free_reply(decision_type, opener.config["planning_package"])
            if wait_ref is not None:
                _ensure_wait_task(loop, mission, wait_ref)

            def forbidden(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("state-free decision entered shape/operation preview")

            # `_hierarchical_admission_context` imports this producer lazily, so
            # patching its defining module proves no live operation snapshot read.
            monkeypatch.setattr(
                "agent_orchestrator.runtime.planning_operations.build_operation_snapshot",
                forbidden,
            )
            monkeypatch.setattr(dispatch, "preview_plan_proposal", forbidden)
            monkeypatch.setattr(dispatch, "commit_preview_plan_proposal", forbidden)

            await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)

            decision = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert decision is not None
            assert decision["status"] == str(PlanningDecisionStatus.NO_STATE_CHANGE)
            assert decision["decision_type"] == decision_type
            assert decision["canonical_hash"] is not None
            assert HtnStore(loop.store).list_plan_revisions(mission.id) == ()
            assert not _events(loop, mission.id, "PlanRevisionCommitted")
            assert _events(loop, mission.id, "PlanningDecisionEvaluated")[-1].payload[
                "status"
            ] == str(PlanningDecisionStatus.NO_STATE_CHANGE)
            if decision_type == "WAIT":
                registered = _events(loop, mission.id, "PlanningWaitRegistered")
                assert len(registered) == 1
                assert registered[0].payload["wait_for"] == [wait_ref]
            assert provider.calls == 0

    asyncio.run(case())


def test_a_blocked_declaration_nobody_can_serve_becomes_a_question_for_the_person(tmp_path: Path) -> None:
    """2026-09-29 真机（收口第 6 项第 3 轮）：执行者报"缺 data/sales.csv"，修复轮里规划器宣告受阻；
    方法本身有效（不会进方法合成），宣告受阻又不产生任何动作，任务以"没有可派发的工作"失败，
    用户只看到失败、从没被问过。改为：没有方法合成可接手时，把受阻原因登记成一条问用户的
    规划问题（阻塞式），任务停下等回答；回答后照原路开下一轮规划。"""

    from agent_orchestrator.storage.planning_human_store import PlanningHumanStore

    async def case() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(loop, tmp_path, key="h1h-blocked-ask")
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(loop.store, tenant_id=mission.tenant_id,
                                     principal=Principal(loop._owner)).issue(
                mission.id, command_id="grant-h1h-blocked-ask", request_id=opener.intent_id)
            body = json.loads((_FIXTURES / "declare-blocked.json").read_text(encoding="utf-8"))
            body["subject_key"] = opener.config["planning_package"]["planning_subjects"][0]["subject_key"]
            body["payload"]["blockers"] = [{"code": "OTHER", "detail": "工作区里没有 data/sales.csv，无法汇总"}]
            reply = serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(body))
            if dispatch.goals_needing_method(mission.id):
                pytest.skip("this seed routes the goal to method synthesis")

            await loop._collect_plan_decision(opener, object(), mission, reply, dispatch)

            questions = PlanningHumanStore(loop.store).list(mission.id)
            assert len(questions) == 1
            question = questions[0]
            assert question["state"] == "PENDING"
            payload = question["request"]["payload"]
            assert "data/sales.csv" in payload["question"] and payload["blocking"] is True
            assert payload["options"] == []
            assert _events(loop, mission.id, "PlanningHumanRequested")
            # 等人回答是合法等待：不会被当成"没有可派发的工作"
            assert loop._has_pending_planning_waits(mission.id) is True
            assert provider.calls == 0

            # 2026-09-29 真机第 4 轮：回答只进规划器，重做的执行者看不到，照样报缺数据。
            # 回答要作为任务级用户备注记下，之后每个执行者开工时都能看到。
            PlanningHumanStore(loop.store).answer(
                decision_id=question["decision_id"], tenant_id=mission.tenant_id,
                principal=Principal(loop._owner), answer="数据如下：2026-07,华东,120",
                expected_version=question["version"], nonce="n-1")
            notes = [e for e in _events(loop, mission.id, "HumanCommentAdded")
                     if e.payload.get("target_id") == mission.id]
            assert len(notes) == 1
            assert "data/sales.csv" in notes[0].payload["text"]
            assert "2026-07,华东,120" in notes[0].payload["text"]
            assert notes[0].payload["via"] == f"planning_answer:{question['decision_id']}"

    asyncio.run(case())
