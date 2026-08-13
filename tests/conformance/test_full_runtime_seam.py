# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen H7 full-runtime oracle.

The test doubles in this module only script physical I/O or Driver state.  They
never call another authority, mutate the database, or manufacture a runtime
result.  Provider, Tool, Authorization, Context, child-signal, terminal, and
delivery coordination must therefore be performed by production code reached
through ``DriverInvocation.services``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from simple_harness.contracts import (
    CallId,
    ExecutionSessionId,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderRequestRejectedError,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
)
from simple_harness.runtime import (
    ChildLaunchRequest,
    ProfileLaunchTicketRef,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    StartupReconciler,
    StartupReconciliationSteps,
    build_runtime,
)
from simple_harness.tools import ToolContext, ToolResult, ToolSpec
from simple_harness.tools.authorization import (
    AuthorizationDecision,
    AuthorizationResult,
)
from simple_harness.tools.executor import EffectExecutor
from simple_harness.tools.reconciliation import (
    ReconciliationObservation,
    ReconciliationState,
)
from simple_harness.tools.registry import ToolRegistry


@pytest.fixture
def production_seam_symbols():
    """Load the planned production state machine, never a conformance backdoor."""

    from simple_harness.runtime import (
        AgentLoopCollaborator,
        ChildSignalRuntime,
        EffectBatchExecutor,
    )
    from simple_harness.runtime.drivers import ReActDriver

    return SimpleNamespace(
        AgentLoopCollaborator=AgentLoopCollaborator,
        ChildSignalRuntime=ChildSignalRuntime,
        EffectBatchExecutor=EffectBatchExecutor,
        ReActDriver=ReActDriver,
    )


@dataclass
class OrderSpy:
    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, operation: str, identity: object = "") -> None:
        self.entries.append((operation, str(identity)))

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.entries]


class ScriptedProvider:
    target = ProviderTarget("fixture", "model", "fixture:model", "local", "fixture")

    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.responses: list[ProviderResponse | BaseException] = []
        self.requests: list[ProviderRequest] = []
        self.physical_requests: list[ProviderRequest] = []

    async def invoke(
        self, request: ProviderRequest, *, cancel: CancelToken
    ) -> ProviderResponse:
        assert not cancel.is_cancelled
        self.requests.append(request)
        scripted = self.responses.pop(0)
        if isinstance(scripted, ProviderRequestRejectedError):
            # Adapter-local validation failed before a network request existed.
            self.order.add("provider.preflight_rejected", request.request_id.value)
            raise scripted
        self.order.add("provider.transport", request.request_id.value)
        self.physical_requests.append(request)
        if isinstance(scripted, BaseException):
            raise scripted
        return ProviderResponse(
            request_id=request.request_id,
            message=scripted.message,
            tool_calls=scripted.tool_calls,
            usage=scripted.usage,
            model=scripted.model,
            finish_reason=scripted.finish_reason,
        )


class RecordingAuthorization:
    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.effect_ids: list[str] = []

    async def authorize(self, prepared) -> AuthorizationResult:
        self.effect_ids.append(prepared.effect_id.value)
        self.order.add("authorization.authorize", prepared.effect_id.value)
        return AuthorizationResult(
            AuthorizationDecision.ALLOW,
            receipt_ref=f"authorization:{prepared.effect_id.value}",
        )


class RecordingReconciliation:
    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.effect_ids: list[str] = []
        self.observation = ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN,
            "fixture:still-unknown",
        )

    async def observe(self, prepared) -> ReconciliationObservation:
        self.effect_ids.append(prepared.effect_id.value)
        self.order.add("tool.reconciliation.observe", prepared.effect_id.value)
        return self.observation


class RecordingTool:
    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.calls: list[dict[str, object]] = []
        self.outcome = "success"

    def invoke(self, arguments, context: ToolContext) -> ToolResult:
        self.calls.append(dict(arguments))
        self.order.add("tool.handler", context.run_id.value)
        if self.outcome == "unknown":
            return ToolResult.unknown(CallId("call-unknown"), "outcome unavailable")
        return ToolResult.succeeded(CallId("call-1"), {"seen": arguments["x"]})


class RecordingSink:
    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.projections: list[tuple[str, bytes]] = []

    async def deliver(
        self, payload: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> None:
        encoded = canonical_json(dict(payload)).encode("utf-8")
        self.projections.append((idempotency_key, encoded))
        self.order.add("delivery.sink", idempotency_key)


class Catalog:
    def current_generation(self) -> int:
        return 1


class ParentStateDriver:
    """Script Driver states; production owns every durable read and write."""

    def __init__(self, order: OrderSpy) -> None:
        self.order = order
        self.starts = 0
        self.recovered_child_run_id: str | None = None

    async def start(self, invocation, *, context, cancel):
        del context
        assert not cancel.is_cancelled
        self.starts += 1
        self.order.add("driver.parent.start", invocation.run.run_id)
        if self.starts == 1:
            from simple_harness.runtime import DriverResult

            return DriverResult(RunState.WAITING, {"awaiting": "child_terminal"})
        continuations = tuple(invocation.continuations)
        assert len(continuations) == 1
        payload = continuations[0].payload
        assert payload["kind"] == "child_terminal"
        child_run_id = str(payload["child_run_id"])
        self.recovered_child_run_id = child_run_id
        terminal_payload = {
            "message": "parent recovered after composed child provider failure",
            "correlation": {
                "recovered_child_run_id": child_run_id,
                "recovery_reason": "child_provider_failure",
            },
        }
        delivery = DeliverySpec(
            "delivery-parent-final",
            "fixture",
            "parent-composed:terminal",
            terminal_payload,
        )
        from simple_harness.runtime import DriverResult

        return DriverResult(RunState.COMPLETED, terminal_payload, (delivery,))


@dataclass
class Seam:
    path: Path
    symbols: object
    database: Database
    uow: SqliteExecutionUnitOfWork
    runtime: object
    provider: ScriptedProvider
    tool: RecordingTool
    authorization: RecordingAuthorization
    reconciliation: RecordingReconciliation
    sink: RecordingSink
    order: OrderSpy

    async def start(self, run_id: str, input_value: Mapping[str, JsonValue]):
        start = RunStart(
            ExecutionSessionId("h7-session"),
            RunId(run_id),
            RequestId(f"request-{run_id}"),
            input_value,
            1,
        )
        await self.runtime.client.start(start)
        await self.runtime.wait_idle(start.run_id)
        return self.runtime.client.query(start.run_id)

    async def reopen(self, *, parent_driver=None) -> Seam:
        await self.runtime.close()
        self.database.close()
        return await make_seam(
            self.symbols,
            self.path,
            order=self.order,
            provider=self.provider,
            tool=self.tool,
            authorization=self.authorization,
            reconciliation=self.reconciliation,
            sink=self.sink,
            parent_driver=parent_driver,
        )


async def make_seam(
    symbols,
    path: Path,
    *,
    order: OrderSpy | None = None,
    provider: ScriptedProvider | None = None,
    tool: RecordingTool | None = None,
    authorization: RecordingAuthorization | None = None,
    reconciliation: RecordingReconciliation | None = None,
    sink: RecordingSink | None = None,
    parent_driver=None,
) -> Seam:
    order = order or OrderSpy()
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    provider = provider or ScriptedProvider(order)
    tool = tool or RecordingTool(order)
    authorization = authorization or RecordingAuthorization(order)
    reconciliation = reconciliation or RecordingReconciliation(order)
    sink = sink or RecordingSink(order)

    registry = ToolRegistry()
    registry.register_function(
        ToolSpec(
            "calculator",
            "Calculate an integer",
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
                "additionalProperties": False,
            },
        ),
        tool.invoke,
    )
    provider_coordinator = ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(max_total_micros=1_000_000),
        estimator=None,
        clock=lambda: 10.0,
    )
    effects = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=authorization,
        reconciliation=reconciliation,
        fences=uow,
        clock=lambda: 10.0,
    )
    context = SqliteContextPort(database, clock=lambda: 10.0)
    delivery = DeliveryDispatcher(uow, {"fixture": sink}, clock=lambda: 10.0)

    async def reconcile_provider() -> None:
        order.add("recovery.provider")
        await provider_coordinator.reconcile_incomplete()

    async def reconcile_effects() -> None:
        order.add("recovery.effects")
        rows = database.connection.execute(
            "SELECT effect_id,run_id FROM execution_effects "
            "WHERE state IN ('handed_off','unknown') ORDER BY effect_id"
        ).fetchall()
        for effect_id, run_id in rows:
            from simple_harness.contracts import EffectId

            record = uow.read_effect(EffectId(str(effect_id)))
            if record is None:
                continue
            run = uow.read_run(str(run_id))
            assert run is not None
            await effects.reconcile(
                record,
                context=ToolContext(
                    RunId(str(run_id)), RequestId(run.request_id), _tool_cancel()
                ),
                current_fence_epoch=record.fence_epoch,
            )

    async def reconcile_child_signals() -> None:
        order.add("recovery.child_signals")
        await symbols.ChildSignalRuntime(uow, owner_id="h7-runtime").reconcile_all(
            now=10.0
        )

    async def reconcile_deliveries() -> None:
        order.add("recovery.deliveries")
        await delivery.drain()

    async def recoverable_runs() -> None:
        order.add("recovery.recoverable_runs")

    startup = StartupReconciler(
        StartupReconciliationSteps(
            provider=reconcile_provider,
            effects=reconcile_effects,
            child_signals=reconcile_child_signals,
            deliveries=reconcile_deliveries,
            recoverable_runs=recoverable_runs,
        )
    )

    # These production objects receive policy/state only.  Runtime authorities
    # are deliberately absent and must come from DriverInvocation.services.
    collaborator = symbols.AgentLoopCollaborator()
    batch = symbols.EffectBatchExecutor(max_batch_size=32)
    react_driver = symbols.ReActDriver(collaborator=collaborator, effects=batch)
    profiles = {
        "agent.general": RuntimeProfile(
            "agent.general", "parent" if parent_driver is not None else "react"
        ),
        "workflow.durable_task": RuntimeProfile(
            "workflow.durable_task", "react"
        ),
    }
    drivers = {"react": react_driver}
    if parent_driver is not None:
        drivers["parent"] = parent_driver
    runtime = build_runtime(
        uow,
        profiles,
        drivers,
        RuntimePorts(
            provider=provider_coordinator,
            tools=effects,
            authorization=authorization,
            context=context,
            delivery=delivery,
            tool_reconciliation=reconciliation,
            reconciliation=startup,
            react_checkpoint=uow,
            tool_catalog=Catalog(),
            owner_id="h7-runtime",
            clock=lambda: 10.0,
        ),
    )
    await runtime.start()
    return Seam(
        path,
        symbols,
        database,
        uow,
        runtime,
        provider,
        tool,
        authorization,
        reconciliation,
        sink,
        order,
    )


def _tool_cancel():
    from simple_harness.tools import CancellationToken

    return CancellationToken()


def response(*, content: str = "", tools=()) -> ProviderResponse:
    return ProviderResponse(
        RequestId("scripted"),
        Message(MessageRole.ASSISTANT, content),
        tuple(tools),
        model="model",
        finish_reason="tool_calls" if tools else "stop",
    )


def calculator_call(call_id: str = "call-1", x: int = 7) -> ProviderToolCall:
    return ProviderToolCall(CallId(call_id), "calculator", {"x": x})


def _count(database: Database, sql: str, parameters=()) -> int:
    return int(database.connection.execute(sql, parameters).fetchone()[0])


def _event_payload(database: Database, run_id: str, kind: str) -> dict[str, object]:
    row = database.connection.execute(
        "SELECT payload_json FROM run_events WHERE run_id=? AND kind=? "
        "ORDER BY created_at DESC,event_id DESC LIMIT 1",
        (run_id, kind),
    ).fetchone()
    assert row is not None
    value = json.loads(str(row[0]))
    assert isinstance(value, dict)
    return value


def test_capability_snapshot_preserves_raw_failure_contract(
    production_seam_symbols, tmp_path
) -> None:
    async def case() -> None:
        seam = await make_seam(production_seam_symbols, tmp_path / "capability.db")
        seam.provider.responses[:] = [
            response(tools=(ProviderToolCall(CallId("denied-1"), "denied", {}),))
        ]
        record = await seam.start(
            "run-capability",
            {
                "messages": [{"role": "user", "content": "use available tools"}],
                "capability_snapshot": {"tools": ["calculator"]},
            },
        )

        assert record.state is RunState.WAITING
        assert [tool.name for tool in seam.provider.requests[0].tools] == ["calculator"]
        assert seam.authorization.effect_ids == []
        assert seam.tool.calls == []
        payload = _event_payload(seam.database, "run-capability", "run.waiting")
        failures = payload["raw_failures"]
        assert isinstance(failures, list) and len(failures) == 1
        assert failures[0]["error_code"] == "tool_not_exposed"
        assert failures[0]["source_kind"] == "tool_parse"
        assert failures[0]["retriable"] is True
        assert failures[0]["replan"] is True
        assert "capability_search" in failures[0]["message"]

    asyncio.run(case())


def test_provider_tool_kernel_effect_and_same_driver_order(
    production_seam_symbols, tmp_path
) -> None:
    async def case() -> None:
        seam = await make_seam(production_seam_symbols, tmp_path / "tool-loop.db")
        seam.provider.responses[:] = [
            response(tools=(calculator_call(),)),
            response(content="seen 7"),
        ]
        record = await seam.start(
            "run-tool",
            {
                "messages": [{"role": "user", "content": "calculate seven"}],
                "capability_snapshot": {"tools": ["calculator"]},
            },
        )

        assert record.state is RunState.COMPLETED
        assert seam.order.operations()[-4:] == [
            "provider.transport",
            "authorization.authorize",
            "tool.handler",
            "provider.transport",
        ]
        assert seam.tool.calls == [{"x": 7}]
        assert _count(
            seam.database,
            "SELECT count(*) FROM provider_invocations "
            "WHERE run_id='run-tool' AND state='succeeded'",
        ) == 2
        assert _count(
            seam.database,
            "SELECT count(*) FROM execution_effects "
            "WHERE run_id='run-tool' AND state='succeeded'",
        ) == 1
        checkpoint = seam.uow.read_react_checkpoint("run-tool")
        assert checkpoint is not None
        assert checkpoint.checkpoint["provider_turns_reserved_total"] == 2
        assert checkpoint.checkpoint["tool_calls_reserved_total"] == 1
        context = seam.runtime._ports.context.load(RunId("run-tool"))
        assert [message.role for message in context.messages][-3:] == [
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]

    asyncio.run(case())


def test_reopen_reconciles_provider_and_tool_unknown_without_replay(
    production_seam_symbols, tmp_path
) -> None:
    async def case() -> None:
        seam = await make_seam(production_seam_symbols, tmp_path / "unknown.db")
        seam.provider.responses[:] = [RuntimeError("transport result lost")]
        await seam.start(
            "run-provider-unknown",
            {"messages": [{"role": "user", "content": "provider unknown"}]},
        )

        seam.tool.outcome = "unknown"
        seam.provider.responses[:] = [response(tools=(calculator_call("call-unknown"),))]
        await seam.start(
            "run-tool-unknown",
            {
                "messages": [{"role": "user", "content": "tool unknown"}],
                "capability_snapshot": {"tools": ["calculator"]},
            },
        )
        provider_before = seam.database.connection.execute(
            "SELECT invocation_id,request_fingerprint FROM provider_invocations "
            "WHERE run_id='run-provider-unknown' AND state='unknown'"
        ).fetchone()
        effect_before = seam.database.connection.execute(
            "SELECT effect_id,request_hash FROM execution_effects "
            "WHERE run_id='run-tool-unknown' AND state='unknown'"
        ).fetchone()
        assert provider_before is not None and effect_before is not None
        transport_count = len(seam.provider.physical_requests)
        handler_count = len(seam.tool.calls)

        reopened = await seam.reopen()
        provider_after = reopened.database.connection.execute(
            "SELECT invocation_id,request_fingerprint FROM provider_invocations "
            "WHERE run_id='run-provider-unknown' AND state='unknown'"
        ).fetchone()
        effect_after = reopened.database.connection.execute(
            "SELECT effect_id,request_hash FROM execution_effects "
            "WHERE run_id='run-tool-unknown' AND state='unknown'"
        ).fetchone()
        assert tuple(provider_after) == tuple(provider_before)
        assert tuple(effect_after) == tuple(effect_before)
        assert len(reopened.provider.physical_requests) == transport_count
        assert len(reopened.tool.calls) == handler_count
        assert reopened.reconciliation.effect_ids == [str(effect_before[0])]
        assert reopened.order.operations().index("recovery.provider") < (
            reopened.order.operations().index("recovery.effects")
        )
        assert reopened.uow.read_run("run-provider-unknown").state is RunState.WAITING
        assert reopened.uow.read_run("run-tool-unknown").state is RunState.WAITING

    asyncio.run(case())


def test_attached_child_failure_wakes_parent_and_delivers_correlated_terminal(
    production_seam_symbols, tmp_path
) -> None:
    async def case() -> None:
        order = OrderSpy()
        parent_driver = ParentStateDriver(order)
        seam = await make_seam(
            production_seam_symbols,
            tmp_path / "child.db",
            order=order,
            parent_driver=parent_driver,
        )
        parent = await seam.start(
            "parent-composed",
            {"messages": [{"role": "user", "content": "delegate this task"}]},
        )
        assert parent.state is RunState.WAITING

        launch_payload = {
            "profile_key": "workflow.durable_task",
            "driver_kind": "react",
            "catalog_generation": 1,
            "messages": [{"role": "user", "content": "child provider failure"}],
        }
        ticket = ProfileLaunchTicket(
            "ticket-composed",
            parent.run_id,
            "workflow.durable_task",
            1,
            child_launch_fingerprint(launch_payload),
        )
        seam.uow.issue_profile_launch_ticket(ticket, now=11.0)
        seam.provider.responses[:] = [
            ProviderRequestRejectedError(public_message="fixture child failure")
        ]
        child = await seam.runtime.children.launch(
            ChildLaunchRequest(
                ProfileLaunchTicketRef(ticket.ticket_id, ticket.catalog_generation),
                "command-composed",
                "child-composed",
                "request-child-composed",
                AttachmentPolicy.ATTACHED,
                launch_payload,
                {
                    "schema_version": 1,
                    "profile_key": "workflow.durable_task",
                    "driver_kind": "react",
                    "tool_catalog_generation": 1,
                    "input": {"messages": launch_payload["messages"]},
                },
            )
        )
        await seam.runtime.wait_idle(RunId(child.run.run_id))
        assert seam.uow.read_run(child.run.run_id).state is RunState.FAILED
        assert seam.provider.physical_requests == []
        child_row = seam.database.connection.execute(
            "SELECT run_id,root_run_id,parent_run_id,state FROM runs "
            "WHERE run_id='child-composed'"
        ).fetchone()
        assert tuple(child_row) == (
            "child-composed",
            "parent-composed",
            "parent-composed",
            "failed",
        )

        await seam.runtime.reconcile()
        await seam.runtime.wait_idle(RunId("parent-composed"))
        root = seam.uow.read_run("parent-composed")
        assert root is not None and root.state is RunState.COMPLETED
        assert parent_driver.recovered_child_run_id == "child-composed"
        assert _count(
            seam.database,
            "SELECT count(*) FROM child_signals "
            "WHERE parent_run_id='parent-composed' AND child_run_id='child-composed' "
            "AND state='acked'",
        ) == 1
        assert _count(
            seam.database,
            "SELECT count(*) FROM continuations WHERE run_id='parent-composed' "
            "AND state='acked'",
        ) == 1
        terminal = _event_payload(seam.database, "parent-composed", "run.completed")
        assert terminal["correlation"] == {
            "recovered_child_run_id": "child-composed",
            "recovery_reason": "child_provider_failure",
        }
        assert _count(
            seam.database,
            "SELECT count(*) FROM delivery_outbox "
            "WHERE run_id='parent-composed' AND state='pending'",
        ) == 1
        assert await seam.runtime._ports.delivery.run_once() is True
        assert seam.sink.projections == [
            (
                "parent-composed:terminal",
                canonical_json(
                    {
                        "correlation": {
                            "recovered_child_run_id": "child-composed",
                            "recovery_reason": "child_provider_failure",
                        },
                        "fence_epoch": terminal["fence_epoch"],
                        "message": "parent recovered after composed child provider failure",
                        "terminal_fence_receipt_ref": terminal[
                            "terminal_fence_receipt_ref"
                        ],
                    }
                ).encode("utf-8"),
            )
        ]

    asyncio.run(case())


def _terminal_public() -> dict[str, object]:
    return {
        "metrics": {
            "actual_requests": 3,
            "hits": 0,
            "empty": 2,
            "timeouts": 1,
            "cooldown_skips": 4,
            "busy_skips": 1,
            "queue_timeouts": 0,
            "probes": 1,
            "rescue_considered_count": 1,
            "rescue_executed_count": 1,
            "candidates": 0,
        },
        "diagnostic_codes": ["no_results"],
        "skipped_stage_ids": [
            "fetch",
            "score",
            "gap",
            "rerank",
            "synth",
            "cite",
            "persist",
        ],
        "retry_action_id": "retry_from_start",
    }


def _terminal_intents(state, *, run_id, status, error, recovery_action):
    from simple_harness.workflow.native import NativeWorkflowExecutable

    return NativeWorkflowExecutable.terminal_intents(
        state,
        run_id=run_id,
        status=status,
        error=error,
        recovery_action=recovery_action,
    )


def test_failed_terminal_projects_only_strict_public_fields() -> None:
    intents = _terminal_intents(
        {
            "workflow_name": "deep_research",
            "workflow_version": "v4",
            "values": {
                "terminal_public": _terminal_public(),
                "delivery_intents": [],
                "topic": "secret topic must not project",
            },
        },
        run_id="run-v4",
        status="failed",
        error={"code": "deep_research_no_results", "message": "No results"},
        recovery_action="retry_from_start",
    )
    assert [intent.event_type for intent in intents] == ["workflow.final"]
    payload = intents[0].payload
    assert payload["metrics"]["actual_requests"] == 3
    assert payload["retry_action_id"] == "retry_from_start"
    assert payload["card"]["skipped_stage_ids"][-1] == "persist"
    assert "topic" not in payload


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"raw_query": "secret"}),
        lambda value: value["metrics"].update({"urls": 1}),
        lambda value: value["metrics"].update({"hits": -1}),
        lambda value: value.update({"diagnostic_codes": ["secret_query"]}),
        lambda value: value.update({"skipped_stage_ids": ["fetch", "fetch"]}),
        lambda value: value.update({"retry_action_id": "retry_with_query"}),
    ),
)
def test_terminal_public_rejects_unknown_or_unbounded_state(mutate) -> None:
    from simple_harness.workflow.errors import InvalidStatePatch

    public = _terminal_public()
    mutate(public)
    with pytest.raises(InvalidStatePatch, match="terminal"):
        _terminal_intents(
            {
                "workflow_name": "deep_research",
                "workflow_version": "v4",
                "values": {"terminal_public": public},
            },
            run_id="run-v4",
            status="failed",
            error=None,
            recovery_action=None,
        )


def test_legacy_terminal_without_public_projection_is_byte_shape_compatible() -> None:
    intents = _terminal_intents(
        {"values": {"delivery_intents": []}},
        run_id="run-v3",
        status="failed",
        error={"code": "legacy", "message": "legacy"},
        recovery_action="retry",
    )
    payload = intents[0].payload
    assert set(payload) == {"kind", "status", "error", "recovery_action", "card"}
    assert canonical_json(payload).encode("utf-8") == (
        b'{"card":null,"error":{"code":"legacy","message":"legacy"},'
        b'"kind":"workflow_terminal","recovery_action":"retry","status":"failed"}'
    )
