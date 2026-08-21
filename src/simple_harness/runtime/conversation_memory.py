# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Product-neutral conversation and durable Memory boundary contracts."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.contracts.messages import (
    ContentBlock,
    Message,
    MessageContent,
    MessageRole,
)

from .agent_memory import AgentIdentity, MemoryScopeKind, MemoryScopeRef


class ConversationMemoryRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ContextPreparationMode(StrEnum):
    SDK_PREPARED = "sdk_prepared"
    CONSUMER_PREPARED = "consumer_prepared"


class ConversationMemoryQueryStatus(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    TIMEOUT = "timeout"


class ConversationMemoryApplyStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


class ConversationMemoryErrorCode(StrEnum):
    QUERY_CONFLICT = "memory_query_conflict"
    APPLY_CONFLICT = "memory_apply_conflict"
    TRANSIENT = "memory_transient"
    PERMANENT = "memory_permanent"
    TIMEOUT = "memory_timeout"


class ConversationMemoryError(RuntimeError):
    """Stable adapter-boundary failure without provider/storage details."""

    def __init__(self, code: ConversationMemoryErrorCode) -> None:
        self.code = ConversationMemoryErrorCode(code)
        super().__init__(self.code.value)


def canonicalize_memory_text(value: str) -> str:
    """Return the one textual projection accepted by the durable Memory sink."""

    if not isinstance(value, str):
        raise TypeError("memory_text must be a string")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise ValueError("memory_text must be non-empty")
    if "\x00" in normalized:
        raise ValueError("memory_text must not contain NUL")
    return normalized


def _conversation_message(
    message: Message,
    *,
    role: MessageRole,
    memory_text: str | None,
) -> tuple[Message, str | None]:
    if not isinstance(message, Message):
        raise TypeError("message must use Message")
    if message.role is not role:
        raise ValueError(f"conversation message role must be {role.value}")
    if memory_text is None:
        if isinstance(message.content, str):
            raise ValueError("text conversation messages require memory_text")
        if not message.content:
            raise ValueError("non-text conversation messages require content blocks")
        for block in message.content:
            if block.type not in {"text", "input_text", "output_text"}:
                continue
            text = block.data.get("text")
            if isinstance(text, str) and text.strip():
                raise ValueError("text content blocks require memory_text")
    else:
        memory_text = canonicalize_memory_text(memory_text)
    return message, memory_text


@dataclass(frozen=True, slots=True)
class ConversationTurnInput:
    identity: AgentIdentity
    message: Message
    memory_text: str | None
    recall_scopes: tuple[MemoryScopeRef, ...] = ()
    context_source_snapshot_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must use AgentIdentity")
        message, memory_text = _conversation_message(
            self.message, role=MessageRole.USER, memory_text=self.memory_text
        )
        scopes = tuple(self.recall_scopes) or (
            MemoryScopeRef.personal(self.identity.actor_id),
            MemoryScopeRef.family(self.identity.household_id),
        )
        if not all(isinstance(scope, MemoryScopeRef) for scope in scopes):
            raise TypeError("recall_scopes must contain MemoryScopeRef values")
        allowed = {
            (MemoryScopeKind.PERSONAL, self.identity.actor_id),
            (MemoryScopeKind.FAMILY, self.identity.household_id),
        }
        if any((scope.kind, scope.owner_id) not in allowed for scope in scopes):
            raise ValueError("recall scope is not owned by the trusted identity")
        if len({(scope.kind, scope.owner_id) for scope in scopes}) != len(scopes):
            raise ValueError("recall_scopes must be unique")
        if self.context_source_snapshot_ref is not None:
            _validate_identity(self.context_source_snapshot_ref, "context_source_snapshot_ref")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "memory_text", memory_text)
        object.__setattr__(self, "recall_scopes", scopes)

    @property
    def user_id(self) -> str:
        """Compatibility projection; trusted authority is ``identity.actor_id``."""

        return self.identity.actor_id

    @property
    def session_id(self) -> str:
        return self.identity.session_id

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "identity": self.identity.to_json(),
            "message": self.message.to_dict(),
            "memory_text": self.memory_text,
            "recall_scopes": [scope.to_json() for scope in self.recall_scopes],
            "context_source_snapshot_ref": self.context_source_snapshot_ref,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> ConversationTurnInput:
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise TypeError("conversation identity must be an object")
        raw_scopes = value.get("recall_scopes")
        if not isinstance(raw_scopes, list):
            raise TypeError("recall_scopes must be an array")
        scopes: list[MemoryScopeRef] = []
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping):
                raise TypeError("recall scope must be an object")
            scopes.append(MemoryScopeRef.from_json(raw_scope))
        source_ref = value.get("context_source_snapshot_ref")
        if source_ref is not None and not isinstance(source_ref, str):
            raise TypeError("context_source_snapshot_ref must be a string or null")
        return cls(
            AgentIdentity.from_json(identity),
            _message_from_json(value.get("message")),
            _optional_text(value.get("memory_text")),
            tuple(scopes),
            source_ref,
        )


@dataclass(frozen=True, slots=True)
class ConversationContinuationInput:
    message: Message
    memory_text: str | None

    def __post_init__(self) -> None:
        message, memory_text = _conversation_message(
            self.message, role=MessageRole.USER, memory_text=self.memory_text
        )
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "memory_text", memory_text)

    def to_json(self) -> dict[str, JsonValue]:
        return {"message": self.message.to_dict(), "memory_text": self.memory_text}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> ConversationContinuationInput:
        return cls(
            _message_from_json(value.get("message")),
            _optional_text(value.get("memory_text")),
        )


@dataclass(frozen=True, slots=True)
class ConversationTurnOutput:
    message: Message
    memory_text: str | None

    def __post_init__(self) -> None:
        message, memory_text = _conversation_message(
            self.message, role=MessageRole.ASSISTANT, memory_text=self.memory_text
        )
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "memory_text", memory_text)

    def to_json(self) -> dict[str, JsonValue]:
        return {"message": self.message.to_dict(), "memory_text": self.memory_text}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> ConversationTurnOutput:
        return cls(
            _message_from_json(value.get("message")),
            _optional_text(value.get("memory_text")),
        )


@dataclass(frozen=True, slots=True)
class ConversationMemoryRecallQuery:
    context_query_id: str
    user_id: str
    session_id: str
    query_text: str
    query_hash: str
    max_items: int
    max_bytes: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        for name in ("context_query_id", "user_id", "session_id"):
            _validate_identity(getattr(self, name), name)
        canonical = canonicalize_memory_text(self.query_text)
        object.__setattr__(self, "query_text", canonical)
        _validate_digest(self.query_hash, "query_hash")
        expected = hashlib.sha256(
            canonical_json(
                {
                    "protocol": "harness-memory-context-query-v1",
                    "user_id": self.user_id,
                    "session_id": self.session_id,
                    "query_text": canonical,
                    "max_items": self.max_items,
                    "max_bytes": self.max_bytes,
                }
            ).encode("utf-8")
        ).hexdigest()
        if self.query_hash != expected:
            raise ValueError("query_hash differs from canonical query")
        for name in ("max_items", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")

    @classmethod
    def create(
        cls,
        *,
        context_query_id: str,
        user_id: str,
        session_id: str,
        query_text: str,
        max_items: int,
        max_bytes: int,
        timeout_seconds: float,
    ) -> ConversationMemoryRecallQuery:
        canonical = canonicalize_memory_text(query_text)
        query_hash = hashlib.sha256(
            canonical_json(
                {
                    "protocol": "harness-memory-context-query-v1",
                    "user_id": user_id,
                    "session_id": session_id,
                    "query_text": canonical,
                    "max_items": max_items,
                    "max_bytes": max_bytes,
                }
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            context_query_id,
            user_id,
            session_id,
            canonical,
            query_hash,
            max_items,
            max_bytes,
            timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class ConversationMemoryRecallResult:
    context_query_id: str
    result_id: str
    query_hash: str
    payload: Mapping[str, JsonValue]
    result_hash: str
    status: ConversationMemoryQueryStatus
    item_count: int
    byte_count: int

    def __post_init__(self) -> None:
        for name in ("context_query_id", "result_id"):
            _validate_identity(getattr(self, name), name)
        _validate_digest(self.query_hash, "query_hash")
        _validate_digest(self.result_hash, "result_hash")
        status = ConversationMemoryQueryStatus(self.status)
        frozen = freeze_json(dict(self.payload))
        assert isinstance(frozen, Mapping)
        canonical = canonical_json(thaw_json(cast(FrozenJsonValue, frozen)))
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != self.result_hash:
            raise ValueError("result_hash differs from canonical result payload")
        for name in ("item_count", "byte_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if len(canonical.encode("utf-8")) != self.byte_count:
            raise ValueError("byte_count differs from canonical result payload")
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "status", status)


@dataclass(frozen=True, slots=True)
class ConversationMemoryIntent:
    source_event_id: str
    user_id: str
    session_id: str
    role: ConversationMemoryRole
    memory_text: str | None
    payload_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("source_event_id", "user_id", "session_id"):
            _validate_identity(getattr(self, name), name)
        role = ConversationMemoryRole(self.role)
        memory_text = (
            None if self.memory_text is None else canonicalize_memory_text(self.memory_text)
        )
        payload_hash = hashlib.sha256(
            canonical_json(
                {
                    "protocol": "harness-conversation-memory-intent-v1",
                    "source_event_id": self.source_event_id,
                    "user_id": self.user_id,
                    "session_id": self.session_id,
                    "role": role.value,
                    "memory_text": memory_text,
                }
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "memory_text", memory_text)
        object.__setattr__(self, "payload_hash", payload_hash)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "source_event_id": self.source_event_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "role": self.role.value,
            "memory_text": self.memory_text,
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True, slots=True)
class ConversationMemoryApplyResult:
    source_event_id: str
    payload_hash: str
    status: ConversationMemoryApplyStatus
    record_id: str

    def __post_init__(self) -> None:
        _validate_identity(self.source_event_id, "source_event_id")
        _validate_identity(self.record_id, "record_id")
        _validate_digest(self.payload_hash, "payload_hash")
        object.__setattr__(self, "status", ConversationMemoryApplyStatus(self.status))


def _validate_identity(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _required_string(value: JsonValue | None, name: str) -> str:
    _validate_identity(value, name)
    assert isinstance(value, str)
    return value


def _optional_text(value: JsonValue | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("memory_text must be a string or null")
    return value


def _message_from_json(value: JsonValue | None) -> Message:
    if not isinstance(value, dict):
        raise TypeError("conversation message must be an object")
    role = value.get("role")
    content = value.get("content")
    if not isinstance(role, str):
        raise TypeError("conversation message role must be a string")
    normalized: MessageContent
    if isinstance(content, str):
        normalized = content
    elif isinstance(content, list):
        blocks: list[ContentBlock] = []
        for item in content:
            if not isinstance(item, dict):
                raise TypeError("conversation content blocks must be objects")
            blocks.append(ContentBlock.from_dict(item))
        normalized = tuple(blocks)
    else:
        raise TypeError("conversation message content is invalid")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("conversation message metadata must be an object")
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        raise TypeError("conversation message name must be a string")
    return Message(MessageRole(role), normalized, name=name, metadata=metadata)


def _validate_digest(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


__all__ = (
    "ContextPreparationMode",
    "ConversationContinuationInput",
    "ConversationMemoryApplyResult",
    "ConversationMemoryApplyStatus",
    "ConversationMemoryError",
    "ConversationMemoryErrorCode",
    "ConversationMemoryIntent",
    "ConversationMemoryQueryStatus",
    "ConversationMemoryRecallQuery",
    "ConversationMemoryRecallResult",
    "ConversationMemoryRole",
    "ConversationTurnInput",
    "ConversationTurnOutput",
    "canonicalize_memory_text",
)
