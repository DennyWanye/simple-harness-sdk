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

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from typing import cast

from simple_harness.contracts import (
    JsonValue,
    Message,
    RequestId,
    RunId,
    canonical_json,
    freeze_json,
    thaw_json,
)
from simple_harness.execution.base_agent import BASE_AGENT_API_MODE, BASE_AGENT_INPUT_KIND
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.dispatch import (
    _DEFINITE_PROVIDER_FAILURES,
    ProviderInvocationUnknownError,
    provider_binding_fingerprint,
)
from simple_harness.execution.uow import RunState
from simple_harness.providers.base import ProviderContinuationCapability
from simple_harness.runtime.context import ContextSnapshot
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
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.runtime.termination import (
    TerminationBudgetExceeded,
    TerminationLimits,
    TerminationReason,
)
from simple_harness.tools.contracts import CancellationToken
from simple_harness.tools.errors import MalformedToolArgumentsError, UnknownToolError
from simple_harness.tools.executor import ToolAuthorizationPending
from simple_harness.tools.runtime_catalog import RunToolExposurePort

from .completion import committed_outcome, failed_outcome
from .contracts import _message_from_json

BASE_AGENT_POLICY_PROTOCOL = "base-agent-hard-policy-v1"
AGENT_TURN_CANCELLED = "agent_turn_cancelled"


class _TurnCancelled(Exception):
    """Internal: the per-turn cooperative cancel token fired inside the loop."""


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
        turn_cancellations: MutableMapping[str, CancellationToken] | None = None,
        empty_response_retries: int = 2,
        max_output_tokens_ceiling: int = 8192,
    ) -> None:
        self._clock = clock
        self._empty_response_retries = max(0, int(empty_response_retries))
        self._max_output_tokens_ceiling = max(1, int(max_output_tokens_ceiling))
        # In-process cooperative cancel tokens keyed by turn_id (T8); the durable
        # intent lives in base_agent_control_commands_v1 for cross-process resumes.
        self.turn_cancellations: MutableMapping[str, CancellationToken] = (
            {} if turn_cancellations is None else turn_cancellations
        )
        self._lifetime_limits = collaborator.limits
        self._effects = effects or EffectBatchExecutor()
        # Kept for callers that inspect the lifetime loop; every turn runs its own
        # ``ReActLoop`` with limits derived from the turn's durable baselines (T6).
        self._loop = ReActLoop(
            collaborator=collaborator,
            effects=self._effects,
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
        totals = _checkpoint_totals(checkpoint_port, invocation.run.run_id)
        ordinal_from = totals.provider_turns
        turn_created_at: float | None = None
        tool_calls_from: int | None = totals.tool_calls
        mark_running = getattr(checkpoint_port, "mark_agent_turn_running", None)
        if callable(mark_running):
            # The baselines are written once: a resume of the same turn (UNKNOWN or
            # authorization wait) reads the first admission back instead of the
            # current cumulative totals, so "per turn" never means "per attempt".
            turn_row = mark_running(
                turn_id=turn_id,
                execution_lease=invocation.execution_lease,
                provider_turn_ordinal_from=ordinal_from,
                tool_call_ordinal_from=totals.tool_calls,
                now=self._clock(),
            )
            if turn_row is not None:
                ordinal_from = getattr(turn_row, "provider_turn_ordinal_from", ordinal_from)
                tool_calls_from = getattr(turn_row, "tool_call_ordinal_from", tool_calls_from)
                turn_created_at = getattr(turn_row, "created_at", None)

        # T7: the run-level ReAct checkpoint may only be resumed by the turn that left
        # it in flight; any other turn on an in-flight checkpoint is a kernel-integrity
        # failure (never silently reused).
        identity_failure = self._check_turn_identity(invocation, run_id, ordinal_from)
        if identity_failure is not None:
            return identity_failure
        if totals.started_at is None:
            # The identity check created the checkpoint for a first turn: re-read the
            # loop's ``started_at`` so the deadline offset is anchored correctly.
            totals = _checkpoint_totals(checkpoint_port, invocation.run.run_id)

        # T8: a durable cancel intent recorded before this (re)admission fails the turn
        # without a single provider call; an in-process intent arrives via the token.
        read_cancel = getattr(checkpoint_port, "read_agent_turn_cancel", None)
        if callable(read_cancel) and read_cancel(turn_id) is not None:
            unknown = _unknown_effects(checkpoint_port, run_id)
            if unknown:
                # The turn still owns an UNKNOWN effect: keep waiting for its
                # reconciliation (the loop identities depend on the in-flight
                # checkpoint).  The intent is durable and applies once it settles.
                return _react_failure_result(ToolEffectUnknownError(unknown[0]))
            self._settle_failed_turn(invocation, run_id)
            return self._cancelled_turn(
                agent_id=agent_id,
                turn_id=turn_id,
                seq=seq,
                input_id=input_id,
                input_hash=input_hash,
                ordinal_from=ordinal_from,
                checkpoint_port=checkpoint_port,
                run_id_value=invocation.run.run_id,
            )
        deadline = _turn_deadline(binding.get("limits"))
        if (
            deadline is not None
            and turn_created_at is not None
            and self._clock() - float(turn_created_at) >= deadline
        ):
            # Anchored on the turn's first durable admission: a restart that arrives
            # after the deadline fails the turn without a single provider call.
            self._settle_failed_turn(invocation, run_id)
            return self._budget_failure(
                TerminationBudgetExceeded(TerminationReason.WALL_CLOCK),
                agent_id=agent_id,
                turn_id=turn_id,
                seq=seq,
                input_id=input_id,
                input_hash=input_hash,
                ordinal_from=ordinal_from,
                checkpoint_port=checkpoint_port,
                run_id_value=invocation.run.run_id,
            )
        turn_limits = _turn_limits(
            self._lifetime_limits,
            binding.get("limits"),
            provider_turns_from=ordinal_from,
            tool_calls_from=tool_calls_from,
            turn_created_at=turn_created_at,
            loop_started_at=totals.started_at,
        )
        loop = ReActLoop(
            collaborator=AgentLoopCollaborator(limits=turn_limits),
            effects=self._effects,
            clock=self._clock,
            policy_fingerprint=self.policy_fingerprint,
        )

        # Both branches append the user message under the turn-scoped id so a rerun of the
        # first turn is idempotent (review F4); instructions get their own id.
        revision_reader = getattr(invocation.services.context, "revision", None)
        if callable(revision_reader):
            # Journal port: only the CAS anchor is needed here, not an assembled
            # request (that happens once per provider turn inside the loop).
            current_context = ContextSnapshot(int(revision_reader(run_id)), ())
        else:
            current_context = invocation.services.context.load(run_id)
        initial = _messages(input_value.get("messages"))
        if current_context.revision == 0 and initial:
            current_context = invocation.services.context.append(
                run_id,
                invocation.execution_lease,
                0,
                f"{invocation.run.run_id}:context:instructions",
                initial,
            )
        current_context = invocation.services.context.append(
            run_id,
            invocation.execution_lease,
            current_context.revision,
            f"{turn_id}:context:user",
            (user_message,),
        )
        initial_messages: tuple[Message, ...] = (user_message,)
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
        output_cap = _optional_int(input_value.get("max_output_tokens"), "max_output_tokens")
        attempt = 0
        escalations: list[dict[str, JsonValue]] = []
        while True:
            token = self.turn_cancellations.get(turn_id)
            if token is None:
                token = CancellationToken()
                self.turn_cancellations[turn_id] = token
            try:
                result = await self._run_turn(
                    loop,
                    invocation,
                    run_id,
                    turn_id,
                    tools,
                    tool_exposure,
                    input_value,
                    initial_messages,
                    cancel,
                    token,
                    output_cap,
                )
            except _TurnCancelled:
                self._settle_failed_turn(invocation, run_id)
                return self._cancelled_turn(
                    agent_id=agent_id,
                    turn_id=turn_id,
                    seq=seq,
                    input_id=input_id,
                    input_hash=input_hash,
                    ordinal_from=ordinal_from,
                    checkpoint_port=checkpoint_port,
                    run_id_value=invocation.run.run_id,
                )
            except TerminationBudgetExceeded as error:
                # The turn failed; the Agent lives on (BA-v1.0 §1.3).  The run-level
                # checkpoint may be mid-flight (a limit breached after the provider
                # answered): release it so the next turn starts a fresh request.
                self._settle_failed_turn(invocation, run_id)
                return self._budget_failure(
                    error,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    seq=seq,
                    input_id=input_id,
                    input_hash=input_hash,
                    ordinal_from=ordinal_from,
                    checkpoint_port=checkpoint_port,
                    run_id_value=invocation.run.run_id,
                )
            except (
                UnknownToolError,
                MalformedToolArgumentsError,
                *_DEFINITE_PROVIDER_FAILURES,
            ) as error:
                # Model protocol violations and definite Provider refusals end this turn as
                # FAILED; the Agent stays alive (UNKNOWN outcomes keep the legacy wait path).
                if isinstance(error, UnknownToolError):
                    code = "tool_not_exposed"
                elif isinstance(error, MalformedToolArgumentsError):
                    code = "invalid_tool_arguments"
                else:
                    code = str(getattr(error, "code", "provider_rejected"))
                detail = getattr(error, "detail", None)
                if (
                    code == "provider_empty_response"
                    and isinstance(detail, Mapping)
                    and detail.get("finish_reason") == "length"
                    and attempt < self._empty_response_retries
                    and output_cap is not None
                    and output_cap < self._max_output_tokens_ceiling
                ):
                    # F-BA-1: the model spent its whole output cap on reasoning and returned
                    # no text.  Escalate the cap and issue a fresh provider turn within the
                    # same AgentTurn; the failed invocation stays settled in the ledger.
                    self._settle_failed_turn(invocation, run_id)
                    attempt += 1
                    output_cap = min(output_cap * 2, self._max_output_tokens_ceiling)
                    escalations.append({"attempt": attempt, "max_output_tokens": output_cap})
                    continue
                # Whatever the cause, the turn is definitely over: release the loop
                # checkpoint so the next AgentTurn starts a fresh provider request.
                self._settle_failed_turn(invocation, run_id)
                error_payload: dict[str, JsonValue] = {
                    "error_code": code,
                    "source_kind": "tool_parse",
                    "error_type": type(error).__name__,
                }
                if isinstance(detail, Mapping):
                    # e.g. finish_reason / observed usage of an empty provider response,
                    # kept in the durable turn result (review F6).
                    error_payload["detail"] = cast(JsonValue, thaw_json(freeze_json(dict(detail))))
                if escalations:
                    error_payload["output_cap_escalations"] = cast(
                        JsonValue, thaw_json(freeze_json(list(escalations)))
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
                        error=error_payload,
                        delegation_count=self._delegations(turn_id),
                        provider_turn_ordinal_from=ordinal_from,
                        provider_turn_ordinal_to=_checkpoint_totals(
                            checkpoint_port, invocation.run.run_id
                        ).provider_turns,
                    ),
                )
            except (
                ProviderInvocationUnknownError,
                ToolAuthorizationPending,
                ToolEffectUnknownError,
            ) as error:
                return _react_failure_result(error)
            finally:
                self.turn_cancellations.pop(turn_id, None)
            break
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

    def _stage_companion(self, invocation: DriverInvocation, turn_id: str):  # type: ignore[no-untyped-def]
        """BA31: stage the committed turn result inside the loop's final checkpoint CAS.

        The outcome built here is byte-identical to the one ``start`` returns after
        the loop, so the kernel's own stage is an idempotent no-op and finalize-first
        recovery commits it even if the process dies right after the CAS.
        """

        input_value = cast(Mapping[str, object], invocation.start.input)
        binding = cast(Mapping[str, object], input_value.get("base_agent_binding") or {})
        agent_id = str(binding.get("agent_id"))
        turn_reader = getattr(invocation.services.react_checkpoint, "read_agent_turn", None)
        turn = turn_reader(turn_id) if callable(turn_reader) else None
        if turn is None:
            return None
        lease_epoch = invocation.execution_lease.epoch
        clock = self._clock
        delegations = self._delegations

        def factory(response, state):  # type: ignore[no-untyped-def]
            from simple_harness.execution.sqlite.base_agent import turns as turn_helpers

            outcome = committed_outcome(
                agent_id=agent_id,
                turn_id=turn_id,
                seq=turn.seq,
                input_id=turn.input_id,
                input_hash=turn.input_hash,
                response_message=response.message,
                usage_refs=(
                    (f"provider-request:{response.request_id.value}",)
                    if isinstance(response.request_id, RequestId)
                    else ()
                ),
                delegation_count=delegations(turn_id),
                provider_turn_ordinal_from=turn.provider_turn_ordinal_from,
                provider_turn_ordinal_to=state.provider_turns_reserved_total,
            )

            def companion(connection) -> None:  # type: ignore[no-untyped-def]
                turn_helpers.stage_result(
                    connection,
                    turn_id=turn_id,
                    result_hash=outcome.result_hash,
                    result_json=outcome.result_object(),
                    provider_turn_ordinal_from=outcome.provider_turn_ordinal_from,
                    provider_turn_ordinal_to=outcome.provider_turn_ordinal_to,
                    lease_epoch=lease_epoch,
                    now=clock(),
                )

            return companion

        return factory

    async def _run_turn(  # type: ignore[no-untyped-def]
        self,
        loop: ReActLoop,
        invocation: DriverInvocation,
        run_id: RunId,
        turn_id: str,
        tools,
        tool_exposure,
        input_value: Mapping[str, object],
        initial_messages: tuple[Message, ...],
        cancel,
        token: CancellationToken,
        max_output_tokens: int | None = None,
    ):
        try:
            return await loop.run(
                ReActRunInput(
                    run_id,
                    RequestId(invocation.run.request_id),
                    turn_id=turn_id,
                    continuation_id=None,
                    tools=tools,
                    tool_exposure=tool_exposure,
                    temperature=_optional_float(input_value.get("temperature"), "temperature"),
                    max_output_tokens=max_output_tokens,
                    initial_route_receipt=invocation.start.initial_route_receipt,
                    initial_route_receipt_hash=invocation.start.initial_route_receipt_hash,
                    final_companion=self._stage_companion(invocation, turn_id),
                ),
                services=invocation.services,
                execution_lease=invocation.execution_lease,
                run_fence=invocation.run_fence,
                cancel=cancel,
                initial_messages=initial_messages,
                tool_cancel=token,
            )
        except asyncio.CancelledError:
            # Only a cooperative per-turn cancel is a *turn* failure; a real task /
            # kernel cancel keeps propagating untouched.
            if token.cancelled and not getattr(cancel, "is_cancelled", False):
                # A tool that honoured the token mid-execution left its effect
                # UNKNOWN (executor marks it before re-raising).  That effect must be
                # reconciled, never orphaned: take the legacy UNKNOWN wait path; the
                # durable cancel intent fails the turn on resume (review F1).
                unknown = _unknown_effects(invocation.services.react_checkpoint, run_id)
                if unknown:
                    raise ToolEffectUnknownError(unknown[0]) from None
                raise _TurnCancelled() from None
            raise

    def _cancelled_turn(  # type: ignore[no-untyped-def]
        self,
        *,
        agent_id: str,
        turn_id: str,
        seq: int,
        input_id: str,
        input_hash: str,
        ordinal_from: int | None,
        checkpoint_port,
        run_id_value: str,
    ) -> DriverResult:
        return DriverResult(
            RunState.WAITING,
            {
                "response_present": False,
                "base_agent_stage": "cancelled",
                "raw_failures": [{"error_code": AGENT_TURN_CANCELLED}],
            },
            agent_turn_outcome=failed_outcome(
                agent_id=agent_id,
                turn_id=turn_id,
                seq=seq,
                input_id=input_id,
                input_hash=input_hash,
                error={"error_code": AGENT_TURN_CANCELLED, "source_kind": "control"},
                delegation_count=self._delegations(turn_id),
                provider_turn_ordinal_from=ordinal_from,
                provider_turn_ordinal_to=_checkpoint_totals(
                    checkpoint_port, run_id_value
                ).provider_turns,
            ),
        )

    def _budget_failure(  # type: ignore[no-untyped-def]
        self,
        error: TerminationBudgetExceeded,
        *,
        agent_id: str,
        turn_id: str,
        seq: int,
        input_id: str,
        input_hash: str,
        ordinal_from: int | None,
        checkpoint_port,
        run_id_value: str,
    ) -> DriverResult:
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
                provider_turn_ordinal_to=_checkpoint_totals(
                    checkpoint_port, run_id_value
                ).provider_turns,
            ),
        )

    def _check_turn_identity(
        self, invocation: DriverInvocation, run_id: RunId, ordinal_from: int | None
    ) -> DriverResult | None:
        """D7: one run-level checkpoint, identity by durable ordinals (no per-turn key).

        A turn's ``provider_turn_ordinal_from`` is written once at first admission.
        A checkpoint left in flight by *this* turn has ``provider_turns_reserved_total``
        strictly above that baseline (the turn reserved it); a checkpoint in flight
        with a total at or below the baseline was reserved by an earlier turn and must
        never be resumed under a different input.
        """

        if ordinal_from is None:
            return None
        checkpoint = DurableReactCheckpoint(invocation.services.react_checkpoint, clock=self._clock)
        state, _ = checkpoint.load_or_create(
            run_id,
            invocation.execution_lease,
            initial_route_receipt=invocation.start.initial_route_receipt,
            initial_route_receipt_hash=invocation.start.initial_route_receipt_hash,
        )
        if state.phase in _INFLIGHT_PHASES and state.provider_turns_reserved_total <= ordinal_from:
            return _binding_failure(
                "base_agent_turn_identity_conflict",
                "The Run's ReAct checkpoint is in flight for another AgentTurn.",
            )
        return None

    def _settle_failed_provider_turn(self, invocation: DriverInvocation, run_id: RunId) -> None:
        """Backward-compatible name: any definitely-failed turn releases the checkpoint."""

        self._settle_failed_turn(invocation, run_id)

    def _settle_failed_turn(self, invocation: DriverInvocation, run_id: RunId) -> None:
        """Return the run-level ReAct checkpoint to ``ready`` after a failed turn.

        Totals (``*_reserved_total``) are never reset; only the in-flight request /
        response / tool progress is dropped, exactly like the loop's own end-of-turn
        transition.  Never called on UNKNOWN outcomes (those keep the checkpoint).
        """

        checkpoint = DurableReactCheckpoint(invocation.services.react_checkpoint, clock=self._clock)
        state, version = checkpoint.load_or_create(
            run_id,
            invocation.execution_lease,
            initial_route_receipt=invocation.start.initial_route_receipt,
            initial_route_receipt_hash=invocation.start.initial_route_receipt_hash,
        )
        if state.phase == "ready":
            return
        if _unknown_effects(invocation.services.react_checkpoint, run_id):
            # Never drop a checkpoint that still owns an UNKNOWN effect: the tool
            # loop's identities are derived from it (review F1).
            return
        checkpoint.cas(
            run_id,
            invocation.execution_lease,
            version,
            replace(
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
                mandatory_context_repairs=(),
                last_observed_at=self._clock(),
            ),
        )

    def _delegations(self, turn_id: str) -> int:
        if self._delegation_counter is None:
            return 0
        return int(self._delegation_counter(turn_id))


_INFLIGHT_PHASES = frozenset(
    {"provider_reserved", "tool_batch_reserved", "response_reserved", "context_action_reserved"}
)


@dataclass(frozen=True, slots=True)
class _CheckpointTotals:
    provider_turns: int | None
    tool_calls: int | None
    started_at: float | None


def _checkpoint_totals(checkpoint_port, run_id: str) -> _CheckpointTotals:  # type: ignore[no-untyped-def]
    reader = getattr(checkpoint_port, "read_react_checkpoint", None)
    if not callable(reader):
        return _CheckpointTotals(None, None, None)
    stored = reader(run_id)
    if stored is None:
        return _CheckpointTotals(0, 0, None)
    payload = thaw_json(stored.checkpoint)
    if not isinstance(payload, dict):
        return _CheckpointTotals(None, None, None)

    def _int(name: str) -> int | None:
        value = payload.get(name)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    started = payload.get("started_at")
    return _CheckpointTotals(
        _int("provider_turns_reserved_total"),
        _int("tool_calls_reserved_total"),
        float(started)
        if isinstance(started, (int, float)) and not isinstance(started, bool)
        else None,
    )


def _unknown_effects(checkpoint_port, run_id: RunId):  # type: ignore[no-untyped-def]
    reader = getattr(checkpoint_port, "list_unknown_effects_for_run", None)
    if not callable(reader):
        return ()
    return tuple(reader(run_id.value))


def _reserved_provider_turns(checkpoint_port, run_id: str) -> int | None:  # type: ignore[no-untyped-def]
    return _checkpoint_totals(checkpoint_port, run_id).provider_turns


def _turn_deadline(per_turn: object) -> float | None:
    if not isinstance(per_turn, Mapping):
        return None
    value = per_turn.get("turn_deadline_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _turn_limits(
    lifetime: TerminationLimits,
    per_turn: object,
    *,
    provider_turns_from: int | None,
    tool_calls_from: int | None,
    turn_created_at: float | None,
    loop_started_at: float | None,
) -> TerminationLimits:
    """Derive this turn's ``TerminationLimits`` from durable baselines (T6).

    ``TerminationState`` totals never reset, so a per-turn cap of N model calls is
    expressed as ``baseline + N`` where the baseline is the turn's first admission.
    Lifetime limits stay the ceiling; the policy fingerprint is untouched.
    """

    if not isinstance(per_turn, Mapping):
        return lifetime
    max_turns = lifetime.max_turns
    max_tool_calls = lifetime.max_tool_calls
    max_wall = lifetime.max_wall_seconds
    calls = per_turn.get("max_model_calls_per_turn")
    if provider_turns_from is not None and isinstance(calls, int) and not isinstance(calls, bool):
        max_turns = min(max_turns, provider_turns_from + calls)
    tools = per_turn.get("max_tool_calls_per_turn")
    if tool_calls_from is not None and isinstance(tools, int) and not isinstance(tools, bool):
        max_tool_calls = min(max_tool_calls, tool_calls_from + tools)
    deadline = per_turn.get("turn_deadline_seconds")
    if (
        turn_created_at is not None
        and loop_started_at is not None
        and isinstance(deadline, (int, float))
        and not isinstance(deadline, bool)
    ):
        # ``_check_common`` compares ``now - started_at``; anchor on the turn's first
        # durable admission so a restart never extends the deadline.
        offset = max(0.0, float(turn_created_at) - float(loop_started_at))
        max_wall = min(max_wall, max(offset + float(deadline), 1e-6))
    return TerminationLimits(
        max_turns=max(1, max_turns),
        max_tool_calls=max(1, max_tool_calls),
        max_wall_seconds=max_wall,
        max_cost_micros=lifetime.max_cost_micros,
        max_consecutive_same_tool=lifetime.max_consecutive_same_tool,
    )


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
    turn_cancellations: MutableMapping[str, CancellationToken] | None = None,
    empty_response_retries: int = 2,
    max_output_tokens_ceiling: int = 8192,
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
        turn_cancellations=turn_cancellations,
        empty_response_retries=empty_response_retries,
        max_output_tokens_ceiling=max_output_tokens_ceiling,
    )


__all__ = (
    "AGENT_TURN_CANCELLED",
    "AgentExecutionDriver",
    "BASE_AGENT_POLICY_PROTOCOL",
    "build_agent_execution_driver",
)
