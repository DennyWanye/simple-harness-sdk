# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BaseAgent runtime assembly (Slice 1).

Built on the consumer composition (``consumer_adapter``), never on
``build_production_runtime``: no ``agent_memory``, no conversation Memory, no
Memory outbox consumer, no context staging.  Nothing here may import
``simple_harness_memory``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Self, cast

from simple_harness.contracts import (
    ExecutionSessionId,
    JsonValue,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
)
from simple_harness.execution.base_agent import BASE_AGENT_API_MODE, AgentBindingRecord
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime.consumer_adapter import (
    _ConsumerAuthorizationAdapter,
    _ConsumerProviderAdapter,
    _ConsumerToolExecutorAdapter,
    _DefaultProviderReconciliation,
    _DefaultRuntimeReconciliation,
    _DefaultToolCatalog,
    _DefaultToolReconciliation,
)
from simple_harness.runtime.context import SqliteContextPort
from simple_harness.runtime.kernel import Runtime, RuntimePorts, RuntimeProfile, build_runtime
from simple_harness.runtime.start_snapshot import RunStart
from simple_harness.tools import EffectExecutor, FunctionTool, Tool

from .base import BaseAgent
from .config import AgentConfig, config_hash
from .contracts import AgentClosingReceipt, AgentId, AgentInputConflict, AgentNotFound
from .execution import build_agent_execution_driver
from .ports import AgentRuntimePorts
from .tool_registry import BaseAgentToolRegistry
from .wire import AgentProviderWire

ROOT_PROFILE_KEY = "agent.general"
CHILD_PROFILE_KEY = "agent.base"
BASE_AGENT_DRIVER_KIND = "base_agent"


class AgentRuntimeReconciliation:
    """Runtime reconciliation for BaseAgents: settle uncertain Provider handoffs first.

    The consumer default port is a no-op, so an UNKNOWN provider outcome would block a
    turn forever (review F2).  On every ``Runtime.reconcile()`` (and at startup) ask the
    provider coordinator to observe incomplete invocations through the injected
    ``provider_reconciliation`` port; resolved blockers are then drained by the kernel.
    """

    def __init__(self, coordinator, provider_reconciliation, *, inner) -> None:  # type: ignore[no-untyped-def]
        self._coordinator = coordinator
        self._provider_reconciliation = provider_reconciliation
        self._inner = inner

    async def reconcile(self) -> None:
        await self._coordinator.reconcile_incomplete(
            provider_reconciliation=self._provider_reconciliation
        )
        await self._inner.reconcile()


@dataclass(frozen=True, slots=True)
class AssembledRuntime:
    runtime: Runtime
    uow: SqliteExecutionUnitOfWork
    database: Database
    driver: object = None
    wire: object = None


def assemble_runtime(
    ports: AgentRuntimePorts,
    *,
    extra_tools: tuple[FunctionTool, ...] = (),
    delegation_counter=None,  # type: ignore[no-untyped-def]
    delegation_reconciliation=None,  # type: ignore[no-untyped-def]
) -> AssembledRuntime:
    """Compose the kernel for BaseAgents; root and child profiles both drive ``base_agent``."""

    database = Database.open(ports.database_path)
    uow = SqliteExecutionUnitOfWork(database)
    tools: tuple[Tool, ...] = ()
    if ports.tool_executor is not None:
        registry_source = _ConsumerToolExecutorAdapter(
            ports.tool_executor, ports.tool_names, ports.tool_schemas
        ).build_registry()
        tools = tuple(registry_source.get(spec.name) for spec in registry_source.specs)

    def _exposure(run_id: str):  # type: ignore[no-untyped-def]
        snapshot = uow.read_start_snapshot(run_id)
        if snapshot is None:
            return ()
        capability = snapshot.get("input", {}).get("capability_snapshot", {})  # type: ignore[union-attr]
        names = capability.get("tools", ()) if isinstance(capability, Mapping) else ()
        return tuple(names) if isinstance(names, (list, tuple)) else ()

    registry = BaseAgentToolRegistry(
        (*tools, *cast(tuple[Tool, ...], extra_tools)), exposure_reader=_exposure
    )
    auth_adapter = _ConsumerAuthorizationAdapter(ports.authorization)
    tool_reconciliation = ports.policies.tool_reconciliation or _DefaultToolReconciliation()
    if delegation_reconciliation is not None:
        tool_reconciliation = delegation_reconciliation(uow, tool_reconciliation)
    effects = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=auth_adapter,
        reconciliation=tool_reconciliation,
        clock=ports.clock,
    )
    wire = AgentProviderWire(ports.provider, database)
    provider_adapter = _ConsumerProviderAdapter(wire, ports.model)
    # The consumer provider adapter reports pricing_key "consumer"; the estimator must match.
    estimator = ports.policies.estimator or FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)
    budget_policy = ports.policies.budget_policy
    provider_coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider_adapter,
        budget_policy=budget_policy,
        estimator=estimator,
        context_use_authority=None,
        clock=ports.clock,
    )
    context = SqliteContextPort(database, clock=ports.clock)
    provider_reconciliation = (
        ports.policies.provider_reconciliation or _DefaultProviderReconciliation()
    )
    runtime_reconciliation = AgentRuntimeReconciliation(
        provider_coordinator,
        provider_reconciliation,
        inner=ports.policies.runtime_reconciliation or _DefaultRuntimeReconciliation(),
    )
    runtime_ports = RuntimePorts(
        provider=provider_coordinator,
        tools=effects,
        authorization=auth_adapter,
        context=context,
        delivery=DeliveryDispatcher(uow, {}),
        tool_reconciliation=tool_reconciliation,
        reconciliation=runtime_reconciliation,
        provider_reconciliation=provider_reconciliation,
        react_checkpoint=uow,
        tool_catalog=_DefaultToolCatalog(),
        owner_id=ports.owner_id,
        clock=ports.clock,
        lease_ttl_seconds=ports.lease_ttl_seconds,
        # Explicitly no user Memory (BA-v1.0 §9.4).
        conversation_memory_enabled=False,
        memory_dispatcher=None,
        context_staging=None,
        context_preparation_mode=None,
        agent_memory=None,
        context_provider=None,
    )
    driver = build_agent_execution_driver(
        limits=ports.termination_limits,
        budget_policy=budget_policy,
        estimator=estimator,
        delegation_counter=delegation_counter,
        clock=ports.clock,
    )
    runtime = build_runtime(
        uow=uow,  # type: ignore[arg-type]
        profiles={
            ROOT_PROFILE_KEY: RuntimeProfile(ROOT_PROFILE_KEY, BASE_AGENT_DRIVER_KIND),
            CHILD_PROFILE_KEY: RuntimeProfile(CHILD_PROFILE_KEY, BASE_AGENT_DRIVER_KIND),
        },
        drivers={BASE_AGENT_DRIVER_KIND: driver},
        ports=runtime_ports,
        close_hook=uow.close,
    )
    return AssembledRuntime(runtime, uow, database, driver, wire)


def agent_id_for(owner_scope: str, creation_key: str) -> str:
    """Stable opaque id derived from the creation key: retries reproduce the same Agent."""

    digest = hashlib.sha256(f"{owner_scope}\x00{creation_key}".encode()).hexdigest()
    return f"agent-{digest[:32]}"


def start_input_for(
    config: AgentConfig,
    *,
    agent_id: str,
    role: str,
    owner_scope: str,
    max_output_tokens: int,
) -> dict[str, JsonValue]:
    """``start.input`` carrying the Agent binding next to the capability snapshot."""

    from simple_harness.contracts import Message

    messages: list[JsonValue] = []
    if config.instructions.strip():
        messages.append(Message(MessageRole.SYSTEM, config.instructions).to_dict())
    return {
        "capability_snapshot": {"tools": list(config.tool_names)},
        "base_agent_binding": {
            "agent_id": agent_id,
            "config_hash": config_hash(config),
            "api_mode": BASE_AGENT_API_MODE,
            "role": role,
            "owner_scope": owner_scope,
        },
        "messages": messages,
        "max_output_tokens": max_output_tokens,
    }


class AgentRuntime:
    """Factory + execution service for BaseAgents (BA-v1.0 §3): shared kernel, isolated Agents."""

    def __init__(
        self,
        assembled: AssembledRuntime,
        ports: AgentRuntimePorts,
        *,
        owner_scope: str = "default",
    ) -> None:
        self._assembled = assembled
        self._ports = ports
        self._owner_scope = owner_scope

    @property
    def kernel(self) -> Runtime:
        return self._assembled.runtime

    @property
    def driver(self) -> object:
        return self._assembled.driver

    @property
    def uow(self) -> SqliteExecutionUnitOfWork:
        return self._assembled.uow

    @property
    def ports(self) -> AgentRuntimePorts:
        return self._ports

    @property
    def owner_scope(self) -> str:
        return self._owner_scope

    async def __aenter__(self) -> Self:
        await self._assembled.runtime.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        """Stop this process' execution and release control; logical Agents stay durable."""

        await self._assembled.runtime.close()

    async def recover_pending_turns(self) -> None:
        """Wake every Agent with queued/running/result_pending turns (closure E6)."""

        await self._assembled.runtime.recover()

    async def create(self, config: AgentConfig, *, creation_key: str) -> BaseAgent:
        """Create one Agent (no model call).  Same key + same config replays the same Agent."""

        if not isinstance(config, AgentConfig):
            raise TypeError("config must use AgentConfig")
        if not isinstance(creation_key, str) or not creation_key.strip():
            raise ValueError("creation_key is required")
        agent_id = agent_id_for(self._owner_scope, creation_key)
        existing = self.uow.read_agent_binding(agent_id)
        if existing is not None:
            if existing.config_hash != config_hash(config):
                raise ValueError("creation_key reused with a different configuration")
            return BaseAgent(self, existing)
        await self.kernel.start_base_agent_run(
            RunStart(
                ExecutionSessionId(f"base-agent:{agent_id}"),
                RunId(agent_id),
                RequestId(f"{agent_id}:create"),
                f"{agent_id}:start",
                start_input_for(
                    config,
                    agent_id=agent_id,
                    role="root",
                    owner_scope=self._owner_scope,
                    max_output_tokens=self._ports.default_max_output_tokens,
                ),
                1,
            )
        )
        binding = self.uow.create_agent_binding(
            agent_id=agent_id,
            run_id=agent_id,
            owner_scope=self._owner_scope,
            role="root",
            creation_key=creation_key,
            config_json=config.to_json(),
            config_hash=config_hash(config),
            now=self._ports.clock(),
        )
        return BaseAgent(self, binding)

    async def create_many(
        self, configs: Sequence[AgentConfig], *, batch_key: str
    ) -> tuple[BaseAgent, ...]:
        """Slice 1 minimal batch: sequential ``create`` with ``{batch_key}:{index}`` keys."""

        if not isinstance(batch_key, str) or not batch_key.strip():
            raise ValueError("batch_key is required")
        agents = []
        for index, config in enumerate(configs):
            agents.append(await self.create(config, creation_key=f"{batch_key}:{index}"))
        return tuple(agents)

    async def close_agent(
        self, agent_id: str, *, command_id: str, drain_timeout: float = 30.0
    ) -> AgentClosingReceipt:
        """Persist "no new inputs" for one Agent, drain its open turn, never kill the Run.

        Transaction 1 moves ``open -> closing`` and records the command; the drain
        waits (bounded by ``drain_timeout``) for the open turn to finish; transaction
        2 moves ``closing -> closed`` only when nothing is open.  The Run stays
        ``WAITING`` throughout: this is a control-plane intent, not a kernel cancel.
        """

        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("command_id is required")
        if not isinstance(drain_timeout, (int, float)) or isinstance(drain_timeout, bool):
            raise TypeError("drain_timeout must be a number")
        if drain_timeout < 0 or drain_timeout != drain_timeout:
            raise ValueError("drain_timeout must be finite and non-negative")
        binding = self.uow.read_agent_binding(agent_id)
        if binding is None or binding.owner_scope != self._owner_scope:
            raise AgentNotFound(agent_id)
        request_hash = hashlib.sha256(
            canonical_json({"kind": "close", "agent_id": agent_id}).encode("utf-8")
        ).hexdigest()
        clock = self._ports.clock
        try:
            command = self.uow.close_agent(
                agent_id=agent_id,
                command_id=command_id,
                request_hash=request_hash,
                now=clock(),
            )
        except UnitOfWorkConflict as error:
            raise AgentInputConflict(str(error)) from error
        deadline = clock() + float(drain_timeout)
        interval = 0.01
        while True:
            open_turn = self.uow.read_open_agent_turn(binding.run_id)
            if open_turn is None:
                try:
                    latest = self.uow.mark_agent_closed(agent_id=agent_id, now=clock())
                except UnitOfWorkConflict:
                    continue  # a turn opened between the read and the write; loop again
                return AgentClosingReceipt(
                    agent_id=agent_id,
                    command_id=command.command_id,
                    state=latest.lifecycle,
                    open_turn_id=None,
                    control_generation=latest.control_generation,
                    created_at=command.created_at,
                )
            if clock() >= deadline:
                current = self.uow.read_agent_binding(agent_id)
                assert current is not None
                return AgentClosingReceipt(
                    agent_id=agent_id,
                    command_id=command.command_id,
                    state=current.lifecycle,
                    open_turn_id=open_turn.turn_id,
                    control_generation=current.control_generation,
                    created_at=command.created_at,
                )
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)

    async def open(self, agent_id: str | AgentId) -> BaseAgent:
        value = agent_id.value if isinstance(agent_id, AgentId) else agent_id
        binding = self.uow.read_agent_binding(value)
        if binding is None:
            raise AgentNotFound(value)
        return BaseAgent(self, binding)

    def binding(self, agent_id: str) -> AgentBindingRecord | None:
        return self.uow.read_agent_binding(agent_id)


def build_agent_runtime(ports: AgentRuntimePorts, *, owner_scope: str = "default") -> AgentRuntime:
    """Assemble a BaseAgent runtime with no user Memory; use ``async with``.

    ``agent.delegate`` is registered before the tool registry seals and late-bound
    to the ``AgentRuntime`` right after the kernel exists (fixed order, closure risk 3).
    """

    from .tools.delegate import AgentDelegateTool, AgentDelegationReconciliation

    delegate = AgentDelegateTool(clock=ports.clock)
    assembled = assemble_runtime(
        ports,
        extra_tools=(delegate.function_tool(),),
        delegation_counter=lambda turn_id: delegate.runtime.uow.count_agent_delegations(turn_id),
        delegation_reconciliation=AgentDelegationReconciliation,
    )
    runtime = AgentRuntime(assembled, ports, owner_scope=owner_scope)
    delegate.bind(runtime)
    runtime._delegate = delegate  # type: ignore[attr-defined]
    return runtime


__all__ = (
    "AgentRuntime",
    "AssembledRuntime",
    "BASE_AGENT_DRIVER_KIND",
    "CHILD_PROFILE_KEY",
    "agent_id_for",
    "assemble_runtime",
    "build_agent_runtime",
    "start_input_for",
)
