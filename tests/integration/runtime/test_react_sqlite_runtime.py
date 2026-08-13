# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

from simple_harness.contracts import ExecutionSessionId, RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderResponse, ProviderTarget
from simple_harness.runtime import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)
from simple_harness.runtime.drivers import ReActDriver
from simple_harness.tools import EffectExecutor, ToolRegistry
from simple_harness.tools.authorization import (
    AuthorizationDecision,
    AuthorizationResult,
)
from simple_harness.tools.reconciliation import (
    ReconciliationObservation,
    ReconciliationState,
)


class Provider:
    target = ProviderTarget("fixture", "model", "model", "local", "fixture")

    def __init__(self) -> None:
        self.requests = []

    async def invoke(self, request, *, cancel):
        assert not cancel.is_cancelled
        self.requests.append(request)
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model="model",
        )


class Noop:
    async def reconcile(self):
        return None


class Authorization:
    async def authorize(self, prepared):
        return AuthorizationResult(
            AuthorizationDecision.ALLOW,
            receipt_ref=f"auth:{prepared.effect_id.value}",
        )


class Reconciliation:
    async def observe(self, prepared):
        return ReconciliationObservation(
            ReconciliationState.STILL_UNKNOWN,
            f"unknown:{prepared.effect_id.value}",
        )


class Catalog:
    def current_generation(self) -> int:
        return 1


class Sink:
    async def deliver(self, payload, *, idempotency_key):
        del payload, idempotency_key


def test_real_runtime_provider_context_checkpoint_and_terminal_are_connected(
    tmp_path,
) -> None:
    async def case() -> None:
        database = Database.open(tmp_path / "react-runtime.db")
        uow = SqliteExecutionUnitOfWork(database)
        provider = Provider()
        authorization = Authorization()
        reconciliation = Reconciliation()
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=BudgetPolicy(),
            estimator=None,
            clock=lambda: 10.0,
        )
        effects = EffectExecutor(
            uow=uow,
            registry=ToolRegistry(),
            authorization=authorization,
            reconciliation=reconciliation,
            clock=lambda: 10.0,
        )
        context = SqliteContextPort(database, clock=lambda: 10.0)
        runtime = build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "react")},
            {
                "react": ReActDriver(
                    collaborator=AgentLoopCollaborator(),
                    effects=EffectBatchExecutor(),
                    clock=lambda: 10.0,
                )
            },
            RuntimePorts(
                provider=coordinator,
                tools=effects,
                authorization=authorization,
                context=context,
                delivery=DeliveryDispatcher(uow, {"fixture": Sink()}, clock=lambda: 10.0),
                tool_reconciliation=reconciliation,
                reconciliation=Noop(),
                react_checkpoint=uow,
                tool_catalog=Catalog(),
                owner_id="runtime-1",
                clock=lambda: 10.0,
            ),
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-1"),
                RunId("run-1"),
                RequestId("request-1"),
                {"messages": [{"role": "user", "content": "hello"}]},
                1,
            )
        )
        await runtime.wait_idle(RunId("run-1"))

        run = uow.read_run("run-1")
        assert run is not None and run.state is RunState.COMPLETED
        assert len(provider.requests) == 1
        assert database.connection.execute(
            "SELECT state FROM provider_invocations WHERE run_id='run-1'"
        ).fetchone()[0] == "succeeded"
        checkpoint = uow.read_react_checkpoint("run-1")
        assert checkpoint is not None
        assert checkpoint.checkpoint["provider_turns_reserved_total"] == 1
        assert [message.role for message in context.load(RunId("run-1")).messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        await runtime.close()
        database.close()

    asyncio.run(case())
