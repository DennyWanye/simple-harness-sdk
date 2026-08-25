# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.tools import (
    CancellationToken,
    FunctionTool,
    MalformedToolArgumentsError,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def test_malformed_error_does_not_disclose_secret_value() -> None:
    canary = "SDK_SECRET_CANARY_8e75597f"
    called = False

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult.succeeded(CallId("call-1"))

    registry = ToolRegistry(
        [
            FunctionTool(
                ToolSpec(
                    "read",
                    "Read public data.",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string", "maxLength": 10}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
                handler,
            )
        ]
    )

    with pytest.raises(MalformedToolArgumentsError) as caught:
        asyncio.run(
            registry.invoke(
                ToolCall(CallId("call-1"), "read", {"secret": canary}),
                ToolContext(RunId("run-1"), RequestId("request-1"), CancellationToken()),
            )
        )

    assert called is False
    assert canary not in str(caught.value)
    assert canary not in repr(caught.value)


def test_handler_exception_does_not_disclose_secret_value(caplog) -> None:
    canary = "SDK_SECRET_CANARY_c43e3500"

    def handler(_arguments: object, _context: ToolContext) -> ToolResult:
        raise RuntimeError(f"upstream body contained {canary}")

    spec = ToolSpec(
        "read",
        "Read public data.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    result = asyncio.run(
        ToolRegistry([FunctionTool(spec, handler)]).invoke(
            ToolCall(CallId("call-2"), "read", {}),
            ToolContext(RunId("run-1"), RequestId("request-1"), CancellationToken()),
        )
    )

    assert result.error_code == "tool_handler_failed"
    assert result.public_message == "Tool execution failed."
    assert canary not in repr(result)
    assert "sdk_tool_handler_failed" in caplog.text
    assert "tool=read" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "stable_code=unclassified" in caplog.text
    assert canary not in caplog.text
