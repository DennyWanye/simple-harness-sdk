# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 (Host 真机): the planning bound counts one question, not a life.

A seven-step Mission answered six repair rounds, then died on PLANNING_BOUND_REACHED
because two provider hiccups in an early round and one reply that spelt a severity
``"high"`` were all charged against the same lifetime bound of two.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import UncertaintySeverity, enum_of
from agent_orchestrator.orchestrator.event_handler import (
    PLANNER_TURN_FAILURE_GRACE,
    Orchestrator,
    PlannerTurnFailed,
    planning_failure_detail,
)


def _count(*events: tuple[str, dict]) -> int:
    store = SimpleNamespace(list_events=lambda mission_id: [
        SimpleNamespace(type=kind, payload=payload) for kind, payload in events
    ])
    return Orchestrator._planning_attempts(SimpleNamespace(store=store), "m1")


def test_refusals_before_the_first_commit_count_as_before() -> None:
    assert _count(("PlanningRejected", {}), ("TaskGraphRejected", {})) == 2


def test_a_committed_decision_starts_the_next_question_at_zero() -> None:
    assert _count(
        ("PlanningRejected", {}), ("PlanningRejected", {}),
        ("PlanningDecisionEvaluated", {"status": "COMMITTED"}),
        ("PlanningRejected", {}),
        ("PlanningDecisionEvaluated", {"status": "REJECTED"}),
    ) == 1


def test_a_severity_in_another_case_is_that_severity() -> None:
    assert enum_of(UncertaintySeverity, "high", "uncertainty.severity") is UncertaintySeverity.HIGH
    with pytest.raises(ContractError, match="must be one of"):
        enum_of(UncertaintySeverity, "severe", "uncertainty.severity")


# 2026-09-28 真机：12 轮规划里 5 轮是模型服务端报错与重启打断（规划器没被听到），
# 与答错一样扣次数，任务因"规划次数用完"失败。
_NO_REPLY = ("PlanningRejected", {"reason": "proposal_unreadable", "detail": {"error": "x", "turn_failed": True}})
_WRONG = ("PlanningRejected", {"reason": "proposal_not_grounded", "detail": {"error": "x"}})


def _forgiven(*events: tuple[str, dict]) -> bool:
    store = SimpleNamespace(list_events=lambda mission_id: [
        SimpleNamespace(type=kind, payload=payload) for kind, payload in events
    ])
    return Orchestrator._planner_turn_failure_forgiven(SimpleNamespace(store=store), "m1")


def test_a_turn_without_a_reply_does_not_count_but_a_wrong_answer_does() -> None:
    assert _count(_NO_REPLY, _NO_REPLY, _NO_REPLY, _WRONG) == 1
    assert _forgiven(_WRONG, _NO_REPLY) is True
    assert _forgiven(_NO_REPLY, _WRONG) is False


def test_turns_without_a_reply_count_again_past_the_grace() -> None:
    events = [_NO_REPLY] * (PLANNER_TURN_FAILURE_GRACE + 2)
    assert _count(*events) == 2
    assert _forgiven(*events) is False
    assert _forgiven(*events[:PLANNER_TURN_FAILURE_GRACE]) is True


def test_the_grace_restarts_with_each_committed_decision() -> None:
    events = [_NO_REPLY] * PLANNER_TURN_FAILURE_GRACE
    committed = ("PlanningDecisionEvaluated", {"status": "COMMITTED"})
    assert _count(*events, committed, *events) == 0
    assert _forgiven(*events, committed, _NO_REPLY) is True


def test_only_a_turn_failure_is_marked() -> None:
    assert planning_failure_detail(PlannerTurnFailed("planner turn failed: {}"), {"error": "e"}) == {
        "error": "e", "turn_failed": True}
    assert planning_failure_detail(ContractError("bad block"), {"error": "e"}) == {"error": "e"}


def _reject(detail: dict, *, prior: list[tuple[str, dict]], format_retry_left: int = 0) -> list[tuple[str, object]]:
    import asyncio

    events = list(prior)
    calls: list[tuple[str, object]] = []
    mission = SimpleNamespace(id="m1", status=None)
    fake = SimpleNamespace()
    fake.store = SimpleNamespace(
        get_mission=lambda mission_id: mission,
        list_events=lambda mission_id: [SimpleNamespace(type=k, payload=p) for k, p in events],
    )
    fake._note = lambda text: None
    fake.commit = SimpleNamespace(record_planning_rejected=lambda mission_id, **kw: events.append(
        ("PlanningRejected", {"reason": kw["reason"], "detail": dict(kw["detail"])})))
    fake._planning_format_retry_remaining = lambda **kw: format_retry_left
    fake._planner_turn_failure_forgiven = lambda mission_id: Orchestrator._planner_turn_failure_forgiven(fake, mission_id)

    async def reopen(mission_id, *, ordinal, phase):
        calls.append(("reopen", phase))

    fake._planner_round_on_committed_plan = reopen
    fake._stop_planning_round = lambda mission_id, **kw: calls.append(("stop", kw["reason"]))
    intent = SimpleNamespace(mission_id="m1", config={"ordinal": 5, "planning_decision_attempt_ordinal": 1})
    asyncio.run(Orchestrator._planning_rejected(fake, intent, reason="proposal_unreadable", detail=detail))
    return calls


def test_no_reply_on_the_format_retry_asks_again_instead_of_ending_the_round() -> None:
    no_reply = {"error": "planner turn failed: {}", "turn_failed": True}
    assert _reject(no_reply, prior=[]) == [("reopen", "planner_turn_retry")]
    assert _reject({"error": "bad block"}, prior=[]) == [("stop", "planning_format_retry_exhausted")]
    spent = [_NO_REPLY] * PLANNER_TURN_FAILURE_GRACE
    # 宽限用完：模型服务按不可用处理，连"全新请求"的格式重试也不再开（审阅 2026-09-28）
    assert _reject(no_reply, prior=spent) == [("stop", "planner_turn_failures_exhausted")]
    assert _reject(no_reply, prior=spent, format_retry_left=1) == [("stop", "planner_turn_failures_exhausted")]
    assert _reject(no_reply, prior=[], format_retry_left=1) == [("reopen", "planning_format_retry")]


def test_an_unknown_blocker_code_is_other_with_the_model_words_kept() -> None:
    from agent_orchestrator.contracts.planning_decisions import BlockedItemV1, BlockerCode

    item = BlockedItemV1.from_json({"code": "RUNTIME_UNAVAILABLE", "detail": "上次执行结果丢失"})
    assert item.code is BlockerCode.OTHER
    assert item.detail == "原因（模型原话）：RUNTIME_UNAVAILABLE；上次执行结果丢失"
    assert BlockedItemV1.from_json({"code": "RUNTIME_UNAVAILABLE"}).detail == "原因（模型原话）：RUNTIME_UNAVAILABLE"
    assert BlockedItemV1.from_json({"code": "capability_missing"}).code is BlockerCode.CAPABILITY_MISSING
    with pytest.raises(ContractError):
        BlockedItemV1.from_json({"code": 7})
    with pytest.raises(ContractError):
        BlockedItemV1.from_json({"code": "OTHER"})  # OTHER 本身仍须说明
