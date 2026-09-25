# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 真机文档任务：文档规则检查回执约 1.7～1.9 万字符，单页上限 8192 时永远读不完整、
永远不能引用，任务内容审阅只能给"无法判断"。上限须覆盖这类回执，且工具说明与实现一致。"""
from agent_orchestrator.runtime.tool_gateway import TOOL_SCHEMAS
from agent_orchestrator.verification.reviewer_evidence_tools import DEFAULT_PAGE_CHARS, MAX_PAGE_CHARS

OBSERVED_DOCUMENT_RULE_CHECK_CHARS = 19074


def test_one_read_can_cover_a_document_rule_check_receipt():
    assert MAX_PAGE_CHARS >= OBSERVED_DOCUMENT_RULE_CHECK_CHARS
    schema = TOOL_SCHEMAS["assurance_read_evidence"]["properties"]["max_chars"]
    assert schema["maximum"] == MAX_PAGE_CHARS
    assert DEFAULT_PAGE_CHARS == 4096  # small reads stay cheap unless the reviewer asks
