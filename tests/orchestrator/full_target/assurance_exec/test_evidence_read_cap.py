# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 真机文档任务：文档规则检查回执约 1.7～1.9 万字符，单页上限 8192 时永远读不完整、
永远不能引用，任务内容审阅只能给"无法判断"。从头读取不超过整读上限的材料时整份返回；
工具说明不变（它属于执行池身份，改了已有执行池就起不来）。"""
import hashlib
import json

from agent_orchestrator.runtime.tool_gateway import TOOL_SCHEMAS
from agent_orchestrator.verification.reviewer_evidence_tools import (
    MAX_PAGE_CHARS,
    MAX_WHOLE_READ_CHARS,
)

OBSERVED_DOCUMENT_RULE_CHECK_CHARS = 19074
# Hash of the assurance_read_evidence schema before this fix: the execution pool
# identity (tool_schema_hash) of existing data directories depends on it.
PUBLISHED_READ_SCHEMA_SHA256 = hashlib.sha256(json.dumps({
    "type": "object",
    "description": (
        "按 ev- 标签读取证据原文（UTF-8）。complete=true 的整段读取进入你的下一次"
        "模型输入后才可引用该标签；分页片段（complete=false）不可作为引用依据。"
    ),
    "properties": {
        "label": {"type": "string", "description": "目录或 find 结果中的 ev- 标签"},
        "offset": {"type": "integer", "minimum": 0, "description": "Unicode 代码点偏移，默认 0"},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 8192, "description": "默认 4096"},
    },
    "required": ["label"],
    "additionalProperties": False,
}, sort_keys=True).encode()).hexdigest()


def test_a_document_rule_check_receipt_is_read_whole_without_changing_the_tool_schema():
    assert MAX_WHOLE_READ_CHARS >= OBSERVED_DOCUMENT_RULE_CHECK_CHARS > MAX_PAGE_CHARS
    schema = TOOL_SCHEMAS["assurance_read_evidence"]
    assert hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == (
        PUBLISHED_READ_SCHEMA_SHA256
    )
