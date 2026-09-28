# SPDX-License-Identifier: Apache-2.0
"""端到端：保证审查第一次调用"这一轮没提交"（TURN_FAILED）→ 唯一一次第二次调用
（TURN_RETRY，原样重发冻结请求，不带 format_feedback）。第二次正常回答则正式导入；
第二次也失败则写 AssuranceReviewFormatExhausted（REVIEW_TURN_RETRY_EXHAUSTED），
不会有第三次调用。

两种让这一轮失败的方式：
* 模型回了空答复（provider_empty_response，如只有思考没有正文）：确定性失败，
  已观测用量随失败记录保存，账目可结清，TURN_RETRY 通道完整可走（前两个用例）。
* 模型服务端错误（provider_server_error，2026-09-25 UI 全量点击的真实触发）：
  失败的那次调用没有用量，原执行清单记为"账目未完整"。按记账硬规则（多算不少算、
  不冻结），第一次的预留按上限继续扣着，第二次调用单独预留（第三个用例）。

走 AssuredRuntime：真实 Store/Commit/AgentRuntime、原版 Critic 入口/收集器/REVIEW
消费者/官方导入器；只在测试内把脚本化 provider 换成"可按步抛错"的替身。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_orchestrator.assurance.codec import canonical, decode
from agent_orchestrator.contracts.models import ContractError
from simple_harness.providers.errors import ProviderServerError

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))

ACCEPT_REPLY = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
    {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture",
     "limitations": []}], "findings": []}


class _Boom:
    """Script marker: this provider call raises a provider 5xx."""


def _provider_class():
    from _assured_fixture import KnownUsageProvider

    class FailingProvider(KnownUsageProvider):
        async def invoke(self, request, *, cancel):
            if self.script and isinstance(self.script[0], _Boom):
                self.script.pop(0)
                self.requests.append(request)
                raise ProviderServerError(status_code=503)
            return await super().invoke(request, cancel=cancel)

    return FailingProvider


def _invocations(rt):
    return rt.store.connection.execute(
        "SELECT ordinal, json_extract(invocation_json,'$.reason') AS reason, dispatch_intent_id "
        "FROM assurance_review_invocations WHERE mission_id=? ORDER BY ordinal", (rt.mission.id,)).fetchall()


def _classified(rt):
    return [json.loads(row[0]) for row in rt.store.connection.execute(
        "SELECT receipt_json FROM commit_receipts WHERE kind IN "
        "('AssuranceReviewClassified','AssuranceReviewFormatRejected') ORDER BY rowid").fetchall()]


def _request_text(request):
    return "\n".join(m.content if isinstance(m.content, str) else str(m.content) for m in request.messages)


# Empty final (e.g. a reasoning-only reply): provider_empty_response, a definite
# failure whose observed usage is kept, so the first turn settles FAILED with known cost.
EMPTY_FINAL = ""


async def _retried_then_imported(rt):
    verdict, record = await asyncio.wait_for(rt.run_critic(), 30)
    assert verdict.passed, verdict
    assert record is not None and rt.provider.calls == 2

    rows = _invocations(rt)
    assert [(r["ordinal"], r["reason"]) for r in rows] == [(1, "INITIAL"), (2, "TURN_RETRY")]
    classes = _classified(rt)
    assert [(c["invocation_ordinal"], c["classification"]) for c in classes] == [
        (1, "TURN_FAILED"), (2, "READY_FOR_CURRENT_REVIEW")]
    assert classes[0]["error_code"] == "REVIEW_TURN_NOT_COMMITTED"

    # The second request resends the frozen request unchanged: the review input's
    # format_feedback slot stays empty (a FORMAT_REPAIR would fill it).
    intents = [rt.store.get_intent(r["dispatch_intent_id"]) for r in rows]
    assert intents[0].intent_id != intents[1].intent_id
    assert intents[0].config["message"] == intents[1].config["message"]
    assert decode(intents[1].config["message"]["content"])["format_feedback"] == ""
    second = _request_text(rt.provider.requests[1])
    assert '"format_feedback":""' in second
    assert "Previous output failed strict" not in second

    assert rt.store.connection.execute(
        "SELECT COUNT(*) FROM review_records WHERE official=1").fetchone()[0] == 1
    assert rt.store.connection.execute(
        "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceReviewFormatExhausted'").fetchone()[0] == 0


def test_failed_first_turn_is_retried_once_and_imported(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [], provider_class=_provider_class()) as rt:
            rt.provider.script[:] = [EMPTY_FINAL, canonical(ACCEPT_REPLY)]
            await _retried_then_imported(rt)

    asyncio.run(body())


def test_failed_second_turn_is_exhausted_without_a_third_call(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [], provider_class=_provider_class()) as rt:
            # A third scripted reply would be consumed only by a (forbidden) third call.
            rt.provider.script[:] = [EMPTY_FINAL, EMPTY_FINAL, canonical(ACCEPT_REPLY)]
            with pytest.raises(ContractError) as raised:
                await asyncio.wait_for(rt.run_critic(), 30)
            assert ("AssuranceReviewFormatExhausted" in str(raised.value)
                    or "TURN_FAILED" in str(raised.value)), raised.value
            # More pump ticks: the exhausted receipt is final, no new invocation appears.
            for _ in range(3):
                await rt.pump.tick()

            assert rt.provider.calls == 2 and len(rt.provider.script) == 1
            rows = _invocations(rt)
            assert [(r["ordinal"], r["reason"]) for r in rows] == [(1, "INITIAL"), (2, "TURN_RETRY")]
            assert [(c["invocation_ordinal"], c["classification"]) for c in _classified(rt)] == [
                (1, "TURN_FAILED"), (2, "TURN_FAILED")]
            exhausted = rt.store.connection.execute(
                "SELECT receipt_json FROM commit_receipts WHERE kind='AssuranceReviewFormatExhausted'").fetchall()
            assert len(exhausted) == 1
            assert json.loads(exhausted[0][0])["reason"] == "REVIEW_TURN_RETRY_EXHAUSTED"
            assert rt.store.connection.execute(
                "SELECT COUNT(*) FROM review_records WHERE official=1").fetchone()[0] == 0

    asyncio.run(body())


def test_provider_server_error_first_turn_is_retried_once_and_imported(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [], provider_class=_provider_class()) as rt:
            rt.provider.script[:] = [_Boom(), canonical(ACCEPT_REPLY)]
            await _retried_then_imported(rt)
            # Count rule: the 5xx call has no usage fact, so its reservation stays
            # held (counted at its upper bound) — never released as if it cost 0.
            first_intent = rt.store.get_intent(_invocations(rt)[0]["dispatch_intent_id"])
            state = rt.store.connection.execute(
                "SELECT state FROM budget_reservations WHERE subject_id=?", (first_intent.subject_id,)
            ).fetchone()
            assert state is not None and state[0] == "RESERVED"

    asyncio.run(body())


def test_an_interrupted_review_reports_unreconciled_and_keeps_its_hold(tmp_path):
    """2026-09-28 真机：进程重启打断审阅，审阅员回合卡在"调用结果不明"。保障层规定只有旧回合
    正式导入（未提交回执）后才能开第二次审阅，而 DeepSeek 的原调用在运行中无法核对，所以这里
    仍报 "awaits original-call reconciliation"、原调用预留按上限继续扣着；这一步由系统原样重做
    （planning_selection._interrupted_review），不交给规划器。"""
    from _assured_fixture import AssuredRuntime
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from simple_harness.providers.errors import ProviderTransportError

    class _Loss:
        pass

    base = _provider_class()

    class LossyProvider(base):
        async def invoke(self, request, *, cancel):
            if self.script and isinstance(self.script[0], _Loss):
                self.script.pop(0)
                self.requests.append(request)
                raise ProviderTransportError(public_message="scripted transport loss after handoff")
            return await super().invoke(request, cancel=cancel)

    async def body():
        async with AssuredRuntime(tmp_path, [], provider_class=LossyProvider) as rt:
            rt.provider.script[:] = [_Loss(), canonical(ACCEPT_REPLY)]

            async def give_up_when_blocked(intent, liveness):
                return "give_up" if Orchestrator._provider_blocked(liveness) else None

            rt.orch._resolve_provider_blocked_service = give_up_when_blocked
            with pytest.raises(ContractError, match="awaits original-call reconciliation"):
                await asyncio.wait_for(rt.run_critic(), 30)
            rows = _invocations(rt)
            first = rt.store.get_intent(rows[0]["dispatch_intent_id"])
            state = rt.store.connection.execute(
                "SELECT state FROM budget_reservations WHERE subject_id=?", (first.subject_id,)).fetchone()
            assert state is not None and state[0] == "RESERVED"

    asyncio.run(body())
