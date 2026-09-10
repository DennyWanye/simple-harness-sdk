# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``session_history_search`` / ``session_history_read``: the model's window into its
own Journal (BA-v1.0 §7.6).  Both tools resolve the Agent from the Run they execute
in; there is no ``agent_id`` parameter (BA23)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from simple_harness.contracts import CallId, FrozenJsonValue, JsonValue, thaw_json
from simple_harness.tools import FunctionTool, ToolContext, ToolHandler, ToolResult, ToolSpec

if TYPE_CHECKING:
    from ..runtime import AgentRuntime

SEARCH_TOOL_NAME = "session_history_search"
READ_TOOL_NAME = "session_history_read"

SEARCH_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "关键词、标识符、路径或一句自然语言。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "required": ["query"],
    "additionalProperties": False,
}
READ_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "from_seq": {"type": "integer", "minimum": 1},
        "to_seq": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "required": ["from_seq"],
    "additionalProperties": False,
}


class SessionHistoryTools:
    def __init__(self) -> None:
        self._runtime: AgentRuntime | None = None
        self.searches = 0
        self.reads = 0

    def bind(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def function_tools(self) -> tuple[FunctionTool, FunctionTool]:
        return (
            FunctionTool(
                ToolSpec(
                    SEARCH_TOOL_NAME,
                    "在当前 Agent 自己的会话历史里检索（词面 + 向量融合），返回记录 seq、"
                    "类型、分数与原文；允许零命中。",
                    cast(Mapping[str, FrozenJsonValue], SEARCH_SCHEMA),
                ),
                cast(ToolHandler, self._search),
            ),
            FunctionTool(
                ToolSpec(
                    READ_TOOL_NAME,
                    "按 seq 顺序精确回读当前 Agent 的会话记录原文（分页，不用 top-k 代替全集）。",
                    cast(Mapping[str, FrozenJsonValue], READ_SCHEMA),
                ),
                cast(ToolHandler, self._read),
            ),
        )

    def _agent_id(self, context: ToolContext) -> str | None:
        runtime = self._runtime
        if runtime is None:
            return None
        binding = runtime.uow.read_agent_binding_for_run(context.run_id.value)
        return None if binding is None else binding.agent_id

    async def _search(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.searches += 1
        agent_id = self._agent_id(context)
        if agent_id is None or self._runtime is None or context.call_id is None:
            return ToolResult.failed(
                cast(CallId, context.call_id), "session_history_unbound", "No Agent for this Run."
            )
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 8))
        result = await self._runtime.retriever.search(agent_id, query, limit=limit)
        return ToolResult.succeeded(
            cast(CallId, context.call_id), cast(JsonValue, result.to_json())
        )

    async def _read(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.reads += 1
        agent_id = self._agent_id(context)
        if agent_id is None or self._runtime is None or context.call_id is None:
            return ToolResult.failed(
                cast(CallId, context.call_id), "session_history_unbound", "No Agent for this Run."
            )
        from_seq = int(arguments.get("from_seq", 1))
        to_seq = arguments.get("to_seq")
        page_size = int(arguments.get("page_size", 50))
        records, next_seq = self._runtime.retriever.read(
            agent_id,
            from_seq=from_seq,
            to_seq=None if to_seq is None else int(to_seq),
            page_size=page_size,
        )
        payload = {
            "records": [
                {
                    "seq": r.seq,
                    "kind": r.kind,
                    "turn_id": r.turn_id,
                    "message": thaw_json(r.message_json),
                }
                for r in records
            ],
            "next_seq": next_seq,
        }
        return ToolResult.succeeded(
            cast(CallId, context.call_id), cast(JsonValue, json.loads(json.dumps(payload)))
        )


__all__ = ("READ_TOOL_NAME", "SEARCH_TOOL_NAME", "SessionHistoryTools")
