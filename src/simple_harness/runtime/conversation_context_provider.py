# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public, read-only non-Memory conversation Context provider contract."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    Message,
    canonical_json,
    freeze_json,
    thaw_json,
)

from .agent_memory import AgentIdentity


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-blank and contain no NUL")
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _snapshot_ref(value: object) -> str:
    text = _text(value, "source_snapshot_ref")
    if not text.startswith("sha256:"):
        raise ValueError("source_snapshot_ref must be content-addressed with sha256")
    _digest(text.removeprefix("sha256:"), "source_snapshot_ref digest")
    return text


@dataclass(frozen=True, slots=True)
class ConversationContextBounds:
    max_items: int = 128
    max_bytes: int = 262_144
    deadline_seconds: float = 3.0

    def __post_init__(self) -> None:
        for value, name in ((self.max_items, "max_items"), (self.max_bytes, "max_bytes")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(float(self.deadline_seconds))
            or self.deadline_seconds <= 0
        ):
            raise ValueError("deadline_seconds must be finite and positive")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "deadline_seconds": float(self.deadline_seconds),
        }


@dataclass(frozen=True, slots=True)
class ConversationContextRequest:
    preparation_id: str
    identity: AgentIdentity
    root_run_id: str
    continuation_id: str | None
    source_snapshot_ref: str
    current_message: Message
    bounds: ConversationContextBounds

    def __post_init__(self) -> None:
        for name in ("preparation_id", "root_run_id"):
            _text(getattr(self, name), name)
        _snapshot_ref(self.source_snapshot_ref)
        if self.continuation_id is not None:
            _text(self.continuation_id, "continuation_id")
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must use AgentIdentity")
        if not isinstance(self.current_message, Message):
            raise TypeError("current_message must use Message")
        if not isinstance(self.bounds, ConversationContextBounds):
            raise TypeError("bounds must use ConversationContextBounds")

    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness-conversation-context/request/v1",
            "preparation_id": self.preparation_id,
            "identity": self.identity.to_json(),
            "root_run_id": self.root_run_id,
            "continuation_id": self.continuation_id,
            "source_snapshot_ref": self.source_snapshot_ref,
            "current_message": self.current_message.to_dict(),
            "bounds": self.bounds.to_json(),
        }


@dataclass(frozen=True, slots=True)
class ConversationContextResult:
    preparation_id: str
    source_snapshot_ref: str
    payload: Mapping[str, JsonValue]
    item_count: int
    byte_count: int
    result_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.preparation_id, "preparation_id")
        _snapshot_ref(self.source_snapshot_ref)
        if (
            isinstance(self.item_count, bool)
            or not isinstance(self.item_count, int)
            or self.item_count < 1
        ):
            raise ValueError("item_count must be a positive integer")
        frozen = freeze_json(dict(self.payload))
        assert isinstance(frozen, Mapping)
        thawed = thaw_json(cast(FrozenJsonValue, frozen))
        assert isinstance(thawed, dict)
        canonical = canonical_json(thawed)
        if len(canonical.encode("utf-8")) != self.byte_count:
            raise ValueError("byte_count differs from canonical payload")
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(
            self, "result_hash", hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )


class ConversationContextProviderPort(Protocol):
    """Stable preparation authority.

    A provider MUST replay the same result for the same ``preparation_id`` and
    canonical request, and MUST reject reuse of that ID with a different request.
    Physical retries are permitted; the durable logical preparation is singular.
    """

    async def prepare_once(
        self, request: ConversationContextRequest
    ) -> ConversationContextResult: ...


class CurrentMessageContextProvider:
    """Deterministic provider for products without additional private Context."""

    async def prepare_once(self, request: ConversationContextRequest) -> ConversationContextResult:
        if not isinstance(request, ConversationContextRequest):
            raise TypeError("request must use ConversationContextRequest")
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "source_snapshot_ref": request.source_snapshot_ref,
            "messages": [request.current_message.to_dict()],
            "current_message": request.current_message.to_dict(),
        }
        byte_count = len(canonical_json(payload).encode("utf-8"))
        if byte_count > request.bounds.max_bytes:
            raise ValueError("current message context exceeds max_bytes")
        return ConversationContextResult(
            request.preparation_id,
            request.source_snapshot_ref,
            payload,
            1,
            byte_count,
        )


def source_snapshot_ref(payload: Mapping[str, JsonValue]) -> str:
    """Return the content-addressed ref an ingress persists before SDK entry."""

    digest = hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = (
    "ConversationContextBounds",
    "ConversationContextProviderPort",
    "ConversationContextRequest",
    "ConversationContextResult",
    "CurrentMessageContextProvider",
    "source_snapshot_ref",
)
