# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit Tool registry with validation before host handler dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from simple_harness.contracts import CallId

from .contracts import (
    FunctionTool,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
    await_tool_result,
)
from .errors import (
    DuplicateToolCallError,
    DuplicateToolError,
    LateToolResultError,
    MalformedToolArgumentsError,
    UnknownToolError,
)
from .schema import ArgumentsValidationError, validate_arguments


class ToolCallState(StrEnum):
    RUNNING = "running"
    SETTLED = "settled"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class _CallRecord:
    state: ToolCallState
    task: asyncio.Task[ToolResult]


class ToolRegistry:
    """Per-runtime registry; it performs no import or environment discovery."""

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._calls: dict[CallId, _CallRecord] = {}
        for tool in tools:
            self.register(tool)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for _, tool in sorted(self._tools.items()))

    @property
    def calls(self) -> MappingProxyType[CallId, ToolCallState]:
        return MappingProxyType(
            {call_id: record.state for call_id, record in self._calls.items()}
        )

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("tool must implement the Tool protocol")
        if tool.spec.name in self._tools:
            raise DuplicateToolError(tool.spec.name)
        self._tools[tool.spec.name] = tool

    def register_function(self, spec: ToolSpec, handler: object) -> None:
        self.register(FunctionTool(spec, handler))  # type: ignore[arg-type]

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(name) from exc

    def validate(self, call: ToolCall) -> Tool:
        tool = self.get(call.name)
        try:
            validate_arguments(call.arguments, tool.spec.input_schema)
        except ArgumentsValidationError as exc:
            raise MalformedToolArgumentsError(
                f"{call.name} at {exc.path}: {exc.reason}"
            ) from exc
        return tool

    async def invoke(self, call: ToolCall, context: ToolContext) -> ToolResult:
        """Validate, claim call_id, then enter the handler at most once."""

        tool = self.validate(call)
        if context.cancellation.cancelled:
            return ToolResult.rejected(
                call.call_id, "tool_cancelled", "Tool call was cancelled."
            )
        if call.call_id in self._calls:
            raise DuplicateToolCallError(call.call_id)

        async def dispatch() -> ToolResult:
            try:
                value = tool.invoke(call.arguments, context)
                result = await await_tool_result(value)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Host exceptions can contain stderr, response bodies, paths,
                # or credentials.  They are private diagnostic causes and do
                # not cross the model-facing ToolResult boundary.
                return ToolResult.failed(
                    call.call_id,
                    "tool_handler_failed",
                    "Tool execution failed.",
                )
            if result.call_id != call.call_id:
                raise LateToolResultError(
                    f"expected {call.call_id}, got {result.call_id}"
                )
            return result

        task = asyncio.create_task(dispatch(), name=f"simple-harness-tool:{call.call_id}")
        record = _CallRecord(ToolCallState.RUNNING, task)
        self._calls[call.call_id] = record
        cancellation_waiter = asyncio.create_task(context.cancellation.wait())
        try:
            try:
                done, _ = await asyncio.wait(
                    {task, cancellation_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancellation_waiter in done and task not in done:
                    record.state = ToolCallState.CANCELLED
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return ToolResult.rejected(
                        call.call_id, "tool_cancelled", "Tool call was cancelled."
                    )
                result = await task
                if record.state is not ToolCallState.RUNNING:
                    raise LateToolResultError(call.call_id)
                record.state = ToolCallState.SETTLED
                return result
            except asyncio.CancelledError:
                record.state = ToolCallState.CANCELLED
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise
        finally:
            cancellation_waiter.cancel()
            try:
                await cancellation_waiter
            except asyncio.CancelledError:
                pass

    def close_call(self, call_id: CallId) -> None:
        try:
            record = self._calls[call_id]
        except KeyError as exc:
            raise LateToolResultError(call_id) from exc
        if record.state is ToolCallState.RUNNING:
            record.state = ToolCallState.CANCELLED
            record.task.cancel()


__all__ = ("ToolCallState", "ToolRegistry")
