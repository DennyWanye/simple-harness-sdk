# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic consumer tool executor for BaseAgent tests."""

from __future__ import annotations

from collections.abc import Callable

from simple_harness.tools import ToolResult

ECHO_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class EchoToolExecutor:
    """Succeeds every call; ``on_call`` lets a test advance a fake clock or record."""

    def __init__(self, on_call: Callable[[], None] | None = None) -> None:
        self.calls: list[str] = []
        self.on_call = on_call

    async def execute(self, call, context):  # type: ignore[no-untyped-def]
        del context
        self.calls.append(call.name)
        if self.on_call is not None:
            self.on_call()
        return ToolResult.succeeded(call.call_id, {"echo": call.name})
