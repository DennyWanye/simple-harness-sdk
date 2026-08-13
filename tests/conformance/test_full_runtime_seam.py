# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H7 integrated runtime seam oracle.

The fakes below only script physical boundaries. Production collaborators must
perform Provider -> ReAct -> Kernel -> authorization -> durable Effect -> Tool
-> same Driver, and child -> signal -> root terminal -> delivery. T3.0 is RED
only while those planned public collaborators are absent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from simple_harness.contracts import CallId, ExecutionSessionId, RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy
from simple_harness.execution.contracts.children import (
    ProfileLaunchTicket,
    child_launch_fingerprint,
)
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
)
from simple_harness.runtime import RunStart, RuntimePorts, RuntimeProfile, build_runtime
from simple_harness.runtime.context import SqliteContextPort
from simple_harness.tools import ToolResult, ToolSpec
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
    # Planned public symbols. No production conformance_case/static-result hook.
    from simple_harness.runtime import AgentLoopCollaborator, EffectBatchExecutor
    from simple_harness.runtime.drivers import ReActDriver

    return SimpleNamespace(
        AgentLoopCollaborator=AgentLoopCollaborator,
        EffectBatchExecutor=EffectBatchExecutor,
        ReActDriver=ReActDriver,
    )


class ScriptedProvider:
    target = ProviderTarget("fixture", "model", "fixture:model", "local", "fixture")

    def __init__(self) -> None:
        self.responses: list[ProviderResponse | BaseException] = []
        self.transport_requests: list[ProviderRequest] = []

    async def invoke(
        self, request: ProviderRequest, *, cancel: CancelToken
    ) -> ProviderResponse:
        assert not cancel.is_cancelled
        self.transport_requests.append(request)
        scripted = self.responses.pop(0)
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
    def __init__(self) -> None:
        self.effect_ids: list[str] = []

    async def authorize(self, prepared) -> AuthorizationResult:
        self.effect_ids.append(prepared.effect_id.value)
        return AuthorizationResult(
            AuthorizationDecision.ALLOW,
            receipt_ref=f"auth:{prepared.effect_id.value}",
        )


class RecordingReconciliation:
    def __init__(self) -> None:
        self.effects: list[str] = []
        self.observation = ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN, "fixture:still-unknown"
        )

    async def observe(self, prepared) -> ReconciliationObservation:
        self.effects.append(prepared.effect_id.value)
        return self.observation


class RecordingTool:
    def __init__(self) -> None:
        self.handler_calls: list[dict[str, object]] = []
        self.release: asyncio.Event | None = None

    def invoke(self, arguments, context):
        del context
        self.handler_calls.append(dict(arguments))
        return ToolResult.succeeded(CallId("call-1"), {"answer": 42})


class RecordingSink:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.fail_once = False

    async def deliver(self, payload, *, idempotency_key: str) -> None:
        del payload
        self.keys.append(idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("fixture delivery failure")


class StaticCatalog:
    def current_generation(self) -> int:
        return 1


class StartupReconciliation:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile(self) -> None:
        self.calls += 1


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
    startup: StartupReconciliation

    async def start(self, run_id: str, input_value: dict[str, object]):
        value = RunStart(
            ExecutionSessionId("h7-session"),
            RunId(run_id),
            RequestId(f"request-{run_id}"),
            input_value,
            1,
        )
        await self.runtime.client.start(value)
        await self.runtime.wait_idle(value.run_id)
        return self.runtime.client.query(value.run_id)

    async def reopen(self) -> Seam:
        await self.runtime.close()
        self.database.close()
        return await make_seam(
            self.symbols,
            self.path,
            provider=self.provider,
            tool=self.tool,
            authorization=self.authorization,
            reconciliation=self.reconciliation,
            sink=self.sink,
        )


async def make_seam(
    symbols,
    path: Path,
    *,
    provider=None,
    tool=None,
    authorization=None,
    reconciliation=None,
    sink=None,
) -> Seam:
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    provider = provider or ScriptedProvider()
    tool = tool or RecordingTool()
    authorization = authorization or RecordingAuthorization()
    reconciliation = reconciliation or RecordingReconciliation()
    sink = sink or RecordingSink()
    startup = StartupReconciliation()
    registry = ToolRegistry()
    registry.register_function(
        ToolSpec(
            "calculator",
            "Calculate",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
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
        owner_id="h7-runtime",
        clock=lambda: 10.0,
    )
    context = SqliteContextPort(database, clock=lambda: 10.0)
    delivery = DeliveryDispatcher(uow, {"fixture": sink}, clock=lambda: 10.0)
    batch = symbols.EffectBatchExecutor(effects=effects, max_batch_size=32)
    collaborator = symbols.AgentLoopCollaborator(
        provider=provider_coordinator,
        effects=batch,
        context=context,
    )
    driver = symbols.ReActDriver(collaborator=collaborator)
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": driver},
        RuntimePorts(
            provider=provider_coordinator,
            tools=effects,
            authorization=authorization,
            context=context,
            delivery=delivery,
            tool_reconciliation=reconciliation,
            reconciliation=startup,
            tool_catalog=StaticCatalog(),
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
        startup,
    )


def response(*, content="", tools=()):
    return ProviderResponse(
        RequestId("scripted"),
        Message(MessageRole.ASSISTANT, content),
        tuple(tools),
        model="model",
        finish_reason="tool_calls" if tools else "stop",
    )


def calculator_call(index: int = 1) -> ProviderToolCall:
    return ProviderToolCall(CallId(f"call-{index}"), "calculator", {"x": index})


def _count(database: Database, sql: str, parameters=()) -> int:
    return int(database.connection.execute(sql, parameters).fetchone()[0])


def test_capability_snapshot_filters_provider_tools(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "capability.db")
        seam.provider.responses[:] = [
            response(tools=(calculator_call(),)),
            response(content="ok"),
        ]
        await seam.start(
            "run-capability",
            {"messages": [{"role": "user", "content": "go"}], "allowed_tools": []},
        )
        assert seam.provider.transport_requests[0].tools == ()
        assert seam.authorization.effect_ids == []
        assert seam.tool.handler_calls == []

    asyncio.run(case())


def test_provider_tool_batch_is_bounded_before_durable_admission(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "batch.db")
        seam.provider.responses[:] = [
            response(tools=tuple(calculator_call(i) for i in range(1, 34)))
        ]
        await seam.start("run-batch", {"allowed_tools": ["calculator"]})
        assert seam.authorization.effect_ids == []
        assert _count(seam.database, "SELECT count(*) FROM execution_effects") == 0

    asyncio.run(case())


def test_single_tool_uses_ledger_and_resumes_same_driver(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "roundtrip.db")
        seam.provider.responses[:] = [
            response(tools=(calculator_call(),)),
            response(content="42"),
        ]
        record = await seam.start("run-tool", {"allowed_tools": ["calculator"]})
        assert record.state is RunState.COMPLETED
        assert len(seam.provider.transport_requests) == 2
        assert seam.tool.handler_calls == [{"x": 1}]
        assert (
            _count(
                seam.database,
                "SELECT count(*) FROM execution_effects WHERE state='succeeded'",
            )
            == 1
        )
        assert seam.uow.read_react_checkpoint("run-tool").checkpoint["turns"] == 2

    asyncio.run(case())


def test_mixed_batch_waits_for_every_outcome(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "mixed.db")
        seam.provider.responses[:] = [
            response(tools=(calculator_call(1), calculator_call(2)))
        ]
        seam.reconciliation.observation = ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN, "late:call-2"
        )
        record = await seam.start("run-mixed", {"allowed_tools": ["calculator"]})
        assert record.state is RunState.WAITING
        assert len(seam.provider.transport_requests) == 1
        assert (
            _count(
                seam.database,
                "SELECT count(*) FROM execution_effects WHERE state='unknown'",
            )
            == 1
        )

    asyncio.run(case())


def test_late_tool_evidence_resumes_exactly_once(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "late.db")
        seam.provider.responses[:] = [response(tools=(calculator_call(),))]
        await seam.start(
            "run-late",
            {"allowed_tools": ["calculator"], "interrupt_after_handoff": True},
        )
        effect_id = seam.database.connection.execute(
            "SELECT effect_id FROM execution_effects"
        ).fetchone()[0]
        seam.reconciliation.observation = ReconciliationObservation(
            ReconciliationState.COMPLETED,
            "late:completed",
            ToolResult.succeeded(CallId("call-1"), {"answer": 42}),
        )
        seam.provider.responses[:] = [response(content="42")]
        reopened = await seam.reopen()
        assert (
            reopened.database.connection.execute(
                "SELECT effect_id FROM execution_effects"
            ).fetchone()[0]
            == effect_id
        )
        assert len(reopened.tool.handler_calls) == 0
        assert len(reopened.provider.transport_requests) == 2

    asyncio.run(case())


def test_child_provider_failure_binds_identity(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "child-provider.db")
        seam.provider.responses[:] = [RuntimeError("provider down")]
        await seam.start("root-child", {"delegate": "workflow.durable_task"})
        child = seam.database.connection.execute(
            "SELECT run_id,root_run_id,parent_run_id,state FROM runs WHERE parent_run_id IS NOT NULL"
        ).fetchone()
        assert tuple(child) == (
            "root-child:child:1",
            "root-child",
            "root-child",
            "failed",
        )
        assert (
            _count(
                seam.database,
                "SELECT count(*) FROM provider_invocations WHERE state='unknown'",
            )
            == 1
        )

    asyncio.run(case())


def test_attached_child_failure_reaches_parent_signal(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "child-signal.db")
        _seed_attached_child(seam)
        seam.uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-1",
            expected_child_version=0,
            terminal_state=RunState.FAILED,
            signal_id="signal-1",
            signal_payload={"code": "child_failed"},
            event_id="child-terminal",
            now=11.0,
        )
        row = seam.database.connection.execute(
            "SELECT parent_run_id,child_run_id,state FROM child_signals WHERE signal_id='signal-1'"
        ).fetchone()
        assert tuple(row) == ("parent-1", "child-1", "pending")

    asyncio.run(case())


def test_child_terminal_wakes_startup_reconciliation(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "child-wakeup.db")
        _seed_attached_child(seam)
        seam.uow.finalize_child_and_enqueue_parent_signal(
            command_id="command-1",
            expected_child_version=0,
            terminal_state=RunState.COMPLETED,
            signal_id="signal-1",
            signal_payload={"value": "ok"},
            event_id="child-terminal",
            now=11.0,
        )
        reopened = await seam.reopen()
        assert reopened.startup.calls == 1
        assert (
            _count(
                reopened.database,
                "SELECT count(*) FROM child_signals WHERE state='acked'",
            )
            == 1
        )

    asyncio.run(case())


def test_root_terminal_and_delivery_outbox_commit_atomically(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "terminal.db")
        seam.provider.responses[:] = [response(content="done")]
        record = await seam.start("run-terminal", {})
        assert record.state is RunState.COMPLETED
        assert (
            _count(
                seam.database,
                "SELECT count(*) FROM delivery_outbox WHERE run_id='run-terminal'",
            )
            == 1
        )

    asyncio.run(case())


def test_delivery_retry_never_reopens_terminal_root(production_seam_symbols, tmp_path):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "delivery-retry.db")
        seam.provider.responses[:] = [response(content="done")]
        await seam.start("run-delivery", {})
        seam.sink.fail_once = True
        assert await seam.runtime._ports.delivery.dispatch_one() is True
        assert seam.uow.read_run("run-delivery").state is RunState.COMPLETED
        assert await seam.runtime._ports.delivery.dispatch_one() is True
        assert seam.sink.keys == ["run-delivery:terminal", "run-delivery:terminal"]
        assert seam.uow.read_run("run-delivery").state is RunState.COMPLETED

    asyncio.run(case())


def test_malformed_terminal_projection_never_leaks_private_state(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "strict.db")
        seam.provider.responses[:] = [RuntimeError("secret-token private path")]
        await seam.start("run-strict", {})
        payload = seam.database.connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id='run-strict' AND kind='run.failed'"
        ).fetchone()[0]
        assert "secret-token" not in payload
        assert "private path" not in payload
        assert "driver_failed" in payload

    asyncio.run(case())


def test_restart_preserves_unknown_ids_without_transport_or_handler_replay(
    production_seam_symbols, tmp_path
):
    async def case():
        seam = await make_seam(production_seam_symbols, tmp_path / "restart-unknown.db")
        seam.provider.responses[:] = [response(tools=(calculator_call(),))]
        await seam.start(
            "run-unknown",
            {"allowed_tools": ["calculator"], "interrupt_after_handoff": True},
        )
        before = seam.database.connection.execute(
            "SELECT effect_id,request_hash FROM execution_effects WHERE state='unknown'"
        ).fetchone()
        provider_calls = len(seam.provider.transport_requests)
        handler_calls = len(seam.tool.handler_calls)
        reopened = await seam.reopen()
        after = reopened.database.connection.execute(
            "SELECT effect_id,request_hash FROM execution_effects WHERE state='unknown'"
        ).fetchone()
        assert tuple(after) == tuple(before)
        assert len(reopened.provider.transport_requests) == provider_calls
        assert len(reopened.tool.handler_calls) == handler_calls
        assert reopened.uow.read_run("run-unknown").state is RunState.WAITING

    asyncio.run(case())


def _seed_attached_child(seam: Seam) -> None:
    seam.uow.create_with_start_snapshot(
        execution_session_id="child-session",
        run_id="parent-1",
        request_id="request-parent",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="parent-created",
        now=1.0,
    )
    launch = {
        "profile_key": "workflow.durable_task",
        "driver_kind": "workflow",
        "catalog_generation": 1,
    }
    seam.uow.issue_profile_launch_ticket(
        ProfileLaunchTicket(
            "ticket-1",
            "parent-1",
            "workflow.durable_task",
            1,
            child_launch_fingerprint(launch),
        ),
        now=2.0,
    )
    seam.uow.claim_profile_launch_and_commit_child(
        ticket_id="ticket-1",
        expected_catalog_generation=1,
        launch_request=launch,
        command_id="command-1",
        child_run_id="child-1",
        request_id="request-child",
        attachment_policy="attached",
        start_snapshot={},
        event_id="child-created",
        now=3.0,
    )
