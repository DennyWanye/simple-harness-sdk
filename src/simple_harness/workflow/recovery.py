# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Typed failure mapping and Port-backed crash recovery helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

from .checkpoint import WorkflowCheckpoint
from .contracts import WorkflowRunStatus
from .errors import (
    ERROR_DISPOSITIONS,
    InvalidStatePatch,
    LeaseLostError,
    StateMergeConflict,
    WorkflowDependencyUnavailable,
    WorkflowErrorCode,
    WorkflowNodeError,
)
from .execution_ports import WorkflowTransaction


@dataclass(frozen=True, slots=True)
class FailureMapping:
    code: WorkflowErrorCode
    target_status: WorkflowRunStatus
    message_ref: str
    retryable: bool
    recovery_action: str
    reason: str

    def envelope(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "code": self.code.value,
            "message_ref": self.message_ref,
            "retryable": self.retryable,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    run_id: str
    previous_status: str
    status: str
    action: str
    reason: str


class RecoveryDisposition(str):
    RETRY = "retry"
    FAIL = "fail"
    QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    disposition: str
    reason: str
    delay_seconds: float | None


class WorkflowRecoveryPort(Protocol):
    def classify(
        self, error: BaseException, *, attempt: int, max_attempts: int
    ) -> RecoveryDecision: ...

    async def quarantine(
        self,
        *,
        run_id: str,
        reason: str,
        checkpoint: WorkflowCheckpoint | None,
        transaction: WorkflowTransaction,
    ) -> None: ...

    async def recover_expired(
        self, *, now: float, transaction: WorkflowTransaction
    ) -> tuple[RecoveryRecord, ...]: ...

    async def repair_head(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        transaction: WorkflowTransaction,
    ) -> RecoveryRecord | None: ...


def map_workflow_failure(exc: BaseException) -> FailureMapping:
    if isinstance(exc, WorkflowNodeError):
        code, message_ref, retryable = exc.code, exc.message_ref, exc.retryable
    elif isinstance(exc, LeaseLostError):
        code, message_ref, retryable = (
            WorkflowErrorCode.LEASE_LOST,
            "workflow_runner:lease_lost",
            True,
        )
    elif isinstance(exc, asyncio.CancelledError):
        code, message_ref, retryable = (
            WorkflowErrorCode.CANCELLED,
            "workflow_runner:cancelled",
            False,
        )
    elif exc.__class__.__name__ == "GraphRecursionError":
        return FailureMapping(
            WorkflowErrorCode.INVALID_STATE,
            WorkflowRunStatus.BLOCKED,
            "workflow_runner:recursion_limit_before_budget_exit",
            False,
            "inspect_budget_or_fork",
            "recursion_limit_before_budget_exit",
        )
    elif isinstance(exc, (InvalidStatePatch, StateMergeConflict)):
        code, message_ref, retryable = (
            WorkflowErrorCode.INVALID_STATE,
            f"workflow_runner:{getattr(exc, 'code', 'invalid_state')}",
            False,
        )
    elif isinstance(exc, WorkflowDependencyUnavailable):
        return FailureMapping(
            WorkflowErrorCode.PERMANENT,
            WorkflowRunStatus.BLOCKED,
            "workflow_runner:graph_version_unavailable",
            False,
            "restore_graph_version_or_fork",
            "graph_version_unavailable",
        )
    elif isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) or (
        isinstance(exc, ValueError)
        and any(
            token in str(exc).lower()
            for token in ("checkpoint", "deserialize", "integrity", "msgpack")
        )
    ):
        code, message_ref, retryable = (
            WorkflowErrorCode.CHECKPOINT_CORRUPT,
            "workflow_runner:checkpoint_corrupt",
            False,
        )
    else:
        code, message_ref, retryable = (
            WorkflowErrorCode.PERMANENT,
            f"workflow_runner:{exc.__class__.__name__}",
            False,
        )
    disposition = ERROR_DISPOSITIONS[code]
    if code is WorkflowErrorCode.CANCELLED:
        target = WorkflowRunStatus.CANCELLED
    elif code in {WorkflowErrorCode.CHECKPOINT_CORRUPT, WorkflowErrorCode.EFFECT_UNCERTAIN}:
        target = WorkflowRunStatus.BLOCKED
    elif retryable:
        target = WorkflowRunStatus.RETRYABLE
    elif code is WorkflowErrorCode.PERMISSION_DENIED:
        target = WorkflowRunStatus.BLOCKED
    else:
        target = WorkflowRunStatus.FAILED
    return FailureMapping(
        code, target, message_ref, retryable, disposition.recovery_action, code.value
    )


async def expire_stale_lease(
    recovery: WorkflowRecoveryPort,
    *,
    now: float,
    transaction: WorkflowTransaction,
) -> tuple[RecoveryRecord, ...]:
    return await recovery.recover_expired(now=now, transaction=transaction)


async def repair_head_projection(
    recovery: WorkflowRecoveryPort,
    checkpoint: WorkflowCheckpoint,
    *,
    transaction: WorkflowTransaction,
) -> RecoveryRecord | None:
    return await recovery.repair_head(checkpoint, transaction=transaction)


async def quarantine_checkpoint(
    recovery: WorkflowRecoveryPort,
    *,
    run_id: str,
    reason: str,
    checkpoint: WorkflowCheckpoint | None,
    transaction: WorkflowTransaction,
) -> RecoveryRecord:
    previous = checkpoint.status.value if checkpoint else "missing"
    await recovery.quarantine(
        run_id=run_id, reason=reason, checkpoint=checkpoint, transaction=transaction
    )
    return RecoveryRecord(run_id, previous, WorkflowRunStatus.BLOCKED.value, "inspect", reason)


__all__ = ("FailureMapping", "RecoveryRecord")
