# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public Tool contracts.  Host secrets and policy state stay outside them."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    freeze_json,
    thaw_json,
    canonical_json,
)

from .schema import validate_tool_schema

if TYPE_CHECKING:
    from simple_harness.runtime.workflow_spawn import WorkflowSpawnToolContext
    from .sidecar import Sidecar


JsonObject = Mapping[str, FrozenJsonValue]


def _required(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _freeze_object(value: object, field_name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    frozen = freeze_json(dict(value))
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    return frozen


class ToolOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonObject
    sidecar: Sidecar | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required(self.name, "name"))
        object.__setattr__(self, "description", _required(self.description, "description"))
        if not isinstance(self.input_schema, dict):
            raise TypeError("input_schema must be a JSON object")
        validate_tool_schema(self.input_schema)
        schema = _freeze_object(self.input_schema, "input_schema")
        object.__setattr__(self, "input_schema", schema)
        if self.sidecar is not None:
            from .sidecar import Sidecar

            if not isinstance(self.sidecar, Sidecar):
                raise TypeError("sidecar must use Sidecar")
            if self.sidecar.inventory.name != self.name:
                raise ValueError("ToolSpec and sidecar inventory name differ")
            schema_hash = hashlib.sha256(
                canonical_json(thaw_json(self.input_schema)).encode()
            ).hexdigest()
            if self.sidecar.inventory.schema_hash != schema_hash:
                raise ValueError("ToolSpec and sidecar schema_hash differ")


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: CallId
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, CallId):
            raise TypeError("call_id must use CallId")
        object.__setattr__(self, "name", _required(self.name, "name"))
        object.__setattr__(
            self, "arguments", _freeze_object(self.arguments, "arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: RunId
    request_id: RequestId
    cancellation: CancellationToken
    metadata: JsonObject = field(default_factory=dict)
    workflow_spawn_context: WorkflowSpawnToolContext | None = None
    call_id: CallId | None = None
    effect_id: EffectId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.request_id, RequestId):
            raise TypeError("request_id must use RequestId")
        if self.call_id is not None and not isinstance(self.call_id, CallId):
            raise TypeError("call_id must use CallId")
        if self.effect_id is not None and not isinstance(self.effect_id, EffectId):
            raise TypeError("effect_id must use EffectId")
        if self.workflow_spawn_context is not None:
            from simple_harness.runtime.workflow_spawn import WorkflowSpawnToolContext

            if not isinstance(self.workflow_spawn_context, WorkflowSpawnToolContext):
                raise TypeError("workflow_spawn_context must use the SDK typed context")
        object.__setattr__(self, "metadata", _freeze_object(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: CallId
    outcome: ToolOutcome
    value: FrozenJsonValue = None
    error_code: str | None = None
    public_message: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, CallId):
            raise TypeError("call_id must use CallId")
        if not isinstance(self.outcome, ToolOutcome):
            object.__setattr__(self, "outcome", ToolOutcome(self.outcome))
        object.__setattr__(self, "value", freeze_json(self.value))
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _required(self.error_code, "error_code"))
        if self.public_message is not None:
            object.__setattr__(
                self, "public_message", _required(self.public_message, "public_message")
            )
        if self.outcome in {ToolOutcome.SUCCEEDED, ToolOutcome.PARTIAL}:
            if self.error_code is not None:
                raise ValueError("successful/partial result cannot have error_code")
        elif self.outcome is not ToolOutcome.UNKNOWN and self.error_code is None:
            raise ValueError("rejected/failed result requires error_code")
        if self.outcome is ToolOutcome.UNKNOWN and self.retryable:
            raise ValueError("unknown effect outcome cannot be marked retryable")

    @classmethod
    def succeeded(cls, call_id: CallId, value: JsonValue = None) -> ToolResult:
        return cls(call_id=call_id, outcome=ToolOutcome.SUCCEEDED, value=value)

    @classmethod
    def partial(
        cls, call_id: CallId, value: JsonValue, *, public_message: str | None = None
    ) -> ToolResult:
        return cls(
            call_id=call_id,
            outcome=ToolOutcome.PARTIAL,
            value=value,
            public_message=public_message,
        )

    @classmethod
    def rejected(
        cls, call_id: CallId, error_code: str, public_message: str
    ) -> ToolResult:
        return cls(
            call_id=call_id,
            outcome=ToolOutcome.REJECTED,
            error_code=error_code,
            public_message=public_message,
        )

    @classmethod
    def failed(
        cls,
        call_id: CallId,
        error_code: str,
        public_message: str,
        *,
        retryable: bool = False,
    ) -> ToolResult:
        return cls(
            call_id=call_id,
            outcome=ToolOutcome.FAILED,
            error_code=error_code,
            public_message=public_message,
            retryable=retryable,
        )

    @classmethod
    def unknown(cls, call_id: CallId, public_message: str) -> ToolResult:
        return cls(
            call_id=call_id,
            outcome=ToolOutcome.UNKNOWN,
            error_code="tool_outcome_unknown",
            public_message=public_message,
        )


class CancellationToken:
    """Cooperative cancellation without owning an event-loop task."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    async def wait(self) -> None:
        await self._event.wait()


ToolHandler = Callable[[JsonObject, ToolContext], object | Awaitable[object]]


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    def invoke(
        self, arguments: JsonObject, context: ToolContext
    ) -> object | Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class FunctionTool:
    spec: ToolSpec
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise TypeError("handler must be callable")

    def invoke(
        self, arguments: JsonObject, context: ToolContext
    ) -> object | Awaitable[object]:
        detached = thaw_json(arguments)
        if not isinstance(detached, dict):
            raise TypeError("Tool arguments must thaw to a JSON object")
        return self.handler(detached, context)


async def await_tool_value(value: object | Awaitable[object]) -> object:
    return await value if inspect.isawaitable(value) else value


async def await_tool_result(value: object | Awaitable[object]) -> ToolResult:
    """Compatibility helper for callers that require an already-normalized result."""

    result = await await_tool_value(value)
    if not isinstance(result, ToolResult):
        raise TypeError("Tool handler must return ToolResult")
    return result


__all__ = (
    "CancellationToken",
    "FunctionTool",
    "JsonObject",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
    "await_tool_value",
)
