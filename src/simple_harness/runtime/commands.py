# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Closed public contracts for durable conversation commands."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)

from .conversation_memory import (
    ConversationContinuationInput,
    ConversationTurnInput,
    ConversationTurnOutput,
)

COMMAND_MESSAGE_MAX_BYTES = 256 * 1024
COMMAND_FRAME_MAX_BYTES = 1024 * 1024


class RunApiMode(StrEnum):
    LEGACY = "legacy"
    COMMAND = "command"


class CommandKind(StrEnum):
    START = "start"
    CONTINUE = "continue"
    CANCEL = "cancel"


class CommandState(StrEnum):
    ACCEPTED = "accepted"
    CONTEXT_CALL_INTENT = "context_call_intent"
    CONTEXT_READY = "context_ready"
    APPLIED = "applied"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.APPLIED, self.REJECTED, self.CANCELLED}


class CommandRetryState(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    BACKOFF = "backoff"
    SETTLED = "settled"


class CommandOutputState(StrEnum):
    PENDING = "pending"
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class CommandErrorCode(StrEnum):
    NOT_FOUND = "command_not_found"
    INTENT_CONFLICT = "command_intent_conflict"
    RUN_MODE_CONFLICT = "run_api_mode_conflict"
    NAMESPACE_KEY_CONFLICT = "command_namespace_key_conflict"
    CANCEL_FENCE = "command_cancel_fence"
    PAYLOAD_TOO_LARGE = "command_payload_too_large"
    RETRY_EXHAUSTED = "command_retry_exhausted"
    TRANSIENT_FAILURE = "command_transient_failure"
    PERMANENT_FAILURE = "command_permanent_failure"


class CommandError(RuntimeError):
    """Stable public failure without SQLite or provider details."""

    def __init__(self, code: CommandErrorCode) -> None:
        self.code = CommandErrorCode(code)
        super().__init__(self.code.value)


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    if len(value.encode("utf-8")) > 512:
        raise ValueError(f"{name} is too large")
    return value


def _digest(value: str, name: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class StartCommandIntent:
    namespace: str
    projection_key_id: str
    command_id: str
    run_id: RunId
    request_id: RequestId
    turn_id: str
    conversation: ConversationTurnInput
    profile_key: str = "agent.general"
    tool_catalog_generation: int = 1
    input: Mapping[str, JsonValue] | None = None

    kind = CommandKind.START

    def __post_init__(self) -> None:
        for name in ("namespace", "projection_key_id", "command_id", "turn_id", "profile_key"):
            _identity(getattr(self, name), name)
        if not isinstance(self.run_id, RunId) or not isinstance(self.request_id, RequestId):
            raise TypeError("run_id/request_id must use their typed identifiers")
        if not isinstance(self.conversation, ConversationTurnInput):
            raise TypeError("conversation must use ConversationTurnInput")
        if isinstance(self.tool_catalog_generation, bool) or self.tool_catalog_generation < 1:
            raise ValueError("tool_catalog_generation must be positive")
        if self.input is not None:
            frozen = freeze_json(dict(self.input))
            assert isinstance(frozen, Mapping)
            object.__setattr__(self, "input", frozen)
        _enforce_message_limit(self.to_json())

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "kind": self.kind.value,
            "namespace": self.namespace,
            "projection_key_id": self.projection_key_id,
            "command_id": self.command_id,
            "run_id": self.run_id.value,
            "request_id": self.request_id.value,
            "turn_id": self.turn_id,
            "conversation": self.conversation.to_json(),
            "profile_key": self.profile_key,
            "tool_catalog_generation": self.tool_catalog_generation,
            "input": (None if self.input is None else thaw_json(cast(FrozenJsonValue, self.input))),
        }

    @property
    def intent_hash(self) -> str:
        return _intent_hash(self.to_json())


@dataclass(frozen=True, slots=True)
class ContinueCommandIntent:
    namespace: str
    projection_key_id: str
    command_id: str
    run_id: RunId
    continuation_id: str
    turn_id: str
    conversation: ConversationContinuationInput

    kind = CommandKind.CONTINUE

    def __post_init__(self) -> None:
        for name in (
            "namespace",
            "projection_key_id",
            "command_id",
            "continuation_id",
            "turn_id",
        ):
            _identity(getattr(self, name), name)
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.conversation, ConversationContinuationInput):
            raise TypeError("conversation must use ConversationContinuationInput")
        _enforce_message_limit(self.to_json())

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "kind": self.kind.value,
            "namespace": self.namespace,
            "projection_key_id": self.projection_key_id,
            "command_id": self.command_id,
            "run_id": self.run_id.value,
            "continuation_id": self.continuation_id,
            "turn_id": self.turn_id,
            "conversation": self.conversation.to_json(),
        }

    @property
    def intent_hash(self) -> str:
        return _intent_hash(self.to_json())


@dataclass(frozen=True, slots=True)
class CancelCommandIntent:
    namespace: str
    projection_key_id: str
    command_id: str
    run_id: RunId

    kind = CommandKind.CANCEL

    def __post_init__(self) -> None:
        for name in ("namespace", "projection_key_id", "command_id"):
            _identity(getattr(self, name), name)
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "kind": self.kind.value,
            "namespace": self.namespace,
            "projection_key_id": self.projection_key_id,
            "command_id": self.command_id,
            "run_id": self.run_id.value,
        }

    @property
    def intent_hash(self) -> str:
        return _intent_hash(self.to_json())


CommandIntent = StartCommandIntent | ContinueCommandIntent | CancelCommandIntent


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    run_id: RunId
    kind: CommandKind
    accept_seq: int
    state: CommandState
    version: int
    namespace: str
    projection_key_id: str
    intent_hash: str
    execution_schema_version: int = 5

    def __post_init__(self) -> None:
        for name in ("command_id", "namespace", "projection_key_id"):
            _identity(getattr(self, name), name)
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        object.__setattr__(self, "kind", CommandKind(self.kind))
        object.__setattr__(self, "state", CommandState(self.state))
        if self.accept_seq < 0 or self.version < 1:
            raise ValueError("accept_seq/version are outside their closed ranges")
        _digest(self.intent_hash, "intent_hash")
        if self.execution_schema_version != 5:
            raise ValueError("command receipts require execution schema v5")


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    receipt: CommandReceipt
    retry_state: CommandRetryState
    output_state: CommandOutputState
    output: ConversationTurnOutput | None = None
    error_code: CommandErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CommandReceipt):
            raise TypeError("receipt must use CommandReceipt")
        object.__setattr__(self, "retry_state", CommandRetryState(self.retry_state))
        object.__setattr__(self, "output_state", CommandOutputState(self.output_state))
        if self.output_state is CommandOutputState.PRESENT and self.output is None:
            raise ValueError("present output state requires a closed output")
        if self.output_state is not CommandOutputState.PRESENT and self.output is not None:
            raise ValueError("only present output state may expose output")
        if self.output is not None and not isinstance(self.output, ConversationTurnOutput):
            raise TypeError("output must use ConversationTurnOutput")
        if self.error_code is not None:
            object.__setattr__(self, "error_code", CommandErrorCode(self.error_code))


def _intent_hash(value: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _enforce_message_limit(value: Mapping[str, JsonValue]) -> None:
    if len(canonical_json(dict(value)).encode("utf-8")) > COMMAND_MESSAGE_MAX_BYTES:
        raise CommandError(CommandErrorCode.PAYLOAD_TOO_LARGE)


def command_intent_from_json(value: Mapping[str, JsonValue]) -> CommandIntent:
    raw_kind = value.get("kind")
    if not isinstance(raw_kind, str):
        raise TypeError("kind must be a string")
    kind = CommandKind(raw_kind)
    namespace = _json_text(value, "namespace")
    projection_key_id = _json_text(value, "projection_key_id")
    command_id = _json_text(value, "command_id")
    run_id = RunId(_json_text(value, "run_id"))
    if kind is CommandKind.CANCEL:
        return CancelCommandIntent(namespace, projection_key_id, command_id, run_id)
    raw_conversation = value.get("conversation")
    if not isinstance(raw_conversation, Mapping):
        raise TypeError("command conversation must be an object")
    turn_id = _json_text(value, "turn_id")
    if kind is CommandKind.CONTINUE:
        return ContinueCommandIntent(
            namespace,
            projection_key_id,
            command_id,
            run_id,
            _json_text(value, "continuation_id"),
            turn_id,
            ConversationContinuationInput.from_json(raw_conversation),
        )
    request_id = RequestId(_json_text(value, "request_id"))
    profile_key = _json_text(value, "profile_key")
    generation = value.get("tool_catalog_generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError("tool_catalog_generation must be an integer")
    raw_input = value.get("input")
    if raw_input is not None and not isinstance(raw_input, Mapping):
        raise TypeError("command input must be an object or null")
    return StartCommandIntent(
        namespace,
        projection_key_id,
        command_id,
        run_id,
        request_id,
        turn_id,
        ConversationTurnInput.from_json(raw_conversation),
        profile_key,
        generation,
        raw_input,
    )


def _json_text(value: Mapping[str, JsonValue], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise TypeError(f"{name} must be a string")
    return item
