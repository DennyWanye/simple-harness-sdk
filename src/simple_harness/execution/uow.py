# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Product-neutral command records for durable execution UoWs."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from simple_harness.contracts import FrozenJsonValue, JsonValue
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildCommandRecord,
    ChildLaunchResult,
    ChildSignalAckReceipt,
    ChildSignalAckResult,
    ChildSignalRecord,
    ChildTerminalResult,
    ProfileLaunchTicket,
)
from simple_harness.execution.dispatch import ProviderInvocationUnitOfWork
from simple_harness.execution.effects import EffectUnitOfWork
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.recovery import (
    ReconciliationResolution,
    WaitActivationReceipt,
    WaitBlockerRecord,
    WaitBlockerSpec,
)

if TYPE_CHECKING:
    from simple_harness.execution.delivery import (
        DeliveryRecord,
        DeliverySpec,
        TerminalCommitResult,
    )
    from simple_harness.execution.memory_outbox import CommittedTurnSpec


FaultHook = Callable[[str], None]
RUNTIME_LEASE_NAMESPACE = "runtime.kernel"


class RunState(StrEnum):
    CREATED = "created"
    ADMISSION_PENDING = "admission_pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdmissionState(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DecisionState(StrEnum):
    OPEN = "open"
    ALLOWED = "allowed"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ContinuationState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACKED = "acked"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    run_id: str
    namespace: str
    owner_id: str
    epoch: int
    expires_at: float

    def __post_init__(self) -> None:
        for name in ("run_id", "namespace", "owner_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if isinstance(self.epoch, bool) or self.epoch < 1:
            raise ValueError("epoch must be a positive integer")
        if not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise ValueError("expires_at must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class WorkflowCheckpoint:
    run_id: str
    namespace: str
    checkpoint: FrozenJsonValue
    checkpoint_hash: str
    lease_epoch: int
    version: int


@dataclass(frozen=True, slots=True)
class LegacyTurnCursorRecord:
    run_id: str
    cursor_version: int
    source_key: str
    source_namespace: str
    source_event_id: str | None
    turn_id: str
    user_text: str
    input_hash: str
    write_fence: str | None
    turn_started_at: float
    state: str
    consumed_terminal_state: str | None
    committed_turn_hash: str | None


class UnitOfWorkConflict(RuntimeError):
    code = "uow_conflict"


class UnitOfWorkNotFound(RuntimeError):
    code = "uow_not_found"


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    execution_session_id: str
    request_id: str
    root_run_id: str
    parent_run_id: str | None
    profile_key: str
    driver_kind: str
    state: RunState
    version: int


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    admission_id: str
    run_id: str
    state: AdmissionState
    prompt: FrozenJsonValue
    response: FrozenJsonValue | None
    expires_at: float | None
    version: int


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    run_id: str
    kind: str
    state: DecisionState
    request: FrozenJsonValue
    response: FrozenJsonValue | None
    version: int


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    continuation_id: str
    run_id: str
    fifo_seq: int
    payload: FrozenJsonValue
    state: ContinuationState
    version: int
    claimed_by: str | None
    runtime_lease_epoch: int | None
    claim_epoch: int
    ack_receipt_id: str | None


@dataclass(frozen=True, slots=True)
class ContinuationProgressReceipt:
    receipt_id: str
    continuation_id: str
    run_id: str
    owner_id: str
    runtime_lease_epoch: int
    claim_epoch: int
    outcome_hash: str


@dataclass(frozen=True, slots=True)
class ContinuationProgressResult:
    run: RunRecord
    continuation: ContinuationRecord
    receipt: ContinuationProgressReceipt


@dataclass(frozen=True, slots=True)
class ContinuationTerminalResult:
    terminal: TerminalCommitResult
    continuation: ContinuationRecord
    receipt: ContinuationProgressReceipt


class ExecutionUnitOfWork(EffectUnitOfWork, ProviderInvocationUnitOfWork, Protocol):
    def read_continuation(self, continuation_id: str) -> ContinuationRecord | None: ...

    def commit_runtime_wait_with_blocker(
        self,
        *,
        run_id: str,
        expected_version: int,
        event_id: str,
        payload: Mapping[str, JsonValue],
        blocker: WaitBlockerSpec,
        lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, WaitBlockerRecord]: ...

    def list_resolved_wait_blockers(
        self,
        *,
        owner_id: str,
        namespace: str,
        now: float,
    ) -> tuple[WaitBlockerRecord, ...]: ...

    def consume_resolved_wait_and_claim_activation(
        self,
        *,
        blocker_id: str,
        owner_id: str,
        namespace: str,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, ExecutionLease, WaitActivationReceipt]: ...

    def read_reconciliation_resolution(
        self, *, kind: str, ledger_identity: str, handoff_attempt: int
    ) -> ReconciliationResolution | None: ...

    def claim_runtime_activation(
        self,
        *,
        run_id: str,
        owner_id: str,
        namespace: str,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> tuple[RunRecord, ExecutionLease]: ...

    def release_runtime_lease(
        self,
        lease: ExecutionLease,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> None: ...

    def renew_runtime_lease(
        self,
        lease: ExecutionLease,
        *,
        now: float,
        lease_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ExecutionLease: ...

    def commit_runtime_state(
        self,
        *,
        run_id: str,
        expected_version: int,
        state: RunState,
        event_id: str,
        payload: Mapping[str, JsonValue],
        lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord: ...

    def request_run_cancel(
        self,
        *,
        run_id: str,
        expected_version: int,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord: ...

    def verify_workflow_cancel_terminal(
        self, *, run_id: str, cancel_id: str, generation: int
    ) -> bool: ...

    def read_run(self, run_id: str) -> RunRecord | None: ...

    def read_start_snapshot(self, run_id: str) -> Mapping[str, JsonValue] | None: ...

    def list_recoverable_root_runs(self) -> tuple[RunRecord, ...]: ...

    def list_recoverable_child_runs(self) -> tuple[RunRecord, ...]: ...

    def read_react_checkpoint(self, run_id: str) -> WorkflowCheckpoint | None: ...

    def read_legacy_turn_cursor(self, run_id: str) -> LegacyTurnCursorRecord | None: ...

    def cas_react_checkpoint(
        self,
        *,
        run_id: str,
        lease: ExecutionLease,
        expected_version: int | None,
        checkpoint: Mapping[str, JsonValue],
        checkpoint_hash: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> WorkflowCheckpoint: ...

    def commit_root_terminal_with_deliveries(
        self,
        *,
        run_id: str,
        expected_version: int,
        terminal_state: RunState,
        event_id: str,
        terminal_payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        fence: RunFenceLease,
        execution_lease: ExecutionLease,
        terminal_fence_receipt_ref: str,
        now: float,
        committed_turn: CommittedTurnSpec | None = None,
        legacy_cursor_version: int | None = None,
        fault: FaultHook | None = None,
    ) -> TerminalCommitResult: ...

    def claim_delivery(
        self,
        *,
        sink_kinds: Sequence[str],
        now: float,
        claim_ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord | None: ...

    def complete_delivery(
        self,
        delivery_id: str,
        *,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord: ...

    def release_delivery(
        self,
        delivery_id: str,
        *,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> DeliveryRecord: ...

    def issue_profile_launch_ticket(
        self,
        ticket: ProfileLaunchTicket,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ProfileLaunchTicket: ...

    def claim_profile_launch_and_commit_child(
        self,
        *,
        ticket_id: str,
        expected_catalog_generation: int,
        launch_request: Mapping[str, JsonValue],
        command_id: str,
        child_run_id: str,
        request_id: str,
        attachment_policy: AttachmentPolicy,
        start_snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildLaunchResult: ...

    def read_child_command_for_run(self, child_run_id: str) -> ChildCommandRecord | None: ...

    def is_workflow_spawn_child(self, child_run_id: str) -> bool: ...

    def read_child_terminal_result_for_run(
        self, child_run_id: str
    ) -> ChildTerminalResult | None: ...

    def read_child_attachment_policy(self, child_run_id: str) -> AttachmentPolicy: ...

    def list_child_signal_parent_run_ids(self) -> tuple[str, ...]: ...

    def finalize_child_and_enqueue_parent_signal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        signal_id: str,
        signal_payload: Mapping[str, JsonValue],
        event_id: str,
        receipt_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildTerminalResult: ...

    def commit_detached_child_terminal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        terminal_payload: Mapping[str, JsonValue],
        event_id: str,
        receipt_id: str,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildTerminalResult: ...

    def claim_next_child_signal(
        self,
        *,
        parent_run_id: str,
        owner_id: str,
        now: float,
        lease_seconds: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalRecord | None: ...

    def ack_child_signal_and_commit_parent_progress(
        self,
        *,
        signal_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt_id: str,
        continuation_id: str,
        continuation_payload: Mapping[str, JsonValue],
        event_id: str,
        event_payload: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalAckResult: ...

    def read_child_signal_ack_receipt(self, receipt_id: str) -> ChildSignalAckReceipt | None: ...

    def create_with_start_snapshot(
        self,
        *,
        execution_session_id: str,
        run_id: str,
        request_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        user_id: str = "harness-system",
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> RunRecord: ...

    def start_admission(
        self,
        *,
        admission_id: str,
        run_id: str,
        prompt: Mapping[str, JsonValue],
        expires_at: float | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord: ...

    def resolve_admission(
        self,
        *,
        admission_id: str,
        state: AdmissionState,
        response: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord: ...

    def commit_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        kind: str,
        state: DecisionState,
        request: Mapping[str, JsonValue],
        response: Mapping[str, JsonValue] | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> DecisionRecord: ...

    def read_decision(self, decision_id: str) -> DecisionRecord | None: ...

    def enqueue_continuation(
        self,
        *,
        continuation_id: str,
        run_id: str,
        payload: Mapping[str, JsonValue],
        now: float,
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord: ...

    def claim_continuation(
        self,
        *,
        run_id: str,
        execution_lease: ExecutionLease,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord | None: ...

    def commit_runtime_state_and_ack_continuation(
        self,
        *,
        run_id: str,
        expected_version: int,
        state: RunState,
        event_id: str,
        payload: Mapping[str, JsonValue],
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        receipt_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationProgressResult: ...

    def commit_root_terminal_with_deliveries_and_ack_continuation(
        self,
        *,
        run_id: str,
        expected_version: int,
        terminal_state: RunState,
        event_id: str,
        terminal_payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        continuation_claim: ContinuationRecord,
        run_fence: RunFenceLease,
        execution_lease: ExecutionLease,
        receipt_id: str,
        terminal_fence_receipt_ref: str,
        now: float,
        committed_turn: CommittedTurnSpec | None = None,
        legacy_cursor_version: int | None = None,
        fault: FaultHook | None = None,
    ) -> ContinuationTerminalResult: ...


__all__ = (
    "RUNTIME_LEASE_NAMESPACE",
    "AdmissionRecord",
    "AdmissionState",
    "ContinuationProgressReceipt",
    "ContinuationProgressResult",
    "ContinuationRecord",
    "ContinuationState",
    "ContinuationTerminalResult",
    "DecisionRecord",
    "DecisionState",
    "ExecutionLease",
    "ExecutionUnitOfWork",
    "FaultHook",
    "LegacyTurnCursorRecord",
    "RunRecord",
    "RunState",
    "UnitOfWorkConflict",
    "UnitOfWorkNotFound",
    "WorkflowCheckpoint",
)
