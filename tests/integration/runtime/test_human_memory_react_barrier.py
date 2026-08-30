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
    RequestId,
    RunId,
    canonical_json,
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
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    ReActLoop,
    ReActRunInput,
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
            request.route_receipt.binding_set_revision - 1,
            request.effect_id,
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
