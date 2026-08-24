# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable, lease-fenced ReAct loop over RuntimeServices."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
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
    ProviderToolSpec,
)
from simple_harness.runtime.kernel import RuntimeServices
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.tools import (
    CancellationToken,
    JsonObject,
    ToolCall,
    ToolContext,
    ToolOutcome,
)
from simple_harness.tools.runtime_catalog import RunToolExposurePort

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
        calls,
        *,
        services: RuntimeServices,
        run_id: RunId,
        request_id: RequestId,
        execution_lease: ExecutionLease,
        run_fence: RunFenceLease,
        cancellation: CancellationToken,
        turn_ordinal: int = 0,
        call_offset: int = 0,
    ):
        calls = tuple(calls)
        if len(calls) > self.max_batch_size:
            raise ValueError("provider Tool batch exceeds the hard batch limit")

        async def one(call, call_ordinal: int):
            arguments = thaw_json(call.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("provider tool arguments must be an object")
            internal_call_id, effect_id = _internal_effect_identity(
                run_id, turn_ordinal, call.call_id.value, call_ordinal
            )
            return await services.tools.execute(
                effect_id=effect_id,
                call=ToolCall(internal_call_id, call.name, cast(JsonObject, arguments)),
                context=ToolContext(run_id, request_id, cancellation),
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
                request = ProviderRequest(
                    RequestId(provider_request_id),
                    context.messages,
                    tools=provider_tools,
                    temperature=value.temperature,
                    max_output_tokens=value.max_output_tokens,
                )
                request_snapshot = provider_request_json(request)
                state = replace(
                    state,
                    provider_request_id=provider_request_id,
                    context_revision=context.revision,
                    provider_request_snapshot=request_snapshot,
                    provider_request_fingerprint=provider_request_fingerprint(request),
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
                response_snapshot = provider_response_json(response)
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
                response = provider_response_from_json(response_payload)
            context = services.context.load(value.run_id)
            context = services.context.append(
                value.run_id,
                execution_lease,
                context.revision,
                f"{state.provider_request_id}:assistant",
                (response.message,),
            )
            if not response.tool_calls:
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
                )
                execution = executions[0]
                result = execution.result
                if result.outcome is ToolOutcome.UNKNOWN:
                    if execution.effect is None:
                        raise RuntimeError("tool_outcome_unknown_without_ledger")
                    raise ToolEffectUnknownError(execution.effect)
                result_value = thaw_json(result.value)
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
