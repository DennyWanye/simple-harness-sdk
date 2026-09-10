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
from typing import Any, Self, cast

from simple_harness.contracts import (
    ExecutionSessionId,
    JsonValue,
    Message,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.base_agent import BASE_AGENT_API_MODE, AgentBindingRecord
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.base_agent.turns import (
    AgentInstanceCapExceeded as InstanceCapConflict,
)
from simple_harness.execution.sqlite.database import ExecutionSchemaIncompatible
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime.consumer_adapter import (
    _ConsumerAuthorizationAdapter,
    _ConsumerProviderAdapter,
    _ConsumerToolExecutorAdapter,
    _DefaultProviderReconciliation,
    _DefaultRuntimeReconciliation,
    _DefaultToolCatalog,
    _DefaultToolReconciliation,
)
from simple_harness.runtime.kernel import Runtime, RuntimePorts, RuntimeProfile, build_runtime
from simple_harness.runtime.start_snapshot import RunStart
from simple_harness.tools import EffectExecutor, FunctionTool, Tool
from simple_harness.tools.contracts import CancellationToken

from .base import BaseAgent
from .config import AgentConfig, config_hash
from .context.port import JournalContextPort, RequestGuard
from .context.tokenizer import UpperBoundTokenizer
from .contracts import (
    AgentBatchIdentityConflict,
    AgentBatchRejected,
    AgentCancelReceipt,
    AgentClosingReceipt,
    AgentId,
    AgentInputConflict,
    AgentInstanceCapExceeded,
    AgentNotFound,
    AgentTurnNotFound,
)
from .execution import AGENT_TURN_CANCELLED, build_agent_execution_driver
from .memory.index_jobs import SessionIndexer
from .memory.retrieval import SearchResult, SessionRetriever
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
    tool_names: tuple[str, ...] = ()
    context: object = None
    retriever: object = None
    indexer: object = None


def assemble_runtime(
    ports: AgentRuntimePorts,
    *,
    extra_tools: tuple[FunctionTool, ...] = (),
    delegation_counter=None,  # type: ignore[no-untyped-def]
    delegation_reconciliation=None,  # type: ignore[no-untyped-def]
) -> AssembledRuntime:
    """Compose the kernel for BaseAgents; root and child profiles both drive ``base_agent``."""

    database = Database.open(ports.database_path)
    if database.schema_version < 10:
        database.close()
        raise ExecutionSchemaIncompatible(
            "execution database requires schema v10 for BaseAgents; "
            "use migrate_execution_to_v10 (after migrate_execution_to_v9)"
        )
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
        (*tools, *cast(tuple[Tool, ...], extra_tools)),
        exposure_reader=_exposure,
        max_concurrent=ports.max_concurrent_tool_calls,
    )
    # An SDK-native authorization port (prepare/bind_decision, able to require a
    # durable user decision) is used as-is; the consumer port is adapted.
    auth_adapter: Any = (
        ports.authorization
        if callable(getattr(ports.authorization, "bind_decision", None))
        and callable(getattr(ports.authorization, "prepare", None))
        else _ConsumerAuthorizationAdapter(ports.authorization)
    )
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
    tokenizer = ports.tokenizer or UpperBoundTokenizer()
    guard = RequestGuard(uow, tokenizer=tokenizer, policy=ports.context_policy, clock=ports.clock)
    wire = AgentProviderWire(
        ports.provider,
        database,
        request_guard=guard,
        max_concurrent=ports.max_concurrent_model_calls,
    )
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

    # BaseAgent Runs use the Journal-backed bounded Context (Slice 3); legacy Runs
    # never reach this runtime, so ``SqliteContextPort`` stays untouched for them.
    def _tool_specs_for_run(run_id: str) -> tuple[ProviderToolSpec, ...]:
        exposed = registry.exposed_tools(run_id)
        names = tuple(sorted(exposed)) if exposed is not None else ()
        return effects.provider_tool_specs(names) if names else ()

    fts_available = uow.ensure_agent_fts()
    retriever = SessionRetriever(
        uow, embedding=ports.embedding, fts_available=fts_available, clock=ports.clock
    )
    indexer = SessionIndexer(
        uow,
        embedding=ports.embedding,
        fts_available=fts_available,
        owner=ports.owner_id,
        clock=ports.clock,
    )
    recall_messages = _RecallAdapter(retriever, tokenizer, ports.recall_limit)
    context = JournalContextPort(
        uow,
        tokenizer=tokenizer,
        policy=ports.context_policy,
        model=ports.model,
        tool_specs_for_run=_tool_specs_for_run,
        clock=ports.clock,
        recall=recall_messages,
        recall_token_share=ports.recall_token_share,
        on_records=indexer.on_records,
    )
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
    turn_cancellations: dict[str, CancellationToken] = {}
    driver = build_agent_execution_driver(
        limits=ports.termination_limits,
        budget_policy=budget_policy,
        estimator=estimator,
        delegation_counter=delegation_counter,
        clock=ports.clock,
        turn_cancellations=turn_cancellations,
        empty_response_retries=ports.empty_response_retries,
        max_output_tokens_ceiling=ports.max_output_tokens_ceiling,
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
    return AssembledRuntime(
        runtime,
        uow,
        database,
        driver,
        wire,
        tuple(spec.name for spec in registry.specs),
        context,
        retriever,
        indexer,
    )


def batch_fingerprint(owner_scope: str, batch_key: str, configs: Sequence[AgentConfig]) -> str:
    """Identity of one ``create_many`` call: owner, key, count and ordered config hashes."""

    payload: dict[str, JsonValue] = {
        "owner_scope": owner_scope,
        "batch_key": batch_key,
        "count": len(configs),
        "config_hashes": [config_hash(config) for config in configs],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


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
            # Per-turn guard-rails travel with the frozen start input so the driver
            # never reads the agents' configuration tables (T6).
            "limits": {
                "max_model_calls_per_turn": config.limits.max_model_calls_per_turn,
                "max_tool_calls_per_turn": config.limits.max_tool_calls_per_turn,
                "turn_deadline_seconds": config.limits.turn_deadline_seconds,
            },
        },
        "messages": messages,
        "max_output_tokens": max_output_tokens,
    }


class _RecallAdapter:
    """Turns retriever hits into bounded, derived recall messages (never Journal rows)."""

    def __init__(self, retriever: SessionRetriever, tokenizer: object, limit: int) -> None:
        self._retriever = retriever
        self._tokenizer = tokenizer
        self._limit = limit
        self.last_result: SearchResult | None = None

    async def prewarm(self, query: str) -> None:
        await self._retriever.prewarm(query)

    @property
    def index_generation(self) -> str | None:
        return self._retriever.embedding_fingerprint

    def __call__(
        self, agent_id: str, query: str, token_budget: int, exclude: tuple[int, ...]
    ) -> tuple[Message, ...]:
        if self._limit <= 0 or token_budget <= 0:
            return ()
        # Synchronous on purpose: the ContextPort is called inside the driver and the
        # SQLite connection is single-threaded; only the one query embedding blocks.
        result = self._retriever.search_sync(
            agent_id, query, limit=self._limit, exclude_seqs=exclude
        )
        self.last_result = result
        if not result.hits:
            return ()
        from .context.tokenizer import count_message

        messages: list[Message] = []
        used = 0
        for hit in result.hits:
            text = (
                f"[会话召回 seq {hit.seq} · {hit.kind} · 来源 {','.join(hit.sources)}]\n{hit.text}"
            )
            message = Message(
                MessageRole.SYSTEM,
                text,
                metadata={
                    "derived": True,
                    "recall": True,
                    "source_seq": hit.seq,
                    "source_hash": hit.content_hash,
                    "query_hash": result.query_hash,
                },
            )
            cost = count_message(self._tokenizer, message)  # type: ignore[arg-type]
            if used + cost > token_budget:
                break
            used += cost
            messages.append(message)
        return tuple(messages)


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
        self._index_task: asyncio.Task[None] | None = None

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
        self._index_task = asyncio.create_task(self._index_pump(), name="base-agent-index-pump")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.shutdown()

    async def _index_pump(self) -> None:
        try:
            while True:
                try:
                    settled = await self.indexer.run_once()
                except Exception:  # noqa: BLE001 - the pump must survive a bad batch
                    settled = 0
                await asyncio.sleep(0.05 if settled else 0.25)
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        """Stop this process' execution and release control; logical Agents stay durable."""

        task = getattr(self, "_index_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._index_task = None
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
            if existing.owner_scope != self._owner_scope:
                # Never reveal that the id belongs to someone else (BA05).
                raise AgentNotFound(agent_id)
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
        try:
            binding = self.uow.create_agent_binding(
                agent_id=agent_id,
                run_id=agent_id,
                owner_scope=self._owner_scope,
                role="root",
                creation_key=creation_key,
                config_json=config.to_json(),
                config_hash=config_hash(config),
                now=self._ports.clock(),
                max_agents=self._ports.max_agents,
            )
        except InstanceCapConflict as error:
            raise AgentInstanceCapExceeded(str(error)) from error
        return BaseAgent(self, binding)

    @property
    def retriever(self) -> SessionRetriever:
        return cast(SessionRetriever, self._assembled.retriever)

    @property
    def indexer(self) -> SessionIndexer:
        return cast(SessionIndexer, self._assembled.indexer)

    async def index_pending(self) -> int:
        """Drain pending index work now (the background pump shares the same lock)."""

        self.indexer.request_backfill()
        return await self.indexer.drain()

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Names the runtime's tool registry can serve (including ``agent_delegate``)."""

        return self._assembled.tool_names

    async def create_many(
        self, configs: Sequence[AgentConfig], *, batch_key: str
    ) -> tuple[BaseAgent, ...]:
        """Idempotent batch creation (BA02/BA03/BA04): no model call, no partial batch.

        1. Whole-batch admission before any write (types, size, instance cap, tool
           names, and per-instance identity conflicts with existing bindings).
        2. Reserve the batch row (same key + same content replays; different content
           conflicts).  3. Create each instance (each ``create`` is idempotent).
        4. Commit the batch receipt and return the handles in submission order.
        """

        if not isinstance(batch_key, str) or not batch_key.strip():
            raise ValueError("batch_key is required")
        configs = tuple(configs)
        for index, config in enumerate(configs):
            if not isinstance(config, AgentConfig):
                raise AgentBatchRejected(
                    "agent_batch_invalid_config", "every item must be an AgentConfig", index=index
                )
        if not configs:
            raise AgentBatchRejected("agent_batch_empty", "batch has no configs")
        if len(configs) > self._ports.max_batch_size:
            raise AgentBatchRejected(
                "agent_batch_too_large",
                f"batch of {len(configs)} exceeds max_batch_size={self._ports.max_batch_size}",
            )
        hashes = tuple(config_hash(config) for config in configs)
        fingerprint = batch_fingerprint(self._owner_scope, batch_key, configs)
        existing = self.uow.read_agent_batch(self._owner_scope, batch_key)
        if existing is not None and existing.batch_fingerprint != fingerprint:
            raise AgentBatchIdentityConflict("batch_key reused with a different batch content")
        agent_ids = tuple(
            agent_id_for(self._owner_scope, f"{batch_key}:{index}") for index in range(len(configs))
        )
        if existing is not None and existing.state == "committed":
            return tuple([await self.open(agent_id) for agent_id in existing.agent_ids])
        known = set(self.tool_names)
        new_instances = 0
        for index, (config, agent_id, digest) in enumerate(zip(configs, agent_ids, hashes)):
            missing = [name for name in config.tool_names if name not in known]
            if missing:
                raise AgentBatchRejected(
                    "agent_batch_unknown_tool",
                    f"config {index} names tools not in this runtime: {missing}",
                    index=index,
                )
            binding = self.uow.read_agent_binding(agent_id)
            if binding is None:
                new_instances += 1
                continue
            if binding.owner_scope != self._owner_scope or binding.config_hash != digest:
                raise AgentBatchIdentityConflict(
                    f"config {index} collides with an existing Agent under this batch key"
                )
        if (
            self.uow.count_agent_bindings(self._owner_scope) + new_instances
            > self._ports.max_agents
        ):
            raise AgentBatchRejected(
                "agent_batch_instance_cap",
                f"batch would exceed max_agents={self._ports.max_agents}",
            )
        batch_id = f"{agent_id_for(self._owner_scope, batch_key)}:batch"
        try:
            record, _ = self.uow.reserve_agent_batch(
                batch_id=batch_id,
                owner_scope=self._owner_scope,
                batch_key=batch_key,
                batch_fingerprint=fingerprint,
                agent_ids=agent_ids,
                config_hashes=hashes,
                now=self._ports.clock(),
            )
        except UnitOfWorkConflict as error:
            raise AgentBatchIdentityConflict(str(error)) from error
        if tuple(record.agent_ids) != agent_ids:
            raise AgentBatchIdentityConflict("reserved batch names different agent ids")
        agents = []
        for index, config in enumerate(configs):
            agents.append(await self.create(config, creation_key=f"{batch_key}:{index}"))
        self.uow.commit_agent_batch(
            batch_id=record.batch_id,
            receipt={"agent_ids": list(agent_ids), "batch_fingerprint": fingerprint},
            now=self._ports.clock(),
        )
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
                    # A turn was admitted between the read and the write (another
                    # owner): treat it like an open turn and fall through to the
                    # bounded wait below, never spin.
                    open_turn = self.uow.read_open_agent_turn(binding.run_id)
                else:
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
                    open_turn_id=None if open_turn is None else open_turn.turn_id,
                    control_generation=current.control_generation,
                    created_at=command.created_at,
                )
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)

    async def cancel_turn(
        self, agent_id: str, turn_id: str, *, command_id: str, wait_timeout: float = 30.0
    ) -> AgentCancelReceipt:
        """Cooperative per-turn cancel (BA10 / T8); never the kernel's Run cancel.

        The intent is made durable first (control command + generation bump), then the
        in-process token fires so the executor's ReAct loop stops at its next cancel
        point and returns a failed ``agent_turn_cancelled`` result through the normal,
        lease-fenced outcome path.  A turn that already settled keeps its result.
        """

        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("command_id is required")
        binding = self.binding(agent_id)
        if binding is None:
            raise AgentNotFound(agent_id)
        request_hash = hashlib.sha256(
            canonical_json(
                {"kind": "cancel_turn", "agent_id": agent_id, "turn_id": turn_id}
            ).encode("utf-8")
        ).hexdigest()
        clock = self._ports.clock
        try:
            command = self.uow.request_agent_turn_cancel(
                agent_id=agent_id,
                turn_id=turn_id,
                command_id=command_id,
                request_hash=request_hash,
                now=clock(),
            )
        except UnitOfWorkConflict as error:
            raise AgentTurnNotFound(str(error)) from error
        token = getattr(self.driver, "turn_cancellations", {}).get(turn_id)
        if token is not None:
            token.cancel()
        deadline = clock() + float(wait_timeout)
        interval = 0.01
        while True:
            turn = self.uow.read_agent_turn(turn_id)
            assert turn is not None
            if turn.phase in ("committed", "failed"):
                stored = self.uow.read_agent_turn_result(turn_id)
                result_error = (
                    None
                    if stored is None
                    else cast(Mapping[str, object], thaw_json(stored.result_json)).get("error")
                )
                cancelled = (
                    isinstance(result_error, Mapping)
                    and result_error.get("error_code") == AGENT_TURN_CANCELLED
                )
                state = "cancelled" if cancelled else "already_settled"
                break
            if clock() >= deadline:
                state = "pending"
                break
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)
        current = self.uow.read_agent_binding(agent_id)
        assert current is not None
        return AgentCancelReceipt(
            agent_id=agent_id,
            turn_id=turn_id,
            command_id=command.command_id,
            state=state,
            control_generation=current.control_generation,
            created_at=command.created_at,
        )

    async def open(self, agent_id: str | AgentId) -> BaseAgent:
        """Open an existing Agent of this owner; foreign or missing ids look identical."""

        value = agent_id.value if isinstance(agent_id, AgentId) else agent_id
        binding = self.binding(value)
        if binding is None:
            raise AgentNotFound(value)
        return BaseAgent(self, binding)

    def binding(self, agent_id: str) -> AgentBindingRecord | None:
        """Owner-scoped read: another owner's Agent is reported as absent (BA05)."""

        binding = self.uow.read_agent_binding(agent_id)
        if binding is None or binding.owner_scope != self._owner_scope:
            return None
        return binding


def build_agent_runtime(ports: AgentRuntimePorts, *, owner_scope: str = "default") -> AgentRuntime:
    """Assemble a BaseAgent runtime with no user Memory; use ``async with``.

    ``agent.delegate`` is registered before the tool registry seals and late-bound
    to the ``AgentRuntime`` right after the kernel exists (fixed order, closure risk 3).
    """

    from .tools.delegate import AgentDelegateTool, AgentDelegationReconciliation
    from .tools.session_history import SessionHistoryTools

    delegate = AgentDelegateTool(clock=ports.clock)
    session_tools = SessionHistoryTools()
    assembled = assemble_runtime(
        ports,
        extra_tools=(delegate.function_tool(), *session_tools.function_tools()),
        delegation_counter=lambda turn_id: delegate.runtime.uow.count_agent_delegations(turn_id),
        delegation_reconciliation=AgentDelegationReconciliation,
    )
    runtime = AgentRuntime(assembled, ports, owner_scope=owner_scope)
    delegate.bind(runtime)
    session_tools.bind(runtime)
    runtime._session_tools = session_tools  # type: ignore[attr-defined]
    runtime._delegate = delegate  # type: ignore[attr-defined]
    return runtime


__all__ = (
    "AgentRuntime",
    "AssembledRuntime",
    "BASE_AGENT_DRIVER_KIND",
    "CHILD_PROFILE_KEY",
    "agent_id_for",
    "assemble_runtime",
    "batch_fingerprint",
    "build_agent_runtime",
    "start_input_for",
)
