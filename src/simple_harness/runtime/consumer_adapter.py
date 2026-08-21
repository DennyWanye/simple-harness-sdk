# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Official production consumer composition for Simple Harness SDK."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.context_authority import ToolCatalogSnapshot
from simple_harness.execution.context_staging import ContextStagingRepository
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySink
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.memory_outbox import MemoryDispatcher, MemoryOutboxRepository
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.providers import (
    ProviderReconciliationObservation,
    ProviderReconciliationPort,
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
    ToolReconciliationPort,
)

from .agent_memory import AgentMemoryPort, MemoryFailurePolicy, ResourceOwnership
from .context import SqliteContextPort
from .conversation_context_provider import (
    ConversationContextProviderPort,
    CurrentMessageContextProvider,
)
from .conversation_memory import ContextPreparationMode
from .drivers import build_react_driver
from .kernel import (
    Runtime,
    RuntimePorts,
    RuntimeProfile,
    RuntimeReconciliationPort,
    build_runtime,
)
from .ports import (
    AuthorizationPort,
    ProviderPort,
    ToolExecutorPort,
)
from .ports import (
    AuthorizationRequest as ConsumerAuthRequest,
)
from .termination import TerminationLimits


@dataclass(frozen=True, slots=True)
class ConsumerRuntimePolicies:
    """Explicit defaults for local, unpriced and fail-closed operation."""

    pricing_mode: str
    external_delivery: bool
    unknown_side_effect: str
    estimator: FrozenPriceEstimator | None = None
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    tool_reconciliation: ToolReconciliationPort | None = None
    provider_reconciliation: ProviderReconciliationPort | None = None
    runtime_reconciliation: RuntimeReconciliationPort | None = None

    @classmethod
    def local_default(cls) -> ConsumerRuntimePolicies:
        return cls("unpriced_local", False, "fail_closed")

    def __post_init__(self) -> None:
        if self.pricing_mode not in {"unpriced_local", "consumer_supplied"}:
            raise ValueError("pricing_mode is invalid")
        if self.unknown_side_effect not in {"fail_closed", "consumer_reconciles"}:
            raise ValueError("unknown_side_effect is invalid")
        if self.pricing_mode == "consumer_supplied" and self.estimator is None:
            raise ValueError("consumer_supplied pricing requires estimator")
        if self.unknown_side_effect == "consumer_reconciles" and any(
            value is None
            for value in (
                self.tool_reconciliation,
                self.provider_reconciliation,
                self.runtime_reconciliation,
            )
        ):
            raise ValueError("consumer_reconciles requires all reconciliation authorities")


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
    # The model name the consumer's ProviderPort echoes back in
    # ProviderResponse.model. The kernel only trusts usage when
    # response.model == target.model, so this must match the provider's echo.
    model: str = "consumer-model"
    # Optional closed input schemas keyed by tool name. Tools without a
    # declared schema keep the fail-closed no-argument default.
    tool_schemas: Mapping[str, dict] = field(default_factory=dict)
    memory: AgentMemoryPort | None = None
    memory_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    memory_failure_policy: MemoryFailurePolicy = MemoryFailurePolicy.DEGRADE_RECALL_AND_RETRY_RECORD
    context_provider: ConversationContextProviderPort = field(
        default_factory=CurrentMessageContextProvider
    )
    policies: ConsumerRuntimePolicies = field(default_factory=ConsumerRuntimePolicies.local_default)

    def __post_init__(self) -> None:
        object.__setattr__(self, "memory_ownership", ResourceOwnership(self.memory_ownership))
        object.__setattr__(
            self, "memory_failure_policy", MemoryFailurePolicy(self.memory_failure_policy)
        )
        if not isinstance(self.policies, ConsumerRuntimePolicies):
            raise TypeError("policies must use ConsumerRuntimePolicies")
        if self.memory is not None:
            for method_name in ("recall_for_turn", "release_recall", "record_committed_turn"):
                if not callable(getattr(self.memory, method_name, None)):
                    raise TypeError(f"memory must implement {method_name}")
            if not callable(getattr(self.context_provider, "prepare_once", None)):
                raise TypeError("context_provider must implement prepare_once")


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

    def __init__(self, consumer_port: ProviderPort, model: str):
        self._port = consumer_port
        self.target = ProviderTarget(
            provider_id="consumer",
            model=model,
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

    def __init__(
        self,
        consumer_port: ToolExecutorPort,
        tool_names: tuple[str, ...],
        tool_schemas: Mapping[str, dict],
    ):
        self._port = consumer_port
        self._tool_names = tool_names
        self._tool_schemas = tool_schemas

    def build_registry(self) -> ToolRegistry:
        """Build tool registry with placeholder specs."""
        from simple_harness.tools import ToolSpec

        tools = []

        for name in self._tool_names:
            # Use the consumer-declared closed schema when present; otherwise
            # keep the fail-closed no-argument default (the SDK schema subset
            # forbids additionalProperties, so a tool with no declared schema
            # accepts exactly zero arguments).
            input_schema = self._tool_schemas.get(name) or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            spec = ToolSpec(
                name=name,
                description=f"Tool: {name}",
                input_schema=input_schema,
            )

            # Create handler closure
            def make_handler(tool_name: str):
                async def handler(arguments: dict, context):  # type: ignore[no-untyped-def]
                    from simple_harness.tools import ToolCall

                    call = ToolCall(context.call_id, tool_name, arguments)
                    tool_ctx = {
                        "run_id": str(context.run_id),
                        "request_id": str(context.request_id),
                        "call_id": str(context.call_id) if context.call_id else None,
                    }
                    return await self._port.execute(call, tool_ctx)

                return handler

            tool = FunctionTool(
                spec=spec,
                handler=make_handler(name),
            )
            tools.append(tool)

        return ToolRegistry(tuple(tools))  # type: ignore[arg-type]


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

    def resolve(self, generation: int, content_fingerprint: str) -> ToolCatalogSnapshot | None:
        del generation, content_fingerprint
        return None


async def _build_consumer_runtime(
    ports: ConsumerRuntimePorts,
    delivery_sinks: Mapping[str, DeliverySink] | None = None,
) -> Runtime:
    """Build the official production Runtime from consumer-provided ports.

    Agent Memory is optional. When supplied, the Runtime owns recall staging,
    retry/recovery orchestration and committed-turn delivery. Resources are
    borrowed unless ``memory_ownership=RUNTIME`` is explicit.

    Args:
        ports: Consumer port implementations
        delivery_sinks: Optional mapping of sink kind → DeliverySink. When
            omitted, no delivery sink is registered and deliveries stay PENDING
            (nothing is silently marked DELIVERED). A no-op sink is available
            in ``simple_harness.testing`` for tests only.

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

    if ports.memory is not None:
        memory_path = _memory_path(ports.memory)
        execution_path = Path(ports.database_path).expanduser().resolve()
        if memory_path is not None and execution_path == memory_path:
            raise ValueError("execution and Memory databases must use different resolved paths")

    # Open database
    database = Database.open(ports.database_path)
    uow = SqliteExecutionUnitOfWork(database)

    # Build tool registry
    tool_adapter = _ConsumerToolExecutorAdapter(
        ports.tool_executor, ports.tool_names, ports.tool_schemas
    )
    tool_registry = tool_adapter.build_registry()

    # Build authorization adapter
    auth_adapter = _ConsumerAuthorizationAdapter(ports.authorization)

    # Build effect executor
    tool_reconciliation = ports.policies.tool_reconciliation or _DefaultToolReconciliation()
    effects = EffectExecutor(
        uow=uow,
        registry=tool_registry,
        authorization=auth_adapter,
        reconciliation=tool_reconciliation,
    )

    # Build provider coordinator
    provider_adapter = _ConsumerProviderAdapter(ports.provider, ports.model)
    estimator = ports.policies.estimator or FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)
    budget_policy = ports.policies.budget_policy
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
        delivery=DeliveryDispatcher(uow, dict(delivery_sinks or {})),
        tool_reconciliation=tool_reconciliation,
        reconciliation=(
            ports.policies.runtime_reconciliation or _DefaultRuntimeReconciliation()
        ),
        provider_reconciliation=(
            ports.policies.provider_reconciliation or _DefaultProviderReconciliation()
        ),
        react_checkpoint=uow,
        tool_catalog=_DefaultToolCatalog(),
        owner_id=ports.owner_id,
        memory_dispatcher=(
            None
            if ports.memory is None
            else MemoryDispatcher(
                MemoryOutboxRepository(database),
                ports.memory,
                owner_id=ports.owner_id,
                clock=time.time,
            )
        ),
        conversation_memory_enabled=ports.memory is not None,
        context_staging=(None if ports.memory is None else ContextStagingRepository(database)),
        context_preparation_mode=(
            None if ports.memory is None else ContextPreparationMode.SDK_PREPARED
        ),
        agent_memory=ports.memory,
        context_provider=ports.context_provider,
        memory_failure_policy=ports.memory_failure_policy,
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
    async_close_hooks: tuple[Callable[[], Awaitable[None]], ...] = ()
    if ports.memory is not None and ports.memory_ownership is ResourceOwnership.RUNTIME:
        close = getattr(ports.memory, "close", None)
        if not callable(close):
            uow.close()
            raise TypeError("runtime-owned memory must expose close()")

        async def close_memory() -> None:
            outcome = close()
            if hasattr(outcome, "__await__"):
                await outcome

        async_close_hooks = (close_memory,)
    runtime = build_runtime(
        uow=uow,  # type: ignore[arg-type]
        profiles={"agent.general": RuntimeProfile("agent.general", "react")},
        drivers={"react": driver},
        ports=runtime_ports,
        close_hook=uow.close,
        async_close_hooks=async_close_hooks,
    )

    return runtime


async def build_consumer_runtime(
    ports: ConsumerRuntimePorts,
    delivery_sinks: Mapping[str, DeliverySink] | None = None,
) -> Runtime:
    """Build the official Runtime and clean up an owned Memory on failure."""

    try:
        return await _build_consumer_runtime(ports, delivery_sinks)
    except BaseException:
        if ports.memory is not None and ports.memory_ownership is ResourceOwnership.RUNTIME:
            close = getattr(ports.memory, "close", None)
            if callable(close):
                outcome = close()
                if hasattr(outcome, "__await__"):
                    await outcome
        raise


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
    "ConsumerRuntimePolicies",
    "ConsumerRuntimePorts",
    "build_consumer_runtime",
)
