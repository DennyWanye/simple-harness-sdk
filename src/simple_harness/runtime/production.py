# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict production composition with explicit authorities and ownership."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.memory_outbox import MemoryDispatcher, MemoryOutboxRepository
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import ProviderReconciliationPort
from simple_harness.tools.authorization import AuthorizationPort
from simple_harness.tools.executor import EffectExecutor
from simple_harness.tools.reconciliation import ToolReconciliationPort

from .agent_memory import (
    AgentMemoryPort,
    MemoryFailurePolicy,
    ResourceOwnership,
)
from .context import ContextPort
from .conversation_context_provider import (
    ConversationContextProviderPort,
    CurrentMessageContextProvider,
)
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


class ManagedProjectionPump(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProductionAuthorities:
    context_staging: ContextStagingRepository
    memory: AgentMemoryPort
    provider_budget_resolver: object
    provider_projection_pump: ManagedProjectionPump
    run_binding: object
    structured_message_services: object


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    execution_path: str | Path
    provider_builder: Callable[[SqliteExecutionUnitOfWork], ProviderInvocationCoordinator]
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
    memory: AgentMemoryPort
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
    memory_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    memory_failure_policy: MemoryFailurePolicy = MemoryFailurePolicy.DEGRADE_RECALL_AND_RETRY_RECORD
    context_provider: ConversationContextProviderPort = CurrentMessageContextProvider()

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
            "memory",
            "provider_budget_resolver",
            "provider_projection_pump",
            "run_binding",
            "structured_message_services",
        )
        for name in required:
            if getattr(self, name) is None:
                raise TypeError(f"{name} is required for production composition")
        for method_name in ("recall_for_turn", "release_recall", "record_committed_turn"):
            if not callable(getattr(self.memory, method_name, None)):
                raise TypeError(f"memory must implement {method_name}")
        if not isinstance(self.execution_path, (str, Path)):
            raise TypeError("execution_path must be str or Path")
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if "agent.general" not in self.profiles:
            raise ValueError("production profiles require agent.general")
        object.__setattr__(self, "memory_ownership", ResourceOwnership(self.memory_ownership))
        object.__setattr__(
            self, "memory_failure_policy", MemoryFailurePolicy(self.memory_failure_policy)
        )


def build_production_runtime(config: ProductionRuntimeConfig) -> Runtime:
    """Build, but do not start, a fully explicit production Runtime."""

    if not isinstance(config, ProductionRuntimeConfig):
        raise TypeError("config must use ProductionRuntimeConfig")
    memory_path = _memory_path(config.memory)
    execution_path = Path(config.execution_path).expanduser().resolve()
    if memory_path is not None and memory_path == execution_path:
        raise ValueError("execution and Memory databases must use different resolved paths")
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
            memory_dispatcher=MemoryDispatcher(
                MemoryOutboxRepository(database),
                config.memory,
                owner_id=config.owner_id,
                clock=config.clock,
                lease_seconds=config.lease_ttl_seconds,
            ),
            conversation_memory_enabled=True,
            context_staging=staging,
            context_preparation_mode=ContextPreparationMode.SDK_PREPARED,
            agent_memory=config.memory,
            context_provider=config.context_provider,
            memory_failure_policy=config.memory_failure_policy,
        )
        root_profile = config.profiles["agent.general"]
        memory_close_hooks: tuple[Callable[[], Awaitable[None]], ...] = ()
        if config.memory_ownership is ResourceOwnership.RUNTIME:
            close = getattr(config.memory, "close", None)
            if not callable(close):
                raise TypeError("runtime-owned memory must expose close()")

            async def close_memory() -> None:
                outcome = close()
                if hasattr(outcome, "__await__"):
                    await outcome

            memory_close_hooks = (close_memory,)
        runtime = build_runtime(
            uow=uow,  # type: ignore[arg-type]
            profiles=config.profiles,
            drivers={root_profile.driver_kind: config.driver},
            ports=ports,
            workflow_runner=config.workflow_runner,
            close_hook=uow.close,
            start_hooks=(config.provider_projection_pump.start,),
            async_close_hooks=(
                *memory_close_hooks,
                config.provider_projection_pump.close,
            ),
        )
        runtime._production_authorities = ProductionAuthorities(
            context_staging=staging,
            memory=config.memory,
            provider_budget_resolver=config.provider_budget_resolver,
            provider_projection_pump=config.provider_projection_pump,
            run_binding=config.run_binding,
            structured_message_services=config.structured_message_services,
        )
        return runtime
    except BaseException:
        uow.close()
        if config.memory_ownership is ResourceOwnership.RUNTIME:
            _close_owned_memory_after_build_failure(config.memory)
        raise


def _close_owned_memory_after_build_failure(memory: AgentMemoryPort) -> None:
    """Begin cleanup without changing the legacy synchronous builder signature."""

    close = getattr(memory, "close", None)
    if not callable(close):
        return
    outcome = close()
    if not inspect.isawaitable(outcome):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        async def await_close() -> None:
            await outcome

        asyncio.run(await_close())
    else:
        task = asyncio.ensure_future(outcome, loop=loop)
        task.add_done_callback(lambda completed: completed.exception())


def _memory_path(memory: AgentMemoryPort) -> Path | None:
    for value in (
        getattr(memory, "db_path", None),
        getattr(memory, "path", None),
        getattr(getattr(memory, "database", None), "path", None),
    ):
        if isinstance(value, (str, Path)):
            return Path(value).expanduser().resolve()
    return None


__all__ = (
    "ManagedProjectionPump",
    "ProductionAuthorities",
    "ProductionRuntimeConfig",
    "build_production_runtime",
)
