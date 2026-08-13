# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable, minimally disclosed Tool error codes."""

from __future__ import annotations


class ToolRegistryError(RuntimeError):
    code = "tool_registry_error"
    public_message = "Tool request could not be processed."

    def __init__(self, detail: str = "") -> None:
        super().__init__(self.public_message)
        self._detail = detail


class DuplicateToolError(ToolRegistryError):
    code = "duplicate_tool"
    public_message = "Tool is already registered."


class UnknownToolError(ToolRegistryError):
    code = "unknown_tool"
    public_message = "Requested Tool is not available."


class MalformedToolArgumentsError(ToolRegistryError):
    code = "malformed_tool_arguments"
    public_message = "Tool arguments are invalid."


class DuplicateToolCallError(ToolRegistryError):
    code = "duplicate_tool_call"
    public_message = "Tool call was already submitted."


class LateToolResultError(ToolRegistryError):
    code = "late_tool_result"
    public_message = "Tool result arrived after the call closed."


__all__ = (
    "DuplicateToolCallError",
    "DuplicateToolError",
    "LateToolResultError",
    "MalformedToolArgumentsError",
    "ToolRegistryError",
    "UnknownToolError",
)
