# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable provider invocation ledger contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from simple_harness.contracts import (
    CallId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
    ProviderToolSpec,
    ProviderUsage,
)

from .budget import BudgetCharge


class ProviderInvocationState(StrEnum):
    CLAIMED = "claimed"
    HANDED_OFF = "handed_off"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


TERMINAL_PROVIDER_INVOCATION_STATES = frozenset(
    {
        ProviderInvocationState.SUCCEEDED,
        ProviderInvocationState.FAILED,
        ProviderInvocationState.UNKNOWN,
    }
)


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _message_json(message: Message) -> dict[str, JsonValue]:
    return {
        "role": getattr(message.role, "value", str(message.role)),
        "content": message.content,
        "name": message.name,
        "call_id": None if message.call_id is None else message.call_id.value,
        "metadata": thaw_json(cast(FrozenJsonValue, message.metadata)),
    }


def provider_request_json(request: ProviderRequest) -> dict[str, JsonValue]:
    return {
        "messages": [_message_json(message) for message in request.messages],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": thaw_json(cast(FrozenJsonValue, tool.parameters)),
            }
            for tool in request.tools
        ],
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "metadata": thaw_json(cast(FrozenJsonValue, request.metadata)),
    }


def provider_request_fingerprint(request: ProviderRequest) -> str:
    return _digest(provider_request_json(request))


def provider_request_from_json(request_id: RequestId, value: object) -> ProviderRequest:
    if not isinstance(value, dict):
        raise TypeError("stored Provider request must be an object")
    raw_messages = value.get("messages")
    raw_tools = value.get("tools", [])
    if not isinstance(raw_messages, list) or not isinstance(raw_tools, list):
        raise TypeError("stored Provider request messages/tools are malformed")
    messages = tuple(_message_from_json(item) for item in raw_messages)
    tools: list[ProviderToolSpec] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            raise TypeError("stored Provider tool spec must be an object")
        parameters = item.get("parameters")
        if not isinstance(parameters, dict):
            raise TypeError("stored Provider tool parameters must be an object")
        tools.append(
            ProviderToolSpec(
                str(item["name"]),
                str(item["description"]),
                parameters,  # type: ignore[arg-type]
            )
        )
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("stored Provider metadata must be an object")
    return ProviderRequest(
        request_id,
        messages,
        tools=tuple(tools),
        temperature=value.get("temperature"),  # type: ignore[arg-type]
        max_output_tokens=value.get("max_output_tokens"),  # type: ignore[arg-type]
        metadata=metadata,  # type: ignore[arg-type]
    )


def provider_invocation_id(run_id: RunId, request_id: RequestId) -> str:
    return _digest(
        {
            "protocol": "simple-harness-provider-invocation-v1",
            "run_id": run_id.value,
            "request_id": request_id.value,
        }
    )


def provider_target_json(target: ProviderTarget) -> dict[str, JsonValue]:
    return {
        "provider_id": target.provider_id,
        "model": target.model,
        "pricing_key": target.pricing_key,
        "endpoint_identity": target.endpoint_identity,
        "adapter_key": target.adapter_key,
    }


def provider_target_digest(target: ProviderTarget) -> str:
    return _digest(provider_target_json(target))


def provider_response_json(response: ProviderResponse) -> dict[str, JsonValue]:
    return {
        "request_id": response.request_id.value,
        "message": _message_json(response.message),
        "tool_calls": [
            {
                "call_id": call.call_id.value,
                "name": call.name,
                "arguments": thaw_json(cast(FrozenJsonValue, call.arguments)),
            }
            for call in response.tool_calls
        ],
        "usage": (
            None
            if response.usage is None
            else {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        ),
        "model": response.model,
        "finish_reason": response.finish_reason,
        "provider_request_id": response.provider_request_id,
    }


def _message_from_json(value: object) -> Message:
    if not isinstance(value, dict):
        raise TypeError("stored provider message must be an object")
    role = value.get("role")
    content = value.get("content")
    name = value.get("name")
    raw_call_id = value.get("call_id")
    metadata = value.get("metadata", {})
    if not isinstance(role, str) or not isinstance(content, str):
        raise TypeError("stored provider message is malformed")
    if name is not None and not isinstance(name, str):
        raise ValueError("stored provider message name is malformed")
    if raw_call_id is not None and not isinstance(raw_call_id, str):
        raise ValueError("stored provider message call_id is malformed")
    if not isinstance(metadata, dict):
        raise TypeError("stored provider message metadata is malformed")
    return Message(
        role=MessageRole(role),
        content=content,
        name=name,
        call_id=None if raw_call_id is None else CallId(raw_call_id),
        metadata=metadata,  # type: ignore[arg-type]
    )


def provider_response_from_json(value: object) -> ProviderResponse:
    if not isinstance(value, dict):
        raise TypeError("stored provider response must be an object")
    raw_request_id = value.get("request_id")
    raw_calls = value.get("tool_calls", [])
    raw_usage = value.get("usage")
    if not isinstance(raw_request_id, str) or not isinstance(raw_calls, list):
        raise TypeError("stored provider response is malformed")
    calls: list[ProviderToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise TypeError("stored provider tool call must be an object")
        call_id = raw_call.get("call_id")
        name = raw_call.get("name")
        arguments = raw_call.get("arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, dict)
        ):
            raise TypeError("stored provider tool call is malformed")
        calls.append(ProviderToolCall(CallId(call_id), name, arguments))  # type: ignore[arg-type]
    usage = None
    if raw_usage is not None:
        if not isinstance(raw_usage, dict):
            raise ValueError("stored provider usage must be an object")
        usage = ProviderUsage(
            raw_usage.get("input_tokens"),  # type: ignore[arg-type]
            raw_usage.get("output_tokens"),  # type: ignore[arg-type]
            raw_usage.get("total_tokens"),  # type: ignore[arg-type]
        )
    optional_text: list[str | None] = []
    for name in ("model", "finish_reason", "provider_request_id"):
        item = value.get(name)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"stored provider {name} is malformed")
        optional_text.append(item)
    return ProviderResponse(
        request_id=RequestId(raw_request_id),
        message=_message_from_json(value.get("message")),
        tool_calls=tuple(calls),
        usage=usage,
        model=optional_text[0],
        finish_reason=optional_text[1],
        provider_request_id=optional_text[2],
    )


@dataclass(frozen=True, slots=True)
class ProviderInvocationRecord:
    invocation_id: str
    run_id: RunId
    request_id: RequestId
    state: ProviderInvocationState
    request_fingerprint: str
    target: ProviderTarget
    target_digest: str
    estimator_snapshot: object | None
    estimator_digest: str | None
    budget_charge: BudgetCharge
    response_json: object | None
    usage_json: object | None
    error_code: str | None
    claimed_at: float
    handed_off_at: float | None
    settled_at: float | None
    version: int
    request_json: object | None = None
    handoff_attempt: int = 0
    rehandoff_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ProviderInvocationState(self.state))
        for name in ("invocation_id", "request_fingerprint", "target_digest"):
            value = getattr(self, name)
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if not isinstance(self.run_id, RunId) or not isinstance(
            self.request_id, RequestId
        ):
            raise TypeError("run_id and request_id must use typed identities")
        if not isinstance(self.target, ProviderTarget):
            raise TypeError("target must use ProviderTarget")
        if self.target_digest != provider_target_digest(self.target):
            raise ValueError("target_digest does not match target")
        if (self.estimator_snapshot is None) != (self.estimator_digest is None):
            raise ValueError("estimator snapshot and digest must be paired")
        if self.estimator_snapshot is not None:
            estimator_snapshot = thaw_json(
                cast(FrozenJsonValue, self.estimator_snapshot)
            )
            if self.estimator_digest != _digest(estimator_snapshot):
                raise ValueError("estimator_digest does not match snapshot")
            object.__setattr__(
                self, "estimator_snapshot", freeze_json(estimator_snapshot)
            )
        if self.version < 1 or isinstance(self.version, bool):
            raise ValueError("version must be a positive integer")
        for name in ("handoff_attempt", "rehandoff_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.rehandoff_count > 1 or self.rehandoff_count > self.handoff_attempt:
            raise ValueError("invalid Provider re-handoff counters")
        if self.claimed_at < 0:
            raise ValueError("claimed_at must be non-negative")
        if self.response_json is not None:
            object.__setattr__(
                self,
                "response_json",
                freeze_json(thaw_json(cast(FrozenJsonValue, self.response_json))),
            )
        if self.usage_json is not None:
            object.__setattr__(
                self,
                "usage_json",
                freeze_json(thaw_json(cast(FrozenJsonValue, self.usage_json))),
            )
        if self.request_json is not None:
            request_payload = thaw_json(cast(FrozenJsonValue, self.request_json))
            if not isinstance(request_payload, dict):
                raise TypeError("request_json must be a JSON object")
            if _digest(request_payload) != self.request_fingerprint:
                raise ValueError("request_json does not match request_fingerprint")
            object.__setattr__(self, "request_json", freeze_json(request_payload))

    @classmethod
    def claimed(
        cls,
        *,
        invocation_id: str,
        run_id: RunId,
        request_id: RequestId,
        request_fingerprint: str,
        target: ProviderTarget,
        estimator_snapshot: JsonValue | None,
        estimator_digest: str | None,
        reservation: BudgetCharge,
        claimed_at: float,
        request_json: JsonValue | None = None,
    ) -> ProviderInvocationRecord:
        return cls(
            invocation_id,
            run_id,
            request_id,
            ProviderInvocationState.CLAIMED,
            request_fingerprint,
            target,
            provider_target_digest(target),
            estimator_snapshot,
            estimator_digest,
            reservation,
            None,
            {"budget": reservation.to_json()},
            None,
            claimed_at,
            None,
            None,
            1,
            request_json,
            0,
            0,
        )

    def _expect(self, state: ProviderInvocationState, expected_version: int) -> None:
        if self.version != expected_version:
            raise ValueError("stale provider invocation version")
        if self.state is not state:
            raise ValueError(
                f"provider invocation is {self.state.value}, expected {state.value}"
            )

    def hand_off(self, *, at: float, expected_version: int) -> ProviderInvocationRecord:
        self._expect(ProviderInvocationState.CLAIMED, expected_version)
        return dataclass_replace(
            self,
            state=ProviderInvocationState.HANDED_OFF,
            handed_off_at=at,
            handoff_attempt=self.handoff_attempt + 1,
            version=self.version + 1,
        )

    def settle_succeeded(
        self,
        *,
        response_json: object,
        usage_json: object,
        at: float,
        expected_version: int,
        budget_charge: BudgetCharge | None = None,
    ) -> ProviderInvocationRecord:
        self._expect(ProviderInvocationState.HANDED_OFF, expected_version)
        return dataclass_replace(
            self,
            state=ProviderInvocationState.SUCCEEDED,
            response_json=response_json,
            usage_json=usage_json,
            budget_charge=self.budget_charge
            if budget_charge is None
            else budget_charge,
            settled_at=at,
            version=self.version + 1,
        )

    def settle_failed(
        self, *, error_code: str, at: float, expected_version: int
    ) -> ProviderInvocationRecord:
        self._expect(ProviderInvocationState.HANDED_OFF, expected_version)
        return dataclass_replace(
            self,
            state=ProviderInvocationState.FAILED,
            error_code=error_code,
            usage_json={"budget": self.budget_charge.to_json()},
            settled_at=at,
            version=self.version + 1,
        )

    def settle_unknown(
        self, *, error_code: str, at: float, expected_version: int
    ) -> ProviderInvocationRecord:
        self._expect(ProviderInvocationState.HANDED_OFF, expected_version)
        charge = BudgetCharge.unknown()
        return dataclass_replace(
            self,
            state=ProviderInvocationState.UNKNOWN,
            error_code=error_code,
            budget_charge=charge,
            usage_json={"budget": charge.to_json()},
            settled_at=at,
            version=self.version + 1,
        )


def dataclass_replace(
    record: ProviderInvocationRecord, **changes: Any
) -> ProviderInvocationRecord:
    values = {
        name: getattr(record, name)
        for name in ProviderInvocationRecord.__dataclass_fields__
    }
    values.update(changes)
    return ProviderInvocationRecord(**values)


__all__ = (
    "TERMINAL_PROVIDER_INVOCATION_STATES",
    "ProviderInvocationRecord",
    "ProviderInvocationState",
    "provider_invocation_id",
    "provider_request_fingerprint",
    "provider_request_from_json",
    "provider_request_json",
    "provider_response_from_json",
    "provider_response_json",
    "provider_target_digest",
    "provider_target_json",
)
