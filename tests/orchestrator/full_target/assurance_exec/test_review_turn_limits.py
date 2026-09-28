# SPDX-License-Identifier: Apache-2.0
"""A review turn can read what a real reviewer reads before concluding.

Host real-model run 13 (2026-09-23): 3+2+5 = 10 evidence tool calls exceeded the
old per-turn cap of 8 and every such review turn failed. The turn cap now equals
the gateway binding cap, and leaves room for a final answering model call.
"""

from types import SimpleNamespace

from agent_orchestrator.orchestrator.assurance_review_runtime import (
    REVIEW_MODEL_CALLS,
    REVIEW_TOOL_CALLS,
)
from agent_orchestrator.verification.reviewer_evidence_tools import (
    EVIDENCE_CALLS_WARNING,
    MAX_EVIDENCE_TOOL_CALLS,
    ReviewerEvidenceTools,
)


def test_turn_tool_cap_matches_gateway_cap_and_covers_the_observed_reviewer():
    # 2026-09-29 第六局：循环上限比查看工具上限多留余量，查满后工具拒绝并提示作答，
    # 模型还有机会给结论（相等时循环先截断，两次都交白卷，任务判失败）。
    assert REVIEW_TOOL_CALLS >= MAX_EVIDENCE_TOOL_CALLS + 6
    assert REVIEW_TOOL_CALLS >= 3 + 2 + 5
    # run 18: the MISSION_FINAL reviewer over the root catalogue
    assert REVIEW_TOOL_CALLS >= 3 + 6 + 4 + 5
    # observed: three tool rounds, then one answering call
    assert REVIEW_MODEL_CALLS >= 4 + 1


def test_near_the_cap_every_evidence_result_says_to_conclude():
    used = {"n": 0}
    tools = SimpleNamespace(orchestrator=SimpleNamespace(assembled=SimpleNamespace(
        gateway=SimpleNamespace(executed_calls=lambda _run: used["n"]))))
    notice = ReviewerEvidenceTools._budget_notice
    assert "budget_notice" not in notice(tools, "r", {})
    used["n"] = MAX_EVIDENCE_TOOL_CALLS - 1 - EVIDENCE_CALLS_WARNING
    assert f"只剩 {EVIDENCE_CALLS_WARNING} 次" in notice(tools, "r", {})["budget_notice"]
    used["n"] = MAX_EVIDENCE_TOOL_CALLS
    assert "只剩 0 次" in notice(tools, "r", {})["budget_notice"]
