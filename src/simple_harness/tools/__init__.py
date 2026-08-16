# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public Tool, authorization, and reconciliation contracts."""

from .authorization import (
    AuthorizationDecision,
    AuthorizationPort,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationResult,
    PreparedToolEffect,
    bind_authorization_receipts,
    sdk_authorization_receipt,
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
from .executor import EffectExecution, EffectExecutor, ToolAuthorizationPending
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
    "AuthorizationReceipt",
    "AuthorizationRequest",
    "AuthorizationResult",
    "CancellationToken",
    "DuplicateToolCallError",
    "DuplicateToolError",
    "EffectExecution",
    "EffectExecutor",
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
    "ToolAuthorizationPending",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolReconciliationPort",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "UnknownToolError",
    "bind_authorization_receipts",
    "sdk_authorization_receipt",
)
