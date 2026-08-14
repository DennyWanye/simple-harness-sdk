# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Sealed workflow-spawn Tool outcomes and child-control contracts."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias

from simple_harness.contracts import JsonValue
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.tools.contracts import ToolResult

from .orchestration import (
    RuntimeStartAdmission,
    RuntimeStartDisposition,
    WorkflowCatalogSelectionSnapshot,
    WorkflowSpawnIssueAuthority,
    WorkflowSpawnOrigin,
    WorkflowSpawnReadyActivation,
    WorkflowSpawnSelection,
)
from .start_snapshot import RunStart


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _non_negative(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


class WorkflowSpawnChildControlKind(StrEnum):
    START = "start"
    RECOVER = "recover"
    ATTACH = "attach"
    WAITING = "waiting"
    CANCEL = "cancel"
    TERMINAL = "terminal"


class WorkflowSpawnBatchAction(StrEnum):
    CONTINUE = "continue"
    PARENT_TERMINAL = "parent_terminal"


_SPAWN_CONTEXT_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnToolContext:
    run_id: str
    request_id: str
    turn_id: str
    internal_tool_call_id: str
    catalog_snapshot_hash: str
    react_checkpoint_revision: int
    issue_authority: WorkflowSpawnIssueAuthority
    _factory_token: object


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnInvocation:
    spawn_operation_id: str
    origin: WorkflowSpawnOrigin
    start: RunStart
    selection: WorkflowSpawnSelection
    catalog_selection: WorkflowCatalogSelectionSnapshot
    issue_authority: WorkflowSpawnIssueAuthority
    _factory_token: object


@dataclass(frozen=True, slots=True)
class ChildStartDispatchRef:
    child_start_receipt_id: str
    child_dispatch_claim_id: str
    child_run_id: str

    def __post_init__(self) -> None:
        for name in (
            "child_start_receipt_id",
            "child_dispatch_claim_id",
            "child_run_id",
        ):
            _required(getattr(self, name), name)


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnResult:
    schema_version: str
    child_run_id: str
    child_request_id: str
    parent_run_id: str
    root_run_id: str
    ticket_receipt_id: str
    runtime_start_receipt_id: str
    child_command_id: str
    attachment_policy: AttachmentPolicy
    _factory_token: object

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "child_run_id": self.child_run_id,
            "child_request_id": self.child_request_id,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "ticket_receipt_id": self.ticket_receipt_id,
            "runtime_start_receipt_id": self.runtime_start_receipt_id,
            "child_command_id": self.child_command_id,
            "attachment_policy": self.attachment_policy.value,
        }


@dataclass(frozen=True, slots=True)
class WorkflowChildWaitBinding:
    parent_run_id: str
    child_run_id: str
    child_command_id: str
    parent_wait_receipt_id: str
    expected_parent_version: int
    react_checkpoint_revision: int
    expected_signal_domain: str
    source_phase: str
    batch_digest: str
    spawn_ordinal: int
    next_tool_ordinal: int
    spawn_result_append_receipt_id: str
    context_revision: int
    termination_started_at: float
    termination_last_observed_at: float
    wall_deadline: float | None
    termination_policy_snapshot_hash: str

    def __post_init__(self) -> None:
        for name in (
            "parent_run_id",
            "child_run_id",
            "child_command_id",
            "parent_wait_receipt_id",
            "expected_signal_domain",
            "batch_digest",
            "spawn_result_append_receipt_id",
            "termination_policy_snapshot_hash",
        ):
            _required(getattr(self, name), name)
        if self.source_phase != "tool_batch_reserved":
            raise ValueError("workflow child wait source phase is invalid")
        for name in (
            "expected_parent_version",
            "react_checkpoint_revision",
            "spawn_ordinal",
            "next_tool_ordinal",
            "context_revision",
        ):
            _non_negative(getattr(self, name), name)
        for name in ("termination_started_at", "termination_last_observed_at"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.wall_deadline is not None and (
            not math.isfinite(self.wall_deadline) or self.wall_deadline < 0
        ):
            raise ValueError("wall_deadline must be finite and non-negative")


_SPAWN_OUTCOME_FACTORY = object()


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnAdmissionOutcome:
    child_start_ref: ChildStartDispatchRef
    result: WorkflowSpawnResult
    suspension: WorkflowChildWaitBinding
    _factory_token: object


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnChildControl:
    kind: WorkflowSpawnChildControlKind
    admission: RuntimeStartAdmission
    ready_activation: WorkflowSpawnReadyActivation | None
    _factory_token: object


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnToolOutcome:
    tool_result: ToolResult
    child_control: WorkflowSpawnChildControl
    child_start_ref: ChildStartDispatchRef
    suspension: WorkflowChildWaitBinding
    _factory_token: object


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnSucceeded:
    control: WorkflowSpawnToolOutcome
    _factory_token: object


@dataclass(frozen=True, slots=True, init=False)
class WorkflowSpawnFailed:
    tool_result: ToolResult
    completion_receipt_id: str
    batch_action: WorkflowSpawnBatchAction
    _factory_token: object


WorkflowSpawnHandlerOutcome: TypeAlias = WorkflowSpawnSucceeded | WorkflowSpawnFailed
WorkflowSpawnCoordinatorOutcome: TypeAlias = (
    WorkflowSpawnToolOutcome | WorkflowSpawnFailed
)


def workflow_spawn_completion_receipt_id(spawn_operation_id: str) -> str:
    _required(spawn_operation_id, "spawn_operation_id")
    return hashlib.sha256(
        (
            "simple-harness.workflow.workflow-spawn/completion/v1|"
            f"{spawn_operation_id}"
        ).encode()
    ).hexdigest()


class WorkflowSpawnToolHandler(Protocol):
    async def execute(
        self, arguments: Mapping[str, JsonValue], context: object
    ) -> WorkflowSpawnHandlerOutcome: ...


def _construct(cls: type, /, **values: object):  # type: ignore[no-untyped-def]
    self = object.__new__(cls)
    for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(
            self,
            name,
            _SPAWN_OUTCOME_FACTORY if name == "_factory_token" else values[name],
        )
    return self


def _construct_context(cls: type, /, **values: object):  # type: ignore[no-untyped-def]
    self = object.__new__(cls)
    for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(
            self,
            name,
            _SPAWN_CONTEXT_FACTORY if name == "_factory_token" else values[name],
        )
    return self


def _create_workflow_spawn_tool_context(
    *,
    run_id: str,
    request_id: str,
    turn_id: str,
    internal_tool_call_id: str,
    catalog_snapshot_hash: str,
    react_checkpoint_revision: int,
    issue_authority: WorkflowSpawnIssueAuthority,
) -> WorkflowSpawnToolContext:
    for name, value in (
        ("run_id", run_id),
        ("request_id", request_id),
        ("turn_id", turn_id),
        ("internal_tool_call_id", internal_tool_call_id),
        ("catalog_snapshot_hash", catalog_snapshot_hash),
    ):
        _required(value, name)
    if react_checkpoint_revision < 0:
        raise ValueError("react_checkpoint_revision must be non-negative")
    if (
        issue_authority.execution_lease.run_id != run_id
        or issue_authority.react_checkpoint_revision != react_checkpoint_revision
    ):
        raise ValueError("workflow spawn Tool context authority differs")
    return _construct_context(
        WorkflowSpawnToolContext,
        run_id=run_id,
        request_id=request_id,
        turn_id=turn_id,
        internal_tool_call_id=internal_tool_call_id,
        catalog_snapshot_hash=catalog_snapshot_hash,
        react_checkpoint_revision=react_checkpoint_revision,
        issue_authority=issue_authority,
    )


def _create_workflow_spawn_invocation(
    *,
    spawn_operation_id: str,
    origin: WorkflowSpawnOrigin,
    start: RunStart,
    selection: WorkflowSpawnSelection,
    catalog_selection: WorkflowCatalogSelectionSnapshot,
    issue_authority: WorkflowSpawnIssueAuthority,
) -> WorkflowSpawnInvocation:
    from .orchestration import workflow_spawn_operation_id

    if spawn_operation_id != workflow_spawn_operation_id(origin):
        raise ValueError("workflow spawn operation identity differs")
    if (
        start.turn_id != origin.turn_id
        or start.execution_session_id.value == ""
        or issue_authority.execution_lease.run_id != origin.parent_run_id
        or catalog_selection.canonical_hash == ""
    ):
        raise ValueError("workflow spawn invocation authority differs")
    return _construct_context(
        WorkflowSpawnInvocation,
        spawn_operation_id=spawn_operation_id,
        origin=origin,
        start=start,
        selection=selection,
        catalog_selection=catalog_selection,
        issue_authority=issue_authority,
    )


def _create_workflow_spawn_result(**values: object) -> WorkflowSpawnResult:
    if values.get("schema_version") != "workflow_spawn.result.v1":
        raise ValueError("workflow spawn result schema version is invalid")
    for name in (
        "child_run_id",
        "child_request_id",
        "parent_run_id",
        "root_run_id",
        "ticket_receipt_id",
        "runtime_start_receipt_id",
        "child_command_id",
    ):
        _required(values[name], name)  # type: ignore[arg-type]
    if values.get("attachment_policy") is not AttachmentPolicy.ATTACHED:
        raise ValueError("workflow spawn result must remain attached")
    return _construct(WorkflowSpawnResult, **values)


def _create_workflow_spawn_admission_outcome(
    *,
    child_start_ref: ChildStartDispatchRef,
    result: WorkflowSpawnResult,
    suspension: WorkflowChildWaitBinding,
) -> WorkflowSpawnAdmissionOutcome:
    if not (
        child_start_ref.child_run_id
        == result.child_run_id
        == suspension.child_run_id
    ):
        raise ValueError("workflow spawn admission child identity differs")
    return _construct(
        WorkflowSpawnAdmissionOutcome,
        child_start_ref=child_start_ref,
        result=result,
        suspension=suspension,
    )


def _create_workflow_spawn_child_control(
    *,
    kind: WorkflowSpawnChildControlKind,
    admission: RuntimeStartAdmission,
    ready_activation: WorkflowSpawnReadyActivation | None = None,
) -> WorkflowSpawnChildControl:
    kind = WorkflowSpawnChildControlKind(kind)
    disposition = admission.disposition
    allowed = {
        WorkflowSpawnChildControlKind.START: {
            RuntimeStartDisposition.START_NEW,
            RuntimeStartDisposition.START_ORPHAN,
        },
        WorkflowSpawnChildControlKind.RECOVER: {
            RuntimeStartDisposition.RECOVER_START,
            RuntimeStartDisposition.RECOVER_RESUME,
        },
        WorkflowSpawnChildControlKind.ATTACH: {
            RuntimeStartDisposition.ATTACH_CURRENT,
        },
        WorkflowSpawnChildControlKind.WAITING: {
            RuntimeStartDisposition.WAITING,
            RuntimeStartDisposition.FOREIGN_ACTIVE,
        },
        WorkflowSpawnChildControlKind.CANCEL: {
            RuntimeStartDisposition.CANCEL_PENDING,
        },
        WorkflowSpawnChildControlKind.TERMINAL: {
            RuntimeStartDisposition.TERMINAL,
        },
    }[kind]
    if disposition not in allowed:
        raise ValueError("workflow spawn child control disposition differs")
    return _construct(
        WorkflowSpawnChildControl,
        kind=kind,
        admission=admission,
        ready_activation=ready_activation,
    )


def _create_workflow_spawn_tool_outcome(
    *,
    tool_result: ToolResult,
    child_control: WorkflowSpawnChildControl,
    child_start_ref: ChildStartDispatchRef,
    suspension: WorkflowChildWaitBinding,
) -> WorkflowSpawnToolOutcome:
    if child_control.admission.receipt.run_id != child_start_ref.child_run_id:
        raise ValueError("workflow spawn Tool outcome child identity differs")
    return _construct(
        WorkflowSpawnToolOutcome,
        tool_result=tool_result,
        child_control=child_control,
        child_start_ref=child_start_ref,
        suspension=suspension,
    )


def _create_workflow_spawn_succeeded(
    control: WorkflowSpawnToolOutcome,
) -> WorkflowSpawnSucceeded:
    return _construct(WorkflowSpawnSucceeded, control=control)


def _create_workflow_spawn_failed(
    *,
    tool_result: ToolResult,
    completion_receipt_id: str,
    batch_action: WorkflowSpawnBatchAction,
) -> WorkflowSpawnFailed:
    _required(completion_receipt_id, "completion_receipt_id")
    return _construct(
        WorkflowSpawnFailed,
        tool_result=tool_result,
        completion_receipt_id=completion_receipt_id,
        batch_action=WorkflowSpawnBatchAction(batch_action),
    )


__all__ = (
    "ChildStartDispatchRef",
    "WorkflowChildWaitBinding",
    "WorkflowSpawnAdmissionOutcome",
    "WorkflowSpawnBatchAction",
    "WorkflowSpawnChildControl",
    "WorkflowSpawnChildControlKind",
    "WorkflowSpawnCoordinatorOutcome",
    "WorkflowSpawnFailed",
    "WorkflowSpawnHandlerOutcome",
    "WorkflowSpawnInvocation",
    "WorkflowSpawnResult",
    "WorkflowSpawnSucceeded",
    "WorkflowSpawnToolContext",
    "WorkflowSpawnToolHandler",
    "WorkflowSpawnToolOutcome",
    "workflow_spawn_completion_receipt_id",
)
