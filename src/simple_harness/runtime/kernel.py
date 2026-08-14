# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable RunKernel lifecycle and fixed-root public client."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, Self, TypeVar, runtime_checkable
from uuid import uuid4

from simple_harness.contracts import (
    ExecutionSessionId,
    HarnessError,
    JsonValue,
    RequestId,
    RunId,
)
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.fences import RunFenceLease, RunFencePort
from simple_harness.execution.recovery import WaitBlockerSpec
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ContinuationRecord,
    ContinuationState,
    ExecutionLease,
    ExecutionUnitOfWork,
    RunRecord,
    RunState,
    UnitOfWorkConflict,
    WorkflowCheckpoint,
)
from simple_harness.providers import CancelToken, ProviderReconciliationPort
from simple_harness.tools.authorization import AuthorizationPort
from simple_harness.tools.executor import EffectExecutor
from simple_harness.tools.reconciliation import ToolReconciliationPort
from simple_harness.workflow.errors import WorkflowDependencyUnavailable

from .admission import AdmissionPort, AllowAllAdmission
from .child_coordinator import ChildCoordinator
from .child_signal_runtime import ChildSignalRuntime
from .context import ContextPort
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
            raise ValueError(
                "workflow spawn ready activation differs from Driver authority"
            )
        recovery = self.workflow_recovery_work
        if recovery is not None and (
            recovery.run_id != self.run.run_id
            or self.workflow_start_dispatch is not None
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

    def __post_init__(self) -> None:
        state = RunState(self.state)
        if state not in {RunState.WAITING, RunState.COMPLETED, RunState.FAILED}:
            raise ValueError("driver result must be waiting, completed, or failed")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))
        if self.workflow_spawn_control is not None and (
            state is not RunState.WAITING or self.wait_blocker is not None
        ):
            raise ValueError(
                "workflow spawn control requires an exclusive WAITING result"
            )
        if self.workflow_terminal is not None and state not in {
            RunState.COMPLETED,
            RunState.FAILED,
        }:
            raise ValueError(
                "workflow terminal outcome requires COMPLETED or FAILED state"
            )
        if self.workflow_retry_wake is not None and (
            state is not RunState.WAITING or self.wait_blocker is not None
        ):
            raise ValueError(
                "workflow retry wake requires an exclusive WAITING result"
            )


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

        return await self._uow.run_atomic(
            operation, fault_label="runtime:workflow_spawn:catalog"
        )

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
                evidence = self._runner._prove_graph_unavailable(
                    verified, activation
                )
                failed = (
                    await self._uow.settle_spawn_continuation_graph_unavailable(
                        transaction,
                        activation.continuation_claim,
                        activation.ready_receipt,
                        evidence,
                        now=self._clock(),
                    )
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

        return await self._uow.run_atomic(
            operation, fault_label="runtime:spawn_ready:continue"
        )


class RuntimeUnitOfWork(
    ExecutionUnitOfWork, RunFencePort, WorkflowLaunchTicketPort, Protocol
):
    @property
    def transaction_owner(self) -> object: ...

    async def run_atomic(
        self,
        operation: Callable[[WorkflowTransaction], Awaitable[T]],
        *,
        fault_label: str,
    ) -> T: ...


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


class RunClient:
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def start(self, value: RunStart) -> RunRecord:
        return await self._runtime._start_run(value)

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
        continuation = self._runtime._uow.enqueue_continuation(
            continuation_id=signal_id,
            run_id=_run_id(run_id),
            payload=payload,
            now=self._runtime._now(),
        )
        asyncio.create_task(self._runtime._wake_continuation(continuation.run_id))
        return continuation

    async def cancel(self, run_id: RunId) -> RunRecord:
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
        checkpoint = self._runtime._ports.react_checkpoint.read_react_checkpoint(
            context.run_id
        )
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
            or selection.profile_key
            not in {item.profile_key for item in catalog.profiles}
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
    ) -> None:
        if WORKFLOW_DRIVER_KIND in drivers:
            raise ValueError("workflow is an SDK-reserved driver key")
        workflow_profiles = tuple(
            item
            for item in profiles.values()
            if item.driver_kind == WORKFLOW_DRIVER_KIND
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
            workflow_spawn = _CanonicalWorkflowSpawnRuntimeCoordinator(
                uow=uow,
                runner=workflow_runner,
                owner_id=ports.owner_id,
                lease_ttl_seconds=ports.lease_ttl_seconds,
                clock=ports.clock,
            )
        if workflow_profiles and workflow_driver is None:
            raise ValueError("workflow profile requires the SDK-owned workflow driver")
        self._uow = uow
        self._workflow_runner = workflow_runner
        self._profiles = dict(profiles)
        self._drivers = dict(drivers)
        self._cancellation_coordinators: dict[
            str, DriverCancellationCoordinator
        ] = {}
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
            workflow_spawn=workflow_spawn,
        )
        self._live = LiveRunIndex()
        self._leases: dict[str, ExecutionLease] = {}
        self._fences: dict[str, RunFenceLease] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
        self._workflow_spawn_ready_activations: dict[
            str, WorkflowSpawnReadyActivation
        ] = {}
        self._workflow_start_dispatches: dict[str, RuntimeStartDispatchClaim] = {}
        self._workflow_recovery_work: dict[str, WorkflowRecoveryWork] = {}
        self._child_signals = ChildSignalRuntime(uow, owner_id=ports.owner_id)
        self._wake_drain_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self.client = RunClient(self)
        self.children = ChildCoordinator(self)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closing = False
        await self._ports.reconciliation.reconcile()
        await self.recover()
        await self._drain_resolved_waits_once()
        self._wake_drain_task = asyncio.create_task(
            self._wake_drain(), name="simple-harness-wake-drain"
        )

    async def recover(self) -> None:
        self._require_started()
        for run in (
            *self._uow.list_recoverable_root_runs(),
            *self._uow.list_recoverable_child_runs(),
        ):
            if run.run_id in self._live.active_run_ids():
                continue
            try:
                prior_ready = await self._uow.run_atomic(
                    lambda tx, run_id=run.run_id: self._uow.read_spawn_ready_activation(
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
                            lambda tx, run_id=run.run_id: self._uow.resume_spawn_child_start(
                                tx,
                                run_id,
                                RuntimeActivationClaim(
                                    self._ports.owner_id,
                                    lease_ttl_seconds=(
                                        self._ports.lease_ttl_seconds
                                    ),
                                ),
                                now=self._now(),
                            ),
                            fault_label="runtime:spawn_child:resume",
                        )
                        if not self._register_recovered_spawn_child(admission):
                            continue
                        activated = self._uow.read_run(run.run_id)
                        if activated is None:
                            raise UnitOfWorkConflict(
                                "workflow spawn child Run disappeared"
                            )
                    else:
                        activated = await self._activate(run.run_id)
                else:
                    ready_activation = await self._uow.run_atomic(
                        lambda tx, prior=prior_ready: self._uow.reclaim_spawn_ready_activation(
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
                        raise UnitOfWorkConflict(
                            "workflow spawn parent Run disappeared"
                        )
            except UnitOfWorkConflict:
                continue
            if activated.state is RunState.CANCEL_REQUESTED:
                await self._terminalize_cancelled(
                    activated, reason="startup_recovery"
                )
            else:
                self._schedule(activated.run_id)

    async def reconcile(self) -> None:
        self._require_started()
        await self._ports.reconciliation.reconcile()
        await self._drain_resolved_waits_once()
        await self.recover()

    async def dispatch_deliveries_once(self) -> bool:
        self._require_started()
        return await self._ports.delivery.run_once()

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        wake_drain = self._wake_drain_task
        self._wake_drain_task = None
        if wake_drain is not None:
            wake_drain.cancel()
            await asyncio.gather(wake_drain, return_exceptions=True)
        for token in self._cancels.values():
            token.cancel()
        await self._live.close(timeout_seconds=self._ports.close_timeout_seconds)
        heartbeat_tasks = tuple(self._heartbeats.values())
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

    async def _wake_drain(self) -> None:
        interval = min(0.05, max(0.001, self._ports.lease_ttl_seconds / 3.0))
        try:
            while self._started and not self._closing:
                await self._drain_resolved_waits_once()
                await asyncio.sleep(interval)
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
                run, lease, _receipt = (
                    self._uow.consume_resolved_wait_and_claim_activation(
                        blocker_id=blocker.blocker_id,
                        owner_id=self._ports.owner_id,
                        namespace=RUNTIME_LEASE_NAMESPACE,
                        now=self._now(),
                        lease_ttl_seconds=self._ports.lease_ttl_seconds,
                    )
                )
                fence = await self._uow.acquire(
                    RunId(run.run_id), lease, now=self._now()
                )
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
        for result in self._child_signals.reconcile_all(now=self._now()):
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
            ready_values, cursor = self._uow.list_ready_spawn_continuations(
                cursor, limit=100
            )
            for ready in ready_values:
                blocker = self._uow.read_spawn_ready_blocker(ready)
                if blocker is None:
                    continue
                try:
                    activation = await self._uow.run_atomic(
                        lambda tx, ready=ready, blocker=blocker: self._uow.consume_spawn_ready_and_claim_activation(
                            tx,
                            ready,
                            blocker,
                            self._ports.owner_id,
                            now=self._now(),
                            ttl_seconds=self._ports.lease_ttl_seconds,
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

    def _register_spawn_ready_activation(
        self, activation: WorkflowSpawnReadyActivation
    ) -> None:
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
        await self._live.wait(_run_id(run_id))

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _start_run(self, start: RunStart) -> RunRecord:
        self._require_started()
        verdict = await self._ports.admission.evaluate(start)
        if not verdict.allowed:
            raise HarnessError("admission_denied", "The Run was denied by admission.")
        profile = self._profiles[self._root_profile_key]
        snapshot = bind_start_snapshot(
            start, profile_key=self._root_profile_key, driver_kind=profile.driver_kind
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
                        lambda tx, receipt_id=ready.activation_receipt_id: self._uow.read_spawn_ready_activation(
                            tx, run_id, receipt_id
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
            if (
                snapshot.tool_catalog_generation
                != self._ports.tool_catalog.current_generation()
            ):
                error = ToolCatalogStale()
                self._terminalize(
                    run,
                    state=RunState.FAILED,
                    payload=error.to_dict(),
                    deliveries=(),
                )
                return
            if run.state is RunState.CANCEL_REQUESTED:
                await self._terminalize_cancelled(
                    run, snapshot=snapshot, reason="pre_drive_cancel"
                )
                return
            driver = self._drivers[snapshot.driver_kind]
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
                    continuations=(
                        () if continuation_claim is None else (continuation_claim,)
                    ),
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
                await self._accept_workflow_spawn_control(
                    run_id, result.workflow_spawn_control
                )
                return
            current = self._uow.read_run(run_id)
            if current is None:
                raise RuntimeError("Run disappeared during execution")
            if result.state is RunState.WAITING:
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
                )
        except asyncio.CancelledError:
            current = self._uow.read_run(run_id)
            if (
                not self._closing
                and current is not None
                and current.state is RunState.CANCEL_REQUESTED
            ):
                if continuation_claim is None:
                    await self._terminalize_cancelled(
                        current, reason="driver_cancelled"
                    )
                else:
                    await self._abandon_run_authority(run_id)
        except UnitOfWorkConflict:
            if continuation_claim is not None:
                await self._abandon_run_authority(run_id)
            return
        except Exception as error:  # noqa: BLE001 - driver boundary becomes a durable failure
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
            await self._terminalize_cancelled(
                latest, snapshot=snapshot, reason="user"
            )
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
                raise UnitOfWorkConflict(
                    "workflow cancellation coordinator is not registered"
                )
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
                raise UnitOfWorkConflict(
                    "workflow cancel terminal receipt is not durable"
                )
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
    ) -> None:
        fence = self._fences[run.run_id]
        if run.parent_run_id is not None:
            if continuation_claim is not None:
                raise UnitOfWorkConflict(
                    "child continuation terminalization has no atomic command"
                )
            committed = self._uow.read_child_terminal_result_for_run(run.run_id)
            if committed is not None:
                if committed.terminal_state != state.value:
                    raise UnitOfWorkConflict(
                        "workflow child terminal result differs from Driver result"
                    )
                self._drop_local_authority(run.run_id)
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
                terminal_fence_receipt_ref=(
                    f"runtime-fence:{fence.owner_id}:{fence.epoch}"
                ),
                now=self._now(),
            )
            self._fences.pop(run.run_id, None)
            return
        self._terminal.commit(
            run,
            state=state,
            payload=payload,
            deliveries=deliveries,
            fence=fence,
            execution_lease=self._leases[run.run_id],
            now=self._now(),
        )
        self._fences.pop(run.run_id, None)

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
            await self._terminalize_cancelled(
                child, reason="workflow_spawn_child_control"
            )
            return
        elif child_control.kind is WorkflowSpawnChildControlKind.TERMINAL:
            if self._uow.read_child_terminal_result_for_run(child_run_id) is None:
                raise UnitOfWorkConflict(
                    "workflow spawn TERMINAL control lacks terminal receipt"
                )
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

    def _register_recovered_spawn_child(
        self, admission: RuntimeStartAdmission
    ) -> bool:
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
        raise UnitOfWorkConflict(
            "workflow child recovery control is not implemented"
        )

    def _register_workflow_child_recovery(
        self, admission: RuntimeStartAdmission
    ) -> None:
        activation = admission.activation
        recovery_work = admission.recovery_work
        if activation is None or recovery_work is None:
            raise UnitOfWorkConflict(
                "workflow spawn RECOVER control lacks child authority"
            )
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

    def _register_workflow_child_start(
        self, admission: RuntimeStartAdmission
    ) -> None:
        activation = admission.activation
        dispatch_claim = admission.dispatch_claim
        if activation is None or dispatch_claim is None:
            raise UnitOfWorkConflict(
                "workflow spawn START control lacks child authority"
            )
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
        if not self._started or self._closing:
            raise RuntimeError("Runtime is not started")


def build_runtime(
    uow: RuntimeUnitOfWork,
    profiles: Mapping[str, RuntimeProfile],
    drivers: Mapping[str, RuntimeDriver],
    ports: RuntimePorts,
    root_profile_key: str = ROOT_PROFILE_KEY,
    *,
    workflow_runner: object | None = None,
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
    "RuntimeDriver",
    "RuntimePorts",
    "RuntimeProfile",
    "RuntimeReconciliationPort",
    "RuntimeServices",
    "RuntimeUnitOfWork",
    "ToolCatalogGenerationPort",
    "build_runtime",
)
