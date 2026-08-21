# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict production composition with explicit authorities and ownership."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.memory_outbox import (
    MemoryDispatcher,
    MemoryOutboxRepository,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import ProviderReconciliationPort
from simple_harness.tools.authorization import AuthorizationPort
from simple_harness.tools.executor import EffectExecutor
from simple_harness.tools.reconciliation import ToolReconciliationPort

from .context import ContextPort
from .conversation_memory import ContextPreparationMode
from .kernel import (
    Runtime,
    RuntimeDriver,
    RuntimePorts,
    RuntimeProfile,
    RuntimeReconciliationPort,
    ToolCatalogGenerationPort,
    build_runtime,
)
from .ports import ConversationMemoryQueryPort, ConversationMemorySinkPort


class ManagedProjectionPump(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProductionAuthorities:
    context_staging: ContextStagingRepository
    conversation_query: ConversationMemoryQueryPort
    provider_budget_resolver: object
    provider_projection_pump: ManagedProjectionPump
    run_binding: object
    structured_message_services: object


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    execution_path: str | Path
    provider_builder: Callable[
        [SqliteExecutionUnitOfWork], ProviderInvocationCoordinator
    ]
    tools_builder: Callable[[SqliteExecutionUnitOfWork], EffectExecutor]
    delivery_builder: Callable[[SqliteExecutionUnitOfWork], DeliveryDispatcher]
    context_builder: Callable[[Database], ContextPort]
    context_staging_builder: Callable[[Database], ContextStagingRepository]
    authorization: AuthorizationPort
    tool_reconciliation: ToolReconciliationPort
    reconciliation: RuntimeReconciliationPort
    provider_reconciliation: ProviderReconciliationPort
    tool_catalog: ToolCatalogGenerationPort
    driver: RuntimeDriver
    profiles: Mapping[str, RuntimeProfile]
    conversation_query: ConversationMemoryQueryPort
    conversation_sink: ConversationMemorySinkPort
    context_preparation_mode: ContextPreparationMode
    provider_budget_resolver: object
    provider_projection_pump: ManagedProjectionPump
    run_binding: object
    structured_message_services: object
    owner_id: str
    workflow_runner: object | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    lease_ttl_seconds: float = 30.0
    close_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        required = (
            "provider_builder",
            "tools_builder",
            "delivery_builder",
            "context_builder",
            "context_staging_builder",
            "authorization",
            "tool_reconciliation",
            "reconciliation",
            "provider_reconciliation",
            "tool_catalog",
            "driver",
            "conversation_query",
            "conversation_sink",
            "provider_budget_resolver",
            "provider_projection_pump",
            "run_binding",
            "structured_message_services",
        )
        for name in required:
            if getattr(self, name) is None:
                raise TypeError(f"{name} is required for production composition")
        if not isinstance(self.execution_path, (str, Path)):
            raise TypeError("execution_path must be str or Path")
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if "agent.general" not in self.profiles:
            raise ValueError("production profiles require agent.general")
        object.__setattr__(
            self,
            "context_preparation_mode",
            ContextPreparationMode(self.context_preparation_mode),
        )


def build_production_runtime(config: ProductionRuntimeConfig) -> Runtime:
    """Build, but do not start, a fully explicit production Runtime."""

    if not isinstance(config, ProductionRuntimeConfig):
        raise TypeError("config must use ProductionRuntimeConfig")
    database = Database.open(config.execution_path, wal=True)
    uow = SqliteExecutionUnitOfWork(database)
    try:
        provider = config.provider_builder(uow)
        tools = config.tools_builder(uow)
        delivery = config.delivery_builder(uow)
        context = config.context_builder(database)
        for value, name in (
            (provider, "provider"),
            (tools, "tools"),
            (delivery, "delivery"),
            (context, "context"),
        ):
            if value is None:
                raise TypeError(f"{name} builder returned None")
        staging = config.context_staging_builder(database)
        if not isinstance(staging, ContextStagingRepository):
            raise TypeError("context_staging_builder returned an invalid repository")
        memory = MemoryDispatcher(
            MemoryOutboxRepository(database),
            config.conversation_sink,
            owner_id=f"{config.owner_id}:memory",
            clock=config.clock,
            lease_seconds=config.lease_ttl_seconds,
        )
        ports = RuntimePorts(
            provider=provider,
            tools=tools,
            authorization=config.authorization,
            context=context,
            delivery=delivery,
            tool_reconciliation=config.tool_reconciliation,
            reconciliation=config.reconciliation,
            provider_reconciliation=config.provider_reconciliation,
            react_checkpoint=uow,
            tool_catalog=config.tool_catalog,
            clock=config.clock,
            sleep=config.sleep,
            owner_id=config.owner_id,
            lease_ttl_seconds=config.lease_ttl_seconds,
            close_timeout_seconds=config.close_timeout_seconds,
            conversation_memory_enabled=True,
            memory_dispatcher=memory,
            context_staging=staging,
            context_preparation_mode=config.context_preparation_mode,
        )
        root_profile = config.profiles["agent.general"]
        runtime = build_runtime(
            uow=uow,  # type: ignore[arg-type]
            profiles=config.profiles,
            drivers={root_profile.driver_kind: config.driver},
            ports=ports,
            workflow_runner=config.workflow_runner,
            close_hook=uow.close,
            start_hooks=(config.provider_projection_pump.start,),
            async_close_hooks=(
                config.conversation_query.close,
                config.provider_projection_pump.close,
            ),
        )
        runtime._production_authorities = ProductionAuthorities(
            context_staging=staging,
            conversation_query=config.conversation_query,
            provider_budget_resolver=config.provider_budget_resolver,
            provider_projection_pump=config.provider_projection_pump,
            run_binding=config.run_binding,
            structured_message_services=config.structured_message_services,
        )
        return runtime
    except BaseException:
        uow.close()
        raise


__all__ = (
    "ManagedProjectionPump",
    "ProductionAuthorities",
    "ProductionRuntimeConfig",
    "build_production_runtime",
)
