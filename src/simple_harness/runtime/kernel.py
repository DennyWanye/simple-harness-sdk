# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable RunKernel lifecycle and fixed-root public client."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, Self, runtime_checkable
from uuid import uuid4

from simple_harness.contracts import HarnessError, JsonValue, RunId
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.fences import RunFenceLease, RunFencePort
from simple_harness.execution.uow import (
    RUNTIME_LEASE_NAMESPACE,
    ContinuationRecord,
    ExecutionLease,
    ExecutionUnitOfWork,
    RunRecord,
    RunState,
    UnitOfWorkConflict,
)
from simple_harness.providers import CancelToken
from simple_harness.tools.authorization import AuthorizationPort
from simple_harness.tools.executor import EffectExecutor
from simple_harness.tools.reconciliation import ToolReconciliationPort

from .admission import AdmissionPort, AllowAllAdmission
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


@dataclass(frozen=True, slots=True)
class DriverResult:
    state: RunState
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    deliveries: tuple[DeliverySpec, ...] = ()

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


class RuntimeUnitOfWork(ExecutionUnitOfWork, RunFencePort, Protocol):
    pass


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    context: ContextPort
    tool_catalog: ToolCatalogGenerationPort
    provider: ProviderInvocationCoordinator | None = None
    tools: EffectExecutor | None = None
    authorization: AuthorizationPort | None = None
    delivery: DeliveryDispatcher | None = None
    tool_reconciliation: ToolReconciliationPort | None = None
    reconciliation: RuntimeReconciliationPort | None = None
    admission: AdmissionPort = field(default_factory=AllowAllAdmission)
    clock: Callable[[], float] = time.time
    owner_id: str = field(default_factory=lambda: f"runtime-{uuid4().hex}")
    lease_ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if (
            not isinstance(self.lease_ttl_seconds, (int, float))
            or isinstance(self.lease_ttl_seconds, bool)
            or not math.isfinite(float(self.lease_ttl_seconds))
            or self.lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be finite and positive")


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
        return self._runtime._uow.enqueue_continuation(
            continuation_id=signal_id,
            run_id=_run_id(run_id),
            payload=payload,
            now=self._runtime._now(),
        )

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
        self._live = LiveRunIndex()
        self._leases: dict[str, ExecutionLease] = {}
        self._fences: dict[str, RunFenceLease] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._heartbeats: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._closing = False
        self.client = RunClient(self)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closing = False
        if self._ports.reconciliation is not None:
            await self._ports.reconciliation.reconcile()
        await self.recover()

    async def recover(self) -> None:
        self._require_started()
        for run in self._uow.list_recoverable_root_runs():
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

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        for token in self._cancels.values():
            token.cancel()
        await self._live.close()
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

    async def _activate(self, run_id: str) -> RunRecord:
        run, lease = self._uow.claim_runtime_activation(
            run_id=run_id,
            owner_id=self._ports.owner_id,
            namespace=RUNTIME_LEASE_NAMESPACE,
            now=self._now(),
            lease_ttl_seconds=self._ports.lease_ttl_seconds,
        )
        self._leases[run_id] = lease
        fence = await self._uow.acquire(RunId(run_id), self._ports.owner_id)
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
            while not self._closing and run_id in self._leases:
                await asyncio.sleep(interval)
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
            result = await driver.start(
                DriverInvocation(run, snapshot, self._leases[run_id]),
                context=self._ports.context,
                cancel=self._cancels[run_id],
            )
            current = self._uow.read_run(run_id)
            if current is None:
                raise RuntimeError("Run disappeared during execution")
            if result.state is RunState.WAITING:
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
                self._terminalize(
                    current,
                    state=result.state,
                    payload=result.payload,
                    deliveries=result.deliveries,
                )
        except asyncio.CancelledError:
            current = self._uow.read_run(run_id)
            if (
                not self._closing
                and current is not None
                and current.state is RunState.CANCEL_REQUESTED
            ):
                await self._terminalize_cancelled(current)
        except UnitOfWorkConflict:
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
    ) -> None:
        fence = self._fences[run.run_id]
        self._terminal.commit(
            run,
            state=state,
            payload=payload,
            deliveries=deliveries,
            fence=fence,
            now=self._now(),
        )
        self._fences.pop(run.run_id, None)

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
    "RunClient",
    "Runtime",
    "RuntimeDriver",
    "RuntimePorts",
    "RuntimeProfile",
    "RuntimeReconciliationPort",
    "RuntimeUnitOfWork",
    "ToolCatalogGenerationPort",
    "build_runtime",
)
