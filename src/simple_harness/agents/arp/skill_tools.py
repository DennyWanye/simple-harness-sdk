# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Model-side catalogue and skill tools on the native plane (model-tools.json:
``skill.discover`` / ``tool.discover`` → CataloguePage, ``skill.load`` → SkillUse,
``skill.execute`` → ToolResultView).

The namespace is forced by the Session (the model never names one), the MODEL access
view hides deployment secrets, and every refusal reaches the model as a named tool
failure. ``ArpModelTools`` bundles these with the session-history tools so the runtime
registers one tool set.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from simple_harness.contracts import CallId, FrozenJsonValue, JsonValue
from simple_harness.tools import FunctionTool, ToolContext, ToolHandler, ToolOutcome, ToolResult, ToolSpec

from . import store
from .errors import ArpError
from .pins import Pin
from .strict import plain
from .tools import ArpSessionHistoryTools

if TYPE_CHECKING:
    from ..runtime import AgentRuntime

SKILL_DISCOVER_TOOL_NAME = "skill_discover"
TOOL_DISCOVER_TOOL_NAME = "tool_discover"
SKILL_LOAD_TOOL_NAME = "skill_load"
SKILL_EXECUTE_TOOL_NAME = "skill_execute"
DEFAULT_PAGE = 16

_PIN = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["skill"]},
        "id": {"type": "string", "minLength": 1, "maxLength": 256},
        "revision": {"type": "integer", "minimum": 0},
        "content_hash": {"type": "string", "minLength": 64, "maxLength": 64},
    },
    "required": ["kind", "id", "revision", "content_hash"],
    "additionalProperties": False,
}
DISCOVER_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "cursor": {"type": "string", "description": "上一页返回的 next_cursor。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
    },
    "additionalProperties": False,
}
LOAD_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"skill_ref": cast(JsonValue, _PIN)},
    "required": ["skill_ref"],
    "additionalProperties": False,
}
EXECUTE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"skill_ref": cast(JsonValue, _PIN), "arguments": {"type": "object", "description": "按该技能 input schema 的 JSON 对象。"}},
    "required": ["skill_ref", "arguments"],
    "additionalProperties": False,
}


class ArpSkillTools:
    def __init__(self) -> None:
        self._runtime: AgentRuntime | None = None
        self.discovers = 0
        self.loads = 0
        self.executes = 0

    def bind(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def function_tools(self) -> tuple[FunctionTool, ...]:
        return (
            FunctionTool(
                ToolSpec(SKILL_DISCOVER_TOOL_NAME, "分页列出本目录里的技能摘要（名称、描述、状态、当前是否可用）；不含文件内容。", cast(Mapping[str, FrozenJsonValue], DISCOVER_SCHEMA)),
                cast(ToolHandler, self._skill_discover),
            ),
            FunctionTool(
                ToolSpec(TOOL_DISCOVER_TOOL_NAME, "分页列出本目录里的工具摘要；发现不等于曝光，只有本请求 schema 里出现的工具才能调用。", cast(Mapping[str, FrozenJsonValue], DISCOVER_SCHEMA)),
                cast(ToolHandler, self._tool_discover),
            ),
            FunctionTool(
                ToolSpec(SKILL_LOAD_TOOL_NAME, "装载一个已准入技能的说明文件到下一次请求的上下文（只读，不执行任何脚本）。返回 SkillUse 回执。", cast(Mapping[str, FrozenJsonValue], LOAD_SCHEMA)),
                cast(ToolHandler, self._skill_load),
            ),
            FunctionTool(
                ToolSpec(SKILL_EXECUTE_TOOL_NAME, "执行一个已准入技能：INSTRUCTIONS 返回说明正文；SCRIPT 经批准的执行器运行并按输出 schema 校验；WORKFLOW 未部署时具名拒绝。", cast(Mapping[str, FrozenJsonValue], EXECUTE_SCHEMA)),
                cast(ToolHandler, self._skill_execute),
            ),
        )

    # ---- resolution -------------------------------------------------------------------------

    def _resolve(self, context: ToolContext) -> tuple[Any, store.SessionRow] | None:
        runtime = self._runtime
        if runtime is None or context.call_id is None:
            return None
        arp = getattr(runtime, "arp", None)
        if arp is None or arp.skill_use is None:
            return None
        binding = runtime.uow.read_agent_binding_for_run(context.run_id.value)
        if binding is None:
            return None
        session = store.read_live_session(runtime.uow.database.connection, binding.agent_id)
        if session is None:
            return None
        return arp, session

    @staticmethod
    def _failure(context: ToolContext, error: ArpError) -> ToolResult:
        return ToolResult.failed(cast(CallId, context.call_id), f"skill_{error.code.lower()}", f"{error.code}: {error}")

    @staticmethod
    def _unbound(context: ToolContext) -> ToolResult:
        return ToolResult.failed(cast(CallId, context.call_id), "skill_unbound", "No Agent for this Run.")

    # ---- handlers ---------------------------------------------------------------------------

    async def _discover(self, kind: str, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.discovers += 1
        resolved = self._resolve(context)
        if resolved is None:
            return self._unbound(context)
        arp, session = resolved
        command = {"namespace_id": arp.catalogue.namespace_id, "kind": kind, "cursor": arguments.get("cursor"), "limit": int(arguments.get("limit", DEFAULT_PAGE))}
        try:
            page = arp.catalogue.page(command, access_view="MODEL")
        except ArpError as error:
            return self._failure(context, error)
        value = _json(page)
        # NEXT-TG-1.0 §11: a Skill in TRIAL is usable by its own evaluation Mission — say so
        # to that Mission's Agents (load/execute re-check it at the call).
        for item in value.get("items", []):
            if (item.get("kind") == "SKILL" and item.get("state") == "TRIAL" and not item.get("current_usable")
                    and item.get("reason_codes") == ["STATE_TRIAL"]
                    and arp.skill_use.trial_session(Pin.from_json(item["definition_ref"]), session)):
                item["current_usable"], item["reason_codes"] = True, []
        return ToolResult.succeeded(cast(CallId, context.call_id), cast(JsonValue, value))

    async def _skill_discover(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        return await self._discover("SKILL", arguments, context)

    async def _tool_discover(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        return await self._discover("TOOL", arguments, context)

    async def _skill_load(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.loads += 1
        resolved = self._resolve(context)
        if resolved is None:
            return self._unbound(context)
        arp, session = resolved
        try:
            use = arp.skill_use.load(session, call_id=cast(CallId, context.call_id).value, request={"skill_ref": arguments.get("skill_ref")})
        except ArpError as error:
            return self._failure(context, error)
        return ToolResult.succeeded(cast(CallId, context.call_id), cast(JsonValue, _json(use)))

    async def _skill_execute(self, arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        self.executes += 1
        resolved = self._resolve(context)
        if resolved is None:
            return self._unbound(context)
        arp, session = resolved
        call_id = cast(CallId, context.call_id)
        try:
            view = await arp.skill_use.execute_async(session, call_id=call_id.value, request={"skill_ref": arguments.get("skill_ref"), "arguments": arguments.get("arguments", {})})
        except ArpError as error:
            return self._failure(context, error)
        value = cast(JsonValue, _json(view))
        if view["status"] == "SUCCEEDED":
            return ToolResult.succeeded(call_id, value)
        if view["status"] == "UNKNOWN":
            return ToolResult(call_id=call_id, outcome=ToolOutcome.UNKNOWN, value=cast(FrozenJsonValue, value))
        code = str(view["error_code"] or "SKILL_OUTPUT_INVALID")
        return ToolResult.failed(call_id, f"skill_{code.lower()}", f"{code}: {view['inline_text'] or view['status']}")


class ArpModelTools:
    """The runtime's one model tool set: session history + catalogue/skill tools."""

    def __init__(self) -> None:
        self.history = ArpSessionHistoryTools()
        self.skills = ArpSkillTools()

    def bind(self, runtime: AgentRuntime) -> None:
        self.history.bind(runtime)
        self.skills.bind(runtime)

    def attach(self, retriever: Any) -> None:
        self.history.attach(retriever)

    def function_tools(self) -> tuple[FunctionTool, ...]:
        return (*self.history.function_tools(), *self.skills.function_tools())

    @property
    def searches(self) -> int:
        return self.history.searches

    @property
    def reads(self) -> int:
        return self.history.reads


def _json(value: Mapping[str, Any]) -> Any:
    return json.loads(json.dumps(plain(value)))


__all__ = ("SKILL_DISCOVER_TOOL_NAME", "SKILL_EXECUTE_TOOL_NAME", "SKILL_LOAD_TOOL_NAME", "TOOL_DISCOVER_TOOL_NAME", "ArpModelTools", "ArpSkillTools")
