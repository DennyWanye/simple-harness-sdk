# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest

from simple_harness.contracts import (
    CallId,
    ExecutionSessionId,
    RequestId,
    RunId,
    canonical_json,
)
from simple_harness.contracts.messages import ContentBlock, Message, MessageRole
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.context_staging import (
    ContextStageKind,
    ContextStagingRepository,
)
from simple_harness.execution.delivery import DeliveryDispatcher
from simple_harness.execution.dispatch import ProviderBinding, ProviderInvocationCoordinator
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState
from simple_harness.providers import (
    CancelToken,
    ProviderResponse,
    ProviderTarget,
    ProviderToolCall,
)
from simple_harness.providers.base import (
    ProviderContinuationCapability,
    ProviderContinuationMode,
)
from simple_harness.runtime import (
    AgentIdentity,
    AgentLoopCollaborator,
    ConversationContinuationInput,
    ConversationTurnInput,
    DriverInvocation,
    EffectBatchExecutor,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    RuntimeServices,
    SqliteContextPort,
    StartSnapshot,
    build_runtime,
)
from simple_harness.runtime.conversation_context import prepare_sdk_conversation_context
from simple_harness.runtime.conversation_memory import (
    ConversationMemoryQueryStatus,
    ConversationMemoryRecallResult,
)
from simple_harness.runtime.drivers import ReActDriver
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.tools import EffectExecutor, FunctionTool, ToolRegistry, ToolResult, ToolSpec
from simple_harness.tools.authorization import (
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationResult,
)
from simple_harness.tools.reconciliation import (
    ReconciliationObservation,
    ReconciliationState,
)


class Provider:
    target = ProviderTarget("fixture", "model", "model", "local", "fixture")

    def __init__(self, *, private_response: bool = False) -> None:
        self.requests = []
        self.private_response = private_response

    async def invoke(self, request, *, cancel):
        assert not cancel.is_cancelled
        self.requests.append(request)
        if self.private_response:
            return ProviderResponse(
                request.request_id,
                Message(
                    MessageRole.ASSISTANT,
                    (
                        ContentBlock("output_text", {"text": "done"}),
                        ContentBlock("reasoning", {"text": "HIDDEN_REASONING_CANARY"}),
                    ),
                    metadata={"private": "PRIVATE_METADATA_CANARY"},
                ),
                model="model",
                opaque_continuation_ref="opaque-public-ref-1",
            )
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model="model",
        )


class OpaqueResolver:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def resolve(self, run_id: RunId) -> ProviderBinding:
        del run_id
        return ProviderBinding(
            self.provider,
            None,
            BudgetPolicy(),
            ProviderContinuationCapability(ProviderContinuationMode.OPAQUE_REFERENCE),
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


def test_context_snapshot_lineage_reopens_from_real_sqlite(tmp_path) -> None:
    path = tmp_path / "context-snapshot-lineage.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"catalog_generation": 1},
        event_id="event-1",
        now=1.0,
    )
    _, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="runtime-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )
    durable = DurableReactCheckpoint(uow, clock=lambda: 3.0)
    state, version = durable.load_or_create(RunId("run-1"), lease)
    state = replace(
        state,
        context_snapshot_revision=7,
        context_snapshot_bindings=(("snapshot-7", "a" * 64),),
    )
    durable.cas(RunId("run-1"), lease, version, state)
    database.close()

    reopened = Database.open(path)
    recovered = SqliteExecutionUnitOfWork(reopened)
    recovered_state, _ = DurableReactCheckpoint(recovered, clock=lambda: 4.0).load_or_create(
        RunId("run-1"), lease
    )
    assert recovered_state.context_snapshot_revision == 7
    assert recovered_state.context_snapshot_bindings == (("snapshot-7", "a" * 64),)
    reopened.close()


@pytest.mark.parametrize("private_response", (False, True))
def test_real_runtime_provider_context_checkpoint_and_terminal_are_connected(
    tmp_path, private_response: bool
) -> None:
    async def case() -> None:
        database = Database.open(tmp_path / "react-runtime.db")
        uow = SqliteExecutionUnitOfWork(database)
        provider = Provider(private_response=private_response)
        authorization = Authorization()
        reconciliation = Reconciliation()
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            resolver=OpaqueResolver(provider),
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
                provider_reconciliation=Noop(),
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
                "turn-1",
                {"messages": [{"role": "user", "content": "hello"}]},
                1,
            )
        )
        await runtime.wait_idle(RunId("run-1"))

        run = uow.read_run("run-1")
        assert run is not None and run.state is RunState.COMPLETED
        assert len(provider.requests) == 1
        assert (
            database.connection.execute(
                "SELECT state FROM provider_invocations WHERE run_id='run-1'"
            ).fetchone()[0]
            == "succeeded"
        )
        checkpoint = uow.read_react_checkpoint("run-1")
        assert checkpoint is not None
        assert checkpoint.checkpoint["provider_turns_reserved_total"] == 1
        assert [message.role for message in context.load(RunId("run-1")).messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        if private_response:
            provider_rows = database.connection.execute(
                "SELECT response_json FROM provider_invocations WHERE run_id='run-1'"
            ).fetchall()
            react_rows = database.connection.execute(
                "SELECT checkpoint_json FROM workflow_checkpoints "
                "WHERE run_id='run-1' AND namespace='react.termination.v1'"
            ).fetchall()
            context_rows = database.connection.execute(
                "SELECT checkpoint_json FROM workflow_checkpoints "
                "WHERE run_id='run-1' AND namespace='react.context.v1'"
            ).fetchall()
            for durable_rows in (provider_rows, react_rows, context_rows):
                assert durable_rows
                encoded = "\n".join(str(row[0]) for row in durable_rows)
                assert "HIDDEN_REASONING_CANARY" not in encoded
                assert "PRIVATE_METADATA_CANARY" not in encoded
            assistant = context.load(RunId("run-1")).messages[-1]
            assert assistant.metadata == {}
            assert [block.type for block in assistant.content] == ["output_text"]
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_continuation_staged_memory_is_frozen_as_untrusted_user_context(
    tmp_path,
) -> None:
    class Recall:
        async def recall_bounded(self, query):  # type: ignore[no-untyped-def]
            payload = {"items": [{"text": "remembered preference"}]}
            encoded = canonical_json(payload).encode()
            return ConversationMemoryRecallResult(
                query.context_query_id,
                "memory-result-1",
                query.query_hash,
                payload,
                hashlib.sha256(encoded).hexdigest(),
                ConversationMemoryQueryStatus.COMPLETE,
                1,
                len(encoded),
            )

        async def release(self, *, user_id: str, context_query_id: str, result_hash: str) -> None:
            del user_id, context_query_id, result_hash

        async def close(self) -> None:
            return None

    async def case() -> None:
        database = Database.open(tmp_path / "react-continuation-memory.db")
        uow = SqliteExecutionUnitOfWork(database)
        root_message = Message(MessageRole.USER, "root question")
        root_conversation = ConversationTurnInput(
            AgentIdentity("deployment-1", "household-1", "user-1", "session-1"),
            root_message,
            "root question",
        )
        start = StartSnapshot(
            profile_key="agent.general",
            driver_kind="react",
            turn_id="turn-1",
            tool_catalog_generation=1,
            input={"messages": [root_message.to_dict()]},
            conversation=root_conversation,
        )
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot=start.to_json(),
            event_id="run-1:created",
            user_id="user-1",
            now=1.0,
        )
        run, lease = uow.claim_runtime_activation(
            run_id="run-1",
            owner_id="runtime-1",
            namespace="runtime.kernel",
            now=2.0,
            lease_ttl_seconds=100.0,
        )
        fence = await uow.acquire(RunId("run-1"), lease, now=2.0)
        context = SqliteContextPort(database, clock=lambda: 3.0)
        context.append(
            RunId("run-1"),
            lease,
            0,
            "run-1:context:root-fixture",
            (root_message,),
        )
        next_message = Message(MessageRole.USER, "next question")
        next_value = ConversationTurnInput(
            AgentIdentity("deployment-1", "household-1", "user-1", "session-1"),
            next_message,
            "next question",
        )
        staged = await prepare_sdk_conversation_context(
            ContextStagingRepository(database),
            Recall(),
            stage_id="stage-continuation-1",
            kind=ContextStageKind.CONTINUATION,
            identity_key="continuation-1",
            value=next_value,
            owner_id="runtime-1",
            now=lambda: 3.0,
            lease_seconds=30.0,
            max_items=8,
            max_bytes=4096,
            timeout_seconds=0.5,
        )
        assert staged.private_snapshot is not None
        continuation_value = ConversationContinuationInput(next_message, "next question")
        uow.enqueue_continuation(
            continuation_id="continuation-1",
            run_id="run-1",
            payload={
                "kind": "conversation_user",
                "conversation": continuation_value.to_json(),
                "prepared_context": dict(staged.private_snapshot),
            },
            context_stage_id=staged.stage_id,
            context_stage_hash=staged.private_snapshot_hash,
            now=3.0,
        )
        continuation = uow.claim_continuation(run_id="run-1", execution_lease=lease, now=4.0)
        assert continuation is not None

        provider = Provider()
        authorization = Authorization()
        reconciliation = Reconciliation()
        services = RuntimeServices(
            provider=ProviderInvocationCoordinator(
                uow=uow,
                provider=provider,
                budget_policy=BudgetPolicy(),
                estimator=None,
                clock=lambda: 5.0,
            ),
            tools=EffectExecutor(
                uow=uow,
                registry=ToolRegistry(),
                authorization=authorization,
                reconciliation=reconciliation,
                clock=lambda: 5.0,
            ),
            authorization=authorization,
            context=context,
            delivery=DeliveryDispatcher(uow, {"fixture": Sink()}, clock=lambda: 5.0),
            tool_reconciliation=reconciliation,
            reconciliation=Noop(),
            provider_reconciliation=Noop(),
            react_checkpoint=uow,
            tool_catalog=Catalog(),
        )
        result = await ReActDriver(clock=lambda: 5.0).start(
            DriverInvocation(
                run,
                start,
                lease,
                fence,
                services,
                continuations=(continuation,),
            ),
            context=context,
            cancel=CancelToken(),
        )
        assert result.state is RunState.COMPLETED
        assert len(provider.requests) == 1
        request_messages = provider.requests[0].messages
        assert [message.role for message in request_messages] == [
            MessageRole.USER,
            MessageRole.USER,
            MessageRole.USER,
        ]
        recalled, current = request_messages[-2:]
        assert recalled.metadata["trust"] == "untrusted_data"
        assert recalled.metadata["source"] == "memory"
        assert current == next_message
        frozen_messages = context.load(RunId("run-1")).messages
        assert frozen_messages[1:3] == (recalled, current)
        assert all(message.role is not MessageRole.SYSTEM for message in frozen_messages[1:3])
        database.close()

    asyncio.run(case())


class AuthorizationScenario:
    def __init__(
        self,
        *,
        expires_at: float | None = None,
        fail_decision_binding: bool = False,
        fail_handoff_binding: bool = False,
    ) -> None:
        self.expires_at = expires_at
        self.fail_decision_binding = fail_decision_binding
        self.fail_handoff_binding = fail_handoff_binding
        self.decision_bind_calls = 0
        self.handoff_bind_calls = 0

    async def prepare(self, prepared):
        del prepared
        return AuthorizationResult(
            AuthorizationDecision.REQUIRE_USER,
            reason_code="confirmation_required",
            public_message="Confirm.",
            request=AuthorizationRequest(
                "Write the note?", "untrusted-host-nonce", self.expires_at
            ),
        )

    async def bind_decision(self, prepared, request, decision, sdk_receipt):
        del prepared, request, decision
        self.decision_bind_calls += 1
        if self.fail_decision_binding:
            raise RuntimeError("host decision receipt unavailable")
        return AuthorizationReceipt(
            f"host:decision:{self.decision_bind_calls}",
            hashlib.sha256(f"host:decision:{self.decision_bind_calls}".encode()).hexdigest(),
            sdk_receipt.receipt_hash,
        )

    async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):
        del prepared, authorization_receipt_ref
        self.handoff_bind_calls += 1
        if self.fail_handoff_binding:
            raise RuntimeError("host handoff receipt unavailable")
        return AuthorizationReceipt(
            f"host:handoff:{self.handoff_bind_calls}",
            hashlib.sha256(f"host:handoff:{self.handoff_bind_calls}".encode()).hexdigest(),
            sdk_receipt.receipt_hash,
        )


class PhysicalToolCounter:
    def __init__(self) -> None:
        self.calls = 0


def authorization_runtime(
    database_path,
    *,
    authorization: AuthorizationScenario,
    physical: PhysicalToolCounter,
    owner_id: str,
    clock,
    emit_tool_call: bool = True,
):
    class ScenarioProvider(Provider):
        async def invoke(self, request, *, cancel):
            assert not cancel.is_cancelled
            self.requests.append(request)
            if emit_tool_call and len(self.requests) == 1:
                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, "use tool"),
                    tool_calls=(ProviderToolCall(CallId("raw-fault"), "write_note", {}),),
                    model="model",
                )
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "done"),
                model="model",
            )

    database = Database.open(database_path)
    uow = SqliteExecutionUnitOfWork(database)
    provider = ScenarioProvider()
    reconciliation = Reconciliation()
    registry = ToolRegistry()

    async def write_note(arguments, context):
        del arguments, context
        physical.calls += 1
        return ToolResult.succeeded(CallId("raw-fault"))

    registry.register(
        FunctionTool(
            ToolSpec(
                "write_note",
                "Write a note.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            write_note,
        )
    )
    effects = EffectExecutor(
        uow=uow,
        registry=registry,
        authorization=authorization,
        reconciliation=reconciliation,
        clock=clock,
    )
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "react")},
        {"react": ReActDriver(clock=clock)},
        RuntimePorts(
            provider=ProviderInvocationCoordinator(
                uow=uow,
                provider=provider,
                budget_policy=BudgetPolicy(),
                estimator=FrozenPriceEstimator("price-v1", "model", 0, 0),
                clock=clock,
            ),
            tools=effects,
            authorization=authorization,
            context=SqliteContextPort(database, clock=clock),
            delivery=DeliveryDispatcher(uow, {"fixture": Sink()}, clock=clock),
            tool_reconciliation=reconciliation,
            reconciliation=Noop(),
            provider_reconciliation=Noop(),
            react_checkpoint=uow,
            tool_catalog=Catalog(),
            owner_id=owner_id,
            clock=clock,
        ),
    )
    return runtime, uow, database


async def start_authorization_wait(runtime, uow):
    await runtime.start()
    await runtime.client.start(
        RunStart(
            ExecutionSessionId("session-fault"),
            RunId("run-fault"),
            RequestId("request-fault"),
            "turn-fault",
            {
                "messages": [{"role": "user", "content": "write"}],
                "capability_snapshot": {"tools": ["write_note"]},
                "max_output_tokens": 100,
            },
            1,
        )
    )
    await runtime.wait_idle(RunId("run-fault"))
    row = uow.database.connection.execute(
        "SELECT decision_id FROM decisions WHERE run_id='run-fault'"
    ).fetchone()
    assert row is not None
    decision = uow.read_decision(str(row[0]))
    assert decision is not None and decision.state.value == "open"
    return decision


async def wait_for_scenario(runtime, predicate) -> None:
    for _ in range(100):
        await asyncio.sleep(0)
        await runtime.wait_idle(RunId("run-fault"))
        if predicate():
            return
    raise AssertionError("authorization scenario did not settle")


def test_require_user_is_durable_and_double_bound_before_tool_handoff(tmp_path) -> None:
    class ToolProvider(Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, "use tool"),
                    tool_calls=(ProviderToolCall(CallId("raw-1"), "write_note", {}),),
                    model="model",
                )
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "done"),
                model="model",
            )

    class UserAuthorization:
        async def prepare(self, prepared):
            return AuthorizationResult(
                AuthorizationDecision.REQUIRE_USER,
                reason_code="confirmation_required",
                public_message="Confirm.",
                request=AuthorizationRequest("Write the note?", "host-nonce"),
            )

        async def bind_decision(self, prepared, request, decision, sdk_receipt):
            del prepared, request, decision
            return AuthorizationReceipt(
                "host:decision",
                hashlib.sha256(b"host:decision").hexdigest(),
                sdk_receipt.receipt_hash,
            )

        async def bind_effect_handoff(self, prepared, authorization_receipt_ref, sdk_receipt):
            del prepared, authorization_receipt_ref
            return AuthorizationReceipt(
                "host:handoff",
                hashlib.sha256(b"host:handoff").hexdigest(),
                sdk_receipt.receipt_hash,
            )

    async def case() -> None:
        database = Database.open(tmp_path / "react-authorization.db")
        uow = SqliteExecutionUnitOfWork(database)
        provider = ToolProvider()
        authorization = UserAuthorization()
        reconciliation = Reconciliation()
        coordinator = ProviderInvocationCoordinator(
            uow=uow,
            provider=provider,
            budget_policy=BudgetPolicy(),
            estimator=FrozenPriceEstimator("price-v1", "model", 0, 0),
            clock=lambda: 10.0,
        )
        physical_calls = 0

        registry = ToolRegistry()

        async def write_note(arguments, context):
            nonlocal physical_calls
            del arguments
            physical_calls += 1
            # accepted_result_call_id lets the registry remap the raw Provider id.
            return ToolResult.succeeded(CallId("raw-1"))

        registry.register(
            FunctionTool(
                ToolSpec(
                    "write_note",
                    "Write a note.",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
                write_note,
            )
        )
        effects = EffectExecutor(
            uow=uow,
            registry=registry,
            authorization=authorization,
            reconciliation=reconciliation,
            clock=lambda: 10.0,
        )
        runtime = build_runtime(
            uow,
            {"agent.general": RuntimeProfile("agent.general", "react")},
            {"react": ReActDriver(clock=lambda: 10.0)},
            RuntimePorts(
                provider=coordinator,
                tools=effects,
                authorization=authorization,
                context=SqliteContextPort(database, clock=lambda: 10.0),
                delivery=DeliveryDispatcher(uow, {"fixture": Sink()}, clock=lambda: 10.0),
                tool_reconciliation=reconciliation,
                reconciliation=Noop(),
                provider_reconciliation=Noop(),
                react_checkpoint=uow,
                tool_catalog=Catalog(),
                owner_id="runtime-auth",
                clock=lambda: 10.0,
            ),
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-auth"),
                RunId("run-auth"),
                RequestId("request-auth"),
                "turn-auth",
                {
                    "messages": [{"role": "user", "content": "write"}],
                    "capability_snapshot": {"tools": ["write_note"]},
                    "max_output_tokens": 100,
                },
                1,
            )
        )
        await runtime.wait_idle(RunId("run-auth"))
        decision_id = str(
            database.connection.execute(
                "SELECT decision_id FROM decisions WHERE run_id='run-auth'"
            ).fetchone()[0]
        )
        decision = uow.read_decision(decision_id)
        assert decision is not None and decision.state.value == "open"
        assert physical_calls == 0
        try:
            await runtime.client.decide_authorization(
                RunId("run-auth"),
                decision_id=decision.decision_id,
                nonce="wrong",
                expected_version=decision.version,
                decision=AuthorizationDecision.ALLOW,
            )
        except Exception as error:
            assert getattr(error, "code", None) == "authorization_decision_nonce_mismatch"
        else:
            raise AssertionError("wrong nonce must fail closed")
        await runtime.client.decide_authorization(
            RunId("run-auth"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        await asyncio.sleep(0)
        await runtime.wait_idle(RunId("run-auth"))
        await asyncio.sleep(0)
        await runtime.wait_idle(RunId("run-auth"))
        assert physical_calls == 1
        effect = database.connection.execute(
            "SELECT authorization_receipt_ref,handoff_receipt_ref,state "
            "FROM execution_effects WHERE run_id='run-auth'"
        ).fetchone()
        assert effect is not None and effect[2] == "succeeded"
        assert str(effect[0]).startswith("authorization-binding-v1:")
        assert str(effect[1]).startswith("authorization-binding-v1:")
        await runtime.close()
        database.close()

    asyncio.run(case())


@pytest.mark.parametrize(
    ("outcome", "expected_decision_state", "expected_run_state"),
    [
        (AuthorizationDecision.DENY, "denied", RunState.FAILED),
        (AuthorizationDecision.ALLOW, "expired", RunState.FAILED),
    ],
)
def test_authorization_deny_and_expiry_fail_before_physical_tool(
    tmp_path, outcome, expected_decision_state, expected_run_state
) -> None:
    async def case() -> None:
        now = [10.0]
        authorization = AuthorizationScenario(
            expires_at=11.0 if expected_decision_state == "expired" else None
        )
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / f"authorization-{expected_decision_state}.db",
            authorization=authorization,
            physical=physical,
            owner_id=f"runtime-{expected_decision_state}",
            clock=lambda: now[0],
        )
        decision = await start_authorization_wait(runtime, uow)
        if expected_decision_state == "expired":
            now[0] = 12.0
        resolved = await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=outcome,
        )
        assert resolved.state.value == expected_decision_state
        assert uow.read_run("run-fault").state is expected_run_state
        assert physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_authorization_identity_fences_and_duplicate_allow_are_fail_closed(
    tmp_path,
) -> None:
    async def case() -> None:
        authorization = AuthorizationScenario()
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "authorization-fences.db",
            authorization=authorization,
            physical=physical,
            owner_id="runtime-fences",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        attempts = (
            (RunId("wrong-run"), str(decision.request["nonce"]), decision.version),
            (RunId("run-fault"), "wrong-nonce", decision.version),
            (
                RunId("run-fault"),
                str(decision.request["nonce"]),
                decision.version + 1,
            ),
        )
        expected_codes = (
            "authorization_decision_not_found",
            "authorization_decision_nonce_mismatch",
            "authorization_decision_version_conflict",
        )
        for (run_id, nonce, version), expected_code in zip(attempts, expected_codes, strict=True):
            with pytest.raises(Exception) as caught:
                await runtime.client.decide_authorization(
                    run_id,
                    decision_id=decision.decision_id,
                    nonce=nonce,
                    expected_version=version,
                    decision=AuthorizationDecision.ALLOW,
                )
            assert getattr(caught.value, "code", None) == expected_code
            assert physical.calls == 0
            assert authorization.decision_bind_calls == 0

        first = await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        duplicate = await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        assert duplicate == first
        assert authorization.decision_bind_calls == 1
        await wait_for_scenario(runtime, lambda: physical.calls == 1)
        assert physical.calls == 1
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_cancelled_authorization_cannot_be_bound_or_invoke_tool(tmp_path) -> None:
    async def case() -> None:
        authorization = AuthorizationScenario()
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "authorization-cancel.db",
            authorization=authorization,
            physical=physical,
            owner_id="runtime-cancel",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        cancelled = await runtime.client.cancel(RunId("run-fault"))
        assert cancelled.state is RunState.CANCELLED
        durable_decision = uow.read_decision(decision.decision_id)
        assert durable_decision is not None
        assert durable_decision.state.value == "cancelled"
        with pytest.raises(Exception) as caught:
            await runtime.client.decide_authorization(
                RunId("run-fault"),
                decision_id=decision.decision_id,
                nonce=str(decision.request["nonce"]),
                expected_version=decision.version,
                decision=AuthorizationDecision.ALLOW,
            )
        assert getattr(caught.value, "code", None) == "authorization_decision_late"
        assert authorization.decision_bind_calls == 0
        assert physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_open_authorization_survives_runtime_restart_and_invokes_at_most_once(
    tmp_path,
) -> None:
    async def case() -> None:
        database_path = tmp_path / "authorization-restart.db"
        authorization = AuthorizationScenario()
        physical = PhysicalToolCounter()
        first, first_uow, first_database = authorization_runtime(
            database_path,
            authorization=authorization,
            physical=physical,
            owner_id="runtime-before-restart",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(first, first_uow)
        await first.close()
        first_database.close()

        second, second_uow, second_database = authorization_runtime(
            database_path,
            authorization=authorization,
            physical=physical,
            owner_id="runtime-after-restart",
            clock=lambda: 10.0,
            emit_tool_call=False,
        )
        await second.start()
        reopened = second_uow.read_decision(decision.decision_id)
        assert reopened is not None and reopened.state.value == "open"
        assert second_uow.read_run("run-fault").state is RunState.WAITING
        await second.client.decide_authorization(
            RunId("run-fault"),
            decision_id=reopened.decision_id,
            nonce=str(reopened.request["nonce"]),
            expected_version=reopened.version,
            decision=AuthorizationDecision.ALLOW,
        )
        await wait_for_scenario(second, lambda: physical.calls == 1)
        assert physical.calls == 1
        await second.close()
        second_database.close()

    asyncio.run(case())


def test_permanent_host_decision_binding_failure_keeps_open_and_never_invokes(
    tmp_path,
) -> None:
    async def case() -> None:
        authorization = AuthorizationScenario(fail_decision_binding=True)
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "authorization-decision-bind-failure.db",
            authorization=authorization,
            physical=physical,
            owner_id="runtime-decision-bind-failure",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="decision receipt unavailable"):
                await runtime.client.decide_authorization(
                    RunId("run-fault"),
                    decision_id=decision.decision_id,
                    nonce=str(decision.request["nonce"]),
                    expected_version=decision.version,
                    decision=AuthorizationDecision.ALLOW,
                )
        still_open = uow.read_decision(decision.decision_id)
        assert still_open is not None and still_open.state.value == "open"
        assert uow.read_run("run-fault").state is RunState.WAITING
        assert authorization.decision_bind_calls == 2
        assert physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())


def test_host_handoff_binding_failure_stops_before_physical_tool(tmp_path) -> None:
    async def case() -> None:
        authorization = AuthorizationScenario(fail_handoff_binding=True)
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "authorization-handoff-bind-failure.db",
            authorization=authorization,
            physical=physical,
            owner_id="runtime-handoff-bind-failure",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        await wait_for_scenario(runtime, lambda: uow.read_run("run-fault").state is RunState.FAILED)
        effect = database.connection.execute(
            "SELECT state,handoff_receipt_ref FROM execution_effects WHERE run_id='run-fault'"
        ).fetchone()
        assert effect is not None and tuple(effect) == ("prepared", None)
        assert authorization.handoff_bind_calls == 1
        assert physical.calls == 0
        await runtime.close()
        database.close()

    asyncio.run(case())
