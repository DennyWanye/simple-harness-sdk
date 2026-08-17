"""Tool executor with calculator and echo tools."""

import json
from typing import Any

from simple_harness.tools import (
    ToolRegistry,
    ToolSpec,
    ToolCall,
    ToolResult,
)


class CalculatorToolExecutor:
    """Execute calculator and echo tools."""

    def __init__(self):
        self.registry = ToolRegistry()
        self._register_tools()

    def _register_tools(self):
        """Register available tools."""

        # Calculator tool
        self.registry.register(
            ToolSpec(
                name="calculate",
                description="Evaluate a mathematical expression",
                input_schema={
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Math expression to evaluate (e.g., '2+2', '10*5')",
                        },
                    },
                    "required": ["expression"],
                    "additionalProperties": False,
                },
            )
        )

        # Echo tool
        self.registry.register(
            ToolSpec(
                name="echo",
                description="Echo back the input text",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to echo",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )
        )

    async def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        """Execute a tool call."""

        # Route to handler
        if call.name == "calculate":
            return await self._calculate(call)
        elif call.name == "echo":
            return await self._echo(call)
        else:
            return ToolResult.rejected(
                call.call_id,
                "unknown_tool",
                f"Tool '{call.name}' not found",
            )

    async def _calculate(self, call: ToolCall) -> ToolResult:
        """Handle calculate tool."""
        try:
            expression = call.arguments["expression"]

            # Security: Only allow safe operations
            if not self._is_safe_expression(expression):
                return ToolResult.rejected(
                    call.call_id,
                    "unsafe_expression",
                    "Expression contains unsafe operations",
                )

            # Evaluate
            result = eval(expression, {"__builtins__": {}}, {})

            print(f"[Tool] calculate(expression={expression!r}) → {result}")

            return ToolResult.succeeded(
                call.call_id,
                {"result": float(result), "expression": expression},
            )

        except (ValueError, SyntaxError, TypeError) as e:
            return ToolResult.failed(
                call.call_id,
                "evaluation_error",
                f"Cannot evaluate expression: {e}",
            )
        except Exception as e:
            # Unknown state
            return ToolResult.unknown(
                call.call_id,
                f"Unexpected error: {type(e).__name__}",
            )

    async def _echo(self, call: ToolCall) -> ToolResult:
        """Handle echo tool."""
        text = call.arguments.get("text", "")

        print(f"[Tool] echo(text={text!r}) → {text!r}")

        return ToolResult.succeeded(
            call.call_id,
            {"echoed": text},
        )

    def _is_safe_expression(self, expr: str) -> bool:
        """Check if expression is safe to evaluate."""
        # Only allow digits, operators, parentheses, spaces
        allowed = set("0123456789+-*/()%. ")
        return all(c in allowed for c in expr)
