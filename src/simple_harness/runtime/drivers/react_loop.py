# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable, lease-fenced ReAct loop over RuntimeServices."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from simple_harness.contracts import (
    CallId,
    EffectId,
    FrozenJsonValue,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.context_authority import (
    ContextRouteReceipt,
    ContextRouteState,
    RunContextAuthorityRequest,
    TaskExecutionEnvelopeRequest,
)
from simple_harness.execution.effects import EffectRecord
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.provider_invocations import (
    provider_request_fingerprint,
    provider_request_from_json,
    provider_request_json,
    provider_response_from_json,
    provider_response_json,
)
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderToolSpec,
)
from simple_harness.providers.base import ProviderContinuationCapability
from simple_harness.runtime.kernel import RuntimeServices
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.runtime.task_scope_protocol import TaskScopeRoute
from simple_harness.tools import (
    CancellationToken,
    JsonObject,
    ToolCall,
    ToolContext,
    ToolOutcome,
    ToolResult,
)
from simple_harness.tools.executor import EffectExecution
from simple_harness.tools.runtime_catalog import (
    RunToolExposurePort,
    ToolEffectClass,
    ToolExecutionPolicy,
    ToolRouteRequirement,
)

from ..termination import TerminationLimits, TerminationState


@dataclass(frozen=True, slots=True)
class ReActRunInput:
    run_id: RunId
    request_id: RequestId
    tools: tuple[ProviderToolSpec, ...] = ()
    tool_exposure: RunToolExposurePort | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ReActResult:
    response: ProviderResponse
    termination: TerminationState


class ToolEffectUnknownError(RuntimeError):
    def __init__(self, effect: EffectRecord) -> None:
        super().__init__("tool_outcome_unknown")
        self.effect = effect


class EffectBatchExecutor:
    """Bound a Provider tool batch before the first durable effect prepare."""

    def __init__(self, *, max_batch_size: int = 32) -> None:
        if isinstance(max_batch_size, bool) or max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")
        self.max_batch_size = max_batch_size

    async def execute(
        self,
        calls: Sequence[ProviderToolCall],
        *,
        services: RuntimeServices,
        run_id: RunId,
        request_id: RequestId,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        cancellation: CancellationToken,
        turn_ordinal: int = 0,
        call_offset: int = 0,
        tool_exposure: RunToolExposurePort | None = None,
        route_receipt: ContextRouteReceipt | None = None,
    ) -> list[EffectExecution]:
        calls = tuple(calls)
        if len(calls) > self.max_batch_size:
            raise ValueError("provider Tool batch exceeds the hard batch limit")

        async def one(call: ProviderToolCall, call_ordinal: int) -> EffectExecution:
            arguments = thaw_json(cast(FrozenJsonValue, call.arguments))
            if not isinstance(arguments, dict):
                raise TypeError("provider tool arguments must be an object")
            internal_call_id, effect_id = _internal_effect_identity(
                run_id, turn_ordinal, call.call_id.value, call_ordinal
            )
            policy = (
                None if tool_exposure is None else tool_exposure.execution_policy(run_id, call.name)
            )
            envelope = None
            if policy is not None and services.task_execution_authority is not None:
                envelope = await services.task_execution_authority.issue_envelope(
                    TaskExecutionEnvelopeRequest(
                        run_id,
                        internal_call_id.value,
                        effect_id.value,
                        call.call_id.value,
                        turn_ordinal,
                        call_ordinal,
                        call.name,
                        policy,
                        route_receipt,
                    )
                )
                if (
                    envelope.run_id != run_id
                    or envelope.call_id != internal_call_id
                    or envelope.effect_id != effect_id
                    or envelope.raw_call_id != call.call_id.value
                    or envelope.turn_ordinal != turn_ordinal
                    or envelope.call_ordinal != call_ordinal
                    or envelope.tool_name != call.name
                    or envelope.capability_id != policy.capability_id
                    or envelope.capability_fingerprint != policy.capability_fingerprint
                ):
                    raise RuntimeError("Host TaskExecutionEnvelope differs from exact effect")
                if route_receipt is not None and (
                    envelope.route_receipt_id != route_receipt.receipt_id
                    or envelope.route_receipt_hash != route_receipt.receipt_hash
                ):
                    raise RuntimeError("Host TaskExecutionEnvelope route receipt differs")
                if policy.effect_class is ToolEffectClass.PROJECT_EFFECT and (
                    route_receipt is None
                    or route_receipt.route_state is not ContextRouteState.ROUTED_TASK
                    or envelope.task_scope_id != route_receipt.task_scope_id
                    or envelope.binding_set_revision != route_receipt.binding_set_revision
                    or envelope.binding_set_receipt_id
                    != route_receipt.binding_set_receipt_id
                    or envelope.binding_set_receipt_hash
                    != route_receipt.binding_set_receipt_hash
                ):
                    raise RuntimeError(
                        "project TaskExecutionEnvelope has stale TaskScope binding authority"
                    )
            elif policy is not None and policy.effect_class is ToolEffectClass.PROJECT_EFFECT:
                raise RuntimeError("project effect requires Host TaskExecutionEnvelope authority")
            return await services.tools.execute(
                effect_id=effect_id,
                call=ToolCall(internal_call_id, call.name, cast(JsonObject, arguments)),
                context=ToolContext(
                    run_id,
                    request_id,
                    cancellation,
                    call_id=internal_call_id,
                    effect_id=effect_id,
                    task_execution_envelope=envelope,
                ),
                execution_lease=execution_lease,
                run_fence=run_fence,
                raw_call_id=call.call_id.value,
                turn_ordinal=turn_ordinal,
                call_ordinal=call_ordinal,
            )

        return await asyncio.gather(
            *(one(call, call_offset + index) for index, call in enumerate(calls))
        )


class AgentLoopCollaborator:
    """Policy-only collaborator; all authorities arrive in RuntimeServices."""

    def __init__(self, *, limits: TerminationLimits | None = None) -> None:
        self.limits = limits or TerminationLimits()


class ReActLoop:
    def __init__(
        self,
        *,
        collaborator: AgentLoopCollaborator,
        effects: EffectBatchExecutor,
        clock: Callable[[], float],
        policy_fingerprint: str | None = None,
    ) -> None:
        self._collaborator = collaborator
        self._effects = effects
        self._clock = clock
        self._policy_fingerprint = policy_fingerprint

    async def run(
        self,
        value: ReActRunInput,
        *,
        services: RuntimeServices,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        cancel: CancelToken,
        initial_messages: Sequence[Message],
        tool_cancel: CancellationToken | None = None,
    ) -> ReActResult:
        if execution_lease.run_id != value.run_id.value or run_fence.run_id != value.run_id:
            raise ValueError("ReAct invocation authority belongs to another Run")
        tool_cancel = tool_cancel or CancellationToken()
        context = services.context.load(value.run_id)
        if context.revision == 0:
            if not initial_messages:
                raise ValueError("ReAct requires at least one initial message")
            context = services.context.append(
                value.run_id,
                execution_lease,
                0,
                f"{value.run_id.value}:context:initial",
                initial_messages,
            )
        checkpoint = DurableReactCheckpoint(services.react_checkpoint, clock=self._clock)
        state, checkpoint_version = checkpoint.load_or_create(value.run_id, execution_lease)
        if value.tool_exposure is not None:
            value.tool_exposure.restore(value.run_id, state.tool_exposure_state)
        if self._policy_fingerprint is not None:
            if state.policy_fingerprint and (state.policy_fingerprint != self._policy_fingerprint):
                raise RuntimeError("ReAct policy fingerprint differs from checkpoint")
            if not state.policy_fingerprint:
                if state.provider_turns_reserved_total or state.tool_calls_reserved_total:
                    raise RuntimeError("legacy active ReAct checkpoint lacks policy binding")
                state = replace(state, policy_fingerprint=self._policy_fingerprint)
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )

        while True:
            _cancel(cancel, tool_cancel)
            budget = services.provider.read_provider_budget(value.run_id)
            if state.phase == "ready":
                state = state.before_provider(
                    self._collaborator.limits, now=self._clock(), budget=budget
                )
                provider_request_id = (
                    f"{value.run_id.value}:provider-turn:{state.provider_turns_reserved_total}"
                )
                context = services.context.load(value.run_id)
                provider_tools = (
                    value.tools
                    if value.tool_exposure is None
                    else value.tool_exposure.provider_specs(value.run_id)
                )
                context_snapshot_revision = state.context_snapshot_revision
                context_snapshot_bindings = state.context_snapshot_bindings
                if services.run_context_authority is None:
                    request = ProviderRequest(
                        RequestId(provider_request_id),
                        context.messages,
                        tools=provider_tools,
                        temperature=value.temperature,
                        max_output_tokens=value.max_output_tokens,
                    )
                    context_authority_receipt = None
                    context_authority_receipt_hash = None
                else:
                    route_receipt = _checkpoint_route_receipt(state, value.run_id)
                    snapshot = await services.run_context_authority.prepare_snapshot(
                        RunContextAuthorityRequest(
                            value.run_id,
                            state.provider_turns_reserved_total,
                            context.revision,
                            ContextRouteState(state.route_state),
                            route_receipt,
                            _tool_catalog_fingerprint(
                                value.run_id, value.tool_exposure, provider_tools
                            ),
                        )
                    )
                    if (
                        snapshot.run_id != value.run_id.value
                        or snapshot.provider_turn_ordinal != state.provider_turns_reserved_total
                        or snapshot.prior_context_revision != context.revision
                    ):
                        raise RuntimeError("Host Context snapshot lineage differs")
                    if snapshot.snapshot_revision <= state.context_snapshot_revision:
                        raise RuntimeError("Host Context snapshot revision is stale")
                    snapshot_bindings = dict(state.context_snapshot_bindings)
                    prior_payload_hash = snapshot_bindings.get(snapshot.snapshot_id)
                    if (
                        prior_payload_hash is not None
                        and prior_payload_hash != snapshot.payload_hash
                    ):
                        raise RuntimeError("Host Context snapshot identity changed payload")
                    snapshot_bindings[snapshot.snapshot_id] = snapshot.payload_hash
                    context_snapshot_revision = snapshot.snapshot_revision
                    context_snapshot_bindings = tuple(sorted(snapshot_bindings.items()))
                    request = ProviderRequest(
                        RequestId(provider_request_id),
                        snapshot.messages,
                        tools=snapshot.tools,
                        temperature=snapshot.temperature,
                        max_output_tokens=snapshot.max_output_tokens,
                        metadata=snapshot.metadata,
                    )
                    if provider_request_fingerprint(request) != (
                        snapshot.expected_request_fingerprint
                    ):
                        raise RuntimeError("Host Context snapshot request fingerprint differs")
                    context_authority_receipt = snapshot.receipt_json()
                    context_authority_receipt_hash = hashlib.sha256(
                        canonical_json(context_authority_receipt).encode()
                    ).hexdigest()
                request_snapshot = provider_request_json(request)
                state = replace(
                    state,
                    provider_request_id=provider_request_id,
                    context_revision=context.revision,
                    provider_request_snapshot=request_snapshot,
                    provider_request_fingerprint=provider_request_fingerprint(request),
                    context_authority_receipt=context_authority_receipt,
                    context_authority_receipt_hash=context_authority_receipt_hash,
                    context_snapshot_revision=context_snapshot_revision,
                    context_snapshot_bindings=context_snapshot_bindings,
                )
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
            if state.phase not in {
                "provider_reserved",
                "response_reserved",
                "tool_batch_reserved",
            }:
                raise RuntimeError(f"unsupported ReAct checkpoint phase: {state.phase}")
            assert state.provider_request_id is not None
            if state.phase == "provider_reserved":
                if state.provider_request_snapshot is None:
                    raise RuntimeError("provider_reserved checkpoint lacks frozen request")
                request = provider_request_from_json(
                    RequestId(state.provider_request_id),
                    state.provider_request_snapshot,
                )
                if provider_request_fingerprint(request) != state.provider_request_fingerprint:
                    raise RuntimeError("frozen Provider request fingerprint mismatch")
                _verify_context_authority_receipt(state, value.run_id, request)
                response = await services.provider.invoke(
                    value.run_id,
                    request,
                    cancel=cancel,
                    execution_lease=execution_lease,
                )
                if response.request_id != request.request_id:
                    raise RuntimeError("Provider response request identity mismatch")
                raw_ids = tuple(call.call_id.value for call in response.tool_calls)
                if len(set(raw_ids)) != len(raw_ids):
                    raise RuntimeError("duplicate raw Provider call ID in one turn")
                if response.tool_calls:
                    keys = tuple(
                        _repeat_key(call.name, call.arguments) for call in response.tool_calls
                    )
                    state = state.before_tool_batch(
                        keys,
                        self._collaborator.limits,
                        now=self._clock(),
                        budget=services.provider.read_provider_budget(value.run_id),
                    )
                else:
                    state = replace(state, phase="response_reserved")
                continuation_capability = _provider_continuation_capability(services, value.run_id)
                response_snapshot = provider_response_json(
                    response, capability=continuation_capability
                )
                state = replace(
                    state,
                    provider_response_snapshot=response_snapshot,
                    provider_response_digest=hashlib.sha256(
                        canonical_json(response_snapshot).encode()
                    ).hexdigest(),
                    tool_result_progress=0,
                )
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
            else:
                if state.provider_response_snapshot is None:
                    raise RuntimeError("response checkpoint lacks frozen response")
                response_payload = state.provider_response_snapshot
                if (
                    hashlib.sha256(canonical_json(response_payload).encode()).hexdigest()
                    != state.provider_response_digest
                ):
                    raise RuntimeError("frozen Provider response digest mismatch")
                continuation_capability = _provider_continuation_capability(services, value.run_id)
                legacy_public_response = state.source_schema_version <= 3
                response = provider_response_from_json(
                    response_payload,
                    expected_capability=continuation_capability,
                    allow_legacy_public_response=legacy_public_response,
                )
                if legacy_public_response:
                    normalized_response = provider_response_json(
                        response, capability=continuation_capability
                    )
                    state = replace(
                        state,
                        provider_response_snapshot=normalized_response,
                        provider_response_digest=hashlib.sha256(
                            canonical_json(normalized_response).encode()
                        ).hexdigest(),
                        source_schema_version=5,
                    )
            # Only the strict durable/public projection may enter Context.  The
            # physical Provider response may contain hidden reasoning or private
            # metadata that is valid for an opaque continuation but never public
            # durable state.
            assert state.provider_response_snapshot is not None
            response = provider_response_from_json(
                state.provider_response_snapshot,
                expected_capability=continuation_capability,
            )
            batch_policies, barrier_rejections = _preflight_tool_batch(
                response.tool_calls,
                run_id=value.run_id,
                tool_exposure=value.tool_exposure,
                route_state=ContextRouteState(state.route_state),
                authority_required=services.run_context_authority is not None,
            )
            context = services.context.load(value.run_id)
            context = services.context.append(
                value.run_id,
                execution_lease,
                context.revision,
                f"{state.provider_request_id}:assistant",
                (response.message,),
            )
            if not response.tool_calls:
                if state.route_state == ContextRouteState.UNROUTED.value:
                    if services.run_context_authority is not None and (
                        services.runtime_decision_sink is None
                    ):
                        raise RuntimeError(
                            "Host Context authority requires a no-recall decision sink"
                        )
                if state.route_state == ContextRouteState.UNROUTED.value and (
                    services.runtime_decision_sink is not None
                ):
                    receipt = await services.runtime_decision_sink.record_no_recall(
                        run_id=value.run_id,
                        provider_turn_ordinal=state.provider_turns_reserved_total,
                        request_fingerprint=cast(str, state.provider_request_fingerprint),
                    )
                    if (
                        receipt.run_id != value.run_id.value
                        or receipt.route is not TaskScopeRoute.DIRECT_STANDALONE
                        or receipt.recall_refs
                    ):
                        raise RuntimeError("Host no-recall receipt differs from terminal Run")
                    state = replace(
                        state,
                        route_state=receipt.route_state.value,
                        route_receipt=receipt.to_json(),
                        route_receipt_hash=receipt.receipt_hash,
                    )
                state = replace(
                    state,
                    phase="ready",
                    provider_request_id=None,
                    tool_batch_id=None,
                    context_revision=None,
                    provider_request_snapshot=None,
                    provider_request_fingerprint=None,
                    provider_response_snapshot=None,
                    provider_response_digest=None,
                    tool_result_progress=0,
                    last_observed_at=self._clock(),
                )
                state, _ = checkpoint.cas(value.run_id, execution_lease, checkpoint_version, state)
                return ReActResult(response, state)
            _cancel(cancel, tool_cancel)
            for call_ordinal in range(state.tool_result_progress, len(response.tool_calls)):
                call = response.tool_calls[call_ordinal]
                internal_call_id, effect_id = _internal_effect_identity(
                    value.run_id,
                    state.provider_turns_reserved_total,
                    call.call_id.value,
                    call_ordinal,
                )
                rejection = barrier_rejections.get(call_ordinal)
                if rejection is None:
                    executions = await self._effects.execute(
                        (call,),
                        services=services,
                        run_id=value.run_id,
                        request_id=value.request_id,
                        execution_lease=execution_lease,
                        run_fence=run_fence,
                        cancellation=tool_cancel,
                        turn_ordinal=state.provider_turns_reserved_total,
                        call_offset=call_ordinal,
                        tool_exposure=value.tool_exposure,
                        route_receipt=_checkpoint_route_receipt(state, value.run_id),
                    )
                    execution = executions[0]
                else:
                    execution = EffectExecution(
                        None,
                        ToolResult.rejected(
                            internal_call_id,
                            rejection,
                            "Observe a Context route before executing this Tool.",
                        ),
                    )
                result = execution.result
                if result.outcome is ToolOutcome.UNKNOWN:
                    if execution.effect is None:
                        raise RuntimeError("tool_outcome_unknown_without_ledger")
                    raise ToolEffectUnknownError(execution.effect)
                result_value = thaw_json(result.value)
                policy = batch_policies.get(call_ordinal)
                if (
                    policy is not None
                    and policy.effect_class is ToolEffectClass.CONTEXT_CONTROL
                    and result.outcome is ToolOutcome.SUCCEEDED
                ):
                    receipt = _route_receipt_from_tool_result(result_value)
                    if (
                        receipt.run_id != value.run_id.value
                        or receipt.raw_call_id != call.call_id.value
                        or receipt.effect_id != effect_id.value
                    ):
                        raise RuntimeError("Context route receipt differs from control effect")
                    state = replace(
                        state,
                        route_state=receipt.route_state.value,
                        route_receipt=receipt.to_json(),
                        route_receipt_hash=receipt.receipt_hash,
                    )
                payload: dict[str, JsonValue] = {
                    "outcome": result.outcome.value,
                    "value": result_value,
                    "error_code": result.error_code,
                    "public_message": result.public_message,
                }
                if value.tool_exposure is not None and isinstance(result_value, dict):
                    value.tool_exposure.observe_tool_result(
                        value.run_id,
                        call.name,
                        result_value,
                    )
                    state = replace(
                        state,
                        tool_exposure_state=value.tool_exposure.checkpoint(value.run_id),
                    )
                context = services.context.append(
                    value.run_id,
                    execution_lease,
                    context.revision,
                    f"{value.run_id.value}:turn:{state.provider_turns_reserved_total}:"
                    f"raw-call:{call.call_id.value}:ordinal:{call_ordinal}:context",
                    (
                        Message(
                            MessageRole.TOOL,
                            canonical_json(payload),
                            name=call.name,
                            call_id=call.call_id,
                        ),
                    ),
                )
                state = replace(state, tool_result_progress=call_ordinal + 1)
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
            if state.workflow_spawn_wait_receipt_id is not None:
                stored = (
                    services.react_checkpoint.commit_pending_spawn_child_completion_and_react_ready(
                        run_id=value.run_id.value,
                        expected_checkpoint_version=checkpoint_version,
                        execution_lease=execution_lease,
                        run_fence=run_fence,
                        now=self._clock(),
                    )
                )
                checkpoint_payload = thaw_json(stored.checkpoint)
                if not isinstance(checkpoint_payload, dict):
                    raise TypeError("ReAct checkpoint payload must be an object")
                state = TerminationState.from_json(checkpoint_payload)
                checkpoint_version = stored.version
                continue
            state = replace(
                state,
                phase="ready",
                provider_request_id=None,
                tool_batch_id=None,
                context_revision=None,
                provider_request_snapshot=None,
                provider_request_fingerprint=None,
                provider_response_snapshot=None,
                provider_response_digest=None,
                tool_result_progress=0,
                workflow_spawn_wait_receipt_id=None,
                pending_child_completion=None,
                pending_child_completion_hash=None,
                pending_child_completion_append_id=None,
                last_observed_at=self._clock(),
            )
            state, checkpoint_version = checkpoint.cas(
                value.run_id, execution_lease, checkpoint_version, state
            )


def _repeat_key(name: str, arguments: object) -> str:
    digest = hashlib.sha256(
        canonical_json(thaw_json(cast(FrozenJsonValue, arguments))).encode()
    ).hexdigest()
    return f"{name}:{digest}"


_HOST_ONLY_ARGUMENTS = frozenset(
    {
        "task_execution_envelope",
        "route_receipt",
        "binding_set_revision",
        "binding_set_receipt_id",
        "binding_set_receipt_hash",
        "root_identity_hash",
    }
)


def _preflight_tool_batch(
    calls: Sequence[ProviderToolCall],
    *,
    run_id: RunId,
    tool_exposure: RunToolExposurePort | None,
    route_state: ContextRouteState,
    authority_required: bool,
) -> tuple[dict[int, ToolExecutionPolicy], dict[int, str]]:
    policies: dict[int, ToolExecutionPolicy] = {}
    rejected: dict[int, str] = {}
    if tool_exposure is None:
        if calls and authority_required:
            raise RuntimeError(
                "Provider Tool execution requires a private catalog execution policy"
            )
        return policies, rejected
    for ordinal, call in enumerate(calls):
        policy = tool_exposure.execution_policy(run_id, call.name)
        policies[ordinal] = policy
        arguments = thaw_json(cast(FrozenJsonValue, call.arguments))
        if not isinstance(arguments, dict):
            raise TypeError("provider tool arguments must be an object")
        if _HOST_ONLY_ARGUMENTS.intersection(arguments):
            rejected[ordinal] = "MODEL_AUTHORITY_FIELD_FORBIDDEN"
    same_batch_control = any(
        item.effect_class is ToolEffectClass.CONTEXT_CONTROL for item in policies.values()
    )
    for ordinal, policy in policies.items():
        if (
            policy.route_requirement is ToolRouteRequirement.REQUIRED
            and route_state is ContextRouteState.UNROUTED
        ):
            rejected[ordinal] = "ROUTE_BARRIER_NOT_OBSERVED"
        elif policy.route_requirement is ToolRouteRequirement.REQUIRED and same_batch_control:
            rejected[ordinal] = "ROUTE_BARRIER_NOT_OBSERVED"
    return policies, rejected


def _checkpoint_route_receipt(state: TerminationState, run_id: RunId) -> ContextRouteReceipt | None:
    if state.route_receipt is None:
        if state.route_state != ContextRouteState.UNROUTED.value:
            raise RuntimeError("routed checkpoint lacks route receipt")
        return None
    if not isinstance(state.route_receipt, Mapping):
        raise TypeError("route receipt checkpoint must be an object")
    receipt = ContextRouteReceipt.from_json(state.route_receipt)
    if receipt.run_id != run_id.value or receipt.route_state.value != state.route_state:
        raise RuntimeError("route receipt checkpoint lineage differs")
    return receipt


def _route_receipt_from_tool_result(value: object) -> ContextRouteReceipt:
    if not isinstance(value, Mapping):
        raise TypeError("context_route result must be an object")
    candidate = value.get("context_route_receipt", value)
    if not isinstance(candidate, Mapping):
        raise TypeError("context_route receipt must be an object")
    return ContextRouteReceipt.from_json(candidate)


def _tool_catalog_fingerprint(
    run_id: RunId,
    exposure: RunToolExposurePort | None,
    tools: tuple[ProviderToolSpec, ...],
) -> str:
    if exposure is not None:
        checkpoint = exposure.checkpoint(run_id)
        if isinstance(checkpoint, Mapping):
            value = checkpoint.get("catalog_fingerprint")
            if isinstance(value, str):
                return value
    payload: JsonValue = [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": thaw_json(cast(FrozenJsonValue, tool.parameters)),
        }
        for tool in tools
    ]
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _verify_context_authority_receipt(
    state: TerminationState, run_id: RunId, request: ProviderRequest
) -> None:
    if state.context_authority_receipt is None:
        return
    if not isinstance(state.context_authority_receipt, Mapping):
        raise TypeError("Context authority receipt must be an object")
    receipt = state.context_authority_receipt
    snapshot_id = receipt.get("snapshot_id")
    snapshot_revision = receipt.get("snapshot_revision")
    snapshot_bindings = dict(state.context_snapshot_bindings)
    if (
        not isinstance(snapshot_id, str)
        or isinstance(snapshot_revision, bool)
        or not isinstance(snapshot_revision, int)
        or snapshot_revision != state.context_snapshot_revision
        or snapshot_bindings.get(snapshot_id) != receipt.get("payload_hash")
        or receipt.get("run_id") != run_id.value
        or receipt.get("provider_turn_ordinal") != state.provider_turns_reserved_total
        or receipt.get("prior_context_revision") != state.context_revision
        or receipt.get("payload_hash") != provider_request_fingerprint(request)
        or receipt.get("expected_request_fingerprint") != provider_request_fingerprint(request)
    ):
        raise RuntimeError("frozen Host Context authority receipt differs")


def _provider_continuation_capability(
    services: RuntimeServices, run_id: RunId
) -> ProviderContinuationCapability:
    resolver = getattr(services.provider, "continuation_capability_for", None)
    if resolver is None:
        return ProviderContinuationCapability()
    capability = resolver(run_id)
    if not isinstance(capability, ProviderContinuationCapability):
        raise TypeError("Provider continuation capability is invalid")
    return capability


def _internal_effect_identity(
    run_id: RunId, turn_ordinal: int, raw_call_id: str, call_ordinal: int
) -> tuple[CallId, EffectId]:
    digest = hashlib.sha256(
        canonical_json(
            {
                "protocol": "simple-harness-react-effect-v1",
                "run_id": run_id.value,
                "turn_ordinal": turn_ordinal,
                "raw_provider_call_id": raw_call_id,
                "call_ordinal": call_ordinal,
            }
        ).encode()
    ).hexdigest()
    return CallId(f"call-{digest}"), EffectId(f"effect-{digest}")


def _cancel(cancel: CancelToken, tool_cancel: CancellationToken) -> None:
    if cancel.is_cancelled or tool_cancel.cancelled:
        raise asyncio.CancelledError


__all__ = (
    "AgentLoopCollaborator",
    "EffectBatchExecutor",
    "ReActLoop",
    "ReActResult",
    "ReActRunInput",
    "ToolEffectUnknownError",
)
