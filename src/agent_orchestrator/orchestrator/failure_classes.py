# SPDX-License-Identifier: Apache-2.0
"""Whose fault an Attempt failure is — the one table every retry/charge rule reads.

2026-09-28 用户决定（plans/2026-09-28-system-operations）：只有模型自己做错才扣任务次数；
格式没写对、服务出错、执行被打断不扣，直接重做，但同一步合计有上限，防止无限重试。
表里没有的原因一律按模型做错：宁可多扣，不能漏扣。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MODEL = "MODEL"
FORMAT = "FORMAT"
INFRA = "INFRA"
INTERRUPTED = "INTERRUPTED"
NON_MODEL = frozenset({FORMAT, INFRA, INTERRUPTED})

#: 不扣次数的失败，同一步合计达到这个数就停下这一步。
NON_MODEL_FAILURE_CAP = 6

INTERRUPTED_REVIEW = "Assurance review awaits original-call reconciliation"

# 模型原地打转、调了不存在的工具、工具参数不合规定：模型自己的错（审阅 2026-09-29）。
_MODEL_TURN_CODES = frozenset({
    "react_max_turns_exceeded", "react_max_tool_calls_exceeded",
    "tool_not_exposed", "tool_not_exposed_for_agent", "invalid_tool_arguments",
})
_INFRA_TURN_KINDS = frozenset({"provider_unavailable", "provider_error"})
# 单轮墙钟超时：2026-09-29 真机第八局，应用停机/重启期间这一轮的时限走完，重启后报超时。
# 这不是模型把内容做错；按被打断处理（原地重做、不扣次数、同一步合计有上限）。
# 执行层内部异常：第九局重启后"模型调用已交出、结果未知"（ProviderInvocationConflictError）
# 以 base_agent_driver_exception 结束这一轮——运行时自己的异常，从来不是模型做错。
_INTERRUPTED_TURN_CODES = frozenset({"react_wall_clock_exceeded", "base_agent_driver_exception"})
_INTERRUPTED_REASONS = frozenset({
    "executor_stalled", "executor_turn_missing", "executor_agent_missing",
    "provider_outcome_unknown",
})


def interrupted_review(failures: Any) -> bool:
    """Every failure is a content review whose call was interrupted (not a verdict)."""

    items = tuple(failures or ())
    return bool(items) and all(
        isinstance(item, Mapping) and item.get("layer") == "critic_review"
        and item.get("status") == "ERROR" and INTERRUPTED_REVIEW in str(item.get("summary", ""))
        for item in items)


def classify_failure(failure: Mapping[str, Any] | None) -> str:
    """MODEL | FORMAT | INFRA | INTERRUPTED for an Attempt's recorded ``failure``."""

    if not isinstance(failure, Mapping):
        return MODEL
    reason = failure.get("reason")
    if reason == "verification_failed":
        # 审阅被打断时原因字段也是 verification_failed：先看失败明细。
        return INTERRUPTED if interrupted_review(failure.get("failures")) else MODEL
    if reason == "envelope_invalid":
        return FORMAT
    if reason == "turn_failed":
        error = failure.get("error")
        error = error if isinstance(error, Mapping) else {}
        if error.get("error_code") in _MODEL_TURN_CODES:
            return MODEL
        if error.get("error_code") in _INTERRUPTED_TURN_CODES:
            return INTERRUPTED
        if failure.get("error_kind") in _INFRA_TURN_KINDS or error.get("source_kind") == "tool_parse":
            return INFRA
        return MODEL
    if reason == "runtime_unavailable":
        return INFRA
    if reason in _INTERRUPTED_REASONS:
        return INTERRUPTED
    return MODEL


def charges_attempt(failure: Mapping[str, Any] | None) -> bool:
    return classify_failure(failure) not in NON_MODEL


def non_model_failures(attempts: Any) -> int:
    """How many of a Task's Attempts ended for a reason that did not charge an attempt."""

    return sum(1 for attempt in attempts
               if getattr(attempt, "failure", None) and not charges_attempt(attempt.failure))
