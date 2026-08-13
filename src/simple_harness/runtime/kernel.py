# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable RunKernel lifecycle and fixed-root public client."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, Self, runtime_checkable
from uuid import uuid4

from simple_harness.contracts import HarnessError, JsonValue, RunId
from simple_harness.execution.contracts.children import AttachmentPolicy
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.fences import RunFenceLease, RunFencePort
from simple_harness.execution.recovery import WaitBlockerSpec
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ContinuationRecord,
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

from .admission import AdmissionPort, AllowAllAdmission
from .child_coordinator import ChildCoordinator
from .context import ContextPort
from .live_index import LiveRunIndex
from .start_snapshot import RunStart, StartSnapshot, bind_start_snapshot
from .terminal import TerminalCoordinator, ToolCatalogStale

ROOT_PROFILE_KEY = "agent.general"


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


@dataclass(frozen=True, slots=True)
class DriverResult:
    state: RunState
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    deliveries: tuple[DeliverySpec, ...] = ()
    wait_blocker: WaitBlockerSpec | None = None

    def __post_init__(self) -> None:
        state = RunState(self.state)
        if state not in {RunState.WAITING, RunState.COMPLETED, RunState.FAILED}:
            raise ValueError("driver result must be waiting, completed, or failed")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))


@runtime_checkable
class RuntimeDriver(Protocol):
    async def start(
        self,
        invocation: DriverInvocation,
        *,
        context: ContextPort,
        cancel: CancelToken,
    ) -> DriverResult: ...


@runtime_checkable
class ToolCatalogGenerationPort(Protocol):
    def current_generation(self) -> int: ...


@runtime_checkable
class RuntimeReconciliationPort(Protocol):
    async def reconcile(self) -> None: ...


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


class RuntimeUnitOfWork(ExecutionUnitOfWork, RunFencePort, Protocol):
    pass


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


class Runtime:
    def __init__(
        self,
        *,
        uow: RuntimeUnitOfWork,
        profiles: Mapping[str, RuntimeProfile],
        drivers: Mapping[str, RuntimeDriver],
        ports: RuntimePorts,
        root_profile_key: str,
    ) -> None:
        self._uow = uow
        self._profiles = dict(profiles)
        self._drivers = dict(drivers)
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
        )
        self._live = LiveRunIndex()
        self._leases: dict[str, ExecutionLease] = {}
        self._fences: dict[str, RunFenceLease] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
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
                activated = await self._activate(run.run_id)
            except UnitOfWorkConflict:
                continue
            if activated.state is RunState.CANCEL_REQUESTED:
                await self._terminalize_cancelled(activated)
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
                    return
                self._leases[run_id] = renewed
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
                await self._terminalize_cancelled(run)
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
                    run,
                    snapshot,
                    self._leases[run_id],
                    self._fences[run_id],
                    self._services,
                    () if continuation_claim is None else (continuation_claim,),
                ),
                context=self._ports.context,
                cancel=self._cancels[run_id],
            )
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
                    await self._terminalize_cancelled(current)
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

    async def _terminalize_cancelled(self, run: RunRecord) -> None:
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
    if profile.driver_kind not in bound_drivers:
        raise ValueError("agent.general driver is not registered")
    return Runtime(
        uow=uow,
        profiles=bound_profiles,
        drivers=bound_drivers,
        ports=ports,
        root_profile_key=ROOT_PROFILE_KEY,
    )


def _run_id(value: RunId) -> str:
    if not isinstance(value, RunId):
        raise TypeError("run_id must use RunId")
    return value.value


__all__ = (
    "ROOT_PROFILE_KEY",
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
