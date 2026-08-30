# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable provider invocation ledger contracts."""

from __future__ import annotations

import hashlib
import re
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
from simple_harness.contracts.messages import ContentBlock, Message, MessageRole
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
    ProviderToolSpec,
    ProviderUsage,
)
from simple_harness.providers.base import (
    ProviderContinuationCapability,
    ProviderContinuationMode,
)

from .budget import BudgetCharge

_PUBLIC_TEXT_BLOCK_FIELDS = {
    "text": frozenset({"text"}),
    "output_text": frozenset({"text"}),
    "refusal": frozenset({"refusal"}),
}
_PUBLIC_MEDIA_BLOCK_FIELDS = {
    "image": frozenset({"url", "data", "body", "media_type", "detail"}),
    "audio": frozenset({"data", "format", "transcript"}),
    "file": frozenset({"file_id", "filename", "mime_type", "url"}),
}
_PUBLIC_IMAGE_URL_FIELDS = frozenset({"image_url"})
_PUBLIC_IMAGE_DETAIL_VALUES = frozenset({"auto", "low", "high"})
_DURABLE_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
)


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
        "content": (
            message.content
            if isinstance(message.content, str)
            else [block.to_dict() for block in message.content]
        ),
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
                parameters,
            )
        )
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("stored Provider metadata must be an object")
    return ProviderRequest(
        request_id,
        messages,
        tools=tuple(tools),
        temperature=value.get("temperature"),
        max_output_tokens=value.get("max_output_tokens"),
        metadata=metadata,
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


def _public_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"public provider {name} must be non-empty text")
    if any(pattern.search(value) for pattern in _DURABLE_CREDENTIAL_PATTERNS):
        raise ValueError(f"public provider {name} contains credential-like material")
    return value


def _strict_public_content_block(block: ContentBlock) -> dict[str, JsonValue]:
    """Return the exact durable/public projection for one provider content block.

    A public block type is not enough to make arbitrary provider-owned fields
    public.  Every supported type therefore has an explicit data schema.  The
    physical response is projected to that schema; the durable decoder later
    requires the stored block to equal this projection exactly.
    """

    data = thaw_json(cast(FrozenJsonValue, block.data))
    if not isinstance(data, dict):
        raise TypeError("public provider content block data must be an object")
    if block.type in _PUBLIC_TEXT_BLOCK_FIELDS:
        expected = _PUBLIC_TEXT_BLOCK_FIELDS[block.type]
        key = next(iter(expected))
        if key not in data:
            raise ValueError("public provider content block fields differ")
        return {"type": block.type, key: _public_text(data[key], key)}
    if block.type == "image_url":
        if not _PUBLIC_IMAGE_URL_FIELDS.issubset(data):
            raise ValueError("public provider image_url block fields differ")
        image_url = data["image_url"]
        if not isinstance(image_url, dict):
            raise ValueError("public provider image_url value fields differ")
        if "url" not in image_url:
            raise ValueError("public provider image_url requires url")
        projected: dict[str, JsonValue] = {"url": _public_text(image_url["url"], "url")}
        detail = image_url.get("detail")
        if detail is not None:
            if not isinstance(detail, str) or detail not in _PUBLIC_IMAGE_DETAIL_VALUES:
                raise ValueError("public provider image_url detail is invalid")
            projected["detail"] = detail
        return {"type": block.type, "image_url": projected}
    if block.type in _PUBLIC_MEDIA_BLOCK_FIELDS:
        allowed = _PUBLIC_MEDIA_BLOCK_FIELDS[block.type]
        public_data = {key: item for key, item in data.items() if key in allowed}
        required_any = {
            "image": {"url", "data", "body"},
            "audio": {"data", "transcript"},
            "file": {"file_id", "url"},
        }[block.type]
        if not required_any.intersection(public_data):
            raise ValueError("public provider media block lacks public content")
        projected_media: dict[str, JsonValue] = {}
        for key, item in public_data.items():
            if key == "detail":
                if not isinstance(item, str) or item not in _PUBLIC_IMAGE_DETAIL_VALUES:
                    raise ValueError("public provider image detail is invalid")
                projected_media[key] = item
            else:
                projected_media[key] = _public_text(item, key)
        return {"type": block.type, **projected_media}
    raise ValueError("provider response contains an unsupported public content block")


def _durable_public_message_json(
    message: Message,
    capability: ProviderContinuationCapability,
    *,
    allow_empty_text: bool = False,
) -> dict[str, JsonValue]:
    if isinstance(message.content, str):
        content: JsonValue = _public_text(
            message.content, "content", allow_empty=allow_empty_text
        )
    else:
        public: list[JsonValue] = []
        hidden_present = False
        for block in message.content:
            if block.type in {"reasoning", "thinking", "chain_of_thought"}:
                hidden_present = True
                continue
            if block.type not in capability.public_content_types:
                raise ValueError("provider response contains a non-public content block")
            public.append(_strict_public_content_block(block))
        if hidden_present and capability.mode is not ProviderContinuationMode.OPAQUE_REFERENCE:
            raise ValueError("provider hidden reasoning cannot enter durable response state")
        content = public
    return {
        "role": getattr(message.role, "value", str(message.role)),
        "content": content,
        "name": message.name,
        "call_id": None if message.call_id is None else message.call_id.value,
        "metadata": {},
    }


def provider_response_json(
    response: ProviderResponse,
    *,
    capability: ProviderContinuationCapability | None = None,
) -> dict[str, JsonValue]:
    capability = capability or ProviderContinuationCapability()
    if capability.mode is ProviderContinuationMode.REJECT:
        raise ValueError("provider continuation capability is rejected")
    has_hidden = not isinstance(response.message.content, str) and any(
        block.type in {"reasoning", "thinking", "chain_of_thought"}
        for block in response.message.content
    )
    if (
        has_hidden
        and capability.mode is ProviderContinuationMode.OPAQUE_REFERENCE
        and response.opaque_continuation_ref is None
    ):
        raise ValueError("opaque reasoning continuation requires a public reference")
    return {
        "request_id": response.request_id.value,
        "message": _durable_public_message_json(
            response.message,
            capability,
            allow_empty_text=bool(response.tool_calls),
        ),
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
                "cache_tokens": response.usage.cache_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
            }
        ),
        "model": response.model,
        "finish_reason": response.finish_reason,
        "provider_request_id": response.provider_request_id,
        "continuation": {
            "schema_version": 1,
            "mode": capability.mode.value,
            "public_content_types": list(capability.public_content_types),
            "capability_fingerprint": capability.fingerprint,
            "opaque_ref": response.opaque_continuation_ref,
        },
    }


def _message_from_json(value: object) -> Message:
    if not isinstance(value, dict):
        raise TypeError("stored provider message must be an object")
    role = value.get("role")
    content = value.get("content")
    name = value.get("name")
    raw_call_id = value.get("call_id")
    metadata = value.get("metadata", {})
    if not isinstance(role, str) or not isinstance(content, (str, list)):
        raise TypeError("stored provider message is malformed")
    normalized_content = (
        content
        if isinstance(content, str)
        else tuple(ContentBlock.from_dict(item) for item in content if isinstance(item, dict))
    )
    if isinstance(content, list) and len(normalized_content) != len(content):
        raise TypeError("stored provider content block is malformed")
    if name is not None and not isinstance(name, str):
        raise ValueError("stored provider message name is malformed")
    if raw_call_id is not None and not isinstance(raw_call_id, str):
        raise ValueError("stored provider message call_id is malformed")
    if not isinstance(metadata, dict):
        raise TypeError("stored provider message metadata is malformed")
    return Message(
        role=MessageRole(role),
        content=normalized_content,
        name=name,
        call_id=None if raw_call_id is None else CallId(raw_call_id),
        metadata=metadata,
    )


def provider_response_from_json(
    value: object,
    *,
    expected_capability: ProviderContinuationCapability | None = None,
    allow_legacy_public_response: bool = False,
) -> ProviderResponse:
    if not isinstance(value, dict):
        raise TypeError("stored provider response must be an object")
    required_fields = {
        "request_id",
        "message",
        "tool_calls",
        "usage",
        "model",
        "finish_reason",
        "provider_request_id",
    }
    expected_fields = required_fields | {"continuation"}
    if allow_legacy_public_response and "continuation" not in value:
        if set(value) != required_fields:
            raise ValueError("stored legacy provider response fields differ")
    elif set(value) != expected_fields:
        raise ValueError("stored provider response fields differ")
    raw_request_id = value.get("request_id")
    raw_calls = value.get("tool_calls", [])
    raw_usage = value.get("usage")
    if not isinstance(raw_request_id, str) or not isinstance(raw_calls, list):
        raise TypeError("stored provider response is malformed")
    calls: list[ProviderToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise TypeError("stored provider tool call must be an object")
        if set(raw_call) != {"call_id", "name", "arguments"}:
            raise ValueError("stored provider tool call fields differ")
        call_id = raw_call.get("call_id")
        name = raw_call.get("name")
        arguments = raw_call.get("arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, dict)
        ):
            raise TypeError("stored provider tool call is malformed")
        calls.append(ProviderToolCall(CallId(call_id), name, arguments))
    usage = None
    if raw_usage is not None:
        if not isinstance(raw_usage, dict):
            raise ValueError("stored provider usage must be an object")
        if set(raw_usage) != {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_tokens",
            "reasoning_tokens",
        }:
            raise ValueError("stored provider usage fields differ")
        usage = ProviderUsage(
            raw_usage.get("input_tokens"),  # type: ignore[arg-type]
            raw_usage.get("output_tokens"),  # type: ignore[arg-type]
            raw_usage.get("total_tokens"),  # type: ignore[arg-type]
            cache_tokens=raw_usage.get("cache_tokens"),
            reasoning_tokens=raw_usage.get("reasoning_tokens"),
        )
    optional_text: list[str | None] = []
    for name in ("model", "finish_reason", "provider_request_id"):
        item = value.get(name)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"stored provider {name} is malformed")
        optional_text.append(item)
    continuation = value.get("continuation")
    if continuation is None and allow_legacy_public_response:
        continuation = {
            "schema_version": 1,
            "mode": ProviderContinuationMode.REASONING_DISABLED.value,
            "public_content_types": list(ProviderContinuationCapability().public_content_types),
            "capability_fingerprint": ProviderContinuationCapability().fingerprint,
            "opaque_ref": None,
        }
    if not isinstance(continuation, dict):
        raise ValueError("stored provider continuation must be an object")
    if set(continuation) != {
        "schema_version",
        "mode",
        "public_content_types",
        "capability_fingerprint",
        "opaque_ref",
    }:
        raise ValueError("stored provider continuation fields differ")
    continuation_schema = continuation.get("schema_version")
    if (
        isinstance(continuation_schema, bool)
        or not isinstance(continuation_schema, int)
        or continuation_schema != 1
    ):
        raise ValueError("unsupported provider continuation schema")
    raw_types = continuation.get("public_content_types")
    if not isinstance(raw_types, list) or not all(isinstance(item, str) for item in raw_types):
        raise TypeError("stored provider public content types are malformed")
    capability = ProviderContinuationCapability(
        ProviderContinuationMode(str(continuation.get("mode"))),
        tuple(raw_types),
    )
    if continuation.get("capability_fingerprint") != capability.fingerprint:
        raise ValueError("stored provider continuation capability hash differs")
    if (
        expected_capability is not None
        and capability.fingerprint != expected_capability.fingerprint
    ):
        raise ValueError("stored Provider response uses another continuation capability")
    opaque_ref = continuation.get("opaque_ref")
    if opaque_ref is not None and not isinstance(opaque_ref, str):
        raise ValueError("stored opaque continuation ref is malformed")
    if capability.mode is not ProviderContinuationMode.OPAQUE_REFERENCE and opaque_ref is not None:
        raise ValueError("stored opaque ref is forbidden for continuation mode")
    message_value = value.get("message")
    if not isinstance(message_value, dict) or set(message_value) != {
        "role",
        "content",
        "name",
        "call_id",
        "metadata",
    }:
        raise ValueError("stored public provider message fields differ")
    metadata = message_value.get("metadata")
    if metadata != {}:
        raise ValueError("stored public provider message metadata must be empty")
    message = _message_from_json(message_value)
    if message.role is not MessageRole.ASSISTANT:
        raise ValueError("stored provider response message must be assistant public content")
    if isinstance(message.content, str):
        _public_text(message.content, "content", allow_empty=bool(calls))
    else:
        for block in message.content:
            if block.type not in capability.public_content_types:
                raise ValueError("stored provider response contains a non-public content block")
            if _strict_public_content_block(block) != block.to_dict():
                raise ValueError("stored provider public content projection differs")
    return ProviderResponse(
        request_id=RequestId(raw_request_id),
        message=message,
        tool_calls=tuple(calls),
        usage=usage,
        model=optional_text[0],
        finish_reason=optional_text[1],
        provider_request_id=optional_text[2],
        opaque_continuation_ref=opaque_ref,
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
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if not isinstance(self.run_id, RunId) or not isinstance(self.request_id, RequestId):
            raise TypeError("run_id and request_id must use typed identities")
        if not isinstance(self.target, ProviderTarget):
            raise TypeError("target must use ProviderTarget")
        if self.target_digest != provider_target_digest(self.target):
            raise ValueError("target_digest does not match target")
        if (self.estimator_snapshot is None) != (self.estimator_digest is None):
            raise ValueError("estimator snapshot and digest must be paired")
        if self.estimator_snapshot is not None:
            estimator_snapshot = thaw_json(cast(FrozenJsonValue, self.estimator_snapshot))
            if self.estimator_digest != _digest(estimator_snapshot):
                raise ValueError("estimator_digest does not match snapshot")
            object.__setattr__(self, "estimator_snapshot", freeze_json(estimator_snapshot))
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
            raise ValueError(f"provider invocation is {self.state.value}, expected {state.value}")

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
            budget_charge=self.budget_charge if budget_charge is None else budget_charge,
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


def dataclass_replace(record: ProviderInvocationRecord, **changes: Any) -> ProviderInvocationRecord:
    values = {name: getattr(record, name) for name in ProviderInvocationRecord.__dataclass_fields__}
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
