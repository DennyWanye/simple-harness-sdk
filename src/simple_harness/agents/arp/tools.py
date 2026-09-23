"""Model-side ``session_history_search`` / ``session_history_read`` on the native plane
(model-tools.json: ``session_history.search`` → SearchRequest/SearchPage,
``session_history.read`` → HistoryReadRequest/HistoryReadPage).

Same tool names as the legacy tools (Agent configs keep working); the handlers resolve
the Agent from the Run they execute in and go through ``SessionRetriever`` under the
MODEL_SEARCH / MODEL_READ purposes. The model never names a Session, never sees the
internal recall purpose, and gets every refusal as a named tool failure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from simple_harness.contracts import CallId, FrozenJsonValue, JsonValue
from simple_harness.tools import FunctionTool, ToolContext, ToolHandler, ToolResult, ToolSpec

from . import store
from .errors import ArpError
from .pins import Pin
from .retriever import SessionRetriever
from .strict import digest, plain

if TYPE_CHECKING:
    from ..runtime import AgentRuntime

SEARCH_TOOL_NAME = "session_history_search"
READ_TOOL_NAME = "session_history_read"
DEFAULT_LIMIT = 8
DEFAULT_MAX_BYTES = 16384

SEARCH_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096, "description": "关键词、标识符、seq N、或一句自然语言。"},
        "cursor": {"type": "string", "description": "上一页返回的 next_cursor；有 cursor 时 query/limit/max_bytes 必须与原查询相同。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 32},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 65536},
    },
    "required": ["query"],
    "additionalProperties": False,
}
READ_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "seq_from": {"type": "integer", "minimum": 1},
        "seq_to": {"type": "integer", "minimum": 1, "description": "含端点；缺省到当前历史末尾。"},
        "cursor": {"type": "string", "description": "上一页返回的 next_cursor。"},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 65536},
    },
    "required": ["seq_from"],
    "additionalProperties": False,
}


class ArpSessionHistoryTools:
    """Drop-in replacement for the legacy ``SessionHistoryTools`` (same names, same hooks)."""

    def __init__(self) -> None:
        self._runtime: AgentRuntime | None = None
        self._retriever: SessionRetriever | None = None
        self.searches = 0
        self.reads = 0

    def bind(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def attach(self, retriever: SessionRetriever) -> None:
        self._retriever = retriever

    def function_tools(self) -> tuple[FunctionTool, FunctionTool]:
        return (
            FunctionTool(
                ToolSpec(
                    SEARCH_TOOL_NAME,
                    "在当前 Agent 自己的会话历史索引里检索（词面/精确 seq + 向量融合，冻结快照分页）。"
                    "扫描未完成时返回空 items 和 next_cursor，继续用 cursor 翻页直到 phase=RESULTS；允许零命中。",
                    cast(Mapping[str, FrozenJsonValue], SEARCH_SCHEMA),
                ),
                cast(ToolHandler, self._search),
            ),
            FunctionTool(
                ToolSpec(
                    READ_TOOL_NAME,
                    "按 seq 顺序精确回读当前 Agent 的会话记录原文（UTF-8 边界切片分页，不用 top-k 代替全集）。",
                    cast(Mapping[str, FrozenJsonValue], READ_SCHEMA),
                ),
                cast(ToolHandler, self._read),
            ),
        )

    # ---- resolution -------------------------------------------------------------------------

    def _resolve(self, context: ToolContext) -> tuple[SessionRetriever, store.SessionRow, str | None] | None:
        runtime, retriever = self._runtime, self._retriever
        if runtime is None or retriever is None or context.call_id is None:
            return None
        binding = runtime.uow.read_agent_binding_for_run(context.run_id.value)
        if binding is None:
            return None
        session = store.read_live_session(runtime.uow.database.connection, binding.agent_id)
        if session is None:
            return None
        turn = runtime.uow.read_open_agent_turn(context.run_id.value)
        return retriever, session, None if turn is None else turn.turn_id

    @staticmethod
    def _caller(session: store.SessionRow) -> Pin:
        return Pin("principal", f"agent:{session.agent_id}", 0, digest({"agent": session.agent_id, "session": session.session_id}))

    @staticmethod
    def _failure(context: ToolContext, error: ArpError) -> ToolResult:
        return ToolResult.failed(
            cast(CallId, context.call_id), f"session_history_{error.code.lower()}", f"{error.code}: {error}",
        )

    # ---- handlers ---------------------------------------------------------------------------

    async def _search(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.searches += 1
        resolved = self._resolve(context)
        if resolved is None:
            return ToolResult.failed(cast(CallId, context.call_id), "session_history_unbound", "No Agent for this Run.")
        retriever, session, turn_id = resolved
        request = {
            "schema_version": 1,
            "query": str(arguments.get("query", "")),
            "cursor": arguments.get("cursor"),
            "limit": int(arguments.get("limit", DEFAULT_LIMIT)),
            "max_bytes": int(arguments.get("max_bytes", DEFAULT_MAX_BYTES)),
        }
        try:
            access = retriever.access_for(session, purpose="MODEL_SEARCH", caller_ref=self._caller(session), turn_id=turn_id)
            page = retriever.search(access, request)
        except ArpError as error:
            return self._failure(context, error)
        return ToolResult.succeeded(cast(CallId, context.call_id), cast(JsonValue, _json(page)))

    async def _read(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.reads += 1
        resolved = self._resolve(context)
        if resolved is None:
            return ToolResult.failed(cast(CallId, context.call_id), "session_history_unbound", "No Agent for this Run.")
        retriever, session, turn_id = resolved
        seq_from = int(arguments.get("seq_from", 1))
        seq_to = arguments.get("seq_to")
        if seq_to is None:
            highwater = retriever.arp.index.uow.agent_journal_highwater(session.agent_id)
            seq_to = max(seq_from, int(highwater))
        request = {
            "schema_version": 1,
            "seq_from": seq_from,
            "seq_to": int(seq_to),
            "cursor": arguments.get("cursor"),
            "max_bytes": int(arguments.get("max_bytes", DEFAULT_MAX_BYTES)),
        }
        try:
            access = retriever.access_for(session, purpose="MODEL_READ", caller_ref=self._caller(session), turn_id=turn_id)
            page = retriever.read(access, request)
        except ArpError as error:
            return self._failure(context, error)
        return ToolResult.succeeded(cast(CallId, context.call_id), cast(JsonValue, _json(page)))


def _json(value: Mapping[str, Any]) -> Any:
    return json.loads(json.dumps(plain(value)))


__all__ = ("READ_TOOL_NAME", "SEARCH_TOOL_NAME", "ArpSessionHistoryTools")
