# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen H7 full-runtime oracle.

I/O doubles only script physical boundaries. Driver doubles and probes only
implement the public RuntimeDriver boundary and never write durable state.
Provider, Tool, Authorization, Context, child-signal, terminal, and delivery
coordination must be performed by production code reached through
``DriverInvocation.services`` and public Runtime facades.
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
    thaw_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildCommandState,
    ProfileLaunchTicket,
    ProfileLaunchTicketState,
    child_launch_fingerprint,
)
from simple_harness.execution.delivery import DeliveryDispatcher, DeliverySpec
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunRecord, RunState
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderRequestRejectedError,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
    ProviderUsage,
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


@dataclass
class RecoverySpy:
    phases: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProviderRecoveryObservation:
    state: str
    evidence_ref: str


class RecordingProviderReconciliation:
    """Script evidence only; production must select and invoke the Port."""

    def __init__(self) -> None:
        self.observed: list[tuple[str, str, str, str, str, int]] = []
        self.last_evidence_ref: str | None = None

    async def observe(self, invocation) -> ProviderRecoveryObservation:
        identity = (
            invocation.invocation_id,
            invocation.run_id.value,
            invocation.request_id.value,
            invocation.request_fingerprint,
            invocation.target_digest,
            invocation.handoff_attempt,
        )
        self.observed.append(identity)
        observation = ProviderRecoveryObservation(
            "still_unknown",
            f"provider-reconciliation:{invocation.invocation_id}:"
            f"attempt:{invocation.handoff_attempt}",
        )
        self.last_evidence_ref = observation.evidence_ref
        return observation


class ReActDriverProbe:
    """Observe one production ReActDriver without implementing its state machine."""

    def __init__(self, delegate, order: OrderSpy) -> None:
        self.delegate = delegate
        self.order = order
        self.start_calls = 0
        self.delegate_ids: list[int] = []
        self.results = []

    async def start(self, invocation, *, context, cancel):
        self.start_calls += 1
        identity = id(self.delegate)
        self.delegate_ids.append(identity)
        self.order.add("driver.react.enter", identity)
        result = await self.delegate.start(
            invocation,
            context=context,
            cancel=cancel,
        )
        self.results.append(result)
        self.order.add("driver.react.exit", identity)
        return result


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
        mutable = thaw_json(payload)
        assert isinstance(mutable, dict)
        encoded = canonical_json(mutable).encode("utf-8")
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
        self.continuation_id: str | None = None
        self.signal_id: str | None = None

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
        continuation = continuations[0]
        self.continuation_id = continuation.continuation_id
        payload = continuation.payload
        assert payload["kind"] == "child_terminal"
        assert payload["signal_id"]
        assert payload["payload"]["status"] == "failed"
        self.signal_id = str(payload["signal_id"])
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
    recovery: RecoverySpy
    provider_reconciliation: RecordingProviderReconciliation
    context: SqliteContextPort
    react_driver: ReActDriverProbe

    async def start(self, run_id: str, input_value: Mapping[str, JsonValue]):
        start = RunStart(
            execution_session_id=ExecutionSessionId("h7-session"),
            run_id=RunId(run_id),
            request_id=RequestId(f"request-{run_id}"),
            turn_id=f"turn-{run_id}",
            input=input_value,
            tool_catalog_generation=1,
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
            recovery=self.recovery,
            provider_reconciliation=self.provider_reconciliation,
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
    recovery: RecoverySpy | None = None,
    provider_reconciliation: RecordingProviderReconciliation | None = None,
    provider: ScriptedProvider | None = None,
    tool: RecordingTool | None = None,
    authorization: RecordingAuthorization | None = None,
    reconciliation: RecordingReconciliation | None = None,
    sink: RecordingSink | None = None,
    parent_driver=None,
) -> Seam:
    order = order or OrderSpy()
    recovery = recovery or RecoverySpy()
    provider_reconciliation = (
        provider_reconciliation or RecordingProviderReconciliation()
    )
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
        budget_policy=BudgetPolicy(hard_cap_micros=None),
        estimator=FrozenPriceEstimator(
            snapshot_id="h7-fixture-prices-v1",
            pricing_key="fixture:model",
            input_micros_per_million_tokens=0,
            output_micros_per_million_tokens=0,
        ),
        clock=lambda: 10.0,
    )
    effects = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=authorization,
        reconciliation=reconciliation,
        clock=lambda: 10.0,
    )
    context = SqliteContextPort(database, clock=lambda: 10.0)
    delivery = DeliveryDispatcher(uow, {"fixture": sink}, clock=lambda: 10.0)

    async def reconcile_provider() -> None:
        recovery.phases.append("provider")
        await provider_coordinator.reconcile_incomplete(
            provider_reconciliation=provider_reconciliation
        )

    async def reconcile_effects() -> None:
        recovery.phases.append("effects")
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
        recovery.phases.append("child_signals")
        receiver = symbols.ChildSignalRuntime(uow, owner_id="h7-runtime")
        parents = database.connection.execute(
            "SELECT DISTINCT parent_run_id FROM child_signals "
            "WHERE state != 'acked' ORDER BY parent_run_id"
        ).fetchall()
        for (parent_run_id,) in parents:
            while receiver.receive_one(parent_run_id=str(parent_run_id), now=10.0):
                pass

    async def reconcile_deliveries() -> None:
        recovery.phases.append("deliveries")
        while await delivery.run_once():
            pass

    async def recoverable_runs() -> None:
        recovery.phases.append("recoverable_runs")

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
    production_react_driver = symbols.ReActDriver(
        collaborator=collaborator,
        effects=batch,
        clock=lambda: 10.0,
    )
    react_driver = ReActDriverProbe(production_react_driver, order)
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
            provider_reconciliation=provider_reconciliation,
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
        recovery,
        provider_reconciliation,
        context,
        react_driver,
    )


def _tool_cancel():
    from simple_harness.tools import CancellationToken

    return CancellationToken()


def response(*, content: str = "", tools=()) -> ProviderResponse:
    return ProviderResponse(
        RequestId("scripted"),
        Message(MessageRole.ASSISTANT, content),
        tuple(tools),
        usage=ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2),
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


@pytest.mark.h7_runtime_gate
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


@pytest.mark.h7_runtime_gate
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
        assert seam.order.operations()[-6:] == [
            "driver.react.enter",
            "provider.transport",
            "authorization.authorize",
            "tool.handler",
            "provider.transport",
            "driver.react.exit",
        ]
        assert seam.react_driver.start_calls == 1
        assert seam.react_driver.delegate_ids == [id(seam.react_driver.delegate)]
        assert len(seam.react_driver.results) == 1
        assert seam.react_driver.results[0].state is RunState.COMPLETED
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
        tool_outcomes = [
            message
            for message in seam.provider.requests[1].messages
            if message.role is MessageRole.TOOL
        ]
        assert len(tool_outcomes) == 1
        tool_outcome = json.loads(tool_outcomes[0].content)
        assert tool_outcome["outcome"] == "succeeded"
        assert tool_outcome["value"] == {"seen": 7}
        context = seam.context.load(RunId("run-tool"))
        assert [message.role for message in context.messages][-3:] == [
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]

    asyncio.run(case())


@pytest.mark.h7_runtime_gate
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
        phase_cursor = len(seam.recovery.phases)
        provider_cursor = len(seam.provider_reconciliation.observed)

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
        assert reopened.recovery.phases[phase_cursor : phase_cursor + 5] == [
            "provider",
            "effects",
            "child_signals",
            "deliveries",
            "recoverable_runs",
        ]
        observations = reopened.provider_reconciliation.observed[provider_cursor:]
        assert len(observations) == 1
        observed = observations[0]
        assert observed[0] == str(provider_before[0])
        assert observed[1:3] == (
            "run-provider-unknown",
            "run-provider-unknown:provider-turn:1",
        )
        assert observed[3] == str(provider_before[1])
        provider_record = reopened.uow.read_provider_invocation(observed[0])
        assert provider_record is not None
        assert observed[4] == provider_record.target_digest
        assert observed[5] == provider_record.handoff_attempt
        assert (
            f"provider-reconciliation:{observed[0]}:attempt:{observed[5]}"
            == reopened.provider_reconciliation.last_evidence_ref
        )
        assert reopened.uow.read_run("run-provider-unknown").state is RunState.WAITING
        assert reopened.uow.read_run("run-tool-unknown").state is RunState.WAITING

    asyncio.run(case())


@pytest.mark.h7_runtime_gate
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
        from simple_harness.runtime import ChildRunHandle

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
                    "turn_id": "turn-child-composed",
                    "tool_catalog_generation": 1,
                    "input": {"messages": launch_payload["messages"]},
                },
            )
        )
        assert isinstance(child, ChildRunHandle)
        assert isinstance(child.run, RunRecord)
        assert child.run.run_id == "child-composed"
        assert child.ticket.ticket_id == ticket.ticket_id
        assert child.ticket.state is ProfileLaunchTicketState.CLAIMED
        assert child.command.command_id == "command-composed"
        assert child.command.ticket_id == ticket.ticket_id
        assert child.command.child_run_id == child.run.run_id
        assert child.command.state in {
            ChildCommandState.PENDING,
            ChildCommandState.SCHEDULED,
        }
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
        provider_row = seam.database.connection.execute(
            "SELECT invocation_id FROM provider_invocations "
            "WHERE run_id='child-composed' AND state='failed'"
        ).fetchone()
        assert provider_row is not None
        provider_record = seam.uow.read_provider_invocation(str(provider_row[0]))
        assert provider_record is not None
        assert provider_record.run_id == RunId("child-composed")
        assert provider_record.request_id == seam.provider.requests[0].request_id
        assert provider_record.target == seam.provider.target
        assert provider_record.error_code == "provider_request_rejected"
        child_event_kinds = [
            str(row[0])
            for row in seam.database.connection.execute(
                "SELECT kind FROM run_events WHERE run_id='child-composed' "
                "ORDER BY durable_seq"
            ).fetchall()
        ]
        assert child_event_kinds.index("child.created") < child_event_kinds.index(
            "child.failed"
        )
        pending_signal = seam.database.connection.execute(
            "SELECT signal_id,payload_json,state FROM child_signals "
            "WHERE parent_run_id='parent-composed' AND child_run_id='child-composed'"
        ).fetchone()
        assert pending_signal is not None
        assert str(pending_signal[2]) == "pending"
        assert json.loads(str(pending_signal[1]))["status"] == "failed"

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
        receipt = seam.database.connection.execute(
            "SELECT receipt_id,signal_id,continuation_id,event_id "
            "FROM child_signal_ack_receipts WHERE signal_id=?",
            (str(pending_signal[0]),),
        ).fetchone()
        assert receipt is not None
        assert parent_driver.signal_id == str(receipt[1]) == str(pending_signal[0])
        assert parent_driver.continuation_id == str(receipt[2])
        continuation = seam.database.connection.execute(
            "SELECT payload_json,state FROM continuations WHERE continuation_id=?",
            (str(receipt[2]),),
        ).fetchone()
        assert continuation is not None and str(continuation[1]) == "acked"
        continuation_payload = json.loads(str(continuation[0]))
        assert continuation_payload["signal_id"] == str(pending_signal[0])
        assert continuation_payload["child_run_id"] == "child-composed"
        assert continuation_payload["payload"]["status"] == "failed"
        assert _count(
            seam.database,
            "SELECT count(*) FROM run_events WHERE event_id=?",
            (str(receipt[3]),),
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
        assert await seam.runtime.dispatch_deliveries_once() is True
        expected_projections = [
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
        assert seam.sink.projections == expected_projections
        assert _count(
            seam.database,
            "SELECT count(*) FROM delivery_outbox "
            "WHERE run_id='parent-composed' AND state='delivered'",
        ) == 1
        assert _count(
            seam.database,
            "SELECT count(*) FROM delivery_outbox "
            "WHERE run_id='parent-composed' AND state='pending'",
        ) == 0
        assert await seam.runtime.dispatch_deliveries_once() is False
        assert seam.sink.projections == expected_projections

        reopened = await seam.reopen(parent_driver=parent_driver)
        assert await reopened.runtime.dispatch_deliveries_once() is False
        assert reopened.sink.projections == expected_projections
        assert _count(
            reopened.database,
            "SELECT count(*) FROM delivery_outbox "
            "WHERE run_id='parent-composed' AND state='delivered'",
        ) == 1
        assert _count(
            reopened.database,
            "SELECT count(*) FROM delivery_outbox "
            "WHERE run_id='parent-composed' AND state='pending'",
        ) == 0

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


@pytest.mark.h7_workflow_terminal_gate
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
    public = _terminal_public()
    expected = {
        "kind": "final",
        "status": "failed",
        "error": {"code": "deep_research_no_results", "message": "No results"},
        "recovery_action": "retry_from_start",
        "card": {
            "run_id": "run-v4",
            "status": "failed",
            "error": {
                "code": "deep_research_no_results",
                "message": "No results",
            },
            "recovery_action": "retry_from_start",
            **public,
        },
        **public,
    }
    assert payload == expected
    assert canonical_json(payload).encode("utf-8") == (
        b'{"card":{"diagnostic_codes":["no_results"],"error":{"code":'
        b'"deep_research_no_results","message":"No results"},"metrics":'
        b'{"actual_requests":3,"busy_skips":1,"candidates":0,"cooldown_skips":4,'
        b'"empty":2,"hits":0,"probes":1,"queue_timeouts":0,'
        b'"rescue_considered_count":1,"rescue_executed_count":1,"timeouts":1},'
        b'"recovery_action":"retry_from_start","retry_action_id":"retry_from_start",'
        b'"run_id":"run-v4","skipped_stage_ids":["fetch","score","gap","rerank",'
        b'"synth","cite","persist"],"status":"failed"},"diagnostic_codes":'
        b'["no_results"],"error":{"code":"deep_research_no_results","message":'
        b'"No results"},"kind":"final","metrics":{"actual_requests":3,'
        b'"busy_skips":1,"candidates":0,"cooldown_skips":4,"empty":2,"hits":0,'
        b'"probes":1,"queue_timeouts":0,"rescue_considered_count":1,'
        b'"rescue_executed_count":1,"timeouts":1},"recovery_action":'
        b'"retry_from_start","retry_action_id":"retry_from_start",'
        b'"skipped_stage_ids":["fetch","score","gap","rerank","synth","cite",'
        b'"persist"],"status":"failed"}'
    )


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
    ids=(
        "unknown-top-level-field",
        "unknown-metric",
        "negative-metric",
        "diagnostic-not-allowlisted",
        "duplicate-stage",
        "retry-action-not-allowlisted",
    ),
)
@pytest.mark.h7_workflow_terminal_gate
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


@pytest.mark.h7_workflow_terminal_gate
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
