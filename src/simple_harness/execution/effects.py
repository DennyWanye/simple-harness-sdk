# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable Tool-effect ledger contracts and legal state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
if TYPE_CHECKING:
    from simple_harness.tools.contracts import ToolResult


class EffectState(StrEnum):
    PREPARED = "prepared"
    HANDED_OFF = "handed_off"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


_TERMINAL_STATES = frozenset(
    {
        EffectState.SUCCEEDED,
        EffectState.PARTIAL,
        EffectState.REJECTED,
        EffectState.FAILED,
    }
)


class EffectTransitionError(RuntimeError):
    code = "invalid_effect_transition"


class EffectConflictError(RuntimeError):
    code = "effect_conflict"


def effect_request_hash(*, tool_name: str, arguments: object) -> str:
    if not isinstance(arguments, dict):
        raise TypeError("effect arguments must be a JSON object")
    payload = {"arguments": arguments, "tool_name": tool_name}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EffectRecord:
    effect_id: EffectId
    run_id: RunId
    call_id: CallId
    tool_name: str
    request_hash: str
    arguments: FrozenJsonValue
    state: EffectState
    version: int
    fence_epoch: int
    authorization_receipt_ref: str
    handoff_receipt_ref: str | None = None
    evidence_ref: str | None = None
    result: "ToolResult | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, EffectId):
            raise TypeError("effect_id must use EffectId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.call_id, CallId):
            raise TypeError("call_id must use CallId")
        if not self.tool_name.strip():
            raise ValueError("tool_name is required")
        if len(self.request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_hash
        ):
            raise ValueError("request_hash must be lowercase SHA-256")
        object.__setattr__(
            self, "arguments", freeze_json(thaw_json(self.arguments))
        )
        if not isinstance(self.state, EffectState):
            object.__setattr__(self, "state", EffectState(self.state))
        if self.version < 0 or self.fence_epoch < 1:
            raise ValueError("version must be non-negative and fence_epoch positive")
        if not self.authorization_receipt_ref.strip():
            raise ValueError("authorization_receipt_ref is required")
        if self.state is EffectState.PREPARED and self.handoff_receipt_ref is not None:
            raise ValueError("prepared effect cannot have handoff receipt")
        if self.state is not EffectState.PREPARED and not self.handoff_receipt_ref:
            raise ValueError("post-prepare effect requires handoff receipt")
        if self.state in _TERMINAL_STATES:
            outcome = getattr(getattr(self.result, "outcome", None), "value", None)
            if self.result is None or outcome != self.state.value:
                raise ValueError("terminal state requires matching ToolResult")
        elif self.result is not None:
            raise ValueError("non-terminal effect cannot carry ToolResult")
        if self.state is EffectState.UNKNOWN and not self.evidence_ref:
            raise ValueError("unknown effect requires evidence_ref")

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @property
    def dispatch_allowed(self) -> bool:
        return self.state is EffectState.PREPARED


@runtime_checkable
class EffectUnitOfWork(Protocol):
    def prepare_effect(
        self,
        *,
        effect_id: EffectId,
        run_id: RunId,
        call_id: CallId,
        tool_name: str,
        arguments: dict[str, object],
        request_hash: str,
        authorization_receipt_ref: str,
        fence_epoch: int,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def read_effect(self, effect_id: EffectId) -> EffectRecord | None: ...

    def mark_effect_handed_off(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        handoff_receipt_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def settle_effect(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        result: "ToolResult",
        evidence_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def mark_effect_unknown(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        evidence_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def reset_effect_not_started(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        new_fence_epoch: int,
        evidence_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...


__all__ = (
    "EffectConflictError",
    "EffectRecord",
    "EffectState",
    "EffectTransitionError",
    "EffectUnitOfWork",
    "effect_request_hash",
)
