# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable RunKernel lifecycle and fixed-root public client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, Self, TypeVar, cast, runtime_checkable
from uuid import uuid4

from simple_harness.contracts import (
    ContentBlock,
    ExecutionSessionId,
    FrozenJsonValue,
    HarnessError,
    JsonValue,
    Message,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.command_ingress import CommandClaim
from simple_harness.execution.context_authority import ToolCatalogSnapshot
from simple_harness.execution.context_staging import (
    ContextStageKind,
    ContextStageRecord,
    ContextStageState,
    ContextStagingRepository,
)
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.fences import RunFenceLease, RunFencePort
from simple_harness.execution.memory_outbox import CommittedTurnSpec, MemoryDispatcher
from simple_harness.execution.recovery import WaitBlockerSpec
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ContinuationRecord,
    ContinuationState,
    DecisionState,
    ExecutionLease,
    ExecutionUnitOfWork,
    FaultHook,
    RunRecord,
    RunState,
    UnitOfWorkConflict,
    UnitOfWorkNotFound,
    WorkflowCheckpoint,
)
from simple_harness.observability import CorrelationContext, ObservabilityRuntime, Outcome
from simple_harness.providers import CancelToken, ProviderReconciliationPort
from simple_harness.tools.authorization import AuthorizationDecision, AuthorizationPort
from simple_harness.tools.executor import EffectExecutor, ToolAuthorizationPending
from simple_harness.tools.reconciliation import ToolReconciliationPort
from simple_harness.tools.sidecar import resource_digest
from simple_harness.workflow.errors import WorkflowDependencyUnavailable

from .admission import AdmissionPort, AllowAllAdmission
from .agent_memory import (
    AgentMemoryError,
    AgentMemoryErrorCode,
    AgentMemoryPort,
    CommittedTurn,
    MemoryFailurePolicy,
    MemoryRecallBounds,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryReleaseRequest,
    MemoryScopeRef,
)
from .child_coordinator import ChildCoordinator
from .child_signal_runtime import ChildSignalRuntime
from .commands import (
    CancelCommandIntent,
    CommandError,
    CommandErrorCode,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContinueCommandIntent,
    StartCommandIntent,
    command_intent_from_json,
)
from .context import ContextPort
from .conversation_context import (
    claim_context_preparation,
    context_query_id,
)
from .conversation_context_provider import (
    ConversationContextBounds,
    ConversationContextProviderPort,
    ConversationContextRequest,
    CurrentMessageContextProvider,
)
from .conversation_memory import (
    ContextPreparationMode,
    ConversationContinuationInput,
    ConversationTurnInput,
    ConversationTurnOutput,
    _message_from_json,
)
from .live_index import LiveRunIndex
from .orchestration import (
    RuntimeActivationClaim,
    RuntimeStartAdmission,
    RuntimeStartDispatchClaim,
    RuntimeStartDisposition,
    WorkflowLaunchTicketPort,
    WorkflowSpawnReadyActivation,
)
from .start_snapshot import RunStart, StartSnapshot, bind_start_snapshot
from .terminal import TerminalCoordinator, ToolCatalogStale

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from simple_harness.runtime.workflow_spawn import (
        WorkflowSpawnCoordinatorOutcome,
        WorkflowSpawnToolOutcome,
    )
    from simple_harness.workflow.execution_ports import (
        WorkflowRecoveryWork,
        WorkflowRetryWake,
        WorkflowTerminalOutcome,
        WorkflowTransaction,
    )
    from simple_harness.workflow.runner import WorkflowRunner

T = TypeVar("T")

ROOT_PROFILE_KEY = "agent.general"
WORKFLOW_DRIVER_KIND = "workflow"


class RuntimeLifecycleState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    profile_key: str
    driver_kind: str

    def __post_init__(self) -> None:
        for name in ("profile_key", "driver_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class DriverInvocation:
    run: RunRecord
    start: StartSnapshot
    execution_lease: ExecutionLease
    run_fence: RunFenceLease
    services: RuntimeServices
    continuations: tuple[ContinuationRecord, ...] = ()
    workflow_start_dispatch: RuntimeStartDispatchClaim | None = None
    workflow_recovery_work: WorkflowRecoveryWork | None = None
    workflow_spawn_ready_activation: WorkflowSpawnReadyActivation | None = None

    def __post_init__(self) -> None:
        ready = self.workflow_spawn_ready_activation
        if ready is not None and (
            ready.execution_lease != self.execution_lease
            or ready.run_fence != self.run_fence
            or ready.execution_lease.run_id != self.run.run_id
        ):
            raise ValueError("workflow spawn ready activation differs from Driver authority")
        recovery = self.workflow_recovery_work
        if recovery is not None and (
            recovery.run_id != self.run.run_id or self.workflow_start_dispatch is not None
        ):
            raise ValueError("workflow recovery carrier differs from Driver authority")


@dataclass(frozen=True, slots=True)
class DriverResult:
    state: RunState
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    deliveries: tuple[DeliverySpec, ...] = ()
    wait_blocker: WaitBlockerSpec | None = None
    workflow_spawn_control: WorkflowSpawnToolOutcome | None = None
    workflow_terminal: WorkflowTerminalOutcome | None = None
    workflow_retry_wake: WorkflowRetryWake | None = None
    authorization_wait: ToolAuthorizationPending | None = None
    conversation_output: ConversationTurnOutput | None = None

    def __post_init__(self) -> None:
        state = RunState(self.state)
        if state not in {RunState.WAITING, RunState.COMPLETED, RunState.FAILED}:
            raise ValueError("driver result must be waiting, completed, or failed")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))
        if self.conversation_output is not None:
            if not isinstance(self.conversation_output, ConversationTurnOutput):
                raise TypeError("conversation_output must use ConversationTurnOutput")
            if state is not RunState.COMPLETED:
                raise ValueError("only COMPLETED may carry conversation_output")
        if self.workflow_spawn_control is not None and (
            state is not RunState.WAITING or self.wait_blocker is not None
        ):
            raise ValueError("workflow spawn control requires an exclusive WAITING result")
        if self.workflow_terminal is not None and state not in {
            RunState.COMPLETED,
            RunState.FAILED,
        }:
            raise ValueError("workflow terminal outcome requires COMPLETED or FAILED state")
        if self.workflow_retry_wake is not None and (
            state is not RunState.WAITING or self.wait_blocker is not None
        ):
            raise ValueError("workflow retry wake requires an exclusive WAITING result")
        if self.authorization_wait is not None and (
            state is not RunState.WAITING
            or self.wait_blocker is not None
            or self.workflow_spawn_control is not None
            or self.workflow_retry_wake is not None
        ):
            raise ValueError("authorization wait requires an exclusive WAITING result")


@runtime_checkable
class RuntimeDriver(Protocol):
    async def start(
        self,
        invocation: DriverInvocation,
        *,
        context: ContextPort,
        cancel: CancelToken,
    ) -> DriverResult: ...


@dataclass(frozen=True, slots=True)
class DriverCancellationRecovery:
    execution_lease: ExecutionLease
    run_fence: RunFenceLease


@dataclass(frozen=True, slots=True)
class DriverCancelOutcome:
    cancel_id: str
    generation: int
    phase: str
    blocker_ids: tuple[str, ...]
    terminal: bool


@runtime_checkable
class DriverCancellationCoordinator(Protocol):
    async def cancel(
        self,
        run: RunRecord,
        start_snapshot: StartSnapshot,
        *,
        reason: str,
        now: float,
        recovery: DriverCancellationRecovery,
    ) -> DriverCancelOutcome: ...


@runtime_checkable
class ToolCatalogGenerationPort(Protocol):
    def current_generation(self) -> int: ...

    def resolve(self, generation: int, content_fingerprint: str) -> ToolCatalogSnapshot | None: ...


@runtime_checkable
class RuntimeReconciliationPort(Protocol):
    async def reconcile(self) -> None: ...


@runtime_checkable
class WorkflowSpawnRuntimeCoordinator(Protocol):
    async def catalog_snapshot(self):  # type: ignore[no-untyped-def]
        ...

    async def execute(self, invocation):  # type: ignore[no-untyped-def]
        ...

    async def continue_ready(
        self, activation: WorkflowSpawnReadyActivation
    ) -> WorkflowSpawnCoordinatorOutcome: ...


@runtime_checkable
class ReactCheckpointPort(Protocol):
    def read_react_checkpoint(self, run_id: str) -> WorkflowCheckpoint | None: ...

    def cas_react_checkpoint(
        self,
        *,
        run_id: str,
        lease: ExecutionLease,
        expected_version: int | None,
        checkpoint: Mapping[str, JsonValue],
        checkpoint_hash: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> WorkflowCheckpoint: ...

    def ack_spawn_child_continuation_and_continue_batch(
        self,
        *,
        run_id: str,
        continuation_claim: ContinuationRecord,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> WorkflowCheckpoint: ...

    def commit_pending_spawn_child_completion_and_react_ready(
        self,
        *,
        run_id: str,
        expected_checkpoint_version: int,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> WorkflowCheckpoint: ...


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    provider: ProviderInvocationCoordinator
    tools: EffectExecutor
    authorization: AuthorizationPort
    context: ContextPort
    delivery: DeliveryDispatcher
    tool_reconciliation: ToolReconciliationPort
    reconciliation: RuntimeReconciliationPort
    provider_reconciliation: ProviderReconciliationPort
    react_checkpoint: ReactCheckpointPort
    tool_catalog: ToolCatalogGenerationPort | None = None
    workflow_spawn: WorkflowSpawnRuntimeCoordinator | None = None


class _CanonicalWorkflowSpawnRuntimeCoordinator:
    def __init__(
        self,
        *,
        uow: RuntimeUnitOfWork,
        runner: WorkflowRunner,
        owner_id: str,
        lease_ttl_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._uow = uow
        self._runner = runner
        self._owner_id = owner_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._clock = clock

    async def catalog_snapshot(self):  # type: ignore[no-untyped-def]
        from .orchestration import workflow_catalog_selection_from_authority

        async def operation(transaction):  # type: ignore[no-untyped-def]
            authority = await self._uow.read_catalog(transaction)
            return workflow_catalog_selection_from_authority(authority)

        return await self._uow.run_atomic(operation, fault_label="runtime:workflow_spawn:catalog")

    async def continue_ready(
        self, activation: WorkflowSpawnReadyActivation
    ) -> WorkflowSpawnCoordinatorOutcome:
        async def operation(
            transaction: WorkflowTransaction,
        ) -> WorkflowSpawnCoordinatorOutcome:
            from .workflow_spawn import (
                WorkflowSpawnBatchAction,
                WorkflowSpawnChildControlKind,
                _create_workflow_spawn_child_control,
                _create_workflow_spawn_failed,
                _create_workflow_spawn_tool_outcome,
                workflow_spawn_completion_receipt_id,
            )

            issued = await self._uow.read_issued(
                transaction, activation.ready_receipt.spawn_operation_id
            )
            if issued is None:
                raise UnitOfWorkConflict("workflow spawn ticket disappeared")
            ticket, launch = issued
            terminal_result = await self._uow.read_spawn_continuation_outcome(
                transaction, activation.ready_receipt.spawn_operation_id
            )
            prior = await self._uow.read_spawn_admission_outcome(
                transaction, activation.ready_receipt.spawn_operation_id
            )
            if terminal_result is not None and prior is None:
                return _create_workflow_spawn_failed(
                    tool_result=terminal_result,
                    completion_receipt_id=workflow_spawn_completion_receipt_id(
                        activation.ready_receipt.spawn_operation_id
                    ),
                    batch_action=WorkflowSpawnBatchAction.CONTINUE,
                )
            if prior is not None:
                if terminal_result is None:
                    raise UnitOfWorkConflict(
                        "workflow spawn admission lost its terminal Tool result"
                    )
                admission = await self._uow.resume_admitted_runtime_start(
                    transaction,
                    ticket,
                    RuntimeActivationClaim(
                        self._owner_id,
                        lease_ttl_seconds=self._lease_ttl_seconds,
                    ),
                    now=self._clock(),
                )
                kind = {
                    RuntimeStartDisposition.START_NEW: WorkflowSpawnChildControlKind.START,
                    RuntimeStartDisposition.START_ORPHAN: WorkflowSpawnChildControlKind.START,
                    RuntimeStartDisposition.RECOVER_START: WorkflowSpawnChildControlKind.RECOVER,
                    RuntimeStartDisposition.RECOVER_RESUME: WorkflowSpawnChildControlKind.RECOVER,
                    RuntimeStartDisposition.ATTACH_CURRENT: WorkflowSpawnChildControlKind.ATTACH,
                    RuntimeStartDisposition.FOREIGN_ACTIVE: WorkflowSpawnChildControlKind.WAITING,
                    RuntimeStartDisposition.WAITING: WorkflowSpawnChildControlKind.WAITING,
                    RuntimeStartDisposition.CANCEL_PENDING: WorkflowSpawnChildControlKind.CANCEL,
                    RuntimeStartDisposition.TERMINAL: WorkflowSpawnChildControlKind.TERMINAL,
                }[admission.disposition]
                child_control = _create_workflow_spawn_child_control(
                    kind=kind,
                    admission=admission,
                )
                return _create_workflow_spawn_tool_outcome(
                    tool_result=terminal_result,
                    child_control=child_control,
                    child_start_ref=prior.child_start_ref,
                    suspension=prior.suspension,
                )
            try:
                verified = await self._uow.verify(transaction, ticket)
            except UnitOfWorkConflict:
                failed = await self._uow.settle_spawn_continuation_catalog_stale(
                    transaction,
                    activation.continuation_claim,
                    activation.ready_receipt,
                    now=self._clock(),
                )
                return _create_workflow_spawn_failed(
                    tool_result=failed,
                    completion_receipt_id=workflow_spawn_completion_receipt_id(
                        activation.ready_receipt.spawn_operation_id
                    ),
                    batch_action=WorkflowSpawnBatchAction.CONTINUE,
                )
            start = RunStart(
                execution_session_id=ExecutionSessionId(verified.session_id),
                run_id=RunId(verified.resolved_run_id),
                request_id=RequestId(verified.request_id),
                turn_id=verified.turn_id,
                input=launch.start_input,
                tool_catalog_generation=verified.tool_catalog_generation,
            )
            try:
                request = self._runner.prepare_start_admission(verified, start)
            except WorkflowDependencyUnavailable:
                evidence = self._runner._prove_graph_unavailable(verified, activation)
                failed = await self._uow.settle_spawn_continuation_graph_unavailable(
                    transaction,
                    activation.continuation_claim,
                    activation.ready_receipt,
                    evidence,
                    now=self._clock(),
                )
                return _create_workflow_spawn_failed(
                    tool_result=failed,
                    completion_receipt_id=workflow_spawn_completion_receipt_id(
                        activation.ready_receipt.spawn_operation_id
                    ),
                    batch_action=WorkflowSpawnBatchAction.CONTINUE,
                )
            snapshot = bind_start_snapshot(
                start,
                profile_key=verified.profile_key,
                driver_kind=WORKFLOW_DRIVER_KIND,
                workflow_admission=request,
            )
            return await self._uow.continue_spawn_admission(
                transaction,
                ticket,
                activation.continuation_claim,
                start,
                request,
                snapshot,
                RuntimeActivationClaim(
                    self._owner_id,
                    lease_ttl_seconds=self._lease_ttl_seconds,
                ),
                now=self._clock(),
            )

        return await self._uow.run_atomic(operation, fault_label="runtime:spawn_ready:continue")


class RuntimeUnitOfWork(ExecutionUnitOfWork, RunFencePort, WorkflowLaunchTicketPort, Protocol):
    @property
    def transaction_owner(self) -> object: ...

    async def run_atomic(
        self,
        operation: Callable[[WorkflowTransaction], Awaitable[T]],
        *,
        fault_label: str,
    ) -> T: ...

    def submit_start_command(self, intent: StartCommandIntent, *, now: float) -> CommandReceipt: ...

    def submit_continue_command(
        self, intent: ContinueCommandIntent, *, now: float
    ) -> CommandReceipt: ...

    def submit_cancel_command(
        self, intent: CancelCommandIntent, *, now: float
    ) -> CommandReceipt: ...

    def get_command_receipt(self, command_id: str) -> CommandReceipt: ...

    def get_command_snapshot(self, command_id: str) -> CommandSnapshot: ...

    def reserve_legacy_run_mode(self, *, run_id: str, intent_hash: str, now: float) -> None: ...

    def require_legacy_or_unmanaged_run(self, run_id: str) -> None: ...

    def claim_next_command(
        self, *, owner_id: str, now: float, lease_seconds: float
    ) -> CommandClaim | None: ...

    def transition_command(
        self,
        claim: CommandClaim,
        *,
        expected: CommandState,
        target: CommandState,
        now: float,
    ) -> CommandReceipt: ...

    def retry_command(
        self,
        claim: CommandClaim,
        *,
        error_code: str,
        retry_at: float,
        now: float,
    ) -> CommandReceipt: ...

    def heartbeat_command(
        self, claim: CommandClaim, *, now: float, lease_seconds: float
    ) -> CommandClaim: ...

    def reject_command(
        self,
        claim: CommandClaim,
        *,
        error_code: CommandErrorCode,
        now: float,
    ) -> CommandReceipt: ...

    def apply_start_command(
        self,
        claim: CommandClaim,
        *,
        execution_session_id: str,
        request_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        user_id: str,
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> RunRecord: ...

    def apply_continue_command(
        self,
        claim: CommandClaim,
        *,
        continuation_id: str,
        payload: Mapping[str, JsonValue],
        now: float,
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord: ...

    def apply_cancel_command(
        self,
        claim: CommandClaim,
        *,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord: ...


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    provider: ProviderInvocationCoordinator
    tools: EffectExecutor
    authorization: AuthorizationPort
    context: ContextPort
    delivery: DeliveryDispatcher
    tool_reconciliation: ToolReconciliationPort
    reconciliation: RuntimeReconciliationPort
    provider_reconciliation: ProviderReconciliationPort
    react_checkpoint: ReactCheckpointPort
    tool_catalog: ToolCatalogGenerationPort
    admission: AdmissionPort = field(default_factory=AllowAllAdmission)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    owner_id: str = field(default_factory=lambda: f"runtime-{uuid4().hex}")
    lease_ttl_seconds: float = 30.0
    close_timeout_seconds: float = 5.0
    conversation_memory_enabled: bool = False
    memory_dispatcher: MemoryDispatcher | None = None
    context_staging: ContextStagingRepository | None = None
    context_preparation_mode: ContextPreparationMode | None = None
    agent_memory: AgentMemoryPort | None = None
    context_provider: ConversationContextProviderPort | None = None
    memory_failure_policy: MemoryFailurePolicy = MemoryFailurePolicy.DEGRADE_RECALL_AND_RETRY_RECORD
    observability: ObservabilityRuntime | None = None

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "tools",
            "authorization",
            "context",
            "delivery",
            "tool_reconciliation",
            "reconciliation",
            "provider_reconciliation",
            "react_checkpoint",
            "tool_catalog",
        ):
            if getattr(self, name) is None:
                raise TypeError(f"{name} Port is required")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if (
            not isinstance(self.lease_ttl_seconds, (int, float))
            or isinstance(self.lease_ttl_seconds, bool)
            or not math.isfinite(float(self.lease_ttl_seconds))
            or self.lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be finite and positive")
        if (
            not isinstance(self.close_timeout_seconds, (int, float))
            or isinstance(self.close_timeout_seconds, bool)
            or not math.isfinite(float(self.close_timeout_seconds))
            or self.close_timeout_seconds <= 0
        ):
            raise ValueError("close_timeout_seconds must be finite and positive")
        if self.conversation_memory_enabled and self.context_staging is None:
            raise TypeError("enabled conversation Memory requires context staging")
        if self.conversation_memory_enabled and (
            self.memory_dispatcher is None and self.agent_memory is None
        ):
            raise TypeError("enabled conversation Memory requires an Agent Memory authority")
        if self.conversation_memory_enabled and self.context_preparation_mode is None:
            raise TypeError("enabled conversation Memory requires preparation mode")
        if self.context_preparation_mode is not None:
            object.__setattr__(
                self,
                "context_preparation_mode",
                ContextPreparationMode(self.context_preparation_mode),
            )
        if self.agent_memory is not None:
            for method_name in ("recall_for_turn", "release_recall", "record_committed_turn"):
                if not callable(getattr(self.agent_memory, method_name, None)):
                    raise TypeError(f"memory must implement {method_name}")
            if self.context_provider is None:
                object.__setattr__(self, "context_provider", CurrentMessageContextProvider())
        object.__setattr__(
            self, "memory_failure_policy", MemoryFailurePolicy(self.memory_failure_policy)
        )


class RunClient:
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def start(self, value: RunStart) -> RunRecord:
        self._runtime._require_started()
        if not isinstance(value, RunStart):
            raise TypeError("value must use RunStart")
        if self._runtime._ports.agent_memory is not None and value.conversation is None:
            raise HarnessError(
                "conversation_entrypoint_required",
                "Enabled Agent Memory requires start_conversation().",
            )
        self._runtime._reserve_legacy_start(value)
        return await self._runtime._start_run(value)

    async def submit_start(self, intent: StartCommandIntent) -> CommandReceipt:
        self._runtime._require_started()
        if not isinstance(intent, StartCommandIntent):
            raise TypeError("intent must use StartCommandIntent")
        receipt = self._runtime._uow.submit_start_command(intent, now=self._runtime._now())
        self._runtime._command_wake.set()
        return receipt

    async def submit_continue(self, intent: ContinueCommandIntent) -> CommandReceipt:
        self._runtime._require_started()
        if not isinstance(intent, ContinueCommandIntent):
            raise TypeError("intent must use ContinueCommandIntent")
        receipt = self._runtime._uow.submit_continue_command(intent, now=self._runtime._now())
        self._runtime._command_wake.set()
        return receipt

    async def submit_cancel(self, intent: CancelCommandIntent) -> CommandReceipt:
        self._runtime._require_started()
        if not isinstance(intent, CancelCommandIntent):
            raise TypeError("intent must use CancelCommandIntent")
        receipt = self._runtime._uow.submit_cancel_command(intent, now=self._runtime._now())
        self._runtime._command_wake.set()
        return receipt

    async def get_command(self, command_id: str) -> CommandSnapshot:
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("command_id is required")
        return self._runtime._uow.get_command_snapshot(command_id)

    async def start_conversation(
        self,
        value: ConversationTurnInput,
        *,
        run_id: RunId | None = None,
        request_id: RequestId | None = None,
        turn_id: str | None = None,
        tool_catalog_generation: int = 1,
        tool_catalog_fingerprint: str | None = None,
        provider_budget_fingerprint: str | None = None,
        input: Mapping[str, JsonValue] | None = None,
    ) -> RunRecord:
        """Prepare one durable Context stage and start a trusted conversation Turn."""

        self._runtime._require_started()
        if not isinstance(value, ConversationTurnInput):
            raise TypeError("value must use ConversationTurnInput")
        resolved_run = run_id or RunId(uuid4().hex)
        resolved_request = request_id or RequestId(f"{resolved_run.value}:request")
        resolved_turn = turn_id or resolved_run.value
        start_input = cast(
            Mapping[str, JsonValue],
            dict(input or {"messages": [value.message.to_dict()]}),
        )
        preflight_start = RunStart(
            ExecutionSessionId(value.identity.session_id),
            resolved_run,
            resolved_request,
            resolved_turn,
            start_input,
            tool_catalog_generation,
            tool_catalog_fingerprint,
            provider_budget_fingerprint,
            conversation=value,
        )
        reserve = getattr(self._runtime._uow, "reserve_legacy_run_mode", None)
        if callable(reserve):
            reserve(
                run_id=resolved_run.value,
                intent_hash=hashlib.sha256(
                    canonical_json(
                        {
                            "run_id": resolved_run.value,
                            "request_id": resolved_request.value,
                            "turn_id": resolved_turn,
                            "conversation": value.to_json(),
                            "input": dict(start_input),
                            "tool_catalog_generation": tool_catalog_generation,
                        }
                    ).encode()
                ).hexdigest(),
                now=self._runtime._now(),
            )
        existing = self._runtime._uow.read_run(resolved_run.value)
        if existing is not None:
            raw = self._runtime._uow.read_start_snapshot(resolved_run.value)
            if raw is None:
                raise UnitOfWorkConflict("conversation start snapshot disappeared")
            snapshot = StartSnapshot.from_json(raw)
            if (
                snapshot.conversation != value
                or snapshot.turn_id != resolved_turn
                or existing.request_id != resolved_request.value
            ):
                raise UnitOfWorkConflict("conversation start identity was reused differently")
            return existing
        memory = self._runtime._ports.agent_memory
        if memory is None:
            return await self._runtime._start_run(preflight_start)
        staged = await self._prepare_agent_context(
            kind=ContextStageKind.ROOT,
            identity_key=resolved_run.value,
            root_run_id=resolved_run.value,
            continuation_id=None,
            turn_id=resolved_turn,
            value=value,
        )
        if staged.private_snapshot is None or staged.private_snapshot_hash is None:
            concurrent = self._runtime._uow.read_run(resolved_run.value)
            raw = self._runtime._uow.read_start_snapshot(resolved_run.value)
            if concurrent is not None and raw is not None:
                snapshot = StartSnapshot.from_json(raw)
                if (
                    snapshot.conversation == value
                    and snapshot.turn_id == resolved_turn
                    and concurrent.request_id == resolved_request.value
                    and snapshot.context_stage_id == staged.stage_id
                ):
                    return concurrent
            raise UnitOfWorkConflict("prepared context bytes are unavailable")
        return await self._runtime._start_run(
            RunStart(
                ExecutionSessionId(value.identity.session_id),
                resolved_run,
                resolved_request,
                resolved_turn,
                start_input,
                tool_catalog_generation,
                tool_catalog_fingerprint,
                provider_budget_fingerprint,
                conversation=value,
                context_preparation_mode=ContextPreparationMode.SDK_PREPARED,
                context_stage_id=staged.stage_id,
                context_stage_hash=staged.private_snapshot_hash,
                prepared_context=staged.private_snapshot,
            )
        )

    async def _prepare_agent_context(
        self,
        *,
        kind: ContextStageKind,
        identity_key: str,
        root_run_id: str,
        continuation_id: str | None,
        turn_id: str,
        value: ConversationTurnInput,
    ) -> ContextStageRecord:
        repository = self._runtime._ports.context_staging
        memory = self._runtime._ports.agent_memory
        provider = self._runtime._ports.context_provider
        if repository is None or memory is None or provider is None:
            raise RuntimeError("Agent Memory composition is incomplete")
        stage_id = (
            f"agent-memory-stage/v1/{context_query_id(kind, identity_key).rsplit('/', 1)[-1]}"
        )
        now = self._runtime._now()
        claim = claim_context_preparation(
            repository,
            stage_id=stage_id,
            kind=kind,
            identity_key=identity_key,
            value=value,
            mode=ContextPreparationMode.SDK_PREPARED,
            owner_id=self._runtime._ports.owner_id,
            now=now,
            lease_seconds=self._runtime._ports.lease_ttl_seconds,
        )
        if claim.record.state in {ContextStageState.STAGED, ContextStageState.CONSUMED}:
            return claim.record
        if not claim.owner:
            context_bounds = ConversationContextBounds()
            recall_bounds = MemoryRecallBounds()
            loop = asyncio.get_running_loop()
            wait_deadline = loop.time() + (
                self._runtime._ports.lease_ttl_seconds
                + context_bounds.deadline_seconds
                + recall_bounds.deadline_seconds
                + 1.0
            )
            while loop.time() < wait_deadline:
                winner = repository.get(stage_id)
                if winner is not None and winner.state in {
                    ContextStageState.STAGED,
                    ContextStageState.CONSUMED,
                }:
                    return winner
                if winner is not None and winner.state is ContextStageState.ABANDONED:
                    raise UnitOfWorkConflict("context preparation was abandoned")
                current_now = self._runtime._now()
                if (
                    winner is not None
                    and winner.state is ContextStageState.PREPARING
                    and winner.lease_expires_at is not None
                    and winner.lease_expires_at <= current_now
                ):
                    claim = claim_context_preparation(
                        repository,
                        stage_id=stage_id,
                        kind=kind,
                        identity_key=identity_key,
                        value=value,
                        mode=ContextPreparationMode.SDK_PREPARED,
                        owner_id=self._runtime._ports.owner_id,
                        now=current_now,
                        lease_seconds=self._runtime._ports.lease_ttl_seconds,
                    )
                    if claim.record.state in {
                        ContextStageState.STAGED,
                        ContextStageState.CONSUMED,
                    }:
                        return claim.record
                    if claim.owner:
                        break
                await asyncio.sleep(min(0.05, max(0.0, wait_deadline - loop.time())))
            else:
                raise TimeoutError("context preparation lease did not converge")
        now = self._runtime._now()
        identity_payload = value.identity.to_json()
        identity_hash = hashlib.sha256(canonical_json(identity_payload).encode("utf-8")).hexdigest()
        with repository.database.transaction() as connection:
            existing = connection.execute(
                "SELECT identity_hash FROM agent_identity_bindings WHERE session_id=?",
                (value.identity.session_id,),
            ).fetchone()
            if existing is not None and str(existing[0]) != identity_hash:
                raise UnitOfWorkConflict("conversation session identity cannot be rebound")
            connection.execute(
                "INSERT OR IGNORE INTO agent_identity_bindings("
                "session_id,deployment_id,household_id,actor_id,identity_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    value.identity.session_id,
                    value.identity.deployment_id,
                    value.identity.household_id,
                    value.identity.actor_id,
                    identity_hash,
                    now,
                ),
            )
        ref = claim.record.source_snapshot_ref
        if ref is None:
            raise UnitOfWorkConflict("context preparation claim has no source snapshot ref")
        context_request = ConversationContextRequest(
            preparation_id=stage_id,
            identity=value.identity,
            root_run_id=root_run_id,
            continuation_id=continuation_id,
            source_snapshot_ref=ref,
            current_message=value.message,
            bounds=ConversationContextBounds(),
        )
        product = await asyncio.wait_for(
            provider.prepare_once(context_request),
            timeout=context_request.bounds.deadline_seconds,
        )
        if (
            product.preparation_id != stage_id
            or product.source_snapshot_ref != ref
            or product.byte_count > context_request.bounds.max_bytes
            or product.item_count > context_request.bounds.max_items
        ):
            raise ValueError("Context provider result differs from its durable request")
        product_messages = self._product_messages(product.payload, value.message)
        recall_bounds = MemoryRecallBounds()
        recall_request = MemoryRecallRequest(
            query_id=context_query_id(kind, identity_key),
            turn_id=turn_id,
            identity=value.identity,
            scopes=value.recall_scopes,
            query_text=value.memory_text or canonical_json(value.message.to_dict()),
            bounds=recall_bounds,
            turn_started_at=now,
        )
        result: MemoryRecallResult | None = None
        release_candidate: MemoryRecallResult | None = None
        error_code: str | None = None
        degraded_write_fence: str | None = None
        try:
            result = await asyncio.wait_for(
                memory.recall_for_turn(recall_request),
                timeout=recall_bounds.deadline_seconds,
            )
            if isinstance(result, MemoryRecallResult):
                release_candidate = result
            if (
                not isinstance(result, MemoryRecallResult)
                or result.query_id != recall_request.query_id
                or result.query_hash != recall_request.query_hash
                or result.item_count > recall_bounds.max_items
                or result.byte_count > recall_bounds.max_bytes
            ):
                raise AgentMemoryError(AgentMemoryErrorCode.CORRUPT_RESULT)
        except TimeoutError:
            error_code = AgentMemoryErrorCode.TIMEOUT.value
            result = None
        except AgentMemoryError as error:
            error_code = error.code.value
            degraded_write_fence = error.write_fence
            result = None
        except Exception:
            error_code = AgentMemoryErrorCode.TRANSIENT.value
            result = None
        thawed_product = thaw_json(cast(FrozenJsonValue, product.payload))
        assert isinstance(thawed_product, dict)
        if result is None:
            memory_payload: Mapping[str, JsonValue] = {}
        else:
            thawed_memory = thaw_json(cast(FrozenJsonValue, result.payload))
            assert isinstance(thawed_memory, dict)
            memory_payload = thawed_memory
        memory_message = Message(
            MessageRole.USER,
            (
                ContentBlock(
                    "text",
                    {
                        "text": "Untrusted recalled memory data:\n"
                        + canonical_json(dict(memory_payload))
                    },
                ),
            ),
            metadata={"source": "memory", "trust": "untrusted_data"},
        )
        provider_messages = [message.to_dict() for message in product_messages[:-1]]
        provider_messages.extend((memory_message.to_dict(), value.message.to_dict()))
        private: dict[str, JsonValue] = {
            "schema_version": 1,
            "lineage": {
                "context_query_id": recall_request.query_id,
                "memory_result_id": None if result is None else result.result_id,
                "memory_result_hash": None if result is None else result.result_hash,
                "product_result_hash": product.result_hash,
                "source_snapshot_ref": ref,
            },
            "memory": {
                "trust": "untrusted_data",
                "role": "user",
                "result": dict(memory_payload),
            },
            "product_context": thawed_product,
            "current_message": value.message.to_dict(),
            "provider_messages": cast(JsonValue, provider_messages),
        }
        release: MemoryReleaseRequest | None = None
        release_id: str | None = None
        if release_candidate is not None:
            release = MemoryReleaseRequest(
                recall_request.query_id,
                recall_request.query_hash,
                release_candidate.result_id,
                release_candidate.result_hash,
                release_candidate.write_fence,
            )
            release_id = hashlib.sha256(
                canonical_json(
                    {
                        "protocol": "simple-harness-agent-memory/release/v1",
                        "query_id": release.query_id,
                        "result_hash": release.result_hash,
                    }
                ).encode("utf-8")
            ).hexdigest()
        staged = repository.complete(
            claim.record,
            private_snapshot=private,
            memory_result_id=None if result is None else result.result_id,
            memory_result_hash=None if result is None else result.result_hash,
            memory_query_hash=recall_request.query_hash,
            memory_write_fence=(degraded_write_fence if result is None else result.write_fence),
            outcome="degraded_empty" if result is None else "ready",
            error_code=error_code,
            product_result_hash=product.result_hash,
            source_snapshot_ref=ref,
            turn_started_at=now,
            release_id=release_id,
            release_query_id=None if release is None else release.query_id,
            release_query_hash=None if release is None else release.query_hash,
            release_result_id=None if release is None else release.result_id,
            release_result_hash=None if release is None else release.result_hash,
            release_write_fence=None if release is None else release.write_fence,
            release_retry_at=None if release is None else now,
            now=self._runtime._now(),
        )
        if release is not None:
            assert release_id is not None
            try:
                await asyncio.wait_for(memory.release_recall(release), timeout=1.0)
            except Exception:
                pass
            else:
                with repository.database.transaction() as connection:
                    connection.execute(
                        "UPDATE memory_recall_releases SET state='released',attempt_count=1,"
                        "released_at=? WHERE release_id=?",
                        (self._runtime._now(), release_id),
                    )
        return staged

    @staticmethod
    def _product_messages(
        payload: Mapping[str, JsonValue], current: Message
    ) -> tuple[Message, ...]:
        raw = payload.get("provider_messages", payload.get("messages"))
        if not isinstance(raw, (list, tuple)):
            raise TypeError("Context provider must return a messages array")
        messages: list[Message] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise TypeError("Context provider messages must be objects")
            thawed_item = thaw_json(cast(FrozenJsonValue, item))
            if not isinstance(thawed_item, dict):
                raise TypeError("Context provider message is invalid")
            parsed = _message_from_json(thawed_item)
            metadata = thaw_json(cast(FrozenJsonValue, parsed.metadata))
            if isinstance(metadata, Mapping) and metadata.get("source") == "memory":
                raise ValueError("Context provider cannot forge the Memory partition")
            messages.append(parsed)
        if not messages or canonical_json(messages[-1].to_dict()) != canonical_json(
            current.to_dict()
        ):
            raise ValueError("Context provider must preserve the current message as the final item")
        return tuple(messages)

    def query(self, run_id: RunId) -> RunRecord | None:
        return self._runtime._uow.read_run(_run_id(run_id))

    def signal(
        self,
        run_id: RunId,
        *,
        signal_id: str,
        payload: Mapping[str, JsonValue],
    ) -> ContinuationRecord:
        self._runtime._require_started()
        self._runtime._require_legacy_mode(_run_id(run_id))
        if payload.get("kind") == "conversation_user":
            raise ValueError("conversation_user continuations require signal_conversation")
        continuation = self._runtime._uow.enqueue_continuation(
            continuation_id=signal_id,
            run_id=_run_id(run_id),
            payload=payload,
            now=self._runtime._now(),
        )
        asyncio.create_task(self._runtime._wake_continuation(continuation.run_id))
        return continuation

    async def signal_conversation(
        self,
        run_id: RunId,
        *,
        continuation_id: str,
        value: ConversationContinuationInput,
        context_stage_id: str | None = None,
        context_stage_hash: str | None = None,
        prepared_context: Mapping[str, JsonValue] | None = None,
    ) -> ContinuationRecord:
        """Enqueue one ordinary user continuation with its durable context stage."""

        self._runtime._require_started()
        self._runtime._require_legacy_mode(_run_id(run_id))
        if not isinstance(value, ConversationContinuationInput):
            raise TypeError("value must use ConversationContinuationInput")
        existing_continuation = self._runtime._uow.read_continuation(continuation_id)
        if existing_continuation is not None:
            existing_payload = thaw_json(existing_continuation.payload)
            existing_conversation = (
                existing_payload.get("conversation")
                if isinstance(existing_payload, Mapping)
                else None
            )
            if (
                existing_continuation.run_id == _run_id(run_id)
                and isinstance(existing_conversation, Mapping)
                and canonical_json(dict(existing_conversation)) == canonical_json(value.to_json())
            ):
                return existing_continuation
            raise UnitOfWorkConflict("continuation identity reused differently")
        raw = self._runtime._uow.read_start_snapshot(_run_id(run_id))
        if raw is None:
            raise KeyError(run_id.value)
        run = self._runtime._uow.read_run(_run_id(run_id))
        if run is None:
            raise KeyError(run_id.value)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            raise UnitOfWorkConflict("terminal Run rejects new continuations")
        start = StartSnapshot.from_json(raw)
        if start.conversation is None:
            raise UnitOfWorkConflict("Run has no conversation identity")
        if self._runtime._ports.agent_memory is not None:
            continuation_value = ConversationTurnInput(
                start.conversation.identity,
                value.message,
                value.memory_text,
                start.conversation.recall_scopes,
                value.context_source_snapshot_ref,
            )
            stage = await self._prepare_agent_context(
                kind=ContextStageKind.CONTINUATION,
                identity_key=continuation_id,
                root_run_id=run_id.value,
                continuation_id=continuation_id,
                turn_id=continuation_id,
                value=continuation_value,
            )
            if stage.private_snapshot is None or stage.private_snapshot_hash is None:
                raise UnitOfWorkConflict("prepared continuation context is unavailable")
            context_stage_id = stage.stage_id
            context_stage_hash = stage.private_snapshot_hash
            prepared_context = stage.private_snapshot
        if context_stage_id is None or context_stage_hash is None or prepared_context is None:
            raise ValueError("conversation continuation requires durable prepared context")
        continuation = self._runtime._uow.enqueue_continuation(
            continuation_id=continuation_id,
            run_id=_run_id(run_id),
            payload={
                "kind": "conversation_user",
                "conversation": value.to_json(),
                "prepared_context": dict(prepared_context),
            },
            context_stage_id=context_stage_id,
            context_stage_hash=context_stage_hash,
            now=self._runtime._now(),
        )
        asyncio.create_task(self._runtime._wake_continuation(continuation.run_id))
        return continuation

    async def decide_authorization(
        self,
        run_id: RunId,
        *,
        decision_id: str,
        nonce: str,
        expected_version: int,
        decision: AuthorizationDecision,
    ):
        """Resolve one durable Tool decision with nonce/version replay fences."""

        self._runtime._require_started()
        record = self._runtime._uow.read_decision(decision_id)
        if record is None or record.run_id != _run_id(run_id):
            raise HarnessError("authorization_decision_not_found", "Decision was not found.")
        if record.state is not DecisionState.OPEN:
            if record.response is not None and isinstance(record.response, Mapping):
                if (
                    record.response.get("nonce") == nonce
                    and record.response.get("decision") == decision.value
                ):
                    return record
            raise HarnessError("authorization_decision_late", "Decision is already closed.")
        run = self._runtime._uow.read_run(record.run_id)
        if run is None or run.state is not RunState.WAITING:
            raise HarnessError(
                "authorization_decision_late",
                "The Run no longer accepts this decision.",
            )
        if record.version != expected_version:
            raise HarnessError(
                "authorization_decision_version_conflict", "Decision version is stale."
            )
        request = thaw_json(record.request)
        if not isinstance(request, dict) or request.get("nonce") != nonce:
            raise HarnessError("authorization_decision_nonce_mismatch", "Decision nonce differs.")
        expires_at = request.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise HarnessError("authorization_decision_invalid", "Decision expiry is invalid.")
        if expires_at is not None and self._runtime._now() >= float(expires_at):
            decision = AuthorizationDecision.DENY
            state = DecisionState.EXPIRED
        elif decision is AuthorizationDecision.ALLOW:
            state = DecisionState.ALLOWED
        elif decision is AuthorizationDecision.DENY:
            state = DecisionState.DENIED
        else:
            raise ValueError("a user decision must be ALLOW or DENY")
        binding_ref = await self._runtime._services.tools.bind_decision(record, decision)
        resolved = self._runtime._uow.commit_decision(
            decision_id=record.decision_id,
            run_id=record.run_id,
            kind=record.kind,
            state=state,
            request=request,
            response={
                "authorization_receipt_ref": binding_ref,
                "decision": decision.value,
                "nonce": nonce,
            },
            event_id=f"{record.decision_id}:{state.value}:{record.version + 1}",
            now=self._runtime._now(),
        )
        if state is DecisionState.ALLOWED:
            asyncio.create_task(self._runtime._wake_continuation(record.run_id))
        return resolved

    async def cancel(self, run_id: RunId) -> RunRecord:
        self._runtime._require_legacy_mode(_run_id(run_id))
        return await self._runtime._cancel_run(run_id)

    async def workflow_spawn_catalog(self):  # type: ignore[no-untyped-def]
        coordinator = self._runtime._services.workflow_spawn
        if coordinator is None:
            raise RuntimeError("workflow spawn is not configured")
        return await coordinator.catalog_snapshot()

    def bind_workflow_spawn(self, context, selection):  # type: ignore[no-untyped-def]
        from .orchestration import (
            WorkflowSpawnOrigin,
            WorkflowSpawnSelection,
            workflow_catalog_selection_from_json,
            workflow_spawn_child_request_id,
            workflow_spawn_child_run_id,
            workflow_spawn_operation_id,
        )
        from .start_snapshot import StartSnapshot
        from .workflow_spawn import (
            WorkflowSpawnToolContext,
            _create_workflow_spawn_invocation,
        )

        if not isinstance(context, WorkflowSpawnToolContext):
            raise TypeError("workflow spawn binder requires its typed Tool context")
        if not isinstance(selection, WorkflowSpawnSelection):
            raise TypeError("workflow spawn binder requires a typed selection")
        parent = self._runtime._uow.read_run(context.run_id)
        raw_snapshot = self._runtime._uow.read_start_snapshot(context.run_id)
        checkpoint = self._runtime._ports.react_checkpoint.read_react_checkpoint(context.run_id)
        if parent is None or raw_snapshot is None or checkpoint is None:
            raise UnitOfWorkConflict("workflow spawn parent authority disappeared")
        parent_start = StartSnapshot.from_json(raw_snapshot)
        checkpoint_payload = checkpoint.checkpoint
        if not isinstance(checkpoint_payload, Mapping):
            raise UnitOfWorkConflict("workflow spawn checkpoint is malformed")
        raw_catalog = checkpoint_payload.get("workflow_catalog_selection")
        if not isinstance(raw_catalog, Mapping):
            raise UnitOfWorkConflict("workflow spawn catalog pin is missing")
        catalog = workflow_catalog_selection_from_json(raw_catalog)
        if (
            catalog.canonical_hash != context.catalog_snapshot_hash
            or checkpoint.version != context.react_checkpoint_revision
            or parent.request_id != context.request_id
            or parent_start.turn_id != context.turn_id
            or selection.profile_key not in {item.profile_key for item in catalog.profiles}
        ):
            raise UnitOfWorkConflict("workflow spawn binding differs from durable pin")
        origin = WorkflowSpawnOrigin(
            context.run_id,
            context.request_id,
            context.turn_id,
            context.internal_tool_call_id,
        )
        operation_id = workflow_spawn_operation_id(origin)
        child_start = RunStart(
            ExecutionSessionId(parent.execution_session_id),
            RunId(workflow_spawn_child_run_id(operation_id)),
            RequestId(workflow_spawn_child_request_id(operation_id)),
            context.turn_id,
            selection.start_input,
            parent_start.tool_catalog_generation,
        )
        return _create_workflow_spawn_invocation(
            spawn_operation_id=operation_id,
            origin=origin,
            start=child_start,
            selection=selection,
            catalog_selection=catalog,
            issue_authority=context.issue_authority,
        )

    async def workflow_spawn(self, invocation):  # type: ignore[no-untyped-def]
        coordinator = self._runtime._services.workflow_spawn
        if coordinator is None:
            raise RuntimeError("workflow spawn is not configured")
        return await coordinator.execute(invocation)

    def prove_graph_unavailable(self, ticket, ready_activation):  # type: ignore[no-untyped-def]
        runner = self._runtime._workflow_runner
        if runner is None:
            raise RuntimeError("workflow spawn is not configured")
        return runner._prove_graph_unavailable(ticket, ready_activation)


class Runtime:
    def __init__(
        self,
        *,
        uow: RuntimeUnitOfWork,
        profiles: Mapping[str, RuntimeProfile],
        drivers: Mapping[str, RuntimeDriver],
        workflow_runner: object | None,
        ports: RuntimePorts,
        root_profile_key: str,
        close_hook: Callable[[], None] | None = None,
        start_hooks: Sequence[Callable[[], Awaitable[None]]] = (),
        async_close_hooks: Sequence[Callable[[], Awaitable[None]]] = (),
    ) -> None:
        self._close_hook = close_hook
        self._start_hooks = tuple(start_hooks)
        self._async_close_hooks = tuple(async_close_hooks)
        self._started_hooks = 0
        if WORKFLOW_DRIVER_KIND in drivers:
            raise ValueError("workflow is an SDK-reserved driver key")
        workflow_profiles = tuple(
            item for item in profiles.values() if item.driver_kind == WORKFLOW_DRIVER_KIND
        )
        workflow_driver: RuntimeDriver | None = None
        workflow_spawn: WorkflowSpawnRuntimeCoordinator | None = None
        if workflow_runner is not None:
            from simple_harness.workflow.runner import WorkflowRunner

            from .drivers.workflow import (
                WorkflowRuntimeDriver,
                build_workflow_runtime_driver,
            )

            if type(workflow_runner) is not WorkflowRunner:
                raise TypeError("official workflow runner identity is invalid")
            if (
                workflow_runner.execution_ports.unit_of_work.transaction_owner
                is not uow.transaction_owner
            ):
                raise ValueError("workflow Runtime and Runner transaction owners differ")
            workflow_driver = build_workflow_runtime_driver(workflow_runner)
            if type(workflow_driver) is not WorkflowRuntimeDriver:
                raise TypeError("official workflow driver factory is invalid")
            workflow_spawn = cast(
                WorkflowSpawnRuntimeCoordinator,
                _CanonicalWorkflowSpawnRuntimeCoordinator(
                    uow=uow,
                    runner=workflow_runner,
                    owner_id=ports.owner_id,
                    lease_ttl_seconds=ports.lease_ttl_seconds,
                    clock=ports.clock,
                ),
            )
        if workflow_profiles and workflow_driver is None:
            raise ValueError("workflow profile requires the SDK-owned workflow driver")
        self._uow = uow
        self._workflow_runner = workflow_runner
        self._profiles = dict(profiles)
        self._drivers = dict(drivers)
        self._cancellation_coordinators: dict[str, DriverCancellationCoordinator] = {}
        if workflow_driver is not None:
            self._drivers[WORKFLOW_DRIVER_KIND] = workflow_driver
            self._cancellation_coordinators[WORKFLOW_DRIVER_KIND] = workflow_driver  # type: ignore[assignment]
        self._ports = ports
        self._root_profile_key = root_profile_key
        self._terminal = TerminalCoordinator(uow)
        self._services = RuntimeServices(
            provider=ports.provider,
            tools=ports.tools,
            authorization=ports.authorization,
            context=ports.context,
            delivery=ports.delivery,
            tool_reconciliation=ports.tool_reconciliation,
            reconciliation=ports.reconciliation,
            provider_reconciliation=ports.provider_reconciliation,
            react_checkpoint=ports.react_checkpoint,
            tool_catalog=ports.tool_catalog,
            workflow_spawn=workflow_spawn,
        )
        self._live = LiveRunIndex()
        self._leases: dict[str, ExecutionLease] = {}
        self._fences: dict[str, RunFenceLease] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
        self._workflow_spawn_ready_activations: dict[str, WorkflowSpawnReadyActivation] = {}
        self._workflow_start_dispatches: dict[str, RuntimeStartDispatchClaim] = {}
        self._workflow_recovery_work: dict[str, WorkflowRecoveryWork] = {}
        self._child_signals = ChildSignalRuntime(uow, owner_id=ports.owner_id)
        self._child_signal_wait_handoffs: dict[str, object] = {}
        self._wake_drain_task: asyncio.Task[None] | None = None
        self._command_pump_task: asyncio.Task[None] | None = None
        self._command_wake = asyncio.Event()
        self._delivery_pump_task: asyncio.Task[None] | None = None
        self._delivery_wake = asyncio.Event()
        self._memory_pump_task: asyncio.Task[None] | None = None
        self._memory_wake = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._state = RuntimeLifecycleState.NEW
        self._started = False
        self._closing = False
        self._production_authorities: object | None = None
        self._observability: object | None = None
        self.client = RunClient(self)
        self.children = ChildCoordinator(self)

    def _reserve_legacy_start(self, start: RunStart) -> None:
        self._uow.reserve_legacy_run_mode(
            run_id=start.run_id.value,
            intent_hash=hashlib.sha256(
                canonical_json(
                    {
                        "execution_session_id": start.execution_session_id.value,
                        "run_id": start.run_id.value,
                        "request_id": start.request_id.value,
                        "turn_id": start.turn_id,
                        "input": thaw_json(cast(FrozenJsonValue, start.input)),
                        "tool_catalog_generation": start.tool_catalog_generation,
                        "conversation": (
                            None if start.conversation is None else start.conversation.to_json()
                        ),
                    }
                ).encode()
            ).hexdigest(),
            now=self._now(),
        )

    def _require_legacy_mode(self, run_id: str) -> None:
        require = getattr(self._uow, "require_legacy_or_unmanaged_run", None)
        if callable(require):
            require(run_id)

    def _emit_transition(
        self,
        event_name: str,
        *,
        run_id: str,
        outcome: Outcome,
        operation: str,
        request_id: str | None = None,
        execution_session_id: str | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        observability = self._ports.observability
        if observability is None:
            return
        observability.emit_transition(
            event_name,
            component="runtime",
            operation=operation,
            outcome=outcome,
            correlation=CorrelationContext.from_authority_ids(
                run_id=run_id,
                request_id=request_id,
                execution_session_id=execution_session_id,
                operation_id=operation,
            ),
            attributes={} if attributes is None else attributes,
        )

    @property
    def state(self) -> RuntimeLifecycleState:
        return self._state

    @property
    def production_authorities(self) -> object | None:
        """Explicit 0.1.5+ authorities retained by production composition."""

        return self._production_authorities

    @property
    def observability(self) -> object | None:
        """Optional Host-injected observability composition object."""

        return self._observability

    def diagnostics_snapshot(self) -> dict[str, object]:
        """Return the stable observability snapshot base without business payloads."""

        if self._observability is None:
            return {
                "schema_version": 1,
                "sdk_version": "unconfigured",
                "lifecycle": "disabled",
                "health": "healthy",
                "queue_depth": 0,
                "queue_capacity": 0,
                "counters": {},
                "active_runs": len(self._live.active_run_ids()),
                "authorities": self._authority_diagnostics_snapshot(),
            }
        snapshot = getattr(self._observability, "diagnostics_snapshot", None)
        if not callable(snapshot):
            return {
                "schema_version": 1,
                "sdk_version": "unknown",
                "lifecycle": "degraded",
                "health": "degraded",
                "queue_depth": 0,
                "queue_capacity": 0,
                "counters": {},
                "active_runs": len(self._live.active_run_ids()),
                "authorities": self._degraded_authority_snapshot(
                    "observability_snapshot_unavailable"
                ),
            }
        try:
            value = snapshot()
            result = dict(value)
            result["active_runs"] = len(self._live.active_run_ids())
            result["authorities"] = self._authority_diagnostics_snapshot()
            return result
        except BaseException:
            return {
                "schema_version": 1,
                "sdk_version": "unknown",
                "lifecycle": "degraded",
                "health": "degraded",
                "queue_depth": 0,
                "queue_capacity": 0,
                "counters": {},
                "active_runs": 0,
                "authorities": self._degraded_authority_snapshot("snapshot_failed"),
            }

    @staticmethod
    def _degraded_authority_snapshot(error_code: str) -> dict[str, object]:
        empty: dict[str, object] = {
            "health": "degraded",
            "counts": {},
            "oldest_age_ms": None,
        }
        return {
            "health": "degraded",
            "error_code": error_code,
            "commands": dict(empty),
            "context": dict(empty),
            "outbox": dict(empty),
            "recovery": dict(empty),
            "recent_error_codes": {},
        }

    def _authority_diagnostics_snapshot(self) -> dict[str, object]:
        staging = self._ports.context_staging
        owner = self._uow.transaction_owner
        try:
            database_connection = getattr(owner, "connection", None)
        except BaseException:
            return self._degraded_authority_snapshot("authority_query_failed")
        command_rows = (
            []
            if database_connection is None
            else database_connection.execute(
                "SELECT state,COUNT(*),MIN(created_at) FROM conversation_commands GROUP BY state"
            ).fetchall()
        )
        command_counts = {str(row[0]): int(row[1]) for row in command_rows[:16]}
        command_oldest = min(
            (float(row[2]) for row in command_rows[:16] if row[2] is not None),
            default=None,
        )
        commands = {
            "health": "healthy",
            "counts": command_counts,
            "oldest_age_ms": (
                None
                if command_oldest is None
                else max(0, int((self._now() - command_oldest) * 1000))
            ),
        }
        if staging is None:
            return {
                "health": "healthy",
                "commands": commands,
                "context": {"health": "disabled", "counts": {}, "oldest_age_ms": None},
                "outbox": {"health": "disabled", "counts": {}, "oldest_age_ms": None},
                "recovery": {"health": "disabled", "counts": {}, "oldest_age_ms": None},
                "recent_error_codes": {},
            }
        started = time.monotonic()
        try:
            connection = staging.database.connection
            now = self._now()

            def group(table: str, state: str, created: str) -> dict[str, object]:
                rows = connection.execute(
                    f"SELECT {state},COUNT(*),MIN({created}) FROM {table} GROUP BY {state}"
                ).fetchall()
                counts = {str(row[0]): int(row[1]) for row in rows[:16]}
                oldest = min(
                    (float(row[2]) for row in rows[:16] if row[2] is not None),
                    default=None,
                )
                return {
                    "health": "healthy",
                    "counts": counts,
                    "oldest_age_ms": (
                        None if oldest is None else max(0, int((now - oldest) * 1000))
                    ),
                }

            context = group("context_preparation_staging", "state", "created_at")
            outbox = group("memory_outbox", "state", "created_at")
            recovery_rows = connection.execute(
                "SELECT CASE WHEN resolved_at IS NULL THEN 'observed' "
                "WHEN wake_consumed=0 THEN 'resolved' ELSE 'consumed' END AS status,"
                "COUNT(*),MIN(created_at) FROM run_wait_blockers GROUP BY status"
            ).fetchall()
            recovery_counts = {str(row[0]): int(row[1]) for row in recovery_rows[:8]}
            recovery_oldest = min(
                (float(row[2]) for row in recovery_rows[:8] if row[2] is not None),
                default=None,
            )
            errors = connection.execute(
                "SELECT error_code,COUNT(*) FROM ("
                "SELECT error_code FROM memory_outbox WHERE error_code IS NOT NULL UNION ALL "
                "SELECT error_code FROM context_preparation_staging WHERE error_code IS NOT NULL"
                ") GROUP BY error_code ORDER BY COUNT(*) DESC,error_code LIMIT 20"
            ).fetchall()
            if time.monotonic() - started > 0.25:
                return self._degraded_authority_snapshot("snapshot_deadline")
            return {
                "health": "healthy",
                "commands": commands,
                "context": context,
                "outbox": outbox,
                "recovery": {
                    "health": "healthy",
                    "counts": recovery_counts,
                    "oldest_age_ms": (
                        None
                        if recovery_oldest is None
                        else max(0, int((now - recovery_oldest) * 1000))
                    ),
                },
                "recent_error_codes": {str(row[0]): int(row[1]) for row in errors},
            }
        except BaseException:
            return self._degraded_authority_snapshot("authority_query_failed")

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state is RuntimeLifecycleState.READY:
                return
            if self._state in {
                RuntimeLifecycleState.CLOSING,
                RuntimeLifecycleState.CLOSED,
                RuntimeLifecycleState.FAILED,
            }:
                raise RuntimeError(f"Runtime cannot start from {self._state.value}")
            if self._state is RuntimeLifecycleState.NEW:
                self._state = RuntimeLifecycleState.STARTING
                self._startup_task = asyncio.create_task(
                    self._start_once(), name="simple-harness-startup"
                )
            startup = self._startup_task
        assert startup is not None
        await asyncio.shield(startup)

    async def _start_once(self) -> None:
        try:
            await self._ports.reconciliation.reconcile()
            await self._drain_commands_bounded(100)
            await self.recover(_startup=True)
            await self._drain_resolved_waits_once()
            await self._drain_deliveries_bounded(100)
            await self._drain_memory_bounded(100)
            await self._drain_recall_releases(100)
            for hook in self._start_hooks:
                await hook()
                self._started_hooks += 1
            async with self._lifecycle_lock:
                if self._state is not RuntimeLifecycleState.STARTING:
                    raise asyncio.CancelledError
                self._wake_drain_task = asyncio.create_task(
                    self._wake_drain(), name="simple-harness-wake-drain"
                )
                self._command_pump_task = asyncio.create_task(
                    self._command_pump(), name="simple-harness-command-pump"
                )
                self._delivery_pump_task = asyncio.create_task(
                    self._delivery_pump(), name="simple-harness-delivery-pump"
                )
                if self._ports.memory_dispatcher is not None:
                    self._memory_pump_task = asyncio.create_task(
                        self._memory_pump(), name="simple-harness-memory-pump"
                    )
                self._started = True
                self._closing = False
                self._state = RuntimeLifecycleState.READY
        except asyncio.CancelledError:
            raise
        except BaseException:
            async with self._lifecycle_lock:
                if self._state is RuntimeLifecycleState.STARTING:
                    self._state = RuntimeLifecycleState.FAILED
            await self._stop_background_tasks()
            raise

    async def recover(self, *, _startup: bool = False) -> None:
        if not _startup:
            self._require_started()
        root_runs = self._uow.list_recoverable_root_runs()
        child_runs = self._uow.list_recoverable_child_runs()
        logger.info(
            "reconcile.recovered",
            extra={"roots": len(root_runs), "children": len(child_runs)},
        )
        for run in (*root_runs, *child_runs):
            if run.run_id in self._live.active_run_ids():
                continue
            self._emit_transition(
                "recovery.observed_state",
                run_id=run.run_id,
                outcome=Outcome.STARTED,
                operation="recovery",
                attributes={
                    "entity_kind": "run",
                    "entity_id": run.run_id,
                    "to_state": run.state.value,
                    "replayed": True,
                    "history_complete": False,
                    "state_version": run.version,
                },
            )
            try:
                prior_ready = await self._uow.run_atomic(
                    lambda tx, run_id=run.run_id: self._uow.read_spawn_ready_activation(  # type: ignore[misc]
                        tx, run_id
                    ),
                    fault_label="runtime:spawn_ready:read",
                )
                if prior_ready is None:
                    if (
                        run.parent_run_id is not None
                        and run.driver_kind == WORKFLOW_DRIVER_KIND
                        and self._uow.is_workflow_spawn_child(run.run_id)
                    ):
                        admission = await self._uow.run_atomic(
                            lambda tx, run_id=run.run_id: self._uow.resume_spawn_child_start(  # type: ignore[misc]
                                tx,
                                run_id,
                                RuntimeActivationClaim(
                                    self._ports.owner_id,
                                    lease_ttl_seconds=(self._ports.lease_ttl_seconds),
                                ),
                                now=self._now(),
                            ),
                            fault_label="runtime:spawn_child:resume",
                        )
                        if not self._register_recovered_spawn_child(admission):
                            continue
                        activated = self._uow.read_run(run.run_id)
                        if activated is None:
                            raise UnitOfWorkConflict("workflow spawn child Run disappeared")
                    else:
                        activated = await self._activate(run.run_id)
                else:
                    ready_activation = await self._uow.run_atomic(
                        lambda tx, prior=prior_ready: self._uow.reclaim_spawn_ready_activation(  # type: ignore[misc]
                            tx,
                            prior,
                            self._ports.owner_id,
                            now=self._now(),
                            ttl_seconds=self._ports.lease_ttl_seconds,
                        ),
                        fault_label="runtime:spawn_ready:reclaim",
                    )
                    self._register_spawn_ready_activation(ready_activation)
                    activated = self._uow.read_run(run.run_id)
                    if activated is None:
                        raise UnitOfWorkConflict("workflow spawn parent Run disappeared")
            except UnitOfWorkConflict:
                continue
            if activated.state is RunState.CANCEL_REQUESTED:
                await self._terminalize_cancelled(activated, reason="startup_recovery")
            else:
                self._schedule(activated.run_id)
            self._emit_transition(
                "recovery.resolved",
                run_id=run.run_id,
                outcome=Outcome.SUCCEEDED,
                operation="recovery",
                attributes={
                    "entity_kind": "run",
                    "entity_id": run.run_id,
                    "recovery_result": "scheduled",
                    "history_complete": False,
                    "state_version": activated.version,
                },
            )

    async def reconcile(self) -> None:
        self._require_started()
        await self._ports.reconciliation.reconcile()
        await self._drain_resolved_waits_once()
        await self.recover()

    async def dispatch_deliveries_once(self) -> bool:
        self._require_started()
        return await self._ports.delivery.run_once()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._state is RuntimeLifecycleState.CLOSED:
                return
            if self._state is RuntimeLifecycleState.NEW:
                self._state = RuntimeLifecycleState.CLOSED
                for hook in reversed(self._async_close_hooks):
                    await hook()
                if self._ports.memory_dispatcher is not None:
                    await self._ports.memory_dispatcher.close()
                if self._close_hook is not None:
                    self._close_hook()
                return
            self._state = RuntimeLifecycleState.CLOSING
            self._closing = True
            startup = self._startup_task
        if startup is not None and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        await self._drain_deliveries_bounded(100)
        await self._drain_memory_bounded(100)
        await self._stop_background_tasks()
        for token in self._cancels.values():
            token.cancel()
        await self._live.close(timeout_seconds=self._ports.close_timeout_seconds)
        heartbeat_tasks = tuple(task for task in self._heartbeats.values() if not task.done())
        for task in heartbeat_tasks:
            task.cancel()
        if heartbeat_tasks:
            await asyncio.gather(*heartbeat_tasks, return_exceptions=True)
        self._heartbeats.clear()
        now = self._now()
        for run_id, fence in tuple(self._fences.items()):
            try:
                await self._uow.release(fence)
            except UnitOfWorkConflict:
                pass
            self._fences.pop(run_id, None)
        for run_id, lease in tuple(self._leases.items()):
            try:
                self._uow.release_runtime_lease(lease, now=now)
            except UnitOfWorkConflict:
                pass
            self._leases.pop(run_id, None)
        self._cancels.clear()
        self._workflow_spawn_ready_activations.clear()
        self._workflow_start_dispatches.clear()
        self._workflow_recovery_work.clear()
        self._started = False
        for hook in reversed(self._async_close_hooks):
            await hook()
        self._started_hooks = 0
        if self._ports.memory_dispatcher is not None:
            await self._ports.memory_dispatcher.close()
        if self._close_hook is not None:
            self._close_hook()
        async with self._lifecycle_lock:
            self._state = RuntimeLifecycleState.CLOSED

    async def _stop_background_tasks(self) -> None:
        tasks = tuple(
            task
            for task in (
                self._wake_drain_task,
                self._command_pump_task,
                self._delivery_pump_task,
                self._memory_pump_task,
            )
            if task is not None and not task.done()
        )
        self._wake_drain_task = None
        self._command_pump_task = None
        self._delivery_pump_task = None
        self._memory_pump_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wake_drain(self) -> None:
        interval = min(0.05, max(0.001, self._ports.lease_ttl_seconds / 3.0))
        try:
            while self._state is RuntimeLifecycleState.READY:
                await self._drain_resolved_waits_once()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def _drain_commands_bounded(self, limit: int) -> bool:
        for _ in range(limit):
            claim = self._uow.claim_next_command(
                owner_id=self._ports.owner_id,
                now=self._now(),
                lease_seconds=self._ports.lease_ttl_seconds,
            )
            if claim is None:
                return True
            heartbeat = asyncio.create_task(
                self._command_heartbeat(claim),
                name=f"simple-harness-command-heartbeat:{claim.receipt.command_id}",
            )
            try:
                await self._process_command(claim)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    current = self._uow.get_command_receipt(claim.receipt.command_id)
                    if current.state.terminal:
                        continue
                    definite = isinstance(
                        error, (TypeError, ValueError, HarnessError, CommandError)
                    )
                    if definite or claim.attempt_count >= 8:
                        self._uow.reject_command(
                            claim,
                            error_code=(
                                CommandErrorCode.PERMANENT_FAILURE
                                if definite
                                else CommandErrorCode.RETRY_EXHAUSTED
                            ),
                            now=self._now(),
                        )
                    else:
                        self._uow.retry_command(
                            claim,
                            error_code=CommandErrorCode.TRANSIENT_FAILURE.value,
                            retry_at=self._now() + min(60.0, 2.0**claim.attempt_count),
                            now=self._now(),
                        )
                except CommandError as settlement_error:
                    if settlement_error.code is not CommandErrorCode.INTENT_CONFLICT:
                        logger.warning(
                            "command.settlement_failed",
                            extra={
                                "command_id": claim.receipt.command_id,
                                "run_id": claim.receipt.run_id.value,
                                "error_code": settlement_error.code.value,
                            },
                        )
                except Exception:
                    logger.warning(
                        "command.settlement_failed",
                        extra={
                            "command_id": claim.receipt.command_id,
                            "run_id": claim.receipt.run_id.value,
                            "error_code": CommandErrorCode.TRANSIENT_FAILURE.value,
                        },
                    )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        return False

    async def _command_heartbeat(self, claim: CommandClaim) -> None:
        interval = max(0.01, self._ports.lease_ttl_seconds / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                self._uow.heartbeat_command(
                    claim,
                    now=self._now(),
                    lease_seconds=self._ports.lease_ttl_seconds,
                )
        except (asyncio.CancelledError, Exception):
            return

    async def _command_pump(self) -> None:
        backoff = 0.01
        try:
            while self._state is RuntimeLifecycleState.READY:
                try:
                    await asyncio.wait_for(self._command_wake.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                self._command_wake.clear()
                empty = await self._drain_commands_bounded(100)
                if empty:
                    backoff = min(1.0, backoff * 2)
                else:
                    backoff = 0.01
                    self._command_wake.set()
        except asyncio.CancelledError:
            return

    async def _process_command(self, claim: CommandClaim) -> None:
        raw = json.loads(claim.raw_payload_json)
        if not isinstance(raw, Mapping):
            raise TypeError("durable command payload is corrupt")
        intent = command_intent_from_json(raw)
        state = claim.receipt.state
        if state is CommandState.ACCEPTED:
            self._uow.transition_command(
                claim,
                expected=CommandState.ACCEPTED,
                target=CommandState.CONTEXT_CALL_INTENT,
                now=self._now(),
            )
            state = CommandState.CONTEXT_CALL_INTENT
        if isinstance(intent, StartCommandIntent):
            await self._process_start_command(claim, intent, state)
        elif isinstance(intent, ContinueCommandIntent):
            await self._process_continue_command(claim, intent, state)
        else:
            if state is CommandState.CONTEXT_CALL_INTENT:
                self._uow.transition_command(
                    claim,
                    expected=state,
                    target=CommandState.CONTEXT_READY,
                    now=self._now(),
                )
            self._uow.apply_cancel_command(
                claim,
                event_id=f"{intent.command_id}:cancel",
                now=self._now(),
            )
            current = self._uow.read_run(intent.run_id.value)
            if current is not None:
                token = self._cancels.get(intent.run_id.value)
                if token is not None:
                    token.cancel()
                if intent.run_id.value in self._live.active_run_ids():
                    await self._live.cancel(intent.run_id.value)
                latest = self._uow.read_run(intent.run_id.value)
                if latest is not None and latest.state is RunState.CANCEL_REQUESTED:
                    await self._terminalize_cancelled(latest)

    async def _process_start_command(
        self, claim: CommandClaim, intent: StartCommandIntent, state: CommandState
    ) -> None:
        if intent.profile_key != self._root_profile_key:
            raise UnitOfWorkConflict("command profile differs from Runtime root")
        profile = self._profiles[self._root_profile_key]
        driver = self._drivers[profile.driver_kind]
        stage: ContextStageRecord | None = None
        if self._ports.agent_memory is not None:
            stage = await self.client._prepare_agent_context(
                kind=ContextStageKind.ROOT,
                identity_key=intent.run_id.value,
                root_run_id=intent.run_id.value,
                continuation_id=None,
                turn_id=intent.turn_id,
                value=intent.conversation,
            )
            if stage.private_snapshot is None or stage.private_snapshot_hash is None:
                raise UnitOfWorkConflict("prepared command context is unavailable")
        if state is CommandState.CONTEXT_CALL_INTENT:
            self._uow.transition_command(
                claim,
                expected=state,
                target=CommandState.CONTEXT_READY,
                now=self._now(),
            )
        start = RunStart(
            ExecutionSessionId(intent.conversation.identity.session_id),
            intent.run_id,
            intent.request_id,
            intent.turn_id,
            (
                {"messages": [intent.conversation.message.to_dict()]}
                if not intent.input
                else cast(
                    Mapping[str, JsonValue],
                    thaw_json(cast(FrozenJsonValue, intent.input)),
                )
            ),
            intent.tool_catalog_generation,
            intent.tool_catalog_fingerprint,
            conversation=intent.conversation,
            context_preparation_mode=(
                None if stage is None else ContextPreparationMode.SDK_PREPARED
            ),
            context_stage_id=None if stage is None else stage.stage_id,
            context_stage_hash=None if stage is None else stage.private_snapshot_hash,
            prepared_context=None if stage is None else stage.private_snapshot,
        )
        verdict = await self._ports.admission.evaluate(start)
        if not verdict.allowed:
            raise HarnessError("admission_denied", "The Run was denied by admission.")
        snapshot = bind_start_snapshot(
            start,
            profile_key=self._root_profile_key,
            driver_kind=profile.driver_kind,
            policy_fingerprint=getattr(driver, "policy_fingerprint", None),
        )
        created = self._uow.apply_start_command(
            claim,
            execution_session_id=start.execution_session_id.value,
            request_id=start.request_id.value,
            profile_key=self._root_profile_key,
            driver_kind=profile.driver_kind,
            snapshot=snapshot.to_json(),
            event_id=f"{start.run_id.value}:created",
            now=self._now(),
            user_id=intent.conversation.user_id,
            context_stage_id=start.context_stage_id,
            context_stage_hash=start.context_stage_hash,
        )
        if created.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            activated = await self._activate(created.run_id)
            self._schedule(activated.run_id)

    async def _process_continue_command(
        self, claim: CommandClaim, intent: ContinueCommandIntent, state: CommandState
    ) -> None:
        start_raw = self._uow.read_start_snapshot(intent.run_id.value)
        if start_raw is None:
            raise UnitOfWorkNotFound(intent.run_id.value)
        start = StartSnapshot.from_json(start_raw)
        stage: ContextStageRecord | None = None
        if self._ports.agent_memory is not None:
            if start.conversation is None:
                raise UnitOfWorkConflict("command Run has no conversation identity")
            stage = await self.client._prepare_agent_context(
                kind=ContextStageKind.CONTINUATION,
                identity_key=intent.continuation_id,
                root_run_id=intent.run_id.value,
                continuation_id=intent.continuation_id,
                turn_id=intent.turn_id,
                value=ConversationTurnInput(
                    start.conversation.identity,
                    intent.conversation.message,
                    intent.conversation.memory_text,
                    start.conversation.recall_scopes,
                    intent.conversation.context_source_snapshot_ref,
                ),
            )
            if stage.private_snapshot is None or stage.private_snapshot_hash is None:
                raise UnitOfWorkConflict("prepared continuation command context is unavailable")
        if state is CommandState.CONTEXT_CALL_INTENT:
            self._uow.transition_command(
                claim,
                expected=state,
                target=CommandState.CONTEXT_READY,
                now=self._now(),
            )
        self._uow.apply_continue_command(
            claim,
            continuation_id=intent.continuation_id,
            payload={
                "kind": "conversation_user",
                "conversation": intent.conversation.to_json(),
                **(
                    {}
                    if stage is None or stage.private_snapshot is None
                    else {"prepared_context": dict(stage.private_snapshot)}
                ),
            },
            now=self._now(),
            context_stage_id=None if stage is None else stage.stage_id,
            context_stage_hash=None if stage is None else stage.private_snapshot_hash,
        )
        await self._wake_continuation(intent.run_id.value)

    async def _drain_deliveries_bounded(self, limit: int) -> bool:
        run_once = getattr(self._ports.delivery, "run_once", None)
        if run_once is None:
            return True
        for _ in range(limit):
            if not await run_once():
                return True
        return False

    async def _delivery_pump(self) -> None:
        backoff = 0.01
        try:
            while self._state is RuntimeLifecycleState.READY:
                try:
                    await asyncio.wait_for(self._delivery_wake.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                self._delivery_wake.clear()
                empty = await self._drain_deliveries_bounded(100)
                await self._drain_recall_releases(100)
                if empty:
                    backoff = min(1.0, backoff * 2)
                else:
                    backoff = 0.01
                    self._delivery_wake.set()
        except asyncio.CancelledError:
            return

    async def _drain_recall_releases(self, limit: int) -> None:
        memory = self._ports.agent_memory
        staging = self._ports.context_staging
        if memory is None or staging is None:
            return
        now = self._now()
        rows = staging.database.connection.execute(
            "SELECT release_id,query_id,query_hash,result_id,result_hash,write_fence,"
            "attempt_count "
            "FROM memory_recall_releases WHERE state='pending' AND retry_at<=? "
            "ORDER BY retry_at,release_id LIMIT ?",
            (now, limit),
        ).fetchall()
        for row in rows:
            request = MemoryReleaseRequest(
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                None if row[5] is None else str(row[5]),
            )
            try:
                await asyncio.wait_for(memory.release_recall(request), timeout=1.0)
            except Exception:
                attempts = int(row[6]) + 1
                with staging.database.transaction() as connection:
                    connection.execute(
                        "UPDATE memory_recall_releases SET attempt_count=?,retry_at=? "
                        "WHERE release_id=? AND state='pending'",
                        (attempts, now + min(60.0, 2.0 ** min(attempts, 6)), str(row[0])),
                    )
            else:
                with staging.database.transaction() as connection:
                    connection.execute(
                        "UPDATE memory_recall_releases SET state='released',attempt_count=?,"
                        "released_at=? WHERE release_id=? AND state='pending'",
                        (int(row[6]) + 1, self._now(), str(row[0])),
                    )

    async def _drain_memory_bounded(self, limit: int) -> bool:
        dispatcher = self._ports.memory_dispatcher
        if dispatcher is None:
            return True
        return await dispatcher.drain(limit=limit)

    async def _memory_pump(self) -> None:
        dispatcher = self._ports.memory_dispatcher
        if dispatcher is None:
            return
        backoff = 0.01
        try:
            while self._state is RuntimeLifecycleState.READY:
                try:
                    await asyncio.wait_for(self._memory_wake.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                self._memory_wake.clear()
                progressed = await dispatcher.run_once()
                backoff = 0.01 if progressed else min(1.0, backoff * 2)
        except asyncio.CancelledError:
            return

    async def _drain_resolved_waits_once(self) -> None:
        await self._drain_child_signals_once()
        for blocker in self._uow.list_resolved_wait_blockers(
            owner_id=self._ports.owner_id,
            namespace=RUNTIME_LEASE_NAMESPACE,
            now=self._now(),
        ):
            try:
                run, lease, _receipt = self._uow.consume_resolved_wait_and_claim_activation(
                    blocker_id=blocker.blocker_id,
                    owner_id=self._ports.owner_id,
                    namespace=RUNTIME_LEASE_NAMESPACE,
                    now=self._now(),
                    lease_ttl_seconds=self._ports.lease_ttl_seconds,
                )
                fence = await self._uow.acquire(RunId(run.run_id), lease, now=self._now())
            except UnitOfWorkConflict:
                continue
            self._leases[run.run_id] = lease
            self._fences[run.run_id] = fence
            self._cancels.setdefault(run.run_id, CancelToken())
            heartbeat = self._heartbeats.get(run.run_id)
            if heartbeat is None or heartbeat.done():
                self._heartbeats[run.run_id] = asyncio.create_task(
                    self._heartbeat(run.run_id),
                    name=f"simple-harness-heartbeat:{run.run_id}",
                )
            if run.run_id not in self._live.active_run_ids():
                self._schedule(run.run_id)
        await self._drain_spawn_ready_once()

    async def _drain_child_signals_once(self) -> None:
        active_child_parents = set(self._child_signal_wait_handoffs)
        for active_run_id in self._live.active_run_ids():
            active_run = self._uow.read_run(active_run_id)
            if active_run is not None and active_run.parent_run_id is not None:
                active_child_parents.add(active_run.parent_run_id)
        for result in self._child_signals.reconcile_all(
            now=self._now(), blocked_parent_run_ids=active_child_parents
        ):
            run_id = result.signal.parent_run_id
            if run_id not in self._leases:
                try:
                    await self._activate(run_id)
                except UnitOfWorkConflict:
                    continue
            if run_id not in self._live.active_run_ids():
                self._schedule(run_id)

    async def _drain_spawn_ready_once(self) -> None:
        cursor: str | None = None
        while True:
            ready_values, cursor = self._uow.list_ready_spawn_continuations(cursor, limit=100)
            for ready in ready_values:
                blocker = self._uow.read_spawn_ready_blocker(ready)
                if blocker is None:
                    continue
                try:
                    activation = await self._uow.run_atomic(
                        lambda tx, ready=ready, blocker=blocker: (  # type: ignore[misc]
                            self._uow.consume_spawn_ready_and_claim_activation(
                                tx,
                                ready,
                                blocker,
                                self._ports.owner_id,
                                now=self._now(),
                                ttl_seconds=self._ports.lease_ttl_seconds,
                            )
                        ),
                        fault_label="runtime:spawn_ready:consume",
                    )
                except UnitOfWorkConflict:
                    continue
                self._register_spawn_ready_activation(activation)
                run_id = activation.execution_lease.run_id
                if run_id not in self._live.active_run_ids():
                    self._schedule(run_id)
            if cursor is None:
                return

    def _register_spawn_ready_activation(self, activation: WorkflowSpawnReadyActivation) -> None:
        run_id = activation.execution_lease.run_id
        self._workflow_spawn_ready_activations[run_id] = activation
        self._leases[run_id] = activation.execution_lease
        self._fences[run_id] = activation.run_fence
        self._cancels.setdefault(run_id, CancelToken())
        heartbeat = self._heartbeats.get(run_id)
        if heartbeat is None or heartbeat.done():
            self._heartbeats[run_id] = asyncio.create_task(
                self._heartbeat(run_id),
                name=f"simple-harness-heartbeat:{run_id}",
            )

    async def wait_idle(self, run_id: RunId) -> None:
        value = _run_id(run_id)
        run = self._uow.read_run(value)
        parent_run_id = None if run is None else run.parent_run_id
        handoff = object()
        if parent_run_id is not None:
            self._child_signal_wait_handoffs[parent_run_id] = handoff
        try:
            await self._live.wait(value)
        finally:
            if parent_run_id is not None:
                asyncio.create_task(self._release_child_signal_wait_handoff(parent_run_id, handoff))

    async def _release_child_signal_wait_handoff(self, parent_run_id: str, handoff: object) -> None:
        await asyncio.sleep(0)
        if self._child_signal_wait_handoffs.get(parent_run_id) is handoff:
            self._child_signal_wait_handoffs.pop(parent_run_id, None)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _start_run(self, start: RunStart) -> RunRecord:
        self._require_started()
        if self._ports.conversation_memory_enabled and start.conversation is None:
            raise HarnessError(
                "conversation_envelope_required",
                "Enabled conversation Memory requires an explicit envelope.",
            )
        if self._ports.conversation_memory_enabled and (
            start.context_stage_id is None or start.prepared_context is None
        ):
            raise HarnessError(
                "context_stage_required",
                "Enabled conversation Memory requires durable prepared context.",
            )
        if self._ports.conversation_memory_enabled and (
            start.context_preparation_mode is not self._ports.context_preparation_mode
        ):
            raise HarnessError(
                "context_preparation_mode_mismatch",
                "Run context preparation mode differs from Runtime composition.",
            )
        verdict = await self._ports.admission.evaluate(start)
        if not verdict.allowed:
            logger.warning(
                "run.admission_denied",
                extra={
                    "run_id": start.run_id.value,
                    "session_id": start.execution_session_id.value,
                },
            )
            raise HarnessError("admission_denied", "The Run was denied by admission.")
        profile = self._profiles[self._root_profile_key]
        driver = self._drivers[profile.driver_kind]
        snapshot = bind_start_snapshot(
            start,
            profile_key=self._root_profile_key,
            driver_kind=profile.driver_kind,
            policy_fingerprint=getattr(driver, "policy_fingerprint", None),
        )
        created = self._uow.create_with_start_snapshot(
            execution_session_id=start.execution_session_id.value,
            run_id=start.run_id.value,
            request_id=start.request_id.value,
            profile_key=self._root_profile_key,
            driver_kind=profile.driver_kind,
            snapshot=snapshot.to_json(),
            event_id=f"{start.run_id.value}:created",
            now=self._now(),
            user_id=(
                "harness-system" if start.conversation is None else start.conversation.user_id
            ),
            context_stage_id=start.context_stage_id,
            context_stage_hash=start.context_stage_hash,
        )
        self._emit_transition(
            "run.started",
            run_id=created.run_id,
            request_id=start.request_id.value,
            execution_session_id=start.execution_session_id.value,
            outcome=Outcome.STARTED,
            operation="run_lifecycle",
            attributes={
                "entity_kind": "run",
                "entity_id": created.run_id,
                "run_id": created.run_id,
                "to_state": created.state.value,
                "state_version": created.version,
                "replayed": created.state is not RunState.CREATED,
            },
        )
        if start.context_stage_id is not None:
            self._emit_transition(
                "context.consumed",
                run_id=created.run_id,
                request_id=start.request_id.value,
                execution_session_id=start.execution_session_id.value,
                outcome=Outcome.SUCCEEDED,
                operation="context_staging",
                attributes={
                    "entity_kind": "context_stage",
                    "entity_id": start.context_stage_id,
                    "from_state": "staged",
                    "to_state": "consumed",
                    "run_id": created.run_id,
                },
            )
        logger.info(
            "run.start",
            extra={
                "run_id": start.run_id.value,
                "session_id": start.execution_session_id.value,
                "profile": self._root_profile_key,
            },
        )
        if created.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return created
        activated = await self._activate(created.run_id)
        self._schedule(activated.run_id)
        return activated

    async def _wake_continuation(self, run_id: str) -> None:
        if run_id not in self._leases:
            try:
                await self._activate(run_id)
            except UnitOfWorkConflict:
                return
        self._schedule(run_id)

    async def _activate(self, run_id: str) -> RunRecord:
        run, lease = self._uow.claim_runtime_activation(
            run_id=run_id,
            owner_id=self._ports.owner_id,
            namespace=RUNTIME_LEASE_NAMESPACE,
            now=self._now(),
            lease_ttl_seconds=self._ports.lease_ttl_seconds,
        )
        self._leases[run_id] = lease
        fence = await self._uow.acquire(RunId(run_id), lease, now=self._now())
        self._fences[run_id] = fence
        self._cancels.setdefault(run_id, CancelToken())
        heartbeat = self._heartbeats.get(run_id)
        if heartbeat is None or heartbeat.done():
            self._heartbeats[run_id] = asyncio.create_task(
                self._heartbeat(run_id), name=f"simple-harness-heartbeat:{run_id}"
            )
        return run

    async def _heartbeat(self, run_id: str) -> None:
        interval = max(0.001, self._ports.lease_ttl_seconds / 3.0)
        try:
            # Closing first cancels and joins every driver while ownership is still
            # renewed.  Only Runtime.close may stop the heartbeat after that join.
            while run_id in self._leases:
                await self._ports.sleep(interval)
                lease = self._leases.get(run_id)
                if lease is None:
                    return
                try:
                    renewed = self._uow.renew_runtime_lease(
                        lease,
                        now=self._now(),
                        lease_ttl_seconds=self._ports.lease_ttl_seconds,
                    )
                except UnitOfWorkConflict:
                    token = self._cancels.get(run_id)
                    if token is not None:
                        token.cancel()
                    await self._live.cancel(run_id)
                    self._leases.pop(run_id, None)
                    self._workflow_spawn_ready_activations.pop(run_id, None)
                    return
                self._leases[run_id] = renewed
                ready = self._workflow_spawn_ready_activations.get(run_id)
                if ready is not None:
                    refreshed = await self._uow.run_atomic(
                        lambda tx, receipt_id=ready.activation_receipt_id: (  # type: ignore[misc]
                            self._uow.read_spawn_ready_activation(tx, run_id, receipt_id)
                        ),
                        fault_label="runtime:spawn_ready:heartbeat_read",
                    )
                    if refreshed is None:
                        raise UnitOfWorkConflict(
                            "workflow spawn activation disappeared after heartbeat"
                        )
                    self._workflow_spawn_ready_activations[run_id] = refreshed
        except asyncio.CancelledError:
            return

    def _schedule(self, run_id: str) -> None:
        self._live.schedule(run_id, self._drive(run_id))

    async def _drive(self, run_id: str) -> None:
        continuation_claim: ContinuationRecord | None = None
        try:
            run = self._uow.read_run(run_id)
            raw_snapshot = self._uow.read_start_snapshot(run_id)
            if run is None or raw_snapshot is None:
                raise RuntimeError("durable Run start state is incomplete")
            snapshot = StartSnapshot.from_json(raw_snapshot)
            driver = self._drivers[snapshot.driver_kind]
            if snapshot.policy_fingerprint is not None and (
                getattr(driver, "policy_fingerprint", None) != snapshot.policy_fingerprint
            ):
                error = HarnessError(
                    "runtime_policy_mismatch",
                    "The frozen Runtime policy is unavailable.",
                )
                self._terminalize(
                    run,
                    state=RunState.FAILED,
                    payload=error.to_dict(),
                    deliveries=(),
                )
                return
            catalog_matches = False
            if snapshot.tool_catalog_fingerprint is not None:
                resolver = getattr(self._ports.tool_catalog, "resolve", None)
                resolved_catalog = (
                    None
                    if resolver is None
                    else resolver(
                        snapshot.tool_catalog_generation,
                        snapshot.tool_catalog_fingerprint,
                    )
                )
                catalog_matches = (
                    resolved_catalog is not None
                    and resolved_catalog.generation == snapshot.tool_catalog_generation
                    and resolved_catalog.content_fingerprint == snapshot.tool_catalog_fingerprint
                )
            else:
                catalog_matches = (
                    snapshot.tool_catalog_generation
                    == self._ports.tool_catalog.current_generation()
                )
            if not catalog_matches:
                error = ToolCatalogStale()
                self._terminalize(
                    run,
                    state=RunState.FAILED,
                    payload=error.to_dict(),
                    deliveries=(),
                )
                return
            if run.state is RunState.CANCEL_REQUESTED:
                await self._terminalize_cancelled(run, snapshot=snapshot, reason="pre_drive_cancel")
                return
            continuation_claim = self._uow.claim_continuation(
                run_id=run_id,
                execution_lease=self._leases[run_id],
                now=self._now(),
            )
            if run.state is RunState.WAITING and continuation_claim is None:
                return
            result = await driver.start(
                DriverInvocation(
                    run=run,
                    start=snapshot,
                    execution_lease=self._leases[run_id],
                    run_fence=self._fences[run_id],
                    services=self._services,
                    continuations=(() if continuation_claim is None else (continuation_claim,)),
                    workflow_spawn_ready_activation=(
                        self._workflow_spawn_ready_activations.get(run_id)
                    ),
                    workflow_start_dispatch=self._workflow_start_dispatches.get(run_id),
                    workflow_recovery_work=self._workflow_recovery_work.get(run_id),
                ),
                context=self._ports.context,
                cancel=self._cancels[run_id],
            )
            if continuation_claim is not None:
                durable_continuation = self._uow.read_continuation(
                    continuation_claim.continuation_id
                )
                if (
                    durable_continuation is not None
                    and durable_continuation.state is ContinuationState.ACKED
                ):
                    continuation_claim = None
            if result.workflow_spawn_control is not None:
                await self._accept_workflow_spawn_control(run_id, result.workflow_spawn_control)
                return
            current = self._uow.read_run(run_id)
            if current is None:
                raise RuntimeError("Run disappeared during execution")
            if result.state is RunState.WAITING:
                if result.authorization_wait is not None:
                    if continuation_claim is not None:
                        raise UnitOfWorkConflict("authorization wait cannot ack a continuation")
                    pending = result.authorization_wait
                    prepared = pending.prepared
                    arguments = thaw_json(prepared.call.arguments)
                    metadata = thaw_json(pending.request.metadata)
                    if not isinstance(arguments, dict) or not isinstance(metadata, dict):
                        raise TypeError("authorization request payload must be objects")
                    self._uow.commit_decision(
                        decision_id=pending.decision_id,
                        run_id=run_id,
                        kind="tool_authorization",
                        state=DecisionState.OPEN,
                        request={
                            "arguments": arguments,
                            "call_id": prepared.call.call_id.value,
                            "effect_id": prepared.effect_id.value,
                            "expires_at": pending.request.expires_at,
                            "metadata": metadata,
                            "nonce": pending.request.nonce,
                            "prompt": pending.request.prompt,
                            "resources": [resource.to_json() for resource in prepared.resources],
                            "resources_digest": resource_digest(prepared.resources),
                            "sidecar_digest": (
                                None if prepared.sidecar is None else prepared.sidecar.digest
                            ),
                            "tool_name": prepared.call.name,
                        },
                        response=None,
                        event_id=f"{pending.decision_id}:open",
                        now=self._now(),
                    )
                    return
                if continuation_claim is None:
                    if result.wait_blocker is None:
                        self._uow.commit_runtime_state(
                            run_id=run_id,
                            expected_version=current.version,
                            state=RunState.WAITING,
                            event_id=f"{run_id}:waiting:{current.version + 1}",
                            payload=result.payload,
                            lease=self._leases[run_id],
                            now=self._now(),
                        )
                    else:
                        self._uow.commit_runtime_wait_with_blocker(
                            run_id=run_id,
                            expected_version=current.version,
                            event_id=f"{run_id}:waiting:{current.version + 1}",
                            payload=result.payload,
                            blocker=result.wait_blocker,
                            lease=self._leases[run_id],
                            now=self._now(),
                        )
                else:
                    if result.wait_blocker is not None:
                        raise UnitOfWorkConflict(
                            "uncertain outbound work cannot ack a continuation"
                        )
                    self._uow.commit_runtime_state_and_ack_continuation(
                        run_id=run_id,
                        expected_version=current.version,
                        state=RunState.WAITING,
                        event_id=(
                            f"{run_id}:waiting:continuation:"
                            f"{continuation_claim.continuation_id}:"
                            f"{continuation_claim.claim_epoch}"
                        ),
                        payload=result.payload,
                        continuation_claim=continuation_claim,
                        execution_lease=self._leases[run_id],
                        receipt_id=(
                            f"{run_id}:progress:{continuation_claim.continuation_id}:"
                            f"{continuation_claim.claim_epoch}"
                        ),
                        now=self._now(),
                    )
                    asyncio.create_task(self._reschedule(run_id))
            else:
                self._terminalize(
                    current,
                    state=result.state,
                    payload=result.payload,
                    deliveries=result.deliveries,
                    continuation_claim=continuation_claim,
                    conversation_output=result.conversation_output,
                )
        except asyncio.CancelledError:
            current = self._uow.read_run(run_id)
            if (
                not self._closing
                and current is not None
                and current.state is RunState.CANCEL_REQUESTED
            ):
                if continuation_claim is None:
                    await self._terminalize_cancelled(current, reason="driver_cancelled")
                else:
                    await self._abandon_run_authority(run_id)
        except UnitOfWorkConflict:
            if continuation_claim is not None:
                await self._abandon_run_authority(run_id)
            return
        except Exception as error:  # noqa: BLE001 - driver boundary becomes a durable failure
            # private_cause 不进 HarnessError.to_dict()（对外只暴露稳定 public 契约），
            # 但必须落到日志供 host 排障；否则 driver 失败完全不可追踪。
            logger.exception(
                "sdk_run_driver_failed",
                extra={"run_id": str(run_id)},
            )
            current = self._uow.read_run(run_id)
            if current is not None and current.state not in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                failure = HarnessError(
                    "driver_failed",
                    "The Run driver failed.",
                    private_cause=error,
                )
                try:
                    self._terminalize(
                        current,
                        state=RunState.FAILED,
                        payload=failure.to_dict(),
                        deliveries=(),
                        continuation_claim=continuation_claim,
                    )
                except UnitOfWorkConflict:
                    pass
        finally:
            current = self._uow.read_run(run_id)
            if current is not None and current.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                self._release_runtime_lease(run_id)

    async def _cancel_run(self, run_id: RunId) -> RunRecord:
        self._require_started()
        value = _run_id(run_id)
        current = self._uow.read_run(value)
        if current is None:
            raise KeyError(value)
        snapshot_raw = self._uow.read_start_snapshot(value)
        if snapshot_raw is None:
            raise RuntimeError("durable Run start state is incomplete")
        snapshot = StartSnapshot.from_json(snapshot_raw)
        if snapshot.driver_kind == WORKFLOW_DRIVER_KIND:
            token = self._cancels.get(value)
            latest = self._uow.read_run(value)
            assert latest is not None
            await self._terminalize_cancelled(latest, snapshot=snapshot, reason="user")
            if token is not None:
                token.cancel()
            if value in self._live.active_run_ids():
                await self._live.cancel(value)
            result = self._uow.read_run(value)
            assert result is not None
            return result
        self._uow.request_run_cancel(
            run_id=value,
            expected_version=current.version,
            event_id=f"{value}:cancel-requested",
            now=self._now(),
        )
        token = self._cancels.get(value)
        if token is not None:
            token.cancel()
        task_active = value in self._live.active_run_ids()
        if task_active:
            await self._live.cancel(value)
        latest = self._uow.read_run(value)
        assert latest is not None
        if latest.state is RunState.CANCEL_REQUESTED:
            await self._terminalize_cancelled(latest)
        result = self._uow.read_run(value)
        assert result is not None
        return result

    async def _reschedule(self, run_id: str) -> None:
        while run_id in self._live.active_run_ids():
            await asyncio.sleep(0)
        if run_id not in self._leases or self._closing:
            return
        self._schedule(run_id)

    async def _terminalize_cancelled(
        self,
        run: RunRecord,
        *,
        snapshot: StartSnapshot | None = None,
        reason: str = "user",
    ) -> None:
        if snapshot is None:
            raw = self._uow.read_start_snapshot(run.run_id)
            if raw is None:
                raise UnitOfWorkConflict("cancelled Run has no durable start snapshot")
            snapshot = StartSnapshot.from_json(raw)
        if snapshot.driver_kind == WORKFLOW_DRIVER_KIND:
            coordinator = self._cancellation_coordinators.get(WORKFLOW_DRIVER_KIND)
            if coordinator is None:
                raise UnitOfWorkConflict("workflow cancellation coordinator is not registered")
            if run.run_id not in self._leases or run.run_id not in self._fences:
                await self._activate(run.run_id)
            outcome = await coordinator.cancel(
                run,
                snapshot,
                reason=reason,
                now=self._now(),
                recovery=DriverCancellationRecovery(
                    self._leases[run.run_id], self._fences[run.run_id]
                ),
            )
            if outcome.terminal and not self._uow.verify_workflow_cancel_terminal(
                run_id=run.run_id,
                cancel_id=outcome.cancel_id,
                generation=outcome.generation,
            ):
                raise UnitOfWorkConflict("workflow cancel terminal receipt is not durable")
            self._drop_local_authority(run.run_id)
            return
        if run.run_id not in self._fences:
            await self._activate(run.run_id)
        self._terminalize(
            run,
            state=RunState.CANCELLED,
            payload={"code": "cancelled", "public_message": "The Run was cancelled."},
            deliveries=(),
        )

    def _terminalize(
        self,
        run: RunRecord,
        *,
        state: RunState,
        payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        continuation_claim: ContinuationRecord | None = None,
        conversation_output: ConversationTurnOutput | None = None,
    ) -> None:
        raw_snapshot = self._uow.read_start_snapshot(run.run_id)
        snapshot = None if raw_snapshot is None else StartSnapshot.from_json(raw_snapshot)
        committed_turn: CommittedTurnSpec | None = None
        legacy_cursor = self._uow.read_legacy_turn_cursor(run.run_id)
        legacy_cursor_version = (
            None
            if legacy_cursor is None or legacy_cursor.state != "active"
            else legacy_cursor.cursor_version
        )
        if (
            self._ports.agent_memory is not None
            and snapshot is not None
            and snapshot.conversation is not None
        ):
            if state is RunState.COMPLETED and conversation_output is None:
                raise UnitOfWorkConflict(
                    "completed conversation requires typed conversation_output"
                )
            if state is not RunState.COMPLETED and conversation_output is not None:
                raise UnitOfWorkConflict("non-completed conversation rejects conversation_output")
            if conversation_output is not None and conversation_output.memory_text is not None:
                if legacy_cursor_version is not None:
                    assert legacy_cursor is not None
                    user_text: str | None = legacy_cursor.user_text
                    turn_id: str = legacy_cursor.turn_id
                    assert user_text is not None
                    committed_turn = CommittedTurnSpec.from_domain(
                        CommittedTurn(
                            turn_id,
                            snapshot.conversation.identity,
                            user_text,
                            conversation_output.memory_text,
                            MemoryScopeRef.personal(snapshot.conversation.identity.actor_id),
                            legacy_cursor.write_fence,
                            legacy_cursor.turn_started_at,
                        )
                    )
                elif continuation_claim is None:
                    user_text = snapshot.conversation.memory_text
                    turn_id = snapshot.turn_id
                    stage_where = "consumed_run_id=?"
                    stage_identity = run.run_id
                else:
                    continuation_payload = thaw_json(continuation_claim.payload)
                    if not isinstance(continuation_payload, dict):
                        raise UnitOfWorkConflict("conversation continuation payload is invalid")
                    raw_conversation = continuation_payload.get("conversation")
                    if not isinstance(raw_conversation, Mapping):
                        raise UnitOfWorkConflict("conversation continuation value is invalid")
                    continuation_value = ConversationContinuationInput.from_json(raw_conversation)
                    user_text = continuation_value.memory_text
                    turn_id = continuation_claim.continuation_id
                    stage_where = "consumed_continuation_id=?"
                    stage_identity = continuation_claim.continuation_id
                if legacy_cursor_version is None and user_text is not None:
                    staging = self._ports.context_staging
                    if staging is None:
                        raise UnitOfWorkConflict("committed turn lacks Context staging")
                    stage = staging.database.connection.execute(
                        "SELECT memory_write_fence,turn_started_at "
                        f"FROM context_preparation_staging WHERE {stage_where}",
                        (stage_identity,),
                    ).fetchone()
                    if stage is None or stage["turn_started_at"] is None:
                        raise UnitOfWorkConflict("committed turn lacks durable recall lineage")
                    committed_turn = CommittedTurnSpec.from_domain(
                        CommittedTurn(
                            turn_id,
                            snapshot.conversation.identity,
                            user_text,
                            conversation_output.memory_text,
                            MemoryScopeRef.personal(snapshot.conversation.identity.actor_id),
                            (
                                None
                                if stage["memory_write_fence"] is None
                                else str(stage["memory_write_fence"])
                            ),
                            float(stage["turn_started_at"]),
                        )
                    )
        event = (
            "run.complete"
            if state is RunState.COMPLETED
            else "run.fail"
            if state is RunState.FAILED
            else "run.cancelled"
        )
        logger.info(
            event,
            extra={
                "run_id": run.run_id,
                "state": state.value,
                "payload_keys": list(payload)[:10],
            },
        )
        fence = self._fences[run.run_id]
        if run.parent_run_id is not None:
            if continuation_claim is not None:
                raise UnitOfWorkConflict("child continuation terminalization has no atomic command")
            committed = self._uow.read_child_terminal_result_for_run(run.run_id)
            if committed is not None:
                if committed.terminal_state != state.value:
                    raise UnitOfWorkConflict(
                        "workflow child terminal result differs from Driver result"
                    )
                self._drop_local_authority(run.run_id)
                self._emit_run_terminal(run, state, replayed=True)
                return
            command = self._uow.read_child_command_for_run(run.run_id)
            if command is None:
                raise UnitOfWorkConflict("child Run has no durable launch command")
            identity = f"{run.run_id}:{run.version}:{state.value}"
            policy = self._uow.read_child_attachment_policy(run.run_id)
            terminal_payload: dict[str, JsonValue] = {
                "status": state.value,
                "result": dict(payload),
            }
            if policy is AttachmentPolicy.DETACHED:
                self._uow.commit_detached_child_terminal(
                    command_id=command.command_id,
                    expected_child_version=run.version,
                    terminal_state=state,
                    terminal_payload=terminal_payload,
                    event_id=f"{identity}:event",
                    receipt_id=f"{identity}:receipt",
                    run_fence=fence,
                    execution_lease=self._leases[run.run_id],
                    now=self._now(),
                )
            else:
                self._uow.finalize_child_and_enqueue_parent_signal(
                    command_id=command.command_id,
                    expected_child_version=run.version,
                    terminal_state=state,
                    signal_id=f"{identity}:signal",
                    signal_payload=terminal_payload,
                    event_id=f"{identity}:event",
                    receipt_id=f"{identity}:receipt",
                    run_fence=fence,
                    execution_lease=self._leases[run.run_id],
                    now=self._now(),
                )
            self._emit_run_terminal(run, state)
            self._fences.pop(run.run_id, None)
            return
        if continuation_claim is not None:
            identity = (
                f"{run.run_id}:terminal:continuation:"
                f"{continuation_claim.continuation_id}:"
                f"{continuation_claim.claim_epoch}:{state.value}"
            )
            self._uow.commit_root_terminal_with_deliveries_and_ack_continuation(
                run_id=run.run_id,
                expected_version=run.version,
                terminal_state=state,
                event_id=f"{identity}:event",
                terminal_payload=payload,
                deliveries=deliveries,
                continuation_claim=continuation_claim,
                run_fence=fence,
                execution_lease=self._leases[run.run_id],
                receipt_id=f"{identity}:receipt",
                terminal_fence_receipt_ref=(f"runtime-fence:{fence.owner_id}:{fence.epoch}"),
                now=self._now(),
                committed_turn=committed_turn,
                conversation_output=(
                    None if conversation_output is None else conversation_output.to_json()
                ),
                legacy_cursor_version=legacy_cursor_version,
            )
            self._emit_run_terminal(run, state)
            self._fences.pop(run.run_id, None)
            if deliveries:
                self._delivery_wake.set()
            if committed_turn is not None:
                self._memory_wake.set()
            return
        self._terminal.commit(
            run,
            state=state,
            payload=payload,
            deliveries=deliveries,
            fence=fence,
            execution_lease=self._leases[run.run_id],
            now=self._now(),
            committed_turn=committed_turn,
            conversation_output=conversation_output,
            legacy_cursor_version=legacy_cursor_version,
        )
        self._emit_run_terminal(run, state)
        self._fences.pop(run.run_id, None)
        if deliveries:
            self._delivery_wake.set()
        if committed_turn is not None:
            self._memory_wake.set()

    def _emit_run_terminal(
        self, run: RunRecord, state: RunState, *, replayed: bool = False
    ) -> None:
        self._emit_transition(
            "run.terminal",
            run_id=run.run_id,
            outcome=Outcome.TERMINAL,
            operation="run_lifecycle",
            attributes={
                "entity_kind": "run",
                "entity_id": run.run_id,
                "run_id": run.run_id,
                "from_state": run.state.value,
                "to_state": state.value,
                "state_version": run.version + (0 if replayed else 1),
                "replayed": replayed,
                "error_code": None if state is RunState.COMPLETED else state.value,
            },
        )

    async def _abandon_run_authority(self, run_id: str) -> None:
        fence = self._fences.pop(run_id, None)
        if fence is not None:
            try:
                await self._uow.release(fence)
            except UnitOfWorkConflict:
                pass
        self._release_runtime_lease(run_id)

    def _release_runtime_lease(self, run_id: str) -> None:
        lease = self._leases.pop(run_id, None)
        self._workflow_spawn_ready_activations.pop(run_id, None)
        self._workflow_start_dispatches.pop(run_id, None)
        self._workflow_recovery_work.pop(run_id, None)
        heartbeat = self._heartbeats.pop(run_id, None)
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
        if lease is None:
            return
        try:
            self._uow.release_runtime_lease(lease, now=self._now())
        except UnitOfWorkConflict:
            pass
        self._cancels.pop(run_id, None)

    def _drop_local_authority(self, run_id: str) -> None:
        self._leases.pop(run_id, None)
        self._fences.pop(run_id, None)
        self._workflow_spawn_ready_activations.pop(run_id, None)
        self._workflow_start_dispatches.pop(run_id, None)
        self._workflow_recovery_work.pop(run_id, None)
        heartbeat = self._heartbeats.pop(run_id, None)
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
        self._cancels.pop(run_id, None)

    async def _accept_workflow_spawn_control(
        self, parent_run_id: str, outcome: WorkflowSpawnToolOutcome
    ) -> None:
        from .workflow_spawn import WorkflowSpawnChildControlKind

        child_control = outcome.child_control
        admission = child_control.admission
        child_run_id = admission.receipt.run_id
        if child_run_id != outcome.child_start_ref.child_run_id:
            raise UnitOfWorkConflict("workflow spawn child control identity differs")
        self._drop_local_authority(parent_run_id)
        if child_control.kind is WorkflowSpawnChildControlKind.START:
            self._register_workflow_child_start(admission)
        elif child_control.kind is WorkflowSpawnChildControlKind.RECOVER:
            self._register_workflow_child_recovery(admission)
        elif child_control.kind is WorkflowSpawnChildControlKind.CANCEL:
            child = self._uow.read_run(child_run_id)
            if child is None or child.state is not RunState.CANCEL_REQUESTED:
                raise UnitOfWorkConflict(
                    "workflow spawn CANCEL control lacks a cancel-pending child"
                )
            await self._terminalize_cancelled(child, reason="workflow_spawn_child_control")
            return
        elif child_control.kind is WorkflowSpawnChildControlKind.TERMINAL:
            if self._uow.read_child_terminal_result_for_run(child_run_id) is None:
                raise UnitOfWorkConflict("workflow spawn TERMINAL control lacks terminal receipt")
            return
        elif child_control.kind in {
            WorkflowSpawnChildControlKind.ATTACH,
            WorkflowSpawnChildControlKind.WAITING,
        }:
            return
        else:  # pragma: no cover - closed enum exhaustiveness
            raise AssertionError(child_control.kind)
        if child_run_id not in self._live.active_run_ids():
            self._schedule(child_run_id)

    def _register_recovered_spawn_child(self, admission: RuntimeStartAdmission) -> bool:
        if admission.disposition in {
            RuntimeStartDisposition.START_NEW,
            RuntimeStartDisposition.START_ORPHAN,
        }:
            self._register_workflow_child_start(admission)
            return True
        if admission.disposition in {
            RuntimeStartDisposition.RECOVER_START,
            RuntimeStartDisposition.RECOVER_RESUME,
        }:
            self._register_workflow_child_recovery(admission)
            return True
        if admission.disposition in {
            RuntimeStartDisposition.FOREIGN_ACTIVE,
            RuntimeStartDisposition.ATTACH_CURRENT,
            RuntimeStartDisposition.WAITING,
            RuntimeStartDisposition.TERMINAL,
        }:
            return False
        if admission.disposition is RuntimeStartDisposition.CANCEL_PENDING:
            # No Driver authority is returned for this disposition.  Let the
            # canonical cancellation coordinator reacquire and converge it.
            return True
        raise UnitOfWorkConflict("workflow child recovery control is not implemented")

    def _register_workflow_child_recovery(self, admission: RuntimeStartAdmission) -> None:
        activation = admission.activation
        recovery_work = admission.recovery_work
        if activation is None or recovery_work is None:
            raise UnitOfWorkConflict("workflow spawn RECOVER control lacks child authority")
        child_run_id = activation.execution_lease.run_id
        self._leases[child_run_id] = activation.execution_lease
        self._fences[child_run_id] = activation.run_fence
        self._workflow_recovery_work[child_run_id] = recovery_work
        self._cancels.setdefault(child_run_id, CancelToken())
        heartbeat = self._heartbeats.get(child_run_id)
        if heartbeat is None or heartbeat.done():
            self._heartbeats[child_run_id] = asyncio.create_task(
                self._heartbeat(child_run_id),
                name=f"simple-harness-heartbeat:{child_run_id}",
            )

    def _register_workflow_child_start(self, admission: RuntimeStartAdmission) -> None:
        activation = admission.activation
        dispatch_claim = admission.dispatch_claim
        if activation is None or dispatch_claim is None:
            raise UnitOfWorkConflict("workflow spawn START control lacks child authority")
        child_run_id = activation.execution_lease.run_id
        self._leases[child_run_id] = activation.execution_lease
        self._fences[child_run_id] = activation.run_fence
        self._workflow_start_dispatches[child_run_id] = dispatch_claim
        self._cancels.setdefault(child_run_id, CancelToken())
        heartbeat = self._heartbeats.get(child_run_id)
        if heartbeat is None or heartbeat.done():
            self._heartbeats[child_run_id] = asyncio.create_task(
                self._heartbeat(child_run_id),
                name=f"simple-harness-heartbeat:{child_run_id}",
            )

    def _now(self) -> float:
        value = self._ports.clock()
        if not math.isfinite(value) or value < 0:
            raise ValueError("runtime clock must return a finite non-negative value")
        return float(value)

    def _require_started(self) -> None:
        if self._state is not RuntimeLifecycleState.READY:
            raise HarnessError("runtime_not_ready", "Runtime is not ready.")


def build_runtime(
    uow: RuntimeUnitOfWork,
    profiles: Mapping[str, RuntimeProfile],
    drivers: Mapping[str, RuntimeDriver],
    ports: RuntimePorts,
    root_profile_key: str = ROOT_PROFILE_KEY,
    *,
    workflow_runner: object | None = None,
    close_hook: Callable[[], None] | None = None,
    start_hooks: Sequence[Callable[[], Awaitable[None]]] = (),
    async_close_hooks: Sequence[Callable[[], Awaitable[None]]] = (),
) -> Runtime:
    """Build a Runtime with one fixed root Profile and no classifier path."""

    if root_profile_key != ROOT_PROFILE_KEY:
        raise ValueError("root_profile_key is fixed to agent.general")
    bound_profiles = dict(profiles)
    if ROOT_PROFILE_KEY not in bound_profiles:
        raise ValueError("agent.general profile is required")
    profile = bound_profiles[ROOT_PROFILE_KEY]
    if profile.profile_key != ROOT_PROFILE_KEY:
        raise ValueError("agent.general profile binding is inconsistent")
    bound_drivers = dict(drivers)
    if WORKFLOW_DRIVER_KIND in bound_drivers:
        raise ValueError("workflow is an SDK-reserved driver key")
    workflow_profiles = tuple(
        item for item in bound_profiles.values() if item.driver_kind == WORKFLOW_DRIVER_KIND
    )
    if workflow_profiles and workflow_runner is None:
        raise ValueError("workflow profile requires the SDK-owned workflow runner")
    if profile.driver_kind not in bound_drivers and not (
        profile.driver_kind == WORKFLOW_DRIVER_KIND and workflow_runner is not None
    ):
        raise ValueError("agent.general driver is not registered")
    return Runtime(
        uow=uow,
        profiles=bound_profiles,
        drivers=bound_drivers,
        workflow_runner=workflow_runner,
        ports=ports,
        root_profile_key=ROOT_PROFILE_KEY,
        close_hook=close_hook,
        start_hooks=start_hooks,
        async_close_hooks=async_close_hooks,
    )


def _run_id(value: RunId) -> str:
    if not isinstance(value, RunId):
        raise TypeError("run_id must use RunId")
    return value.value


__all__ = (
    "ROOT_PROFILE_KEY",
    "WORKFLOW_DRIVER_KIND",
    "DriverCancelOutcome",
    "DriverCancellationCoordinator",
    "DriverCancellationRecovery",
    "DriverInvocation",
    "DriverResult",
    "ReactCheckpointPort",
    "RunClient",
    "Runtime",
    "RuntimeLifecycleState",
    "RuntimeDriver",
    "RuntimePorts",
    "RuntimeProfile",
    "RuntimeReconciliationPort",
    "RuntimeServices",
    "RuntimeUnitOfWork",
    "ToolCatalogGenerationPort",
    "build_runtime",
)
