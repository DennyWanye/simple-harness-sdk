# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Canonical execution-ledger authority used by workflow checkpoints."""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
    validate_json_value,
)
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import ExecutionLease, FaultHook

from .lease import WorkflowLease

if TYPE_CHECKING:
    from simple_harness.runtime.orchestration import RuntimeStartDispatchClaim

T = TypeVar("T")


class StartMode(StrEnum):
    STANDALONE = "standalone"
    PRECREATED = "precreated"


class StartPhase(StrEnum):
    ADMITTED = "admitted"
    CLAIMED = "claimed"
    RUNNING = "running"
    SETTLED = "settled"


class StartClaimAction(StrEnum):
    NEW = "new"
    RESUME = "resume"


class ResumePhase(StrEnum):
    ADMITTED = "admitted"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    SETTLED = "settled"


class ForkPhase(StrEnum):
    PREPARED = "prepared"
    CLAIMED = "claimed"
    CHECKPOINTED = "checkpointed"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class PrecreatedStartAction(StrEnum):
    NEW_CLAIMED = "new_claimed"
    RESUME_CLAIMED = "resume_claimed"
    RESUME_RUNNING = "resume_running"
    SETTLED = "settled"


class WorkflowRecoveryReceiptKind(StrEnum):
    START = "start"
    RESUME = "resume"


def _frozen_object(
    value: Mapping[str, JsonValue], *, path: str
) -> Mapping[str, FrozenJsonValue]:
    detached = copy.deepcopy(dict(value))
    validate_json_value(detached, path=path)
    frozen = freeze_json(detached)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    return MappingProxyType(dict(frozen))


@dataclass(frozen=True, slots=True)
class WorkflowActivation:
    execution_lease: ExecutionLease
    run_fence: RunFenceLease
    workflow_lease: WorkflowLease

    def __post_init__(self) -> None:
        run_id = self.execution_lease.run_id
        if (
            self.run_fence.run_id.value != run_id
            or self.workflow_lease.run_id != run_id
            or self.run_fence.owner_id != self.execution_lease.owner_id
            or self.workflow_lease.owner_id != self.execution_lease.owner_id
            or self.run_fence.runtime_lease_epoch != self.execution_lease.epoch
            or self.workflow_lease.runtime_lease_epoch != self.execution_lease.epoch
            or self.workflow_lease.expires_at != self.execution_lease.expires_at
        ):
            raise ValueError("workflow activation authorities are not co-fenced")


@dataclass(frozen=True, slots=True)
class StartAdmissionRequest:
    request_key: str
    mode: StartMode
    session_id: str
    request_id: str
    turn_id: str
    profile_key: str
    driver_kind: str
    tool_catalog_generation: int
    workflow_name: str
    workflow_version: str
    requested_run_id: str | None
    requested_trace_id: str | None
    requested_thread_id: str | None
    resolved_run_id: str | None
    resolved_trace_id: str | None
    resolved_thread_id: str | None
    checkpoint_namespace: str
    manifest_hash: str
    implementation_hash: str
    state_schema_version: int
    start_input_schema_ref: str | None
    start_input_schema_hash: str | None
    terminal_projection_descriptor: Mapping[str, JsonValue] | None
    terminal_request_factory_hash: str | None
    start_input: Mapping[str, JsonValue]
    capability_snapshot: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.driver_kind != "workflow":
            raise ValueError("workflow start requires reserved driver kind")
        if self.tool_catalog_generation < 1 or self.state_schema_version < 1:
            raise ValueError("workflow start versions must be positive")
        for name in (
            "request_key",
            "session_id",
            "request_id",
            "turn_id",
            "profile_key",
            "workflow_name",
            "workflow_version",
            "manifest_hash",
            "implementation_hash",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        for name in (
            "requested_run_id",
            "requested_trace_id",
            "requested_thread_id",
            "resolved_run_id",
            "resolved_trace_id",
            "resolved_thread_id",
            "start_input_schema_ref",
            "start_input_schema_hash",
            "terminal_request_factory_hash",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or null")
        if self.mode is StartMode.PRECREATED and any(
            getattr(self, name) is None
            for name in (
                "resolved_run_id",
                "resolved_trace_id",
                "resolved_thread_id",
                "start_input_schema_ref",
                "start_input_schema_hash",
            )
        ):
            raise ValueError("precreated workflow start lacks durable identities")
        object.__setattr__(
            self, "start_input", _frozen_object(self.start_input, path="$.start_input")
        )
        object.__setattr__(
            self,
            "capability_snapshot",
            _frozen_object(self.capability_snapshot, path="$.capability_snapshot"),
        )
        if self.terminal_projection_descriptor is not None:
            object.__setattr__(
                self,
                "terminal_projection_descriptor",
                _frozen_object(
                    self.terminal_projection_descriptor,
                    path="$.terminal_projection_descriptor",
                ),
            )


def start_admission_request_to_json(
    request: StartAdmissionRequest,
) -> dict[str, JsonValue]:
    start_input = thaw_json(cast(FrozenJsonValue, request.start_input))
    capability_snapshot = thaw_json(
        cast(FrozenJsonValue, request.capability_snapshot)
    )
    if not isinstance(start_input, dict) or not isinstance(
        capability_snapshot, dict
    ):
        raise TypeError("workflow start request objects did not thaw as objects")
    descriptor = (
        None
        if request.terminal_projection_descriptor is None
        else thaw_json(cast(FrozenJsonValue, request.terminal_projection_descriptor))
    )
    if descriptor is not None and not isinstance(descriptor, dict):
        raise TypeError("terminal projection descriptor did not thaw as an object")
    return {
        "request_key": request.request_key,
        "mode": request.mode.value,
        "session_id": request.session_id,
        "request_id": request.request_id,
        "turn_id": request.turn_id,
        "profile_key": request.profile_key,
        "driver_kind": request.driver_kind,
        "tool_catalog_generation": request.tool_catalog_generation,
        "workflow_name": request.workflow_name,
        "workflow_version": request.workflow_version,
        "requested_run_id": request.requested_run_id,
        "requested_trace_id": request.requested_trace_id,
        "requested_thread_id": request.requested_thread_id,
        "resolved_run_id": request.resolved_run_id,
        "resolved_trace_id": request.resolved_trace_id,
        "resolved_thread_id": request.resolved_thread_id,
        "checkpoint_namespace": request.checkpoint_namespace,
        "manifest_hash": request.manifest_hash,
        "implementation_hash": request.implementation_hash,
        "state_schema_version": request.state_schema_version,
        "start_input_schema_ref": request.start_input_schema_ref,
        "start_input_schema_hash": request.start_input_schema_hash,
        "terminal_projection_descriptor": descriptor,
        "terminal_request_factory_hash": request.terminal_request_factory_hash,
        "start_input": start_input,
        "capability_snapshot": capability_snapshot,
    }


def start_admission_request_from_json(
    value: Mapping[str, JsonValue],
) -> StartAdmissionRequest:
    descriptor = value.get("terminal_projection_descriptor")
    if descriptor is not None and not isinstance(descriptor, dict):
        raise TypeError("terminal projection descriptor must be a JSON object")
    start_input = value.get("start_input")
    capability_snapshot = value.get("capability_snapshot")
    if not isinstance(start_input, dict) or not isinstance(capability_snapshot, dict):
        raise TypeError("workflow start request JSON objects are invalid")

    def required_string(name: str) -> str:
        item = value.get(name)
        if not isinstance(item, str):
            raise TypeError(f"{name} must be a string")
        return item

    def optional_string(name: str) -> str | None:
        item = value.get(name)
        if item is not None and not isinstance(item, str):
            raise TypeError(f"{name} must be a string or null")
        return item

    generation = value.get("tool_catalog_generation")
    schema_version = value.get("state_schema_version")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise TypeError("tool_catalog_generation must be an integer")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise TypeError("state_schema_version must be an integer")
    return StartAdmissionRequest(
        request_key=required_string("request_key"),
        mode=StartMode(required_string("mode")),
        session_id=required_string("session_id"),
        request_id=required_string("request_id"),
        turn_id=required_string("turn_id"),
        profile_key=required_string("profile_key"),
        driver_kind=required_string("driver_kind"),
        tool_catalog_generation=generation,
        workflow_name=required_string("workflow_name"),
        workflow_version=required_string("workflow_version"),
        requested_run_id=optional_string("requested_run_id"),
        requested_trace_id=optional_string("requested_trace_id"),
        requested_thread_id=optional_string("requested_thread_id"),
        resolved_run_id=optional_string("resolved_run_id"),
        resolved_trace_id=optional_string("resolved_trace_id"),
        resolved_thread_id=optional_string("resolved_thread_id"),
        checkpoint_namespace=required_string("checkpoint_namespace"),
        manifest_hash=required_string("manifest_hash"),
        implementation_hash=required_string("implementation_hash"),
        state_schema_version=schema_version,
        start_input_schema_ref=optional_string("start_input_schema_ref"),
        start_input_schema_hash=optional_string("start_input_schema_hash"),
        terminal_projection_descriptor=descriptor,
        terminal_request_factory_hash=optional_string(
            "terminal_request_factory_hash"
        ),
        start_input=start_input,
        capability_snapshot=capability_snapshot,
    )


@dataclass(frozen=True, slots=True)
class StartAdmissionReceipt:
    request: StartAdmissionRequest
    request_id: str
    request_key: str
    request_fingerprint: str
    run_id: str
    trace_id: str
    thread_id: str
    phase: StartPhase
    version: int
    claim_action: StartClaimAction | None = None
    claim_owner: str | None = None
    claim_epoch: int | None = None
    claim_expires_at: float | None = None
    activation: WorkflowActivation | None = None
    serialized_outcome: FrozenJsonValue | None = None

    def __post_init__(self) -> None:
        phase = StartPhase(self.phase)
        action = (
            None
            if self.claim_action is None
            else StartClaimAction(self.claim_action)
        )
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "claim_action", action)
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("start receipt version must be non-negative")
        claim_values = (
            action,
            self.claim_owner,
            self.claim_epoch,
            self.claim_expires_at,
        )
        if phase is StartPhase.ADMITTED:
            if (
                any(value is not None for value in claim_values)
                or self.activation is not None
                or self.serialized_outcome is not None
            ):
                raise ValueError("admitted start cannot carry claim authority")
            return
        if any(value is None for value in claim_values):
            raise ValueError("claimed start receipt lacks claim authority")
        assert action is not None
        assert self.claim_owner is not None
        assert self.claim_epoch is not None
        assert self.claim_expires_at is not None
        if (
            isinstance(self.claim_epoch, bool)
            or self.claim_epoch < 1
            or not math.isfinite(self.claim_expires_at)
        ):
            raise ValueError("start claim epoch/expiry is invalid")
        if action is StartClaimAction.RESUME and self.version < 1:
            raise ValueError("resumed start claim must advance receipt version")
        if phase is StartPhase.SETTLED:
            if self.activation is not None or self.serialized_outcome is None:
                raise ValueError("settled start must retain only claim audit authority")
            return
        if self.activation is None or self.serialized_outcome is not None:
            raise ValueError("active start receipt requires one activation")
        activation = self.activation
        if (
            activation.execution_lease.run_id != self.run_id
            or activation.workflow_lease.run_id != self.run_id
            or activation.run_fence.run_id.value != self.run_id
            or activation.workflow_lease.namespace
            != self.request.checkpoint_namespace
            or activation.execution_lease.owner_id != self.claim_owner
            or activation.workflow_lease.owner_id != self.claim_owner
            or activation.run_fence.owner_id != self.claim_owner
            or activation.workflow_lease.epoch != self.claim_epoch
            or activation.workflow_lease.runtime_lease_epoch
            != activation.execution_lease.epoch
            or activation.run_fence.runtime_lease_epoch
            != activation.execution_lease.epoch
            or activation.workflow_lease.expires_at != self.claim_expires_at
            or activation.execution_lease.expires_at != self.claim_expires_at
        ):
            raise ValueError("start receipt activation differs from claim authority")


@dataclass(frozen=True, slots=True)
class WorkflowTerminalOutcome:
    receipt_id: str
    run_id: str
    checkpoint_id: str
    state: str
    event_id: str
    delivery_ids: tuple[str, ...]
    outcome_hash: str


@dataclass(frozen=True, slots=True)
class WorkflowRetryWake:
    run_id: str
    receipt_id: str
    receipt_version: int
    mode: StartMode
    due_at: float
    wait_event_id: str
    generic_run_version: int
    outcome_hash: str


@dataclass(frozen=True, slots=True)
class ResumeAdmissionRequest:
    receipt_id: str
    run_id: str
    expected_run_version: int
    expected_checkpoint_head: str
    pending_interrupts: tuple[tuple[str, str], ...]
    responses: Mapping[str, JsonValue]
    responses_hash: str
    mode: StartMode = StartMode.STANDALONE

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.pending_interrupts))
        if ordered != self.pending_interrupts or len(
            {item[0] for item in ordered}
        ) != len(ordered):
            raise ValueError(
                "pending interrupts must be unique and canonically ordered"
            )
        object.__setattr__(
            self, "responses", _frozen_object(self.responses, path="$.responses")
        )


@dataclass(frozen=True, slots=True)
class ResumeCommitBinding:
    receipt_id: str
    expected_receipt_version: int
    target_run_revision: int
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResumeAdmissionReceipt:
    request: ResumeAdmissionRequest
    request_fingerprint: str
    phase: ResumePhase
    version: int
    claim_owner: str | None = None
    claim_epoch: int | None = None
    claim_expires_at: float | None = None
    activation: WorkflowActivation | None = None
    serialized_outcome: FrozenJsonValue | None = None
    next_attempt_at: float | None = None

    def __post_init__(self) -> None:
        phase = ResumePhase(self.phase)
        object.__setattr__(self, "phase", phase)
        if self.version < 0:
            raise ValueError("resume receipt version must be non-negative")
        claim_values = (
            self.claim_owner,
            self.claim_epoch,
            self.claim_expires_at,
        )
        if phase is ResumePhase.CLAIMED:
            if any(value is None for value in claim_values):
                raise ValueError("claimed resume receipt lacks activation authority")
            if self.activation is not None and (
                self.activation.execution_lease.run_id != self.request.run_id
                or self.activation.execution_lease.owner_id != self.claim_owner
                or self.activation.execution_lease.epoch != self.claim_epoch
            ):
                raise ValueError("resume activation differs from claim identity")
            if self.next_attempt_at is not None:
                raise ValueError("claimed resume cannot retain retry due time")
        elif phase is not ResumePhase.SETTLED and (
            any(value is not None for value in claim_values)
            or self.activation is not None
        ):
            raise ValueError("non-claimed resume cannot carry activation authority")
        elif phase is ResumePhase.SETTLED and self.activation is not None:
            raise ValueError("settled resume cannot carry activation authority")
        if phase is ResumePhase.RETRY_WAIT:
            if self.next_attempt_at is None:
                raise ValueError("retry-wait resume receipt lacks due time")
        elif self.next_attempt_at is not None:
            raise ValueError("non-retry resume cannot carry due time")


@dataclass(frozen=True, slots=True)
class WorkflowRecoveryWork:
    run_id: str
    receipt_kind: WorkflowRecoveryReceiptKind
    receipt_id: str
    receipt_version: int
    mode: StartMode
    due_at: float | None
    request_fingerprint: str
    receipt_snapshot: StartAdmissionReceipt | ResumeAdmissionReceipt

    def __post_init__(self) -> None:
        kind = WorkflowRecoveryReceiptKind(self.receipt_kind)
        mode = StartMode(self.mode)
        object.__setattr__(self, "receipt_kind", kind)
        object.__setattr__(self, "mode", mode)
        snapshot_due_at: float | None = None
        if kind is WorkflowRecoveryReceiptKind.START:
            if not isinstance(self.receipt_snapshot, StartAdmissionReceipt):
                raise TypeError("start recovery work requires a start receipt")
            snapshot_id = self.receipt_snapshot.request_key
            snapshot_mode = self.receipt_snapshot.request.mode
            snapshot_run_id = self.receipt_snapshot.run_id
        else:
            if not isinstance(self.receipt_snapshot, ResumeAdmissionReceipt):
                raise TypeError("resume recovery work requires a resume receipt")
            snapshot_id = self.receipt_snapshot.request.receipt_id
            snapshot_mode = self.receipt_snapshot.request.mode
            snapshot_run_id = self.receipt_snapshot.request.run_id
            snapshot_due_at = self.receipt_snapshot.next_attempt_at
        if self.run_id != snapshot_run_id:
            raise ValueError("recovery work Run differs from receipt")
        if self.receipt_id != snapshot_id or self.mode is not snapshot_mode:
            raise ValueError("recovery work identity differs from receipt")
        if self.receipt_version != self.receipt_snapshot.version:
            raise ValueError("recovery work version differs from receipt")
        if self.request_fingerprint != self.receipt_snapshot.request_fingerprint:
            raise ValueError("recovery work fingerprint differs from receipt")
        if kind is WorkflowRecoveryReceiptKind.START:
            if self.due_at is not None:
                raise ValueError("start recovery work cannot carry a due time")
        elif self.due_at != snapshot_due_at:
            raise ValueError("resume recovery due time differs from receipt")


@dataclass(frozen=True, slots=True)
class PrecreatedStartDispatch:
    action: PrecreatedStartAction
    receipt: StartAdmissionReceipt
    activation: WorkflowActivation | None = None
    serialized_outcome: FrozenJsonValue | None = None

    def __post_init__(self) -> None:
        action = PrecreatedStartAction(self.action)
        object.__setattr__(self, "action", action)
        if action is PrecreatedStartAction.SETTLED:
            if (
                self.receipt.phase is not StartPhase.SETTLED
                or self.activation is not None
                or self.serialized_outcome != self.receipt.serialized_outcome
            ):
                raise ValueError("settled dispatch outcome differs from receipt phase")
            return
        expected_phase = (
            StartPhase.RUNNING
            if action is PrecreatedStartAction.RESUME_RUNNING
            else StartPhase.CLAIMED
        )
        if self.receipt.phase is not expected_phase or self.activation is None:
            raise ValueError("active dispatch action differs from receipt phase")
        expected_claim_action = (
            StartClaimAction.NEW
            if action is PrecreatedStartAction.NEW_CLAIMED
            else StartClaimAction.RESUME
        )
        if self.receipt.claim_action is not expected_claim_action:
            raise ValueError("dispatch action differs from receipt claim action")
        if self.serialized_outcome is not None:
            raise ValueError("active dispatch cannot carry a serialized outcome")
        if (
            self.receipt.activation != self.activation
            or self.activation.execution_lease.run_id != self.receipt.run_id
            or self.activation.workflow_lease.run_id != self.receipt.run_id
            or self.activation.workflow_lease.namespace
            != self.receipt.request.checkpoint_namespace
            or self.receipt.claim_owner != self.activation.execution_lease.owner_id
            or self.receipt.claim_owner != self.activation.workflow_lease.owner_id
            or self.receipt.claim_owner != self.activation.run_fence.owner_id
            or self.receipt.claim_epoch != self.activation.workflow_lease.epoch
            or self.activation.workflow_lease.runtime_lease_epoch
            != self.activation.execution_lease.epoch
            or self.activation.run_fence.runtime_lease_epoch
            != self.activation.execution_lease.epoch
            or self.receipt.claim_expires_at
            != self.activation.workflow_lease.expires_at
            or self.receipt.claim_expires_at
            != self.activation.execution_lease.expires_at
        ):
            raise ValueError("active dispatch namespace/identity differs from receipt")


@dataclass(frozen=True, slots=True)
class CancelWorkflowRequest:
    cancel_id: str
    run_id: str
    reason: str
    expected_generation: int


@dataclass(frozen=True, slots=True)
class CancelWorkflowOutcome:
    cancel_id: str
    generation: int
    phase: str
    blocker_ids: tuple[str, ...]
    terminal: bool | None = None


@dataclass(frozen=True, slots=True)
class CancelConvergenceLease:
    run_id: str
    generation: int
    owner_id: str
    epoch: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class DangerousEffectObservation:
    effect_id: str
    kind: str
    state: str
    ledger_version: int
    request_hash: str
    handoff_attempt: int


@dataclass(frozen=True, slots=True)
class DangerousEffectConfirmation:
    scope: str
    observations: tuple[DangerousEffectObservation, ...]
    digest: str

    def __post_init__(self) -> None:
        if (
            tuple(sorted(self.observations, key=lambda item: item.effect_id))
            != self.observations
        ):
            raise ValueError("dangerous observations must be canonically ordered")


@dataclass(frozen=True, slots=True)
class ForkRequest:
    fork_id: str
    fingerprint: str
    source_run_id: str
    source_namespace: str
    source_checkpoint_id: str
    source_run_version: int
    source_head: str
    engine_hash: str
    manifest_hash: str
    implementation_hash: str
    schema_hash: str
    patch: Mapping[str, JsonValue]
    dangerous_confirmation: DangerousEffectConfirmation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "patch", _frozen_object(self.patch, path="$.fork.patch")
        )


@dataclass(frozen=True, slots=True)
class ForkReceipt:
    request: ForkRequest
    target_run_id: str
    target_trace_id: str
    target_thread_id: str
    target_checkpoint_id: str
    phase: ForkPhase
    version: int
    claim_owner: str | None = None
    claim_epoch: int | None = None
    claim_expires_at: float | None = None
    outcome: FrozenJsonValue | None = None


@dataclass(frozen=True, slots=True)
class ForkWriteLease:
    fork_id: str
    target_run_id: str
    owner_id: str
    claim_epoch: int
    expires_at: float
    expected_receipt_version: int
    mode: str = "write"


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    run_id: str
    run_version: int
    status: str
    runtime_lease_owner: str | None
    runtime_lease_epoch: int | None
    runtime_lease_expires_at: float | None
    workflow_lease_namespace: str | None
    workflow_lease_owner: str | None
    workflow_lease_epoch: int | None
    workflow_lease_expires_at: float | None
    run_fence_owner: str | None
    run_fence_runtime_lease_epoch: int | None
    run_fence_epoch: int | None
    run_fence_state: str | None
    checkpoint_head: str | None

    def __post_init__(self) -> None:
        if not self.run_id or self.run_version < 0 or not self.status:
            raise ValueError("recovery candidate identity is invalid")
        runtime = (
            self.runtime_lease_owner,
            self.runtime_lease_epoch,
            self.runtime_lease_expires_at,
        )
        workflow = (
            self.workflow_lease_owner,
            self.workflow_lease_epoch,
            self.workflow_lease_expires_at,
        )
        fence = (
            self.run_fence_owner,
            self.run_fence_runtime_lease_epoch,
            self.run_fence_epoch,
            self.run_fence_state,
        )
        for name, group in (
            ("runtime lease", runtime),
            ("workflow lease", workflow),
            ("Run fence", fence),
        ):
            present = tuple(value is not None for value in group)
            if any(present) and not all(present):
                raise ValueError(f"{name} authority must be wholly present or absent")
        if self.workflow_lease_owner is not None and not self.workflow_lease_namespace:
            raise ValueError("workflow lease requires its pinned namespace")


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    previous_status: str
    status: str
    action: str
    reason: str
    receipt_id: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    candidate: RecoveryCandidate
    manifest_hash: str | None
    implementation_hash: str | None
    checkpoint_hash: str | None
    unresolved_blocker_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    blocker_id: str
    owner_id: str
    epoch: int
    expires_at: float


class WorkflowOperationConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowOperationReceipt:
    operation_id: str
    adapter_method: str
    identity: tuple[str, ...]
    payload_hash: str
    outcome: JsonValue
    run_id: str
    namespace: str
    checkpoint_id: str | None
    lease_epoch: int
    created_at: float


class WorkflowTransaction(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    @property
    def is_open(self) -> bool: ...

    async def read_workflow_operation(
        self, operation_id: str
    ) -> WorkflowOperationReceipt | None: ...

    async def apply_workflow_operation(
        self,
        *,
        adapter_method: str,
        identity: tuple[str, ...],
        payload: Mapping[str, JsonValue],
    ) -> JsonValue: ...

    async def write_workflow_operation(
        self, receipt: WorkflowOperationReceipt
    ) -> None: ...


class WorkflowBlobReferencePort(Protocol):
    async def validate_references(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        owner_kind: str,
        owner_id: str,
        blob_refs: Sequence[str],
    ) -> None: ...


class WorkflowUnitOfWork(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    def read_run(self, run_id: str) -> object | None: ...

    def read_start_snapshot(self, run_id: str) -> Mapping[str, JsonValue] | None: ...

    async def run_atomic(
        self,
        operation: Callable[[WorkflowTransaction], Awaitable[T]],
        *,
        fault_label: str,
    ) -> T: ...


class WorkflowLifecyclePort(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    def read_cancel_resolution_snapshot(
        self, cancel_id: str
    ) -> Mapping[str, JsonValue] | None: ...

    def read_cancel_outcome(
        self, *, run_id: str, generation: int
    ) -> CancelWorkflowOutcome | None: ...

    def read_cancel_request(
        self, *, run_id: str, generation: int
    ) -> CancelWorkflowRequest | None: ...

    async def admit_start_standalone(
        self,
        transaction: WorkflowTransaction,
        request: StartAdmissionRequest,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> StartAdmissionReceipt: ...

    async def ensure_and_bind_precreated_start(
        self,
        transaction: WorkflowTransaction,
        request: StartAdmissionRequest,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        dispatch_claim: RuntimeStartDispatchClaim,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> PrecreatedStartDispatch: ...

    async def recover_precreated_start(
        self,
        transaction: WorkflowTransaction,
        recovery_work: WorkflowRecoveryWork,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> PrecreatedStartDispatch: ...

    def list_unsettled_start_admissions(
        self,
        snapshot_cursor: str | None,
        *,
        limit: int,
    ) -> tuple[tuple[StartAdmissionReceipt, ...], str | None]: ...

    def list_unsettled_resume_admissions(
        self,
        snapshot_cursor: str | None,
        *,
        limit: int,
    ) -> tuple[tuple[ResumeAdmissionReceipt, ...], str | None]: ...

    async def claim_activation(
        self,
        transaction: WorkflowTransaction,
        run_id: str,
        expected_run_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation: ...

    async def bind_activation(
        self,
        transaction: WorkflowTransaction,
        run_id: str,
        expected_run_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation: ...

    async def renew_activation(
        self,
        transaction: WorkflowTransaction,
        activation: WorkflowActivation,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> WorkflowActivation: ...

    async def release_activation(
        self,
        transaction: WorkflowTransaction,
        activation: WorkflowActivation,
        expected_run_version: int,
        outcome: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> None: ...

    async def request_cancel(
        self,
        transaction: WorkflowTransaction,
        request: CancelWorkflowRequest,
        expected_run_version: int,
        activation: WorkflowActivation | None,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> CancelWorkflowOutcome: ...

    async def admit_resume(
        self,
        transaction: WorkflowTransaction,
        request: ResumeAdmissionRequest,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt: ...

    async def claim_resume_standalone(
        self,
        transaction: WorkflowTransaction,
        receipt_id: str,
        expected_receipt_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt: ...

    async def claim_resume_precreated(
        self,
        transaction: WorkflowTransaction,
        receipt_id: str,
        expected_receipt_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt: ...

    async def settle_resume(
        self,
        transaction: WorkflowTransaction,
        binding: ResumeCommitBinding,
        activation: WorkflowActivation,
        committed_checkpoint: str,
        outcome: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt: ...

    async def defer_resume_retry(
        self,
        transaction: WorkflowTransaction,
        binding: ResumeCommitBinding,
        activation: WorkflowActivation,
        retry_operation_id: str,
        retry_attempt: int,
        next_attempt_at: float,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ResumeAdmissionReceipt: ...

    async def claim_cancel_convergence(
        self,
        transaction: WorkflowTransaction,
        cancel_id: str,
        expected_generation: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> CancelConvergenceLease: ...

    async def settle_cancel_convergence(
        self,
        transaction: WorkflowTransaction,
        cancel_lease: CancelConvergenceLease,
        resolution_snapshot: Mapping[str, JsonValue],
        terminal_checkpoint: Mapping[str, JsonValue],
        terminal_event: Mapping[str, JsonValue],
        deliveries: Sequence[Mapping[str, JsonValue]],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> CancelWorkflowOutcome: ...


class WorkflowRecoveryStorePort(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    def list_candidates(
        self,
        snapshot_cursor: str | None,
    ) -> tuple[tuple[RecoveryCandidate, ...], str | None]: ...

    def read_recovery_snapshot(self, run_id: str) -> RecoverySnapshot: ...

    async def commit_recovery_outcome(
        self,
        transaction: WorkflowTransaction,
        candidate: RecoveryCandidate,
        expected_snapshot: RecoverySnapshot,
        outcome: RecoveryOutcome,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> RecoveryOutcome: ...

    async def claim_resolved_recovery(
        self,
        transaction: WorkflowTransaction,
        blocker_id: str,
        expected_resolution_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> RecoveryClaim: ...


class WorkflowReplayPort(Protocol):
    @property
    def transaction_owner(self) -> object: ...

    def read_fork(self, fork_id: str) -> ForkReceipt | None: ...

    async def prepare_fork(
        self,
        transaction: WorkflowTransaction,
        request: ForkRequest,
        expected_source_snapshot: RecoverySnapshot,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt: ...

    async def claim_fork(
        self,
        transaction: WorkflowTransaction,
        fork_id: str,
        expected_receipt_version: int,
        owner_id: str,
        *,
        now: float,
        ttl_seconds: float,
        fault: FaultHook | None = None,
    ) -> ForkWriteLease: ...

    async def checkpoint_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_target_head: str | None,
        checkpoint_operation_id: str,
        checkpoint: Mapping[str, JsonValue],
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt: ...

    async def commit_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_receipt_version: int,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt: ...

    async def rollback_fork(
        self,
        transaction: WorkflowTransaction,
        fork_lease: ForkWriteLease,
        expected_receipt_version: int,
        reason: str,
        *,
        now: float,
        fault: FaultHook | None = None,
    ) -> ForkReceipt: ...

    def list_orphaned_forks(
        self,
        snapshot_cursor: str | None,
        *,
        now: float,
    ) -> tuple[tuple[ForkReceipt, ...], str | None]: ...


def _operation_id(identity: Sequence[str]) -> str:
    return hashlib.sha256(canonical_json(list(identity)).encode()).hexdigest()


class CheckpointExecutionAdapter:
    """Receipt-first adapter; it never owns or commits a transaction."""

    __slots__ = ("transaction_owner",)

    def __init__(self, transaction_owner: object) -> None:
        self.transaction_owner = transaction_owner

    async def _apply(
        self,
        transaction: WorkflowTransaction,
        *,
        method: str,
        identity: tuple[str, ...],
        payload: Mapping[str, JsonValue],
        operation_id: str | None,
        run_id: str,
        namespace: str,
        checkpoint_id: str | None,
        lease_epoch: int,
        created_at: float,
    ) -> JsonValue:
        if (
            not transaction.is_open
            or transaction.transaction_owner is not self.transaction_owner
        ):
            raise WorkflowOperationConflict("foreign or closed workflow transaction")
        expected_id = _operation_id(identity)
        if operation_id is not None and operation_id != expected_id:
            raise WorkflowOperationConflict(
                "operation id does not match durable identity"
            )
        resolved_id = expected_id
        detached = copy.deepcopy(dict(payload))
        payload_hash = hashlib.sha256(canonical_json(detached).encode()).hexdigest()
        existing = await transaction.read_workflow_operation(resolved_id)
        if existing is not None:
            if existing.adapter_method != method or existing.identity != identity:
                raise WorkflowOperationConflict(
                    "operation id reused across adapter methods"
                )
            if existing.payload_hash != payload_hash:
                raise WorkflowOperationConflict("operation payload changed")
            return copy.deepcopy(existing.outcome)
        outcome = await transaction.apply_workflow_operation(
            adapter_method=method, identity=identity, payload=detached
        )
        await transaction.write_workflow_operation(
            WorkflowOperationReceipt(
                resolved_id,
                method,
                identity,
                payload_hash,
                copy.deepcopy(outcome),
                run_id,
                namespace,
                checkpoint_id,
                lease_epoch,
                created_at,
            )
        )
        return copy.deepcopy(outcome)

    async def mark_running_on_claim(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        checkpoint_namespace: str,
        lease_epoch: int,
        claim_epoch: int,
        now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        identity = (run_id, checkpoint_namespace, str(lease_epoch), str(claim_epoch))
        return await self._apply(
            transaction,
            method="mark_running_on_claim",
            identity=identity,
            payload={
                "run_id": run_id,
                "checkpoint_namespace": checkpoint_namespace,
                "lease_epoch": lease_epoch,
                "claim_epoch": claim_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=None,
            lease_epoch=lease_epoch,
            created_at=now,
        )

    async def consume_decisions(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        checkpoint_id: str,
        decision_ids: Sequence[str],
        responses: Mapping[str, JsonValue],
        checkpoint_namespace: str,
        lease_epoch: int,
        now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        ordered = tuple(sorted(decision_ids))
        identity = (run_id, checkpoint_id, *ordered)
        return await self._apply(
            transaction,
            method="consume_decisions",
            identity=identity,
            payload={
                "run_id": run_id,
                "checkpoint_id": checkpoint_id,
                "decision_ids": list(ordered),
                "responses": copy.deepcopy(dict(responses)),
                "checkpoint_namespace": checkpoint_namespace,
                "lease_epoch": lease_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch,
            created_at=now,
        )

    async def open_decision(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        interrupt_id: str,
        request: Mapping[str, JsonValue],
        operation_id: str | None = None,
        checkpoint_namespace: str = "native",
        checkpoint_id: str | None = None,
        lease_epoch: int = 1,
        now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, interrupt_id)
        return await self._apply(
            transaction,
            method="open_decision",
            identity=identity,
            payload={
                "run_id": run_id,
                "interrupt_id": interrupt_id,
                "request": copy.deepcopy(dict(request)),
                "checkpoint_namespace": checkpoint_namespace,
                "checkpoint_id": checkpoint_id,
                "lease_epoch": lease_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch,
            created_at=now,
        )

    async def materialize_intent(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        intent_id: str,
        intent: Mapping[str, JsonValue],
        operation_id: str | None = None,
        checkpoint_namespace: str = "native",
        checkpoint_id: str | None = None,
        lease_epoch: int = 1,
        now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, intent_id)
        return await self._apply(
            transaction,
            method="materialize_intent",
            identity=identity,
            payload={
                "run_id": run_id,
                "intent_id": intent_id,
                "intent": copy.deepcopy(dict(intent)),
                "checkpoint_namespace": checkpoint_namespace,
                "checkpoint_id": checkpoint_id,
                "lease_epoch": lease_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch,
            created_at=now,
        )

    async def link_effects(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        checkpoint_namespace: str,
        checkpoint_id: str,
        effect_ids: Sequence[str],
        lease_epoch: int,
        now: float,
        operation_id: str | None = None,
    ) -> JsonValue:
        ordered = tuple(sorted(effect_ids))
        identity = (run_id, checkpoint_namespace, checkpoint_id, *ordered)
        return await self._apply(
            transaction,
            method="link_effects",
            identity=identity,
            payload={
                "run_id": run_id,
                "checkpoint_namespace": checkpoint_namespace,
                "checkpoint_id": checkpoint_id,
                "effect_ids": list(ordered),
                "lease_epoch": lease_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=checkpoint_id,
            lease_epoch=lease_epoch,
            created_at=now,
        )

    async def finalize_run(
        self,
        transaction: WorkflowTransaction,
        *,
        run_id: str,
        terminal_checkpoint_id: str,
        status: str,
        outcome: Mapping[str, JsonValue],
        operation_id: str | None = None,
        checkpoint_namespace: str = "native",
        lease_epoch: int = 1,
        now: float = 0.0,
    ) -> JsonValue:
        identity = (run_id, terminal_checkpoint_id)
        return await self._apply(
            transaction,
            method="finalize_run",
            identity=identity,
            payload={
                "run_id": run_id,
                "terminal_checkpoint_id": terminal_checkpoint_id,
                "status": status,
                "outcome": copy.deepcopy(dict(outcome)),
                "checkpoint_namespace": checkpoint_namespace,
                "lease_epoch": lease_epoch,
                "now": now,
            },
            operation_id=operation_id,
            run_id=run_id,
            namespace=checkpoint_namespace,
            checkpoint_id=terminal_checkpoint_id,
            lease_epoch=lease_epoch,
            created_at=now,
        )


@dataclass(frozen=True, slots=True)
class WorkflowExecutionPorts:
    unit_of_work: WorkflowUnitOfWork
    checkpoint: CheckpointExecutionAdapter
    lifecycle: WorkflowLifecyclePort
    recovery: WorkflowRecoveryStorePort
    replay: WorkflowReplayPort

    def __post_init__(self) -> None:
        owner = self.unit_of_work.transaction_owner
        if any(
            authority.transaction_owner is not owner
            for authority in (
                self.checkpoint,
                self.lifecycle,
                self.recovery,
                self.replay,
            )
        ):
            raise ValueError(
                "workflow execution ports do not share one transaction owner"
            )


__all__ = (
    "CancelConvergenceLease",
    "CancelWorkflowOutcome",
    "CancelWorkflowRequest",
    "CheckpointExecutionAdapter",
    "DangerousEffectConfirmation",
    "DangerousEffectObservation",
    "ForkPhase",
    "ForkReceipt",
    "ForkRequest",
    "ForkWriteLease",
    "PrecreatedStartAction",
    "PrecreatedStartDispatch",
    "RecoveryCandidate",
    "RecoveryClaim",
    "RecoveryOutcome",
    "RecoverySnapshot",
    "ResumeAdmissionReceipt",
    "ResumeAdmissionRequest",
    "ResumeCommitBinding",
    "ResumePhase",
    "StartAdmissionReceipt",
    "StartAdmissionRequest",
    "StartClaimAction",
    "StartMode",
    "StartPhase",
    "WorkflowActivation",
    "WorkflowBlobReferencePort",
    "WorkflowExecutionPorts",
    "WorkflowLifecyclePort",
    "WorkflowOperationConflict",
    "WorkflowOperationReceipt",
    "WorkflowRecoveryReceiptKind",
    "WorkflowRecoveryStorePort",
    "WorkflowRecoveryWork",
    "WorkflowReplayPort",
    "WorkflowRetryWake",
    "WorkflowTerminalOutcome",
    "WorkflowTransaction",
    "WorkflowUnitOfWork",
    "start_admission_request_from_json",
    "start_admission_request_to_json",
)
