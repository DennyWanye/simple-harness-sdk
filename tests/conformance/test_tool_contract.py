# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.tools import (
    CancellationToken,
    DuplicateToolCallError,
    FunctionTool,
    LateToolResultError,
    MalformedToolArgumentsError,
    SchemaDefinitionError,
    ToolCall,
    ToolCallState,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def _spec() -> ToolSpec:
    return ToolSpec(
        name="project_summary",
        description="Read a bounded project summary.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 40,
                },
                "format": {
                    "type": "string",
                    "enum": ["short", "long"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 8},
                    "maxItems": 2,
                },
            },
            "required": ["path", "format"],
            "additionalProperties": False,
        },
    )


def _context(token: CancellationToken | None = None) -> ToolContext:
    return ToolContext(
        run_id=RunId("run-1"),
        request_id=RequestId("request-1"),
        cancellation=token or CancellationToken(),
    )


def test_valid_arguments_reach_handler_and_return_five_outcomes() -> None:
    seen: list[object] = []

    def handler(arguments: object, _context: ToolContext) -> ToolResult:
        seen.append(arguments)
        return ToolResult.succeeded(CallId("call-1"), {"summary": "ok"})

    registry = ToolRegistry([FunctionTool(_spec(), handler)])
    call = ToolCall(
        call_id=CallId("call-1"),
        name="project_summary",
        arguments={"path": ".", "format": "short", "limit": 3, "tags": ["sdk"]},
    )

    result = asyncio.run(registry.invoke(call, _context()))

    assert result.outcome is ToolOutcome.SUCCEEDED
    assert seen == [
        {"path": ".", "format": "short", "limit": 3, "tags": ["sdk"]}
    ]
    assert registry.calls[CallId("call-1")] is ToolCallState.SETTLED
    assert {
        ToolResult.succeeded(CallId("a")).outcome,
        ToolResult.partial(CallId("b"), {}).outcome,
        ToolResult.rejected(CallId("c"), "denied", "Denied.").outcome,
        ToolResult.failed(CallId("d"), "failed", "Failed.").outcome,
        ToolResult.unknown(CallId("e"), "Unknown.").outcome,
    } == set(ToolOutcome)


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": ".", "format": "short", "extra": True},
        {"path": ".", "format": "short", "limit": True},
        {"path": ".", "format": "medium"},
        {"path": "x" * 41, "format": "short"},
        {"path": ".", "format": "short", "tags": ["a", "b", "c"]},
        {"path": ".", "format": "short", "run-id": "forged"},
    ],
)
def test_malformed_arguments_are_rejected_before_handler(arguments: object) -> None:
    calls = 0

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("call-1"))

    registry = ToolRegistry([FunctionTool(_spec(), handler)])

    with pytest.raises(MalformedToolArgumentsError) as caught:
        asyncio.run(
            registry.invoke(
                ToolCall(CallId("call-1"), "project_summary", arguments),  # type: ignore[arg-type]
                _context(),
            )
        )

    assert calls == 0
    assert caught.value.code == "malformed_tool_arguments"


def test_schema_is_fail_closed_and_cannot_declare_reserved_fields() -> None:
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "unsafe",
            "Unsafe schema.",
            {
                "type": "object",
                "properties": {"authorization": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "loose",
            "Loose schema.",
            {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        )
    with pytest.raises(SchemaDefinitionError):
        ToolSpec(
            "unknown-keyword",
            "Unknown schema keyword.",
            {
                "type": "object",
                "properties": {},
                "patternProperties": {},
                "additionalProperties": False,
            },
        )


def test_duplicate_call_id_never_invokes_handler_twice() -> None:
    calls = 0

    async def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult.succeeded(CallId("same"))

    async def scenario() -> None:
        registry = ToolRegistry([FunctionTool(_spec(), handler)])
        call = ToolCall(
            CallId("same"), "project_summary", {"path": ".", "format": "short"}
        )
        assert (await registry.invoke(call, _context())).outcome is ToolOutcome.SUCCEEDED
        with pytest.raises(DuplicateToolCallError):
            await registry.invoke(call, _context())

    asyncio.run(scenario())
    assert calls == 1


def test_mismatched_or_late_result_is_rejected() -> None:
    registry = ToolRegistry(
        [
            FunctionTool(
                _spec(),
                lambda _arguments, _context: ToolResult.succeeded(CallId("wrong-call")),
            )
        ]
    )
    call = ToolCall(
        CallId("expected"),
        "project_summary",
        {"path": ".", "format": "short"},
    )

    with pytest.raises(LateToolResultError):
        asyncio.run(registry.invoke(call, _context()))


def test_cancelled_call_cancels_handler_and_keeps_stable_result() -> None:
    started = asyncio.Event()
    cancelled = False

    async def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def scenario() -> tuple[ToolResult, ToolCallState]:
        token = CancellationToken()
        registry = ToolRegistry([FunctionTool(_spec(), handler)])
        pending = asyncio.create_task(
            registry.invoke(
                ToolCall(
                    CallId("call-cancel"),
                    "project_summary",
                    {"path": ".", "format": "short"},
                ),
                _context(token),
            )
        )
        await started.wait()
        token.cancel()
        result = await pending
        return result, registry.calls[CallId("call-cancel")]

    result, state = asyncio.run(scenario())
    assert result == ToolResult.rejected(
        CallId("call-cancel"), "tool_cancelled", "Tool call was cancelled."
    )
    assert state is ToolCallState.CANCELLED
    assert cancelled is True


def test_public_contracts_are_immutable() -> None:
    result = ToolResult.succeeded(CallId("call-1"), {"ok": True})

    with pytest.raises(FrozenInstanceError):
        result.call_id = CallId("changed")  # type: ignore[misc]
    with pytest.raises(TypeError):
        _spec().input_schema["type"] = "array"  # type: ignore[index]


def test_no_shell_tool_is_registered_by_default() -> None:
    assert ToolRegistry().specs == ()
