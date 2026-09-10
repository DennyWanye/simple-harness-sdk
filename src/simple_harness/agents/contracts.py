# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public BaseAgent contracts: identities, turn receipts, turn results, errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, cast

from simple_harness.contracts import (
    CallId,
    ContentBlock,
    FrozenJsonValue,
    JsonValue,
    Message,
    MessageContent,
    MessageRole,
    freeze_json,
    thaw_json,
)
from simple_harness.contracts.identity import _Identifier
from simple_harness.runtime.agent_turn import AgentTurnOutcome, result_json_hash


@dataclass(frozen=True, slots=True)
class AgentId(_Identifier):
    _kind: ClassVar[str] = "agent_id"


@dataclass(frozen=True, slots=True)
class AgentTurnId(_Identifier):
    _kind: ClassVar[str] = "agent_turn_id"


class AgentTurnState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RESULT_PENDING = "result_pending"
    COMMITTED = "committed"
    FAILED = "failed"


class AgentError(Exception):
    code: ClassVar[str] = "agent_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AgentNotFound(AgentError):
    code = "agent_not_found"


class AgentTurnNotFound(AgentError):
    code = "agent_turn_not_found"


class AgentInputConflict(AgentError):
    """Same ``input_id`` re-submitted with different canonical content."""

    code = "agent_input_conflict"


class AgentClosed(AgentError):
    code = "agent_closed"


class AgentBatchRejected(AgentError):
    """``create_many`` refused before any write; ``index`` names the offending config."""

    code = "agent_batch_rejected"

    def __init__(self, error_code: str, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.index = index


class AgentBatchIdentityConflict(AgentError):
    """Same ``batch_key`` reused with different batch content."""

    code = "batch_identity_conflict"


class AgentInstanceCapExceeded(AgentError):
    """``AgentRuntimePorts.max_agents`` reached for this owner (decided at insert time)."""

    code = "agent_instance_cap_exceeded"


class AgentPendingInputsExhausted(AgentError):
    """``AgentLimits.max_pending_inputs`` open turns already queued for this Agent."""

    code = "agent_pending_inputs_exhausted"


class AgentTurnTimeout(AgentError):
    """``wait_turn`` gave up waiting; the underlying turn keeps running."""

    code = "agent_turn_timeout"

    def __init__(self, turn_id: str, receipt: AgentTurnReceipt | None = None) -> None:
        super().__init__(f"turn {turn_id} has no committed result yet")
        self.turn_id = turn_id
        # BA-v1.0 §4.2: a timeout carries the original receipt; the turn keeps running.
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class AgentTurnReceipt:
    """Durable acceptance of one input; returned by ``submit`` before any model call."""

    turn_id: str
    agent_id: str
    input_id: str
    seq: int
    state: AgentTurnState

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", AgentTurnState(self.state))
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise ValueError("seq must be a positive integer")


def _is_frozen(value: object) -> bool:
    return not isinstance(value, dict)


def _message_to_json(message: Message) -> dict[str, JsonValue]:
    return message.to_dict()


def _message_from_json(value: object) -> Message:
    if not isinstance(value, Mapping):
        raise TypeError("message must be an object")
    content = value.get("content")
    normalized: MessageContent
    if isinstance(content, str):
        normalized = content
    elif isinstance(content, (list, tuple)):
        blocks = tuple(
            ContentBlock.from_dict(block) for block in content if isinstance(block, Mapping)
        )
        if len(blocks) != len(content):
            raise TypeError("message content blocks must be objects")
        normalized = blocks
    else:
        raise TypeError("message content must be text or content blocks")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("message metadata must be an object")
    name = value.get("name")
    call_id = value.get("call_id")
    return Message(
        MessageRole(str(value.get("role"))),
        normalized,
        name=name if isinstance(name, str) else None,
        call_id=CallId(call_id) if isinstance(call_id, str) else None,
        metadata=dict(metadata),
    )


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Public output of one AgentTurn.  Not a ``ConversationTurnOutput``: it never
    ends the Agent's lifecycle."""

    turn_id: str
    agent_id: str
    seq: int
    state: AgentTurnState
    public_output: Message | None = None
    artifact_refs: tuple[str, ...] = ()
    usage_refs: tuple[str, ...] = ()
    delegation_count: int = 0
    error: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        state = AgentTurnState(self.state)
        if state not in {AgentTurnState.COMMITTED, AgentTurnState.FAILED}:
            raise ValueError("a turn result is either committed or failed")
        object.__setattr__(self, "state", state)
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise ValueError("seq must be a positive integer")
        if self.public_output is not None and not isinstance(self.public_output, Message):
            raise TypeError("public_output must use Message")
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        object.__setattr__(self, "usage_refs", tuple(self.usage_refs))
        if (
            isinstance(self.delegation_count, bool)
            or not isinstance(self.delegation_count, int)
            or self.delegation_count < 0
        ):
            raise ValueError("delegation_count must be a non-negative integer")
        if self.error is not None:
            if not isinstance(self.error, Mapping):
                raise TypeError("error must be an object")
            object.__setattr__(self, "error", freeze_json(dict(self.error)))
        if state is AgentTurnState.FAILED and self.error is None:
            raise ValueError("a failed turn result requires an error object")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "seq": self.seq,
            "state": self.state.value,
            "public_output": (
                None if self.public_output is None else _message_to_json(self.public_output)
            ),
            "artifact_refs": list(self.artifact_refs),
            "usage_refs": list(self.usage_refs),
            "delegation_count": self.delegation_count,
            "error": (
                None
                if self.error is None
                else thaw_json(freeze_json(thaw_json(cast(FrozenJsonValue, self.error))))
            ),
        }

    @classmethod
    def from_json(cls, value: object) -> AgentTurnResult:
        if not isinstance(value, Mapping):
            raise TypeError("AgentTurnResult JSON must be an object")
        value = thaw_json(freeze_json(dict(value)) if not _is_frozen(value) else value)
        assert isinstance(value, dict)
        if value.get("schema_version") != 1:
            raise ValueError("unsupported AgentTurnResult schema_version")
        output = value.get("public_output")
        artifact_refs = value.get("artifact_refs", [])
        usage_refs = value.get("usage_refs", [])
        if not isinstance(artifact_refs, list) or not isinstance(usage_refs, list):
            raise TypeError("artifact_refs and usage_refs must be lists")
        error = value.get("error")
        return cls(
            turn_id=str(value.get("turn_id")),
            agent_id=str(value.get("agent_id")),
            seq=value.get("seq"),  # type: ignore[arg-type]
            state=AgentTurnState(str(value.get("state"))),
            public_output=None if output is None else _message_from_json(output),
            artifact_refs=tuple(str(ref) for ref in artifact_refs),
            usage_refs=tuple(str(ref) for ref in usage_refs),
            delegation_count=value.get("delegation_count", 0),  # type: ignore[arg-type]
            error=None if error is None else dict(error),  # type: ignore[arg-type]
        )

    @property
    def result_hash(self) -> str:
        return result_json_hash(self.to_json())

    def to_outcome(self, *, input_id: str, input_hash: str, **extra: object) -> AgentTurnOutcome:
        body = self.to_json()
        return AgentTurnOutcome(
            agent_id=self.agent_id,
            turn_id=self.turn_id,
            input_id=input_id,
            input_hash=input_hash,
            result_hash=result_json_hash(body),
            result_json=freeze_json(body),
            usage_refs=self.usage_refs,
            **extra,  # type: ignore[arg-type]
        )

    @classmethod
    def from_outcome(cls, outcome: AgentTurnOutcome) -> AgentTurnResult:
        return cls.from_json(outcome.result_object())


@dataclass(frozen=True, slots=True)
class AgentCancelReceipt:
    """Answer to ``cancel_turn``: what happened to the turn after the intent was durable.

    ``state`` is ``cancelled`` (the turn ended as a failed ``agent_turn_cancelled``
    result), ``already_settled`` (a result existed before the intent; it is kept) or
    ``pending`` (the executor has not yet observed the intent, e.g. it is blocked on an
    uncertain outbound call; the intent stays durable and is honoured on resume).
    """

    agent_id: str
    turn_id: str
    command_id: str
    state: str
    control_generation: int
    created_at: float

    def __post_init__(self) -> None:
        if self.state not in ("cancelled", "already_settled", "pending"):
            raise ValueError("cancel receipt state is unknown")


@dataclass(frozen=True, slots=True)
class AgentTurnSnapshot:
    """Read model of one turn while it is open or after it settled (BA11 / T7)."""

    turn_id: str
    agent_id: str
    input_id: str
    seq: int
    state: AgentTurnState
    blocked: bool
    blocker: Mapping[str, JsonValue] | None
    provider_turn_ordinal_from: int | None
    provider_turn_ordinal_to: int | None
    created_at: float


@dataclass(frozen=True, slots=True)
class AgentDelegationResult:
    """What the parent receives back from one ``agent.delegate`` call."""

    delegation_id: str
    child_agent_id: str
    status: str
    result_hash: str | None = None
    result: Mapping[str, JsonValue] | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "delegation_id": self.delegation_id,
            "child_agent_id": self.child_agent_id,
            "status": self.status,
            "result_hash": self.result_hash,
            "result": None if self.result is None else thaw_json(freeze_json(dict(self.result))),
        }


@dataclass(frozen=True, slots=True)
class AgentClosingReceipt:
    """Durable answer to ``close``: what the Agent's lifecycle is after this command."""

    agent_id: str
    command_id: str
    state: str
    open_turn_id: str | None
    control_generation: int
    created_at: float

    def __post_init__(self) -> None:
        if self.state not in ("closing", "closed"):
            raise ValueError("closing receipt state must be closing or closed")


__all__ = (
    "AgentBatchIdentityConflict",
    "AgentBatchRejected",
    "AgentCancelReceipt",
    "AgentClosed",
    "AgentClosingReceipt",
    "AgentDelegationResult",
    "AgentError",
    "AgentId",
    "AgentInputConflict",
    "AgentInstanceCapExceeded",
    "AgentNotFound",
    "AgentPendingInputsExhausted",
    "AgentTurnId",
    "AgentTurnNotFound",
    "AgentTurnReceipt",
    "AgentTurnResult",
    "AgentTurnSnapshot",
    "AgentTurnState",
    "AgentTurnTimeout",
)
