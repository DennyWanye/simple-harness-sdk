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
    EffectId,
    JsonValue,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.fences import RunFenceLease
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

from ..termination import TerminationLimits, TerminationState


@dataclass(frozen=True, slots=True)
class ReActRunInput:
    run_id: RunId
    request_id: RequestId
    tools: tuple[ProviderToolSpec, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ReActResult:
    response: ProviderResponse
    termination: TerminationState


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
    ):
        calls = tuple(calls)
        if len(calls) > self.max_batch_size:
            raise ValueError("provider Tool batch exceeds the hard batch limit")

        async def one(call):
            arguments = thaw_json(call.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("provider tool arguments must be an object")
            return await services.tools.execute(
                effect_id=EffectId(f"{run_id.value}:effect:{call.call_id.value}"),
                call=ToolCall(call.call_id, call.name, cast(JsonObject, arguments)),
                context=ToolContext(run_id, request_id, cancellation),
                execution_lease=execution_lease,
                run_fence=run_fence,
            )

        return await asyncio.gather(*(one(call) for call in calls))


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
    ) -> None:
        self._collaborator = collaborator
        self._effects = effects
        self._clock = clock

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

        while True:
            _cancel(cancel, tool_cancel)
            budget = services.provider.read_provider_budget(value.run_id)
            if state.phase == "ready":
                state = state.before_provider(
                    self._collaborator.limits, now=self._clock(), budget=budget
                )
                state = replace(
                    state,
                    provider_request_id=(
                        f"{value.run_id.value}:provider-turn:"
                        f"{state.provider_turns_reserved_total}"
                    ),
                )
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
            if state.phase not in {"provider_reserved", "tool_batch_reserved"}:
                raise RuntimeError(f"unsupported ReAct checkpoint phase: {state.phase}")
            assert state.provider_request_id is not None
            context = services.context.load(value.run_id)
            response = await services.provider.invoke(
                value.run_id,
                ProviderRequest(
                    RequestId(state.provider_request_id),
                    context.messages,
                    tools=value.tools,
                    temperature=value.temperature,
                    max_output_tokens=value.max_output_tokens,
                ),
                cancel=cancel,
                execution_lease=execution_lease,
            )
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
                    last_observed_at=self._clock(),
                )
                state, _ = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
                return ReActResult(response, state)
            if state.phase == "provider_reserved":
                keys = tuple(_repeat_key(call.name, call.arguments) for call in response.tool_calls)
                state = state.before_tool_batch(
                    keys,
                    self._collaborator.limits,
                    now=self._clock(),
                    budget=services.provider.read_provider_budget(value.run_id),
                )
                state, checkpoint_version = checkpoint.cas(
                    value.run_id, execution_lease, checkpoint_version, state
                )
            _cancel(cancel, tool_cancel)
            executions = await self._effects.execute(
                response.tool_calls,
                services=services,
                run_id=value.run_id,
                request_id=value.request_id,
                execution_lease=execution_lease,
                run_fence=run_fence,
                cancellation=tool_cancel,
            )
            for call, execution in zip(response.tool_calls, executions, strict=True):
                result = execution.result
                if result.outcome is ToolOutcome.UNKNOWN:
                    raise RuntimeError("tool_outcome_unknown")
                payload: dict[str, JsonValue] = {
                    "outcome": result.outcome.value,
                    "value": thaw_json(result.value),
                    "error_code": result.error_code,
                    "public_message": result.public_message,
                }
                context = services.context.append(
                    value.run_id,
                    execution_lease,
                    context.revision,
                    f"{value.run_id.value}:effect:{call.call_id.value}:context",
                    (
                        Message(
                            MessageRole.TOOL,
                            canonical_json(payload),
                            name=call.name,
                            call_id=call.call_id,
                        ),
                    ),
                )
            state = replace(
                state,
                phase="ready",
                provider_request_id=None,
                tool_batch_id=None,
                last_observed_at=self._clock(),
            )
            state, checkpoint_version = checkpoint.cas(
                value.run_id, execution_lease, checkpoint_version, state
            )


def _repeat_key(name: str, arguments: object) -> str:
    digest = hashlib.sha256(canonical_json(thaw_json(arguments)).encode()).hexdigest()
    return f"{name}:{digest}"


def _cancel(cancel: CancelToken, tool_cancel: CancellationToken) -> None:
    if cancel.is_cancelled or tool_cancel.cancelled:
        raise asyncio.CancelledError


__all__ = (
    "AgentLoopCollaborator",
    "EffectBatchExecutor",
    "ReActLoop",
    "ReActResult",
    "ReActRunInput",
)
