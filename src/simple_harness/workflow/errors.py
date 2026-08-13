# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable error vocabulary for durable Simple Harness workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WorkflowErrorCode(StrEnum):
    RETRYABLE_PROVIDER = "retryable_provider"
    RETRYABLE_TOOL = "retryable_tool"
    INVALID_STATE = "invalid_state"
    CHECKPOINT_CORRUPT = "checkpoint_corrupt"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    EFFECT_UNCERTAIN = "effect_uncertain"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class ErrorDisposition:
    retryable: bool
    user_message: str
    recovery_action: str


ERROR_DISPOSITIONS: Mapping[WorkflowErrorCode, ErrorDisposition] = {
    WorkflowErrorCode.RETRYABLE_PROVIDER: ErrorDisposition(
        True, "The model provider is temporarily unavailable.", "retry"
    ),
    WorkflowErrorCode.RETRYABLE_TOOL: ErrorDisposition(
        True, "A tool failed temporarily.", "retry"
    ),
    WorkflowErrorCode.INVALID_STATE: ErrorDisposition(
        False, "The workflow state is invalid.", "inspect_or_cancel"
    ),
    WorkflowErrorCode.CHECKPOINT_CORRUPT: ErrorDisposition(
        False, "A workflow checkpoint is corrupt.", "fork_from_valid_ancestor"
    ),
    WorkflowErrorCode.PERMISSION_DENIED: ErrorDisposition(
        False, "Permission was denied.", "request_permission_or_cancel"
    ),
    WorkflowErrorCode.CANCELLED: ErrorDisposition(
        False, "The workflow was cancelled.", "none"
    ),
    WorkflowErrorCode.LEASE_LOST: ErrorDisposition(
        True, "Workflow ownership changed while work was running.", "reclaim"
    ),
    WorkflowErrorCode.EFFECT_UNCERTAIN: ErrorDisposition(
        False, "A side effect has an uncertain outcome.", "reconcile"
    ),
    WorkflowErrorCode.PERMANENT: ErrorDisposition(
        False, "The workflow could not continue.", "inspect_or_cancel"
    ),
}


class WorkflowContractError(ValueError):
    """Base class for stable, machine-readable contract failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class WorkflowDefinitionError(WorkflowContractError):
    pass


class InvalidStatePatch(WorkflowContractError):
    pass


class StateMergeConflict(WorkflowContractError):
    pass


class WorkflowDependencyUnavailable(RuntimeError):
    pass


class AsyncOnlyWorkflowError(RuntimeError):
    pass


class UnsupportedDeltaChannelError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


class WorkflowNodeError(Exception):
    """Versioned, serializer-safe node failure.

    Raw provider/tool exceptions are deliberately not retained. Only stable
    codes and message references may cross a checkpoint boundary.
    """

    SERIAL_VERSION = 1

    def __init__(
        self,
        *,
        code: WorkflowErrorCode | str,
        message_ref: str,
        retryable: bool | None = None,
        node_id: str | None = None,
    ) -> None:
        parsed_code = WorkflowErrorCode(code)
        disposition = ERROR_DISPOSITIONS[parsed_code]
        super().__init__(message_ref)
        self.code = parsed_code
        self.message_ref = message_ref
        self.retryable = disposition.retryable if retryable is None else retryable
        self.node_id = node_id

    def to_envelope(self) -> dict[str, object]:
        envelope: dict[str, object] = {
            "schema_version": self.SERIAL_VERSION,
            "code": self.code.value,
            "message_ref": self.message_ref,
            "retryable": self.retryable,
        }
        if self.node_id is not None:
            envelope["node_id"] = self.node_id
        return envelope


__all__ = [
    "ERROR_DISPOSITIONS",
    "AsyncOnlyWorkflowError",
    "ErrorDisposition",
    "InvalidStatePatch",
    "LeaseLostError",
    "StateMergeConflict",
    "UnsupportedDeltaChannelError",
    "WorkflowContractError",
    "WorkflowDefinitionError",
    "WorkflowDependencyUnavailable",
    "WorkflowErrorCode",
    "WorkflowNodeError",
]
