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

from typing import cast

from simple_harness.tools import FunctionTool, ToolCall, ToolRegistry, ToolResult
from simple_harness.tools.contracts import Tool, ToolContext, ToolHandler, ToolOutcome
from simple_harness.tools.errors import MalformedToolArgumentsError

INVALID_ARGUMENTS_CODE = "invalid_tool_arguments"


class BaseAgentToolRegistry(ToolRegistry):
    def validate(self, call: ToolCall) -> Tool:
        try:
            return super().validate(call)
        except MalformedToolArgumentsError as error:
            reason = str(error)
            tool = self.get(call.name)  # unknown names still raise UnknownToolError

            async def reject(arguments: dict, context: ToolContext) -> ToolResult:  # type: ignore[type-arg]
                del arguments
                return ToolResult(
                    call_id=context.call_id,  # type: ignore[arg-type]
                    outcome=ToolOutcome.REJECTED,
                    error_code=INVALID_ARGUMENTS_CODE,
                    public_message=reason[:500],
                )

            return cast(Tool, FunctionTool(tool.spec, cast(ToolHandler, reject)))


__all__ = ("INVALID_ARGUMENTS_CODE", "BaseAgentToolRegistry")
