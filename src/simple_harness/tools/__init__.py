# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public Tool, authorization, and reconciliation contracts."""

from .authorization import (
    AuthorizationDecision,
    AuthorizationPort,
    AuthorizationResult,
    PreparedToolEffect,
)
from .contracts import (
    CancellationToken,
    FunctionTool,
    JsonObject,
    Tool,
    ToolCall,
    ToolContext,
    ToolHandler,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from .errors import (
    DuplicateToolCallError,
    DuplicateToolError,
    LateToolResultError,
    MalformedToolArgumentsError,
    ToolRegistryError,
    UnknownToolError,
)
from .reconciliation import (
    ReconciliationObservation,
    ReconciliationState,
    ToolReconciliationPort,
)
from .registry import ToolCallState, ToolRegistry
from .schema import ArgumentsValidationError, SchemaDefinitionError


__all__ = (
    "ArgumentsValidationError",
    "AuthorizationDecision",
    "AuthorizationPort",
    "AuthorizationResult",
    "CancellationToken",
    "DuplicateToolCallError",
    "DuplicateToolError",
    "FunctionTool",
    "JsonObject",
    "LateToolResultError",
    "MalformedToolArgumentsError",
    "PreparedToolEffect",
    "ReconciliationObservation",
    "ReconciliationState",
    "SchemaDefinitionError",
    "Tool",
    "ToolCall",
    "ToolCallState",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolReconciliationPort",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "UnknownToolError",
)
