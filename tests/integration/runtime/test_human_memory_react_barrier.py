# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace

import pytest

from simple_harness.contracts import (
    CallId,
    EffectId,
    JsonValue,
    Message,
    MessageRole,
    RequestId,
    RunId,
    canonical_json,
    fingerprint_json,
    freeze_json,
    thaw_json,
)
from simple_harness.execution.context_authority import (
    ContextRouteReceipt,
    RunContextSnapshot,
)
from simple_harness.execution.effects import TaskExecutionEnvelope
from simple_harness.execution.provider_invocations import provider_request_fingerprint
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderToolSpec,
)
from simple_harness.runtime.disclosure_protocol import (
    DeliveryRecipient,
    DisclosureContext,
    DisclosureGeneration,
    DisclosurePurpose,
    DisclosureReasonCode,
    DisclosureSource,
    DisclosureTrust,
    IntendedAudience,
)
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    ReActLoop,
    ReActRunInput,
)
from simple_harness.runtime.evidence_protocol import (
    EvidenceRef,
    EvidenceSourceKind,
    RemovedSpanSummary,
    RemovedSpanType,
    SanitizedEvidenceEnvelope,
)
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute
from simple_harness.runtime.termination import TerminationLimits
from simple_harness.tools import CancellationToken, ToolResult
from simple_harness.tools.executor import EffectExecution
from simple_harness.tools.runtime_catalog import (
    ToolEffectClass,
    ToolExecutionPolicy,
    ToolRouteRequirement,
    ToolTaskScopeRequirement,
)

from .react_fakes import (
    FENCE,
    LEASE,
    Checkpoint,
    MemoryContext,
    ScriptedProviderCoordinator,
    response,
    services,
)


class PolicyExposure:
    def restore(self, run_id: RunId, checkpoint: JsonValue | None) -> None:
        del run_id, checkpoint

    def provider_specs(self, run_id: RunId) -> tuple[ProviderToolSpec, ...]:
        del run_id
        schema = {"type": "object", "additionalProperties": False}
        return (
            ProviderToolSpec("context_route", "Route Context", schema),
            ProviderToolSpec("write_project", "Write project", schema),
        )

    def execution_policy(self, run_id: RunId, provider_name: str) -> ToolExecutionPolicy:
        del run_id
        if provider_name == "context_route":
            return ToolExecutionPolicy(
                "host:context_route",
                "a" * 64,
                ToolEffectClass.CONTEXT_CONTROL,
                ToolRouteRequirement.FORBIDDEN,
                ToolTaskScopeRequirement.FORBIDDEN,
            )
        return ToolExecutionPolicy(
            "host:write_project",
            "b" * 64,
            ToolEffectClass.PROJECT_EFFECT,
            ToolRouteRequirement.REQUIRED,
            ToolTaskScopeRequirement.REQUIRED,
        )

    def observe_tool_result(
        self, run_id: RunId, tool_name: str, result: Mapping[str, object]
    ) -> None:
        del run_id, tool_name, result

    def checkpoint(self, run_id: RunId) -> JsonValue:
        del run_id
        return {"catalog_fingerprint": "c" * 64}


class RouteEffectExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **values):  # type: ignore[no-untyped-def]
        self.calls.append(values)
        call = values["call"]
        context = values["context"]
        assert context.effect_id is not None
        receipt = ContextRouteReceipt(
            "route-1",
            context.run_id.value,
            str(values["raw_call_id"]),
            context.effect_id.value,
            TaskScopeRoute.DIRECT_STANDALONE,
            None,
            None,
        )
        return EffectExecution(None, ToolResult.succeeded(call.call_id, receipt.to_json()))


def _loop(tools) -> ReActLoop:  # type: ignore[no-untyped-def]
    del tools
    return ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(4, 8, 30, 1000, 2)),
        effects=EffectBatchExecutor(),
        clock=lambda: 1.0,
    )


def test_same_batch_project_effect_cannot_cross_context_route_barrier() -> None:
    first = ProviderResponse(
        RequestId("fixture"),
        response("calling").message,
        (
            ProviderToolCall(CallId("raw-route"), "context_route", {}),
            ProviderToolCall(CallId("raw-write"), "write_project", {}),
        ),
    )
    provider = ScriptedProviderCoordinator([first, response("done")])
    tools = RouteEffectExecutor()
    result = asyncio.run(
        _loop(tools).run(
            ReActRunInput(RunId("run-1"), RequestId("request-1"), tool_exposure=PolicyExposure()),
            services=services(provider, tools, MemoryContext()),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )

    assert [item["call"].name for item in tools.calls] == ["context_route"]
    rejected_payload = provider.calls[1][1].messages[-1].content
    assert "ROUTE_BARRIER_NOT_OBSERVED" in str(rejected_payload)
    assert result.termination.route_state == "routed_standalone"


class ContextAuthority:
    def __init__(
        self,
        context: MemoryContext,
        *,
        snapshot_ids: tuple[str, ...] = ("snapshot-1",),
        snapshot_revisions: tuple[int, ...] = (1,),
    ) -> None:
        self.context = context
        self.calls = []
        self.snapshot_ids = snapshot_ids
        self.snapshot_revisions = snapshot_revisions

    async def prepare_snapshot(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        provider_request = ProviderRequest(
            RequestId("hash-only"),
            tuple(self.context.messages),
            metadata={"authority": "host"},
        )
        index = len(self.calls) - 1
        return RunContextSnapshot(
            self.snapshot_ids[index],
            request.run_id.value,
            request.provider_turn_ordinal,
            request.prior_context_revision,
            self.snapshot_revisions[index],
            {"host": self.snapshot_revisions[index]},
            provider_request.messages,
            (),
            None,
            None,
            provider_request.metadata,
            provider_request_fingerprint(provider_request),
        )


class NoRecallSink:
    async def record_no_recall(self, **values):  # type: ignore[no-untyped-def]
        return ContextRouteReceipt(
            "no-recall-1",
            values["run_id"].value,
            "terminal-direct",
            "terminal-direct",
            TaskScopeRoute.DIRECT_STANDALONE,
            None,
            None,
        )


class StaleTaskExecutionAuthority:
    async def issue_envelope(self, request):  # type: ignore[no-untyped-def]
        return TaskExecutionEnvelope(
            request.run_id,
            CallId(request.call_id),
            EffectId(request.effect_id),
            request.raw_call_id,
            request.turn_ordinal,
            request.call_ordinal,
            request.tool_name,
            request.policy.capability_id,
            request.policy.capability_fingerprint,
            request.route_receipt.receipt_id,
            request.route_receipt.receipt_hash,
            request.route_receipt.task_scope_id,
            "root-1",
            "d" * 64,
            request.route_receipt.binding_set_revision,
            request.effect_id,
            binding_set_receipt_id=request.route_receipt.binding_set_receipt_id,
            binding_set_receipt_hash="f" * 64,
        )


class FixedBindingTaskExecutionAuthority:
    def __init__(self, revision: int, receipt_id: str, receipt_hash: str) -> None:
        self.revision = revision
        self.receipt_id = receipt_id
        self.receipt_hash = receipt_hash

    async def issue_envelope(self, request):  # type: ignore[no-untyped-def]
        return TaskExecutionEnvelope(
            request.run_id,
            CallId(request.call_id),
            EffectId(request.effect_id),
            request.raw_call_id,
            request.turn_ordinal,
            request.call_ordinal,
            request.tool_name,
            request.policy.capability_id,
            request.policy.capability_fingerprint,
            request.route_receipt.receipt_id,
            request.route_receipt.receipt_hash,
            request.route_receipt.task_scope_id,
            "new-root",
            "9" * 64,
            self.revision,
            request.effect_id,
            binding_set_receipt_id=self.receipt_id,
            binding_set_receipt_hash=self.receipt_hash,
        )


class WrongRunContextAuthority(ContextAuthority):
    async def prepare_snapshot(self, request):  # type: ignore[no-untyped-def]
        snapshot = await super().prepare_snapshot(request)
        return RunContextSnapshot(
            snapshot.snapshot_id,
            "another-run",
            snapshot.provider_turn_ordinal,
            snapshot.prior_context_revision,
            snapshot.snapshot_revision,
            snapshot.source_revisions,
            snapshot.messages,
            snapshot.tools,
            snapshot.temperature,
            snapshot.max_output_tokens,
            snapshot.metadata,
            snapshot.expected_request_fingerprint,
        )


def test_host_context_snapshot_is_exact_provider_request_and_no_recall_is_durable() -> None:
    context = MemoryContext()
    authority = ContextAuthority(context)
    runtime_services = services(
        ScriptedProviderCoordinator([response("done")]), RouteEffectExecutor(), context
    )
    runtime_services = replace(
        runtime_services,
        run_context_authority=authority,
        runtime_decision_sink=NoRecallSink(),
    )
    result = asyncio.run(
        _loop(RouteEffectExecutor()).run(
            ReActRunInput(RunId("run-1"), RequestId("request-1")),
            services=runtime_services,
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )

    request = runtime_services.provider.calls[0][1]
    assert request.metadata["authority"] == "host"
    assert len(authority.calls) == 1
    assert result.termination.route_state == "routed_standalone"
    assert result.termination.context_authority_receipt_hash is not None


def test_host_context_authority_without_no_recall_sink_fails_closed() -> None:
    context = MemoryContext()
    runtime_services = services(
        ScriptedProviderCoordinator([response("done")]), RouteEffectExecutor(), context
    )
    runtime_services = replace(
        runtime_services,
        run_context_authority=ContextAuthority(context),
    )
    with pytest.raises(RuntimeError, match="no-recall decision sink"):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )


def test_wrong_run_host_context_snapshot_fails_before_provider() -> None:
    context = MemoryContext()
    provider = ScriptedProviderCoordinator([response("must-not-run")])
    runtime_services = replace(
        services(provider, RouteEffectExecutor(), context),
        run_context_authority=WrongRunContextAuthority(context),
        runtime_decision_sink=NoRecallSink(),
    )
    with pytest.raises(RuntimeError, match="lineage differs"):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert provider.calls == []


def test_stale_task_execution_binding_fails_before_tool_execution() -> None:
    tools = RouteEffectExecutor()
    runtime_services = replace(
        services(ScriptedProviderCoordinator([]), tools, MemoryContext()),
        task_execution_authority=StaleTaskExecutionAuthority(),
    )
    route_receipt = ContextRouteReceipt(
        "route-task-1",
        "run-1",
        "raw-route",
        "effect-route",
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        3,
        "binding-set-3",
        "e" * 64,
    )
    with pytest.raises(RuntimeError, match="stale TaskScope binding authority"):
        asyncio.run(
            EffectBatchExecutor().execute(
                (ProviderToolCall(CallId("raw-write"), "write_project", {}),),
                services=runtime_services,
                run_id=RunId("run-1"),
                request_id=RequestId("request-1"),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancellation=CancellationToken(),
                turn_ordinal=2,
                tool_exposure=PolicyExposure(),
                route_receipt=route_receipt,
            )
        )
    assert tools.calls == []


def test_new_binding_revision_is_unusable_until_a_later_trusted_route_freezes_it() -> None:
    tools = RouteEffectExecutor()
    authority = FixedBindingTaskExecutionAuthority(4, "binding-set-4", "f" * 64)
    runtime_services = replace(
        services(ScriptedProviderCoordinator([]), tools, MemoryContext()),
        task_execution_authority=authority,
    )
    current_route = ContextRouteReceipt(
        "route-task-3",
        "run-1",
        "raw-route-3",
        "effect-route-3",
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        3,
        "binding-set-3",
        "e" * 64,
    )
    with pytest.raises(RuntimeError, match="stale TaskScope binding authority"):
        asyncio.run(
            EffectBatchExecutor().execute(
                (ProviderToolCall(CallId("raw-write-current"), "write_project", {}),),
                services=runtime_services,
                run_id=RunId("run-1"),
                request_id=RequestId("request-current"),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancellation=CancellationToken(),
                turn_ordinal=2,
                tool_exposure=PolicyExposure(),
                route_receipt=current_route,
            )
        )
    assert tools.calls == []

    later_route = ContextRouteReceipt(
        "route-task-4",
        "run-2",
        "raw-route-4",
        "effect-route-4",
        TaskScopeRoute.RESUME_EXISTING,
        "task-1",
        4,
        "binding-set-4",
        "f" * 64,
    )
    asyncio.run(
        EffectBatchExecutor().execute(
            (ProviderToolCall(CallId("raw-write-later"), "write_project", {}),),
            services=runtime_services,
            run_id=RunId("run-2"),
            request_id=RequestId("request-later"),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancellation=CancellationToken(),
            turn_ordinal=1,
            tool_exposure=PolicyExposure(),
            route_receipt=later_route,
        )
    )
    assert [call["call"].name for call in tools.calls] == ["write_project"]


def test_provider_reserved_reopen_reuses_frozen_host_context_receipt() -> None:
    context = MemoryContext()
    checkpoint = Checkpoint()
    authority = ContextAuthority(context)
    first_services = services(
        ScriptedProviderCoordinator([RuntimeError("provider crashed")]),
        RouteEffectExecutor(),
        context,
        checkpoint=checkpoint,
    )
    first_services = replace(
        first_services,
        run_context_authority=authority,
        runtime_decision_sink=NoRecallSink(),
    )
    with pytest.raises(RuntimeError, match="provider crashed"):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=first_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert len(authority.calls) == 1

    replay_authority = ContextAuthority(context)
    reopened_services = services(
        ScriptedProviderCoordinator([response("done")]),
        RouteEffectExecutor(),
        context,
        checkpoint=checkpoint,
    )
    reopened_services = replace(
        reopened_services,
        run_context_authority=replay_authority,
        runtime_decision_sink=NoRecallSink(),
    )
    result = asyncio.run(
        _loop(RouteEffectExecutor()).run(
            ReActRunInput(RunId("run-1"), RequestId("request-1")),
            services=reopened_services,
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert replay_authority.calls == []
    assert result.termination.context_authority_receipt_hash is not None
    assert result.termination.context_snapshot_revision == 1
    assert len(result.termination.context_snapshot_bindings) == 1


def test_provider_reserved_reopen_rejects_context_snapshot_binding_drift() -> None:
    context = MemoryContext()
    checkpoint = Checkpoint()
    first_services = replace(
        services(
            ScriptedProviderCoordinator([RuntimeError("provider crashed")]),
            RouteEffectExecutor(),
            context,
            checkpoint=checkpoint,
        ),
        run_context_authority=ContextAuthority(context),
        runtime_decision_sink=NoRecallSink(),
    )
    with pytest.raises(RuntimeError, match="provider crashed"):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=first_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert checkpoint.value is not None
    payload = thaw_json(checkpoint.value.checkpoint)
    assert isinstance(payload, dict)
    bindings = payload["context_snapshot_bindings"]
    assert isinstance(bindings, dict)
    bindings["snapshot-1"] = "e" * 64
    checkpoint.value = replace(
        checkpoint.value,
        checkpoint=freeze_json(payload),
        checkpoint_hash=hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
    )

    provider = ScriptedProviderCoordinator([response("must-not-run")])
    reopened_services = replace(
        services(provider, RouteEffectExecutor(), context, checkpoint=checkpoint),
        run_context_authority=ContextAuthority(context),
        runtime_decision_sink=NoRecallSink(),
    )
    with pytest.raises(RuntimeError, match="authority receipt differs"):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=reopened_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert provider.calls == []


@pytest.mark.parametrize(
    ("snapshot_ids", "snapshot_revisions", "error"),
    (
        (("snapshot-1", "snapshot-2"), (1, 1), "snapshot revision is stale"),
        (("snapshot-1", "snapshot-1"), (1, 2), "snapshot identity changed payload"),
    ),
)
def test_cross_turn_context_snapshot_lineage_rejects_before_provider_reservation(
    snapshot_ids: tuple[str, ...],
    snapshot_revisions: tuple[int, ...],
    error: str,
) -> None:
    context = MemoryContext()
    authority = ContextAuthority(
        context,
        snapshot_ids=snapshot_ids,
        snapshot_revisions=snapshot_revisions,
    )
    route_call = ProviderResponse(
        RequestId("fixture"),
        response("route").message,
        (ProviderToolCall(CallId("raw-route"), "context_route", {}),),
    )
    provider = ScriptedProviderCoordinator([route_call, response("must-not-run")])
    runtime_services = replace(
        services(provider, RouteEffectExecutor(), context),
        run_context_authority=authority,
        runtime_decision_sink=NoRecallSink(),
    )

    with pytest.raises(RuntimeError, match=error):
        asyncio.run(
            _loop(RouteEffectExecutor()).run(
                ReActRunInput(
                    RunId("run-1"),
                    RequestId("request-1"),
                    tool_exposure=PolicyExposure(),
                ),
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )

    assert len(provider.calls) == 1
    assert len(authority.calls) == 2


def _consumer_disclosure(*, unknown: bool = False) -> DisclosureContext:
    if unknown:
        return DisclosureContext(
            "run-1",
            "actor-1",
            DeliveryRecipient.UNKNOWN,
            None,
            IntendedAudience.UNKNOWN,
            DisclosurePurpose.UNKNOWN,
            DisclosureSource.UNKNOWN,
            DisclosureTrust.UNTRUSTED_PROPOSAL,
            DisclosureGeneration.UNKNOWN,
            None,
            (
                DisclosureReasonCode.UNKNOWN_RECIPIENT,
                DisclosureReasonCode.UNKNOWN_PURPOSE,
            ),
        )
    return DisclosureContext(
        "run-1",
        "actor-1",
        DeliveryRecipient.USER_SELF,
        "actor-1",
        IntendedAudience.USER_SELF,
        DisclosurePurpose.TASK_EXECUTION,
        DisclosureSource.AUTHENTICATED_HOST,
        DisclosureTrust.TRUSTED_AUTHORITY,
        DisclosureGeneration.CURRENT,
        "host-disclosure-1",
        (DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


class FakeMemoryConsumer:
    def __init__(self) -> None:
        self.recall_calls: list[str] = []

    def sanitize(self, disclosure: DisclosureContext) -> SanitizedEvidenceEnvelope:
        if disclosure.recipient is DeliveryRecipient.UNKNOWN:
            raise ValueError("unknown disclosure cannot enter the Memory consumer")
        raw = {
            "public_text": "User prefers deterministic tests.",
            "api_key": "CREDENTIAL_CANARY",
            "hidden_reasoning": "HIDDEN_COT_CANARY",
        }
        public = {"public_text": raw["public_text"]}
        return SanitizedEvidenceEnvelope(
            "evidence-1",
            "run-1",
            "actor-1",
            EvidenceSourceKind.USER_MESSAGE,
            "turn-1/user",
            fingerprint_json(raw),
            public,
            fingerprint_json(public),
            "credential-filter/v1",
            (
                RemovedSpanSummary(RemovedSpanType.API_KEY, 1),
                RemovedSpanSummary(RemovedSpanType.HIDDEN_REASONING, 1),
            ),
            disclosure,
            (),
        )

    async def recall(self, evidence: SanitizedEvidenceEnvelope) -> str:
        encoded = canonical_json(evidence.to_json())
        assert "CREDENTIAL_CANARY" not in encoded
        assert "HIDDEN_COT_CANARY" not in encoded
        self.recall_calls.append(evidence.evidence_id)
        return str(evidence.sanitized_payload["public_text"])


class IntegratedContextAuthority:
    def __init__(
        self,
        context: MemoryContext,
        memory: FakeMemoryConsumer,
        evidence: SanitizedEvidenceEnvelope,
    ) -> None:
        self.context = context
        self.memory = memory
        self.evidence = evidence
        self.calls = []

    async def prepare_snapshot(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(request)
        messages = list(self.context.messages)
        if request.route_state.value == "routed_task":
            messages.append(Message(MessageRole.USER, await self.memory.recall(self.evidence)))
        provider_request = ProviderRequest(
            RequestId("hash-only"),
            tuple(messages),
            tools=PolicyExposure().provider_specs(request.run_id),
            metadata={"authority": "integrated-host"},
        )
        revision = len(self.calls)
        return RunContextSnapshot(
            f"integrated-snapshot-{revision}",
            request.run_id.value,
            request.provider_turn_ordinal,
            request.prior_context_revision,
            revision,
            {"host": revision, "memory": len(self.memory.recall_calls)},
            provider_request.messages,
            provider_request.tools,
            None,
            None,
            provider_request.metadata,
            provider_request_fingerprint(provider_request),
        )


class IntegratedEffects:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.project_envelopes: list[TaskExecutionEnvelope] = []

    async def execute(self, **values):  # type: ignore[no-untyped-def]
        call = values["call"]
        context = values["context"]
        self.calls.append(call.name)
        if call.name == "context_route":
            assert context.effect_id is not None
            receipt = ContextRouteReceipt(
                "integrated-route-1",
                context.run_id.value,
                str(values["raw_call_id"]),
                context.effect_id.value,
                TaskScopeRoute.RESUME_EXISTING,
                "task-1",
                3,
                "binding-set-3",
                "e" * 64,
                ("evidence-1",),
            )
            return EffectExecution(None, ToolResult.succeeded(call.call_id, receipt.to_json()))
        envelope = context.task_execution_envelope
        assert isinstance(envelope, TaskExecutionEnvelope)
        self.project_envelopes.append(envelope)
        return EffectExecution(None, ToolResult.succeeded(call.call_id, {"written": True}))


class IntegratedTaskExecutionAuthority:
    async def issue_envelope(self, request):  # type: ignore[no-untyped-def]
        route = request.route_receipt
        return TaskExecutionEnvelope(
            request.run_id,
            CallId(request.call_id),
            EffectId(request.effect_id),
            request.raw_call_id,
            request.turn_ordinal,
            request.call_ordinal,
            request.tool_name,
            request.policy.capability_id,
            request.policy.capability_fingerprint,
            None if route is None else route.receipt_id,
            None if route is None else route.receipt_hash,
            None if route is None else route.task_scope_id,
            None if route is None else "root-1",
            None if route is None else "d" * 64,
            None if route is None else route.binding_set_revision,
            request.effect_id,
            binding_set_receipt_id=None if route is None else route.binding_set_receipt_id,
            binding_set_receipt_hash=None if route is None else route.binding_set_receipt_hash,
        )


def test_integrated_fake_host_memory_consumer_reaches_project_effect_in_one_run() -> None:
    memory = FakeMemoryConsumer()
    evidence = memory.sanitize(_consumer_disclosure())
    context = MemoryContext()
    authority = IntegratedContextAuthority(context, memory, evidence)
    effects = IntegratedEffects()
    route_response = ProviderResponse(
        RequestId("fixture"),
        Message(MessageRole.ASSISTANT, "route"),
        (ProviderToolCall(CallId("raw-route"), "context_route", {}),),
    )
    project_response = ProviderResponse(
        RequestId("fixture"),
        Message(MessageRole.ASSISTANT, "write"),
        (ProviderToolCall(CallId("raw-write"), "write_project", {}),),
    )
    provider = ScriptedProviderCoordinator([route_response, project_response, response("done")])
    runtime_services = replace(
        services(provider, effects, context),
        run_context_authority=authority,
        task_execution_authority=IntegratedTaskExecutionAuthority(),
    )

    result = asyncio.run(
        _loop(effects).run(
            ReActRunInput(
                RunId("run-1"),
                RequestId("request-1"),
                tool_exposure=PolicyExposure(),
            ),
            services=runtime_services,
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )

    assert result.termination.route_state == "routed_task"
    assert effects.calls == ["context_route", "write_project"]
    assert len(effects.project_envelopes) == 1
    envelope = effects.project_envelopes[0]
    assert envelope.task_scope_id == "task-1"
    assert envelope.root_id == "root-1"
    assert envelope.binding_set_revision == 3
    assert envelope.idempotency_key == envelope.effect_id.value
    assert len(authority.calls) == 3
    assert memory.recall_calls == ["evidence-1", "evidence-1"]
    assert any(
        "deterministic tests" in str(message.content) for message in provider.calls[1][1].messages
    )


def test_integrated_consumer_negative_protocol_matrix_fails_closed() -> None:
    memory = FakeMemoryConsumer()
    with pytest.raises(ValueError, match="unknown disclosure"):
        memory.sanitize(_consumer_disclosure(unknown=True))

    evidence = memory.sanitize(_consumer_disclosure())
    payload = evidence.to_json()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="extra"):
        SanitizedEvidenceEnvelope.from_json(payload)

    for refs in (
        (EvidenceRef("evidence-a", "a" * 64, 2),),
        (
            EvidenceRef("evidence-a", "a" * 64, 1),
            EvidenceRef("evidence-a", "b" * 64, 2),
        ),
    ):
        with pytest.raises(ValueError):
            SanitizedEvidenceEnvelope(
                evidence.evidence_id,
                evidence.run_id,
                evidence.subject,
                evidence.source_kind,
                evidence.source_ref,
                evidence.source_hash,
                evidence.sanitized_payload,
                evidence.sanitized_hash,
                evidence.filter_policy_version,
                evidence.removed_spans,
                evidence.disclosure_context,
                refs,
            )

    with pytest.raises(ValueError, match="bounded"):
        SanitizedEvidenceEnvelope(
            evidence.evidence_id,
            evidence.run_id,
            evidence.subject,
            evidence.source_kind,
            "x" * 1025,
            evidence.source_hash,
            evidence.sanitized_payload,
            evidence.sanitized_hash,
            evidence.filter_policy_version,
            evidence.removed_spans,
            evidence.disclosure_context,
            (),
        )


def test_integrated_context_timeout_stops_before_provider() -> None:
    class TimeoutAuthority:
        async def prepare_snapshot(self, request):  # type: ignore[no-untyped-def]
            del request
            raise TimeoutError("fake Memory timeout")

    context = MemoryContext()
    provider = ScriptedProviderCoordinator([response("must-not-run")])
    runtime_services = replace(
        services(provider, IntegratedEffects(), context),
        run_context_authority=TimeoutAuthority(),
    )
    with pytest.raises(TimeoutError, match="fake Memory timeout"):
        asyncio.run(
            _loop(IntegratedEffects()).run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert provider.calls == []
