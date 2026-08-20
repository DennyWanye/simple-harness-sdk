# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Provider-facing immutable value objects and the single-call port."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from simple_harness.contracts.identity import CallId, RequestId
from simple_harness.contracts.json import JsonValue, freeze_json
from simple_harness.contracts.messages import Message


def _immutable_json_mapping(
    value: Mapping[str, JsonValue], *, field_name: str
) -> Mapping[str, JsonValue]:
    copied = dict(value)
    if not all(isinstance(key, str) for key in copied):
        raise TypeError(f"{field_name} keys must be strings")
    frozen = freeze_json(copied)
    assert isinstance(frozen, Mapping)
    return frozen  # type: ignore[return-value]


@dataclass(frozen=True, slots=True, repr=False)
class Secret:
    """An explicitly injected secret whose string forms are always redacted."""

    _value: str

    def __post_init__(self) -> None:
        if not isinstance(self._value, str) or not self._value:
            raise ValueError("secret must be a non-empty string")

    def reveal(self) -> str:
        """Reveal the value only at the transport boundary."""

        return self._value

    def __str__(self) -> str:
        return "[REDACTED]"

    def __repr__(self) -> str:
        return "Secret([REDACTED])"


class CancelToken:
    """Cooperative cancellation signal accepted by every provider invocation."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    """Stable identity derived by a Provider from its physical call config."""

    provider_id: str
    model: str
    pricing_key: str
    endpoint_identity: str
    adapter_key: str

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "model",
            "pricing_key",
            "endpoint_identity",
            "adapter_key",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class ProviderToolSpec:
    """A provider-neutral structured tool declaration."""

    name: str
    description: str
    parameters: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must not be blank")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("tool description must not be blank")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a mapping")
        object.__setattr__(
            self,
            "parameters",
            _immutable_json_mapping(self.parameters, field_name="parameters"),
        )


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    """A structured tool call returned by a provider."""

    call_id: CallId
    name: str
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, CallId):
            raise TypeError("tool call_id must use CallId")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool call name must not be blank")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be a mapping")
        object.__setattr__(
            self,
            "arguments",
            _immutable_json_mapping(self.arguments, field_name="arguments"),
        )


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Token accounting reported by the provider, without inferred cost."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("token usage values must be non-negative integers")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens cannot be less than input + output")
        for name in ("cache_tokens", "reasoning_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One stateless model invocation."""

    request_id: RequestId
    messages: tuple[Message, ...]
    tools: tuple[ProviderToolSpec, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, RequestId):
            raise TypeError("request_id must use RequestId")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not all(isinstance(message, Message) for message in self.messages):
            raise TypeError("messages must contain Message values")
        if not all(isinstance(tool, ProviderToolSpec) for tool in self.tools):
            raise TypeError("tools must contain ProviderToolSpec values")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            _immutable_json_mapping(self.metadata, field_name="metadata"),
        )
        if not self.messages:
            raise ValueError("provider request requires at least one message")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool) or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Normalized result of exactly one provider invocation."""

    request_id: RequestId
    message: Message
    tool_calls: tuple[ProviderToolCall, ...] = ()
    usage: ProviderUsage | None = None
    model: str | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, RequestId):
            raise TypeError("request_id must use RequestId")
        if not isinstance(self.message, Message):
            raise TypeError("message must use Message")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not all(isinstance(call, ProviderToolCall) for call in self.tool_calls):
            raise TypeError("tool_calls must contain ProviderToolCall values")
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage must use ProviderUsage")


@runtime_checkable
class Provider(Protocol):
    """A stateless port that performs one physical provider call per invocation."""

    @property
    def target(self) -> ProviderTarget:
        """Return identity derived from the same config used by ``invoke``."""

        ...

    async def invoke(
        self, request: ProviderRequest, *, cancel: CancelToken
    ) -> ProviderResponse: ...
