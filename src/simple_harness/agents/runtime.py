# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BaseAgent runtime assembly (Slice 1).

Built on the consumer composition (``consumer_adapter``), never on
``build_production_runtime``: no ``agent_memory``, no conversation Memory, no
Memory outbox consumer, no context staging.  Nothing here may import
``simple_harness_memory``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
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
from simple_harness.tools import EffectExecutor, FunctionTool, Tool, ToolRegistry

from .execution import build_agent_execution_driver
from .ports import AgentRuntimePorts

ROOT_PROFILE_KEY = "agent.general"
CHILD_PROFILE_KEY = "agent.base"
BASE_AGENT_DRIVER_KIND = "base_agent"


@dataclass(frozen=True, slots=True)
class AssembledRuntime:
    runtime: Runtime
    uow: SqliteExecutionUnitOfWork
    database: Database


def assemble_runtime(
    ports: AgentRuntimePorts,
    *,
    extra_tools: tuple[FunctionTool, ...] = (),
    delegation_counter=None,  # type: ignore[no-untyped-def]
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
    registry = ToolRegistry((*tools, *cast(tuple[Tool, ...], extra_tools)))
    auth_adapter = _ConsumerAuthorizationAdapter(ports.authorization)
    tool_reconciliation = ports.policies.tool_reconciliation or _DefaultToolReconciliation()
    effects = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=auth_adapter,
        reconciliation=tool_reconciliation,
    )
    provider_adapter = _ConsumerProviderAdapter(ports.provider, ports.model)
    # The consumer provider adapter reports pricing_key "consumer"; the estimator must match.
    estimator = ports.policies.estimator or FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)
    budget_policy = ports.policies.budget_policy
    provider_coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider_adapter,
        budget_policy=budget_policy,
        estimator=estimator,
        context_use_authority=None,
    )
    context = SqliteContextPort(database, clock=ports.clock)
    runtime_ports = RuntimePorts(
        provider=provider_coordinator,
        tools=effects,
        authorization=auth_adapter,
        context=context,
        delivery=DeliveryDispatcher(uow, {}),
        tool_reconciliation=tool_reconciliation,
        reconciliation=(ports.policies.runtime_reconciliation or _DefaultRuntimeReconciliation()),
        provider_reconciliation=(
            ports.policies.provider_reconciliation or _DefaultProviderReconciliation()
        ),
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
    return AssembledRuntime(runtime, uow, database)


__all__ = ("AssembledRuntime", "BASE_AGENT_DRIVER_KIND", "CHILD_PROFILE_KEY", "assemble_runtime")
