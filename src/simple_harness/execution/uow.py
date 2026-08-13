# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Product-neutral command records for durable execution UoWs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping, Protocol

from simple_harness.contracts import FrozenJsonValue, JsonValue
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildLaunchResult,
    ChildSignalRecord,
    ProfileLaunchTicket,
)
from simple_harness.execution.effects import EffectUnitOfWork


FaultHook = Callable[[str], None]


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


class ExecutionUnitOfWork(EffectUnitOfWork, Protocol):
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

    def finalize_child_and_enqueue_parent_signal(
        self,
        *,
        command_id: str,
        expected_child_version: int,
        terminal_state: RunState,
        signal_id: str,
        signal_payload: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalRecord: ...

    def ack_child_signal(
        self,
        *,
        signal_id: str,
        expected_version: int,
        continuation_id: str,
        continuation_payload: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ChildSignalRecord: ...

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

    def enqueue_continuation(
        self,
        *,
        continuation_id: str,
        run_id: str,
        payload: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord: ...

    def claim_continuation(
        self,
        *,
        run_id: str,
        owner_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord | None: ...

    def ack_continuation(
        self,
        *,
        continuation_id: str,
        owner_id: str,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord: ...


__all__ = (
    "AdmissionRecord",
    "AdmissionState",
    "ContinuationRecord",
    "ContinuationState",
    "DecisionRecord",
    "DecisionState",
    "ExecutionUnitOfWork",
    "FaultHook",
    "RunRecord",
    "RunState",
    "UnitOfWorkConflict",
    "UnitOfWorkNotFound",
)
