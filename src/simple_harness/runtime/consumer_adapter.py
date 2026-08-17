# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Consumer adapter layer bridging simple consumer ports to SDK kernel ports.

This module provides the missing link between external consumer implementations
and the internal SDK kernel, making it easy for external projects to integrate
Simple Harness SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.providers import (
    ProviderReconciliationObservation,
    ProviderReconciliationState,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
)
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationResult,
    EffectExecutor,
    FunctionTool,
    ToolRegistry,
)
from simple_harness.tools.reconciliation import (
    ReconciliationObservation,
    ReconciliationState,
)

from .context import SqliteContextPort
from .drivers import build_react_driver
from .kernel import Runtime, RuntimePorts, RuntimeProfile, RunClient, build_runtime
from .ports import (
    AuthorizationPort,
    AuthorizationRequest as ConsumerAuthRequest,
    AuthorizationResult as ConsumerAuthResult,
    ProviderPort,
    ToolExecutorPort,
)
from .termination import TerminationLimits


@dataclass(frozen=True, slots=True)
class ConsumerRuntimePorts:
    """Simplified port interface for external consumers.

    This is the high-level interface that consumers implement. The adapter
    layer bridges these simple ports to the complex internal kernel ports.

    Example:
        ports = ConsumerRuntimePorts(
            provider=MyLLMProvider(),
            tool_executor=MyToolExecutor(),
            authorization=MyAuthorization(),
            database_path="/path/to/execution.db",
        )
        runtime = await build_consumer_runtime(ports)
    """

    provider: ProviderPort
    tool_executor: ToolExecutorPort
    authorization: AuthorizationPort
    database_path: str
    tool_names: tuple[str, ...] = ()
    max_turns: int = 50
    max_tool_calls: int = 100
    owner_id: str = field(default_factory=lambda: "consumer-runtime")


class _ConsumerAuthorizationAdapter:
    """Adapts consumer AuthorizationPort to SDK AuthorizationPort."""

    def __init__(self, consumer_port: AuthorizationPort):
        self._port = consumer_port

    async def prepare(self, prepared):  # type: ignore[no-untyped-def]
        """Called before tool execution to check authorization."""
        request = ConsumerAuthRequest(
            tool_call=prepared.call,
            run_id=str(prepared.run_id),
            risk_level=None,
        )

        result = await self._port.request_authorization(request)

        if result.decision == "allow":
            return AuthorizationResult(
                AuthorizationDecision.ALLOW,
                receipt_ref=f"consumer-allow:{prepared.effect_id.value}",
            )
        elif result.decision == "deny":
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                reason_code="user_denied",
                public_message=result.reason or "User denied permission",
            )
        else:  # defer
            return AuthorizationResult(
                AuthorizationDecision.DENY,
                reason_code="user_deferred",
                public_message=result.reason or "User did not respond",
            )

    async def bind_decision(self, prepared, request, decision, sdk_receipt):  # type: ignore[no-untyped-def]
        """Bind authorization decision."""
        return AuthorizationReceipt(
            receipt_ref=f"consumer-decision:{prepared.effect_id.value}",
            receipt_hash=sdk_receipt.receipt_hash,
            bound_sdk_receipt_hash=sdk_receipt.receipt_hash,
        )

    async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):  # type: ignore[no-untyped-def]
        """Bind effect handoff."""
        return AuthorizationReceipt(
            receipt_ref=f"consumer-handoff:{prepared.effect_id.value}",
            receipt_hash=sdk_receipt.receipt_hash,
            bound_sdk_receipt_hash=sdk_receipt.receipt_hash,
        )


class _ConsumerProviderAdapter:
    """Adapts consumer ProviderPort to SDK provider interface."""

    def __init__(self, consumer_port: ProviderPort):
        self._port = consumer_port
        self.target = ProviderTarget(
            provider_id="consumer",
            model="consumer-model",
            pricing_key="consumer",
            endpoint_identity="consumer-endpoint",
            adapter_key="consumer-adapter",
        )

    async def invoke(
        self,
        request: ProviderRequest,
        *,
        cancel,
    ) -> ProviderResponse:
        """Forward to consumer provider."""
        return await self._port.invoke(request, cancel=cancel)


class _ConsumerToolExecutorAdapter:
    """Adapts consumer ToolExecutorPort to SDK tool registry."""

    def __init__(self, consumer_port: ToolExecutorPort, tool_names: tuple[str, ...]):
        self._port = consumer_port
        self._tool_names = tool_names

    def build_registry(self) -> ToolRegistry:
        """Build tool registry with placeholder specs."""
        from simple_harness.tools import ToolSpec

        tools = []

        for name in self._tool_names:
            # Create tool spec
            spec = ToolSpec(
                name=name,
                description=f"Tool: {name}",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )

            # Create handler closure
            def make_handler(tool_name: str):
                async def handler(arguments: dict, context):  # type: ignore[no-untyped-def]
                    from simple_harness.tools import ToolCall
                    call = ToolCall(context.call_id, tool_name, arguments)
                    return await self._port.execute(call, {})
                return handler

            tool = FunctionTool(
                spec=spec,
                handler=make_handler(name),
            )
            tools.append(tool)

        return ToolRegistry(tuple(tools))


class _DefaultToolReconciliation:
    """Default tool reconciliation that marks everything as unknown."""

    async def observe(self, effect):  # type: ignore[no-untyped-def]
        return ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN,
            f"consumer-tool-unknown:{effect.effect_id.value}",
        )


class _DefaultProviderReconciliation:
    """Default provider reconciliation that marks everything as unknown."""

    async def observe(self, invocation):  # type: ignore[no-untyped-def]
        return ProviderReconciliationObservation(
            ProviderReconciliationState.STILL_UNKNOWN,
            f"consumer-provider-unknown:{invocation.invocation_id}",
        )


class _DefaultRuntimeReconciliation:
    """Default runtime reconciliation (no-op)."""

    async def reconcile(self) -> None:
        return None


class _DefaultToolCatalog:
    """Default tool catalog with fixed generation."""

    def current_generation(self) -> int:
        return 1


class _DefaultDeliverySink:
    """Default delivery sink (no-op)."""

    async def deliver(self, payload, *, idempotency_key):  # type: ignore[no-untyped-def]
        pass  # Consumer runtime doesn't need delivery


async def build_consumer_runtime(ports: ConsumerRuntimePorts) -> Runtime:
    """Build a Runtime from consumer-provided simple ports.

    This is the main entry point for external consumers. It bridges the simple
    ConsumerRuntimePorts interface to the complex internal RuntimePorts interface.

    Args:
        ports: Consumer port implementations

    Returns:
        Ready-to-use Runtime instance. Use RunClient(runtime) to start runs.

    Example:
        ports = ConsumerRuntimePorts(
            provider=MyLLMProvider(),
            tool_executor=MyToolExecutor(),
            authorization=MyAuthorization(),
            database_path="/path/to/execution.db",
            tool_names=("read_file", "web_search"),
        )

        runtime = await build_consumer_runtime(ports)
        await runtime.__aenter__()

        try:
            # Create run client
            client = RunClient(runtime)

            # Start a run
            await client.start(run_start)
            await runtime.wait_idle(run_id)
        finally:
            await runtime.__aexit__(None, None, None)
    """

    # Open database
    database = Database.open(ports.database_path)
    uow = SqliteExecutionUnitOfWork(database)

    # Build tool registry
    tool_adapter = _ConsumerToolExecutorAdapter(ports.tool_executor, ports.tool_names)
    tool_registry = tool_adapter.build_registry()

    # Build authorization adapter
    auth_adapter = _ConsumerAuthorizationAdapter(ports.authorization)

    # Build effect executor
    tool_reconciliation = _DefaultToolReconciliation()
    effects = EffectExecutor(
        uow=uow,
        registry=tool_registry,
        authorization=auth_adapter,
        reconciliation=tool_reconciliation,
    )

    # Build provider coordinator
    provider_adapter = _ConsumerProviderAdapter(ports.provider)
    estimator = FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)
    budget_policy = BudgetPolicy()
    provider_coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider_adapter,
        budget_policy=budget_policy,
        estimator=estimator,
    )

    # Build context port
    context = SqliteContextPort(database)

    # Build runtime ports
    runtime_ports = RuntimePorts(
        provider=provider_coordinator,
        tools=effects,
        authorization=auth_adapter,
        context=context,
        delivery=DeliveryDispatcher(uow, {"consumer": _DefaultDeliverySink()}),
        tool_reconciliation=tool_reconciliation,
        reconciliation=_DefaultRuntimeReconciliation(),
        provider_reconciliation=_DefaultProviderReconciliation(),
        react_checkpoint=uow,
        tool_catalog=_DefaultToolCatalog(),
        owner_id=ports.owner_id,
    )

    # Build ReAct driver
    driver = build_react_driver(
        limits=TerminationLimits(
            max_turns=ports.max_turns,
            max_tool_calls=ports.max_tool_calls,
        ),
        budget_policy=budget_policy,
        estimator=estimator,
    )

    # Build runtime
    runtime = build_runtime(
        uow=uow,
        profiles={"agent.general": RuntimeProfile("agent.general", "react")},
        drivers={"react": driver},
        ports=runtime_ports,
    )

    return runtime


__all__ = (
    "ConsumerRuntimePorts",
    "build_consumer_runtime",
)
