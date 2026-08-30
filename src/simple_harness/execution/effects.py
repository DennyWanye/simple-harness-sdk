# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable Tool-effect ledger contracts and legal state transitions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)

if TYPE_CHECKING:
    from simple_harness.execution.fences import RunFenceLease
    from simple_harness.execution.recovery import (
        ReconciliationResolution,
        ResolutionOutcome,
    )
    from simple_harness.execution.uow import ExecutionLease
    from simple_harness.tools.contracts import ToolResult
    from simple_harness.workflow.lease import WorkflowLease


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
    return hashlib.sha256(canonical_json(cast(JsonValue, payload)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskExecutionEnvelope:
    run_id: RunId
    call_id: CallId
    effect_id: EffectId
    raw_call_id: str
    turn_ordinal: int
    call_ordinal: int
    tool_name: str
    capability_id: str
    capability_fingerprint: str
    route_receipt_id: str | None
    route_receipt_hash: str | None
    task_scope_id: str | None
    root_id: str | None
    root_identity_hash: str | None
    binding_set_revision: int | None
    idempotency_key: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported TaskExecutionEnvelope schema")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if not isinstance(self.call_id, CallId):
            raise TypeError("call_id must use CallId")
        if not isinstance(self.effect_id, EffectId):
            raise TypeError("effect_id must use EffectId")
        for name in (
            "raw_call_id",
            "tool_name",
            "capability_id",
            "idempotency_key",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.turn_ordinal, self.call_ordinal)
        ):
            raise ValueError("TaskExecutionEnvelope ordinals must be non-negative")
        if self.idempotency_key != self.effect_id.value:
            raise ValueError("TaskExecutionEnvelope idempotency identity differs from effect")
        for name in ("capability_fingerprint", "route_receipt_hash", "root_identity_hash"):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if (self.route_receipt_id is None) != (self.route_receipt_hash is None):
            raise ValueError("route receipt identity/hash must be paired")
        task_values = (
            self.task_scope_id,
            self.root_id,
            self.root_identity_hash,
            self.binding_set_revision,
        )
        if any(value is not None for value in task_values) and not all(
            value is not None for value in task_values
        ):
            raise ValueError("TaskScope execution authority must be complete")
        if self.binding_set_revision is not None and (
            isinstance(self.binding_set_revision, bool) or self.binding_set_revision < 1
        ):
            raise ValueError("binding_set_revision must be positive")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id.value,
            "call_id": self.call_id.value,
            "effect_id": self.effect_id.value,
            "raw_call_id": self.raw_call_id,
            "turn_ordinal": self.turn_ordinal,
            "call_ordinal": self.call_ordinal,
            "tool_name": self.tool_name,
            "capability_id": self.capability_id,
            "capability_fingerprint": self.capability_fingerprint,
            "route_receipt_id": self.route_receipt_id,
            "route_receipt_hash": self.route_receipt_hash,
            "task_scope_id": self.task_scope_id,
            "root_id": self.root_id,
            "root_identity_hash": self.root_identity_hash,
            "binding_set_revision": self.binding_set_revision,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskExecutionEnvelope:
        expected = {
            "schema_version",
            "run_id",
            "call_id",
            "effect_id",
            "raw_call_id",
            "turn_ordinal",
            "call_ordinal",
            "tool_name",
            "capability_id",
            "capability_fingerprint",
            "route_receipt_id",
            "route_receipt_hash",
            "task_scope_id",
            "root_id",
            "root_identity_hash",
            "binding_set_revision",
            "idempotency_key",
        }
        if set(value) != expected:
            raise ValueError("TaskExecutionEnvelope fields differ")
        for name in ("turn_ordinal", "call_ordinal", "schema_version"):
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int):
                raise TypeError(f"{name} must be an integer")
        revision = value["binding_set_revision"]
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise TypeError("binding_set_revision must be an integer or null")

        def optional_text(name: str) -> str | None:
            item = value[name]
            if item is not None and not isinstance(item, str):
                raise TypeError(f"{name} must be a string or null")
            return item

        required_text = (
            "run_id",
            "call_id",
            "effect_id",
            "raw_call_id",
            "tool_name",
            "capability_id",
            "capability_fingerprint",
            "idempotency_key",
        )
        if not all(isinstance(value[name], str) for name in required_text):
            raise TypeError("TaskExecutionEnvelope required identities must be strings")
        return cls(
            RunId(cast(str, value["run_id"])),
            CallId(cast(str, value["call_id"])),
            EffectId(cast(str, value["effect_id"])),
            cast(str, value["raw_call_id"]),
            cast(int, value["turn_ordinal"]),
            cast(int, value["call_ordinal"]),
            cast(str, value["tool_name"]),
            cast(str, value["capability_id"]),
            cast(str, value["capability_fingerprint"]),
            optional_text("route_receipt_id"),
            optional_text("route_receipt_hash"),
            optional_text("task_scope_id"),
            optional_text("root_id"),
            optional_text("root_identity_hash"),
            revision,
            cast(str, value["idempotency_key"]),
            cast(int, value["schema_version"]),
        )

    @property
    def envelope_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode("utf-8")).hexdigest()


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
    result: ToolResult | None = None
    raw_call_id: str | None = None
    turn_ordinal: int = 0
    call_ordinal: int = 0
    handoff_attempt: int = 0
    rehandoff_count: int = 0
    task_execution_envelope: TaskExecutionEnvelope | None = None

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
        object.__setattr__(self, "arguments", freeze_json(thaw_json(self.arguments)))
        if not isinstance(self.state, EffectState):
            object.__setattr__(self, "state", EffectState(self.state))
        if self.version < 0 or self.fence_epoch < 1:
            raise ValueError("version must be non-negative and fence_epoch positive")
        for name in (
            "turn_ordinal",
            "call_ordinal",
            "handoff_attempt",
            "rehandoff_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.rehandoff_count > 1 or self.rehandoff_count > self.handoff_attempt:
            raise ValueError("invalid Tool re-handoff counters")
        if self.raw_call_id is not None and not self.raw_call_id:
            raise ValueError("raw_call_id must not be blank")
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
        if self.task_execution_envelope is not None:
            envelope = self.task_execution_envelope
            if not isinstance(envelope, TaskExecutionEnvelope):
                raise TypeError("task_execution_envelope must use TaskExecutionEnvelope")
            if (
                envelope.run_id != self.run_id
                or envelope.call_id != self.call_id
                or envelope.effect_id != self.effect_id
                or envelope.tool_name != self.tool_name
                or envelope.raw_call_id != self.raw_call_id
                or envelope.turn_ordinal != self.turn_ordinal
                or envelope.call_ordinal != self.call_ordinal
            ):
                raise ValueError("TaskExecutionEnvelope differs from effect identity")

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
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        raw_call_id: str | None = None,
        turn_ordinal: int = 0,
        call_ordinal: int = 0,
        task_execution_envelope: TaskExecutionEnvelope | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def read_effect(self, effect_id: EffectId) -> EffectRecord | None: ...

    def mark_effect_handed_off(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        run_fence: RunFenceLease,
        handoff_receipt_ref: str,
        execution_lease: ExecutionLease,
        workflow_lease: WorkflowLease | None = None,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def settle_effect(
        self,
        effect_id: EffectId,
        *,
        expected_version: int,
        expected_fence_epoch: int,
        result: ToolResult,
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

    def record_tool_reconciliation(
        self,
        record: EffectRecord,
        *,
        outcome: ResolutionOutcome,
        result: ToolResult | None,
        evidence_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def read_reconciliation_resolution(
        self, *, kind: str, ledger_identity: str, handoff_attempt: int
    ) -> ReconciliationResolution | None: ...

    def reauthorize_effect_not_started(
        self,
        record: EffectRecord,
        *,
        authorization_receipt_ref: str,
        resolution: ReconciliationResolution,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...

    def refresh_prepared_effect_authority(
        self,
        record: EffectRecord,
        *,
        authorization_receipt_ref: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> EffectRecord: ...


__all__ = (
    "EffectConflictError",
    "EffectRecord",
    "EffectState",
    "EffectTransitionError",
    "EffectUnitOfWork",
    "TaskExecutionEnvelope",
    "effect_request_hash",
)
