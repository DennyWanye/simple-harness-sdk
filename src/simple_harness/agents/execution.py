# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``AgentExecutionDriver``: the ReAct core driven per AgentTurn, never ending the Agent.

Differences from the legacy ``ReActDriver``:

* a Run with no ``base_agent_input`` continuation is idle: the driver returns
  ``WAITING`` without loading Context or calling the Provider;
* the final Provider response becomes an ``AgentTurnOutcome`` (the kernel stages
  and finalizes it; the Run stays WAITING);
* a per-turn budget failure becomes a *failed turn result*, not a dead Agent.

``react_loop.py`` is not modified: tool gates, UNKNOWN handling and Context-use
checks are exactly the legacy ones.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import cast

from simple_harness.contracts import (
    JsonValue,
    Message,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.base_agent import BASE_AGENT_API_MODE, BASE_AGENT_INPUT_KIND
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import (
    ProviderInvocationUnknownError,
    provider_binding_fingerprint,
)
from simple_harness.execution.uow import RunState
from simple_harness.providers.base import ProviderContinuationCapability
from simple_harness.runtime.drivers.react import (
    _messages,
    _optional_float,
    _optional_int,
    _react_failure_result,
    _tools,
)
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    ReActLoop,
    ReActRunInput,
    ToolEffectUnknownError,
)
from simple_harness.runtime.kernel import DriverInvocation, DriverResult
from simple_harness.runtime.termination import TerminationBudgetExceeded, TerminationLimits
from simple_harness.tools.errors import MalformedToolArgumentsError, UnknownToolError
from simple_harness.tools.executor import ToolAuthorizationPending
from simple_harness.tools.runtime_catalog import RunToolExposurePort

from .completion import committed_outcome, failed_outcome
from .contracts import _message_from_json

BASE_AGENT_POLICY_PROTOCOL = "base-agent-hard-policy-v1"


def _binding_failure(code: str, message: str) -> DriverResult:
    """Kernel-integrity failure (BA-v1.0 §1.3): the Run may fail, the input is not retried."""

    return DriverResult(
        RunState.FAILED,
        {
            "raw_failures": [
                {
                    "error_code": code,
                    "source_kind": "runtime",
                    "retriable": False,
                    "message": message,
                }
            ]
        },
    )


class AgentExecutionDriver:
    def __init__(
        self,
        *,
        collaborator: AgentLoopCollaborator,
        effects: EffectBatchExecutor | None = None,
        clock: Callable[[], float] = time.time,
        policy_fingerprint: str | None = None,
        provider_budget_fingerprint: str | None = None,
        tool_exposure_resolver: Callable[[RunId], RunToolExposurePort | None] | None = None,
        delegation_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._clock = clock
        self._loop = ReActLoop(
            collaborator=collaborator,
            effects=effects or EffectBatchExecutor(),
            clock=clock,
            policy_fingerprint=policy_fingerprint,
        )
        self.policy_fingerprint = policy_fingerprint
        self.provider_budget_fingerprint = provider_budget_fingerprint
        self._tool_exposure_resolver = tool_exposure_resolver
        self._delegation_counter = delegation_counter

    async def start(  # type: ignore[no-untyped-def]
        self, invocation: DriverInvocation, *, context, cancel
    ) -> DriverResult:
        if context is not invocation.services.context:
            raise ValueError("Runtime context service mismatch")
        run_id = RunId(invocation.run.run_id)
        input_value = cast(Mapping[str, object], invocation.start.input)
        binding = input_value.get("base_agent_binding")
        if not isinstance(binding, Mapping) or binding.get("api_mode") != BASE_AGENT_API_MODE:
            return _binding_failure(
                "base_agent_binding_missing", "Run start snapshot lacks a BaseAgent binding."
            )
        agent_id = binding.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return _binding_failure(
                "base_agent_binding_missing", "BaseAgent binding lacks agent_id."
            )

        turn_input: dict[str, JsonValue] | None = None
        unexpected: list[str] = []
        for continuation in invocation.continuations:
            payload = thaw_json(continuation.payload)
            if isinstance(payload, dict) and payload.get("kind") == BASE_AGENT_INPUT_KIND:
                turn_input = payload
            else:
                unexpected.append(
                    str(payload.get("kind"))
                    if isinstance(payload, dict)
                    else type(payload).__name__
                )
        if turn_input is None:
            if unexpected:
                # Closure E3: consume (the kernel acks it) but never terminalize the Agent.
                return DriverResult(
                    RunState.WAITING,
                    {
                        "base_agent_stage": "idle",
                        "raw_failures": [
                            {
                                "error_code": "base_agent_unexpected_continuation",
                                "source_kind": "runtime",
                                "retriable": False,
                                "observed_kind": kind,
                            }
                            for kind in unexpected
                        ],
                    },
                )
            # Closure E3/#6: creation drives once; no input means no model call.
            return DriverResult(RunState.WAITING, {"base_agent_stage": "idle"})

        turn_id = str(turn_input.get("turn_id"))
        input_id = str(turn_input.get("input_id"))
        input_hash = str(turn_input.get("input_hash"))
        seq = int(cast(int, turn_input.get("seq", 0)))
        if turn_input.get("agent_id") != agent_id:
            raise ValueError("base_agent_input belongs to another Agent")
        message_value = turn_input.get("message")
        if not isinstance(message_value, Mapping):
            raise TypeError("base_agent_input requires a message object")
        user_message = _message_from_json(message_value)

        expected_budget = invocation.start.provider_budget_fingerprint
        if expected_budget is not None:
            actual_budget = invocation.services.provider.budget_policy_fingerprint_for(run_id)
            if actual_budget != expected_budget:
                raise ValueError("Provider budget policy differs from frozen Run binding")
        elif self.provider_budget_fingerprint is not None and (
            getattr(invocation.services.provider, "budget_policy_fingerprint", None)
            != self.provider_budget_fingerprint
        ):
            raise ValueError("Provider budget policy differs from BaseAgent composition")

        checkpoint_port = invocation.services.react_checkpoint
        ordinal_from = _reserved_provider_turns(checkpoint_port, invocation.run.run_id)
        mark_running = getattr(checkpoint_port, "mark_agent_turn_running", None)
        if callable(mark_running):
            mark_running(
                turn_id=turn_id,
                execution_lease=invocation.execution_lease,
                provider_turn_ordinal_from=ordinal_from,
                now=self._clock(),
            )

        current_context = invocation.services.context.load(run_id)
        initial = _messages(input_value.get("messages"))
        if current_context.revision == 0:
            initial_messages: tuple[Message, ...] = (*initial, user_message)
        else:
            invocation.services.context.append(
                run_id,
                invocation.execution_lease,
                current_context.revision,
                f"{turn_id}:context:user",
                (user_message,),
            )
            initial_messages = (user_message,)
        tools = _tools(
            input_value.get("capability_snapshot"),
            invocation.services.tools,
            catalog=invocation.services.tool_catalog,
            generation=invocation.start.tool_catalog_generation,
            fingerprint=invocation.start.tool_catalog_fingerprint,
        )
        tool_exposure = (
            None if self._tool_exposure_resolver is None else self._tool_exposure_resolver(run_id)
        )
        try:
            result = await self._loop.run(
                ReActRunInput(
                    run_id,
                    RequestId(invocation.run.request_id),
                    turn_id=turn_id,
                    continuation_id=None,
                    tools=tools,
                    tool_exposure=tool_exposure,
                    temperature=_optional_float(input_value.get("temperature"), "temperature"),
                    max_output_tokens=_optional_int(
                        input_value.get("max_output_tokens"), "max_output_tokens"
                    ),
                    initial_route_receipt=invocation.start.initial_route_receipt,
                    initial_route_receipt_hash=invocation.start.initial_route_receipt_hash,
                ),
                services=invocation.services,
                execution_lease=invocation.execution_lease,
                run_fence=invocation.run_fence,
                cancel=cancel,
                initial_messages=initial_messages,
            )
        except TerminationBudgetExceeded as error:
            # The turn failed; the Agent lives on (BA-v1.0 §1.3).
            return DriverResult(
                RunState.WAITING,
                {"response_present": False, "raw_failures": [{"error_code": str(error.code)}]},
                agent_turn_outcome=failed_outcome(
                    agent_id=agent_id,
                    turn_id=turn_id,
                    seq=seq,
                    input_id=input_id,
                    input_hash=input_hash,
                    error={"error_code": str(error.code), "source_kind": "termination"},
                    delegation_count=self._delegations(turn_id),
                    provider_turn_ordinal_from=ordinal_from,
                    provider_turn_ordinal_to=_reserved_provider_turns(
                        checkpoint_port, invocation.run.run_id
                    ),
                ),
            )
        except (UnknownToolError, MalformedToolArgumentsError) as error:
            # Model protocol violations end this turn as FAILED; the Agent stays alive.
            code = (
                "tool_not_exposed"
                if isinstance(error, UnknownToolError)
                else "invalid_tool_arguments"
            )
            return DriverResult(
                RunState.WAITING,
                {"response_present": False, "raw_failures": [{"error_code": code}]},
                agent_turn_outcome=failed_outcome(
                    agent_id=agent_id,
                    turn_id=turn_id,
                    seq=seq,
                    input_id=input_id,
                    input_hash=input_hash,
                    error={
                        "error_code": code,
                        "source_kind": "tool_parse",
                        "message": str(error)[:500],
                    },
                    delegation_count=self._delegations(turn_id),
                    provider_turn_ordinal_from=ordinal_from,
                    provider_turn_ordinal_to=_reserved_provider_turns(
                        checkpoint_port, invocation.run.run_id
                    ),
                ),
            )
        except (
            ProviderInvocationUnknownError,
            ToolAuthorizationPending,
            ToolEffectUnknownError,
        ) as error:
            return _react_failure_result(error)
        response = result.response
        outcome = committed_outcome(
            agent_id=agent_id,
            turn_id=turn_id,
            seq=seq,
            input_id=input_id,
            input_hash=input_hash,
            response_message=response.message,
            usage_refs=(
                (f"provider-request:{response.request_id.value}",)
                if isinstance(response.request_id, RequestId)
                else ()
            ),
            delegation_count=self._delegations(turn_id),
            provider_turn_ordinal_from=ordinal_from,
            provider_turn_ordinal_to=result.termination.provider_turns_reserved_total,
        )
        return DriverResult(
            RunState.WAITING,
            {
                "response_present": True,
                "finish_reason": getattr(response, "finish_reason", None),
                "base_agent_stage": "result_pending",
            },
            agent_turn_outcome=outcome,
        )

    def _delegations(self, turn_id: str) -> int:
        if self._delegation_counter is None:
            return 0
        return int(self._delegation_counter(turn_id))


def _reserved_provider_turns(checkpoint_port, run_id: str) -> int | None:  # type: ignore[no-untyped-def]
    reader = getattr(checkpoint_port, "read_react_checkpoint", None)
    if not callable(reader):
        return None
    stored = reader(run_id)
    if stored is None:
        return 0
    payload = thaw_json(stored.checkpoint)
    if not isinstance(payload, dict):
        return None
    value = payload.get("provider_turns_reserved_total")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def build_agent_execution_driver(
    *,
    limits: TerminationLimits,
    budget_policy: BudgetPolicy,
    estimator: FrozenPriceEstimator | None,
    effects: EffectBatchExecutor | None = None,
    tool_exposure_resolver: Callable[[RunId], RunToolExposurePort | None] | None = None,
    continuation_capability: ProviderContinuationCapability = ProviderContinuationCapability(),
    delegation_counter: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.time,
) -> AgentExecutionDriver:
    """Hard-policy builder; the fingerprint protocol differs from legacy ReAct on purpose."""

    if not isinstance(limits, TerminationLimits):
        raise TypeError("limits must use TerminationLimits")
    if not isinstance(budget_policy, BudgetPolicy):
        raise TypeError("budget_policy must use BudgetPolicy")
    if estimator is not None and not isinstance(estimator, FrozenPriceEstimator):
        raise TypeError("estimator must use FrozenPriceEstimator or None")
    provider_fingerprint = provider_binding_fingerprint(
        budget_policy, estimator, continuation_capability
    )
    policy_payload: dict[str, JsonValue] = {
        "limits": {
            "max_consecutive_same_tool": limits.max_consecutive_same_tool,
            "max_cost_micros": limits.max_cost_micros,
            "max_tool_calls": limits.max_tool_calls,
            "max_turns": limits.max_turns,
            "max_wall_seconds": limits.max_wall_seconds,
        },
        "protocol": BASE_AGENT_POLICY_PROTOCOL,
        "provider_budget_fingerprint": provider_fingerprint,
    }
    policy_fingerprint = hashlib.sha256(canonical_json(policy_payload).encode("utf-8")).hexdigest()
    return AgentExecutionDriver(
        collaborator=AgentLoopCollaborator(limits=limits),
        effects=effects,
        clock=clock,
        policy_fingerprint=policy_fingerprint,
        provider_budget_fingerprint=provider_fingerprint,
        tool_exposure_resolver=tool_exposure_resolver,
        delegation_counter=delegation_counter,
    )


__all__ = ("AgentExecutionDriver", "BASE_AGENT_POLICY_PROTOCOL", "build_agent_execution_driver")
