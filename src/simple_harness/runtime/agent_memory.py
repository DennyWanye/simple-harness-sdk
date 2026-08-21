# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Canonical Agent Memory v1 contracts owned by Simple Harness SDK."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)


def _identifier(value: object, name: str) -> str:
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


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _timestamp(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative timestamp")
    return float(value)


def _hash(value: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    deployment_id: str
    household_id: str
    actor_id: str
    session_id: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "household_id", "actor_id", "session_id"):
            _identifier(getattr(self, name), name)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "deployment_id": self.deployment_id,
            "household_id": self.household_id,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> AgentIdentity:
        return cls(
            *(
                _identifier(value.get(name), name)
                for name in ("deployment_id", "household_id", "actor_id", "session_id")
            )
        )


class MemoryScopeKind(StrEnum):
    PERSONAL = "personal"
    FAMILY = "family"


@dataclass(frozen=True, slots=True)
class MemoryScopeRef:
    kind: MemoryScopeKind
    owner_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", MemoryScopeKind(self.kind))
        _identifier(self.owner_id, "owner_id")

    @classmethod
    def personal(cls, owner: str) -> MemoryScopeRef:
        return cls(MemoryScopeKind.PERSONAL, owner)

    @classmethod
    def family(cls, owner: str) -> MemoryScopeRef:
        return cls(MemoryScopeKind.FAMILY, owner)

    def to_json(self) -> dict[str, JsonValue]:
        return {"kind": self.kind.value, "owner_id": self.owner_id}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> MemoryScopeRef:
        kind = value.get("kind")
        if not isinstance(kind, str):
            raise TypeError("scope kind must be a string")
        return cls(MemoryScopeKind(kind), _identifier(value.get("owner_id"), "owner_id"))


@dataclass(frozen=True, slots=True)
class MemoryRecallBounds:
    max_items: int = 12
    max_bytes: int = 32_768
    deadline_seconds: float = 2.0

    def __post_init__(self) -> None:
        _positive_int(self.max_items, "max_items")
        _positive_int(self.max_bytes, "max_bytes")
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


class MemoryRecallStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    query_id: str
    turn_id: str
    identity: AgentIdentity
    scopes: tuple[MemoryScopeRef, ...]
    query_text: str
    bounds: MemoryRecallBounds
    turn_started_at: float
    query_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.query_id, "query_id")
        _identifier(self.turn_id, "turn_id")
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must use AgentIdentity")
        scopes = tuple(self.scopes)
        if not scopes or not all(isinstance(scope, MemoryScopeRef) for scope in scopes):
            raise ValueError("scopes must contain MemoryScopeRef values")
        if len({(scope.kind, scope.owner_id) for scope in scopes}) != len(scopes):
            raise ValueError("scopes must be unique")
        if (
            not isinstance(self.query_text, str)
            or not self.query_text.strip()
            or "\x00" in self.query_text
        ):
            raise ValueError("query_text must be non-blank and contain no NUL")
        if not isinstance(self.bounds, MemoryRecallBounds):
            raise TypeError("bounds must use MemoryRecallBounds")
        _timestamp(self.turn_started_at, "turn_started_at")
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "query_hash", _hash(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness-agent-memory/recall-request/v1",
            "query_id": self.query_id,
            "turn_id": self.turn_id,
            "identity": self.identity.to_json(),
            "scopes": [scope.to_json() for scope in self.scopes],
            "query_text": self.query_text,
            "bounds": self.bounds.to_json(),
            "turn_started_at": float(self.turn_started_at),
        }


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    query_id: str
    query_hash: str
    result_id: str
    payload: Mapping[str, JsonValue]
    status: MemoryRecallStatus
    item_count: int
    byte_count: int
    write_fence: str | None
    result_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.query_id, "query_id")
        _identifier(self.result_id, "result_id")
        _digest(self.query_hash, "query_hash")
        object.__setattr__(self, "status", MemoryRecallStatus(self.status))
        if self.write_fence is not None:
            _identifier(self.write_fence, "write_fence")
        if isinstance(self.item_count, bool) or self.item_count < 0:
            raise ValueError("item_count must be non-negative")
        frozen = freeze_json(dict(self.payload))
        assert isinstance(frozen, Mapping)
        thawed = thaw_json(cast(FrozenJsonValue, frozen))
        assert isinstance(thawed, dict)
        byte_count = len(canonical_json(thawed).encode("utf-8"))
        if self.byte_count != byte_count:
            raise ValueError("byte_count differs from canonical payload")
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "result_hash", _hash(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness-agent-memory/recall-result/v1",
            "query_id": self.query_id,
            "query_hash": self.query_hash,
            "result_id": self.result_id,
            "payload": thaw_json(cast(FrozenJsonValue, self.payload)),
            "status": self.status.value,
            "item_count": self.item_count,
            "byte_count": self.byte_count,
            "write_fence": self.write_fence,
        }


@dataclass(frozen=True, slots=True)
class MemoryReleaseRequest:
    query_id: str
    query_hash: str
    result_id: str
    result_hash: str
    write_fence: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.query_id, "query_id")
        _identifier(self.result_id, "result_id")
        _digest(self.query_hash, "query_hash")
        _digest(self.result_hash, "result_hash")
        if self.write_fence is not None:
            _identifier(self.write_fence, "write_fence")


@dataclass(frozen=True, slots=True)
class CommittedTurn:
    turn_id: str
    identity: AgentIdentity
    user_text: str
    assistant_text: str
    write_scope: MemoryScopeRef
    write_fence: str | None
    turn_started_at: float
    payload_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.turn_id, "turn_id")
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must use AgentIdentity")
        if not isinstance(self.write_scope, MemoryScopeRef):
            raise TypeError("write_scope must use MemoryScopeRef")
        if self.write_scope.kind is not MemoryScopeKind.PERSONAL:
            raise ValueError("automatic committed turns write only to personal scope")
        if self.write_scope.owner_id != self.identity.actor_id:
            raise ValueError("personal write scope owner must be the trusted actor")
        for value, name in ((self.user_text, "user_text"), (self.assistant_text, "assistant_text")):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be non-blank and contain no NUL")
        if self.write_fence is not None:
            _identifier(self.write_fence, "write_fence")
        _timestamp(self.turn_started_at, "turn_started_at")
        object.__setattr__(self, "payload_hash", _hash(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness-agent-memory/committed-turn/v1",
            "turn_id": self.turn_id,
            "identity": self.identity.to_json(),
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "write_scope": self.write_scope.to_json(),
            "write_fence": self.write_fence,
            "turn_started_at": float(self.turn_started_at),
        }


class CommittedTurnStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REJECTED_ERASED = "rejected_erased"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class CommittedTurnReceipt:
    turn_id: str
    payload_hash: str
    status: CommittedTurnStatus
    receipt_id: str

    def __post_init__(self) -> None:
        _identifier(self.turn_id, "turn_id")
        _identifier(self.receipt_id, "receipt_id")
        _digest(self.payload_hash, "payload_hash")
        object.__setattr__(self, "status", CommittedTurnStatus(self.status))


class AgentMemoryErrorCode(StrEnum):
    TRANSIENT = "memory_transient"
    TIMEOUT = "memory_timeout"
    CORRUPT_RESULT = "memory_corrupt_result"
    CONFLICT = "memory_conflict"
    PERMANENT = "memory_permanent"


class AgentMemoryError(RuntimeError):
    def __init__(self, code: AgentMemoryErrorCode, *, write_fence: str | None = None) -> None:
        self.code = AgentMemoryErrorCode(code)
        if write_fence is not None:
            _identifier(write_fence, "write_fence")
        self.write_fence = write_fence
        super().__init__(self.code.value)


class MemoryFailurePolicy(StrEnum):
    DEGRADE_RECALL_AND_RETRY_RECORD = "degrade_recall_and_retry_record"


class ResourceOwnership(StrEnum):
    BORROWED = "borrowed"
    RUNTIME = "runtime"


class AgentMemoryPort(Protocol):
    async def recall_for_turn(self, request: MemoryRecallRequest) -> MemoryRecallResult: ...

    async def release_recall(self, request: MemoryReleaseRequest) -> None: ...

    async def record_committed_turn(self, request: CommittedTurn) -> CommittedTurnReceipt: ...


__all__ = (
    "AgentIdentity",
    "AgentMemoryError",
    "AgentMemoryErrorCode",
    "AgentMemoryPort",
    "CommittedTurn",
    "CommittedTurnReceipt",
    "CommittedTurnStatus",
    "MemoryFailurePolicy",
    "MemoryRecallBounds",
    "MemoryRecallRequest",
    "MemoryRecallResult",
    "MemoryRecallStatus",
    "MemoryReleaseRequest",
    "MemoryScopeKind",
    "MemoryScopeRef",
    "ResourceOwnership",
)
