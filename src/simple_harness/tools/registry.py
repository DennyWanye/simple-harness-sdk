# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit Tool registry with validation before host handler dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from simple_harness.contracts import CallId, FrozenJsonValue, thaw_json

from .contracts import (
    FunctionTool,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
    await_tool_value,
)
from .errors import (
    DuplicateToolCallError,
    DuplicateToolError,
    LateToolResultError,
    MalformedToolArgumentsError,
    UnknownToolError,
)
from .schema import ArgumentsValidationError, validate_arguments
from .sidecar import Sidecar, inventory_digest


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
        self._sealed_digest: str | None = None
        for tool in tools:
            self.register(tool)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for _, tool in sorted(self._tools.items()))

    @property
    def calls(self) -> MappingProxyType[CallId, ToolCallState]:
        return MappingProxyType({call_id: record.state for call_id, record in self._calls.items()})

    @property
    def sidecars(self) -> MappingProxyType[str, Sidecar]:
        return MappingProxyType(
            {
                name: tool.spec.sidecar
                for name, tool in sorted(self._tools.items())
                if tool.spec.sidecar is not None
            }
        )

    @property
    def inventory_digest(self) -> str:
        return self._sealed_digest or inventory_digest(self.sidecars)

    @property
    def sealed(self) -> bool:
        return self._sealed_digest is not None

    def seal(self, *, require_sidecars: bool = False) -> str:
        """Freeze registration and return the canonical inventory digest."""

        if require_sidecars:
            missing = tuple(
                name for name, tool in sorted(self._tools.items()) if tool.spec.sidecar is None
            )
            if missing:
                raise ValueError(f"Tool sidecars are required: {missing}")
        digest = inventory_digest(self.sidecars)
        if self._sealed_digest is not None and self._sealed_digest != digest:
            raise RuntimeError("sealed Tool inventory changed")
        self._sealed_digest = digest
        return digest

    def register(self, tool: Tool) -> None:
        if self.sealed:
            raise RuntimeError("ToolRegistry is sealed")
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
            raise MalformedToolArgumentsError(f"{call.name} at {exc.path}: {exc.reason}") from exc
        return tool

    async def invoke(
        self,
        call: ToolCall,
        context: ToolContext,
        *,
        accepted_result_call_id: CallId | None = None,
    ) -> ToolResult:
        """Validate, claim call_id, then enter the handler at most once."""

        tool = self.validate(call)
        if context.cancellation.cancelled:
            return ToolResult.rejected(call.call_id, "tool_cancelled", "Tool call was cancelled.")
        if call.call_id in self._calls:
            raise DuplicateToolCallError(call.call_id.value)
        trusted_context = replace(context, call_id=call.call_id)

        async def dispatch() -> ToolResult:
            try:
                value = tool.invoke(call.arguments, trusted_context)
                raw = await await_tool_value(value)
                result = (
                    tool.spec.sidecar.parse_outcome(call.call_id, raw)
                    if tool.spec.sidecar is not None
                    else raw
                )
                if not isinstance(result, ToolResult):
                    raise TypeError("Tool handler must return ToolResult")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - handler is an untrusted boundary
                # Host exceptions can contain stderr, response bodies, paths,
                # or credentials.  They are private diagnostic causes and do
                # not cross the model-facing ToolResult boundary.
                return ToolResult.failed(
                    call.call_id,
                    "tool_handler_failed",
                    "Tool execution failed.",
                )
            if result.call_id == accepted_result_call_id:
                result = ToolResult(
                    call_id=call.call_id,
                    outcome=result.outcome,
                    value=cast(FrozenJsonValue, thaw_json(result.value)),
                    error_code=result.error_code,
                    public_message=result.public_message,
                    retryable=result.retryable,
                )
            elif result.call_id != call.call_id:
                raise LateToolResultError(f"expected {call.call_id}, got {result.call_id}")
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
                    raise LateToolResultError(call.call_id.value)
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
            raise LateToolResultError(call_id.value) from exc
        if record.state is ToolCallState.RUNNING:
            record.state = ToolCallState.CANCELLED
            record.task.cancel()

    def allow_confirmed_not_started(self, call_id: CallId) -> None:
        """Release only a settled local claim after durable external evidence."""

        record = self._calls.get(call_id)
        if record is None:
            return
        if record.state is ToolCallState.RUNNING:
            raise LateToolResultError("running Tool call cannot be re-authorized")
        del self._calls[call_id]


__all__ = ("ToolCallState", "ToolRegistry")
