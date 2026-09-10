# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tool registry for BaseAgents: schema violations become visible REJECTED observations.

The SDK registry raises ``MalformedToolArgumentsError`` for arguments that violate
a tool's closed schema; in the kernel that exception terminalizes the Run.  A
BaseAgent must survive model protocol violations (acceptance V3), so this
registry answers such a call with a rejection *tool* whose handler returns
``ToolResult(REJECTED, error_code="invalid_tool_arguments")``.  Nothing of the
real tool runs; the effect ledger records the rejection; the model sees it.
"""

from __future__ import annotations

import asyncio
from typing import cast

from simple_harness.tools import FunctionTool, ToolCall, ToolRegistry, ToolResult, ToolSpec
from simple_harness.tools.contracts import Tool, ToolContext, ToolHandler, ToolOutcome
from simple_harness.tools.errors import MalformedToolArgumentsError, UnknownToolError
from simple_harness.tools.permit import _ACTIVE_PERMIT, ToolPermit

INVALID_ARGUMENTS_CODE = "invalid_tool_arguments"
UNKNOWN_TOOL_CODE = "tool_not_exposed"


class BaseAgentToolRegistry(ToolRegistry):
    """Registry shared by every Agent in one runtime; exposure is enforced per Run.

    ``capability_snapshot.tools`` in a Run's start snapshot is the execution boundary
    (closure review F1): a call from a Run whose snapshot does not list the tool is
    rejected before the handler runs, so a child cannot reach ``agent_delegate`` and
    Agent A cannot call a tool configured only for Agent B.
    """

    def __init__(self, tools=(), *, exposure_reader=None, max_concurrent=None) -> None:  # type: ignore[no-untyped-def]
        self.tool_semaphore = None if max_concurrent is None else asyncio.Semaphore(max_concurrent)
        self.tools_in_flight = 0
        self.max_tools_in_flight = 0
        super().__init__(tools)
        self._exposure_reader = exposure_reader
        self._exposure_cache: dict[str, frozenset[str]] = {}

    def exposed_tools(self, run_id: str) -> frozenset[str] | None:
        if self._exposure_reader is None:
            return None
        cached = self._exposure_cache.get(run_id)
        if cached is None:
            names = self._exposure_reader(run_id)
            if names is None:
                return None
            cached = frozenset(str(name) for name in names)
            self._exposure_cache[run_id] = cached
        return cached

    def get(self, name: str) -> Tool:
        return super().get(name)

    def validate(self, call: ToolCall) -> Tool:
        try:
            return cast(Tool, ExposureGuardedTool(super().validate(call), self))
        except UnknownToolError:
            # A hallucinated tool name is a visible rejection too (never a dead Agent).
            spec = ToolSpec(
                call.name,
                "unknown tool",
                {"type": "object", "additionalProperties": False, "properties": {}},
            )
            return cast(
                Tool,
                FunctionTool(
                    spec, cast(ToolHandler, _reject_with(UNKNOWN_TOOL_CODE, "tool is not exposed"))
                ),
            )
        except MalformedToolArgumentsError as error:
            reason = str(error)
            tool = self.get(call.name)

            return cast(
                Tool,
                FunctionTool(
                    tool.spec, cast(ToolHandler, _reject_with(INVALID_ARGUMENTS_CODE, reason))
                ),
            )


def _reject_with(code: str, reason: str):  # type: ignore[no-untyped-def]
    async def reject(arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
        del arguments
        return ToolResult(
            call_id=context.call_id,  # type: ignore[arg-type]
            outcome=ToolOutcome.REJECTED,
            error_code=code,
            public_message=reason[:500],
        )

    return reject


NOT_EXPOSED_CODE = "tool_not_exposed_for_agent"


class ExposureGuardedTool:
    """Wrap a registry tool so the handler runs only for Runs that expose it."""

    def __init__(self, inner: Tool, registry: BaseAgentToolRegistry) -> None:
        self._inner = inner
        self._registry = registry

    @property
    def spec(self) -> ToolSpec:
        return self._inner.spec

    def invoke(self, arguments, context: ToolContext):  # type: ignore[no-untyped-def]
        exposed = self._registry.exposed_tools(context.run_id.value)
        if exposed is not None and self.spec.name not in exposed:
            return _reject_with(NOT_EXPOSED_CODE, f"{self.spec.name} is not exposed to this Agent")(
                {}, context
            )
        semaphore = self._registry.tool_semaphore
        if semaphore is None:
            return self._inner.invoke(arguments, context)

        registry = self._registry

        def on_release() -> None:
            registry.tools_in_flight -= 1

        async def limited():  # type: ignore[no-untyped-def]
            await semaphore.acquire()  # BA35: FIFO across Agents
            registry.tools_in_flight += 1
            registry.max_tools_in_flight = max(
                registry.max_tools_in_flight, registry.tools_in_flight
            )
            # The handler sees its permit through the context variable so a tool that
            # parks on another Agent (agent_delegate) can hand it back before waiting
            # (review C1); the finally releases whatever is still held, exactly once.
            permit = ToolPermit(semaphore, on_release)
            token = _ACTIVE_PERMIT.set(permit)
            try:
                return await self._inner.invoke(arguments, context)
            finally:
                _ACTIVE_PERMIT.reset(token)
                permit.release()

        return limited()


__all__ = (
    "INVALID_ARGUMENTS_CODE",
    "NOT_EXPOSED_CODE",
    "UNKNOWN_TOOL_CODE",
    "BaseAgentToolRegistry",
    "ExposureGuardedTool",
)
