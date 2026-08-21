# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Official ReAct RuntimeDriver."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from simple_harness.contracts import (
    CallId,
    FrozenJsonValue,
    RequestId,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.contracts.messages import (
    ContentBlock,
    Message,
    MessageContent,
    MessageRole,
)
from simple_harness.execution.budget import (
    BudgetPolicy,
    FrozenPriceEstimator,
    budget_policy_fingerprint,
)
from simple_harness.execution.dispatch import ProviderInvocationUnknownError
from simple_harness.execution.recovery import RecoveryKind, WaitBlockerSpec
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime.conversation_memory import (
    ConversationContinuationInput,
    ConversationTurnOutput,
)
from simple_harness.runtime.kernel import DriverInvocation, DriverResult
from simple_harness.runtime.workflow_spawn import WorkflowSpawnFailed
from simple_harness.tools.errors import UnknownToolError
from simple_harness.tools.executor import ToolAuthorizationPending

from ..termination import TerminationBudgetExceeded, TerminationLimits
from .react_loop import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    ReActLoop,
    ReActRunInput,
    ToolEffectUnknownError,
)


class ReActDriver:
    def __init__(
        self,
        *,
        collaborator: AgentLoopCollaborator | None = None,
        effects: EffectBatchExecutor | None = None,
        clock=time.time,
        policy_fingerprint: str | None = None,
        provider_budget_fingerprint: str | None = None,
    ) -> None:
        self._clock = clock
        self._loop = ReActLoop(
            collaborator=collaborator or AgentLoopCollaborator(),
            effects=effects or EffectBatchExecutor(),
            clock=clock,
            policy_fingerprint=policy_fingerprint,
        )
        self.policy_fingerprint = policy_fingerprint
        self.provider_budget_fingerprint = provider_budget_fingerprint

    async def start(
        self, invocation: DriverInvocation, *, context, cancel
    ) -> DriverResult:
        if context is not invocation.services.context:
            raise ValueError("Runtime context service mismatch")
        expected_budget = invocation.start.provider_budget_fingerprint
        if expected_budget is not None:
            actual_budget = invocation.services.provider.budget_policy_fingerprint_for(
                RunId(invocation.run.run_id)
            )
            if actual_budget != expected_budget:
                raise ValueError("Provider budget policy differs from frozen Run binding")
        elif self.provider_budget_fingerprint is not None and (
            getattr(invocation.services.provider, "budget_policy_fingerprint", None)
            != self.provider_budget_fingerprint
        ):
            raise ValueError("Provider budget policy differs from ReAct composition")
        ready = invocation.workflow_spawn_ready_activation
        if ready is not None:
            coordinator = invocation.services.workflow_spawn
            if coordinator is None:
                raise RuntimeError(
                    "workflow spawn ready activation lacks its SDK coordinator"
                )
            outcome = await coordinator.continue_ready(ready)
            if isinstance(outcome, WorkflowSpawnFailed):
                return await self.start(
                    replace(
                        invocation, workflow_spawn_ready_activation=None
                    ),
                    context=context,
                    cancel=cancel,
                )
            return DriverResult(
                RunState.WAITING,
                {
                    "workflow_spawn_child_run_id": outcome.child_start_ref.child_run_id,
                    "workflow_spawn_wait_receipt_id": (
                        outcome.suspension.parent_wait_receipt_id
                    ),
                },
                workflow_spawn_control=outcome,
            )
        if invocation.continuations:
            stored = invocation.services.react_checkpoint.read_react_checkpoint(
                invocation.run.run_id
            )
            payload = None if stored is None else thaw_json(stored.checkpoint)
            if isinstance(payload, dict) and payload.get("phase") == "child_wait":
                if len(invocation.continuations) != 1:
                    raise RuntimeError(
                        "workflow child wait requires exactly one continuation"
                    )
                invocation.services.react_checkpoint.ack_spawn_child_continuation_and_continue_batch(
                    run_id=invocation.run.run_id,
                    continuation_claim=invocation.continuations[0],
                    execution_lease=invocation.execution_lease,
                    run_fence=invocation.run_fence,
                    now=self._clock(),
                )
            for continuation in invocation.continuations:
                continuation_payload = thaw_json(continuation.payload)
                if not isinstance(continuation_payload, dict) or (
                    continuation_payload.get("kind") != "conversation_user"
                ):
                    continue
                conversation_value = continuation_payload.get("conversation")
                if not isinstance(conversation_value, dict):
                    raise TypeError("conversation continuation envelope is required")
                conversation = ConversationContinuationInput.from_json(
                    conversation_value
                )
                continuation_messages = _continuation_prepared_messages(
                    continuation_payload.get("prepared_context"),
                    current_message=conversation.message,
                )
                current_context = invocation.services.context.load(
                    RunId(invocation.run.run_id)
                )
                invocation.services.context.append(
                    RunId(invocation.run.run_id),
                    invocation.execution_lease,
                    current_context.revision,
                    f"{continuation.continuation_id}:context:user",
                    continuation_messages,
                )
        input_value = cast(Mapping[str, object], invocation.start.input)
        prepared_context = invocation.start.prepared_context
        prepared_value = (
            None if prepared_context is None else thaw_json(prepared_context)
        )
        prepared_messages = (
            prepared_value.get("provider_messages")
            if isinstance(prepared_value, dict)
            else None
        )
        initial = _messages(
            input_value.get("messages")
            if prepared_messages is None
            else prepared_messages
        )
        if invocation.start.conversation is not None and (
            not initial
            or canonical_json(initial[-1].to_dict())
            != canonical_json(invocation.start.conversation.message.to_dict())
        ):
            raise ValueError(
                "prepared current message differs from conversation envelope"
            )
        tools = _tools(
            input_value.get("capability_snapshot"),
            invocation.services.tools,
            catalog=invocation.services.tool_catalog,
            generation=invocation.start.tool_catalog_generation,
            fingerprint=invocation.start.tool_catalog_fingerprint,
        )
        try:
            result = await self._loop.run(
                ReActRunInput(
                    RunId(invocation.run.run_id),
                    RequestId(invocation.run.request_id),
                    tools=tools,
                    temperature=(
                        None
                        if input_value.get("temperature") is None
                        else float(input_value["temperature"])
                    ),
                    max_output_tokens=(
                        None
                        if input_value.get("max_output_tokens") is None
                        else int(input_value["max_output_tokens"])
                    ),
                ),
                services=invocation.services,
                execution_lease=invocation.execution_lease,
                run_fence=invocation.run_fence,
                cancel=cancel,
                initial_messages=initial,
            )
        except UnknownToolError:
            return DriverResult(
                RunState.WAITING,
                {
                    "raw_failures": [
                        {
                            "error_code": "tool_not_exposed",
                            "source_kind": "tool_parse",
                            "retriable": True,
                            "replan": True,
                            "message": "Tool is not exposed; use capability_search.",
                        }
                    ]
                },
            )
        except ProviderInvocationUnknownError as error:
            invocation_record = error.invocation
            return DriverResult(
                RunState.WAITING,
                {"raw_failures": [{"error_code": "provider_outcome_unknown"}]},
                wait_blocker=(
                    None
                    if invocation_record is None
                    else WaitBlockerSpec(
                        RecoveryKind.PROVIDER,
                        invocation_record.invocation_id,
                        invocation_record.handoff_attempt,
                        invocation_record.version,
                    )
                ),
            )
        except TerminationBudgetExceeded as error:
            return DriverResult(
                RunState.FAILED,
                {"raw_failures": [{"error_code": str(error.code)}]},
            )
        except ToolAuthorizationPending as pending:
            return DriverResult(
                RunState.WAITING,
                {
                    "authorization_decision_id": pending.decision_id,
                    "authorization_prompt": pending.request.prompt,
                },
                authorization_wait=pending,
            )
        except ToolEffectUnknownError as error:
            return DriverResult(
                RunState.WAITING,
                {"raw_failures": [{"error_code": "tool_outcome_unknown"}]},
                wait_blocker=WaitBlockerSpec(
                    RecoveryKind.TOOL,
                    error.effect.effect_id.value,
                    error.effect.handoff_attempt,
                    error.effect.version,
                ),
            )
        response_message = result.response.message
        return DriverResult(
            RunState.COMPLETED,
            {
                "response_present": True,
                "finish_reason": getattr(result.response, "finish_reason", None),
            },
            conversation_output=(
                ConversationTurnOutput(
                    response_message,
                    _assistant_memory_text(response_message),
                )
                if isinstance(response_message, Message)
                else None
            ),
        )


def _assistant_memory_text(message: Message) -> str | None:
    if isinstance(message.content, str):
        return message.content
    values: list[str] = []
    for block in message.content:
        if block.type not in {"text", "input_text", "output_text"}:
            continue
        text = block.data.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text)
    return "\n".join(values) or None


def _messages(value: object) -> tuple[Message, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("ReAct input messages are required")
    messages: list[Message] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("ReAct input message must be an object")
        content = item["content"]
        normalized_content: MessageContent
        if isinstance(content, (list, tuple)):
            blocks = tuple(
                ContentBlock.from_dict(block)
                for block in content
                if isinstance(block, Mapping)
            )
            if len(blocks) != len(content):
                raise TypeError("ReAct structured content blocks must be objects")
            normalized_content = blocks
        elif isinstance(content, str):
            normalized_content = content
        else:
            raise TypeError("ReAct message content must be text or content blocks")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("ReAct message metadata must be an object")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise TypeError("ReAct message name must be a string")
        call_id = item.get("call_id")
        if call_id is not None and not isinstance(call_id, str):
            raise TypeError("ReAct message call_id must be a string")
        messages.append(
            Message(
                MessageRole(str(item["role"])),
                normalized_content,
                name=name,
                call_id=None if call_id is None else CallId(call_id),
                metadata=metadata,
            )
        )
    return tuple(messages)


def _continuation_prepared_messages(
    value: object,
    *,
    current_message: Message,
) -> tuple[Message, ...]:
    if not isinstance(value, Mapping):
        raise TypeError("conversation continuation prepared_context is required")
    messages = _messages(value.get("provider_messages"))
    if len(messages) < 2 or canonical_json(messages[-1].to_dict()) != canonical_json(
        current_message.to_dict()
    ):
        raise ValueError(
            "prepared continuation current message differs from conversation envelope"
        )
    for message in messages[:-1]:
        metadata = thaw_json(cast(FrozenJsonValue, message.metadata))
        if (
            message.role is not MessageRole.USER
            or not isinstance(metadata, dict)
            or metadata.get("trust") != "untrusted_data"
        ):
            raise ValueError(
                "prepared continuation memory must remain USER/untrusted data"
            )
    return messages


def _tools(
    value: object,
    executor,
    *,
    catalog,
    generation: int,
    fingerprint: str | None,
) -> tuple[ProviderToolSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise TypeError("capability_snapshot must be an object")
    names = value.get("tools", ())
    if not isinstance(names, (list, tuple)) or not all(
        isinstance(name, str) for name in names
    ):
        raise TypeError("capability_snapshot.tools must contain strings")
    if fingerprint is not None:
        resolver = getattr(catalog, "resolve", None)
        snapshot = None if resolver is None else resolver(generation, fingerprint)
        if snapshot is None:
            raise ValueError("frozen Tool catalog snapshot is unavailable")
        by_name = {spec.name: spec for spec in snapshot.specs}
        if any(name not in by_name for name in names):
            raise ValueError("capability snapshot differs from frozen Tool catalog")
        return tuple(by_name[name] for name in names)
    return executor.provider_tool_specs(tuple(names))


def build_react_driver(
    *,
    limits: TerminationLimits,
    budget_policy: BudgetPolicy,
    estimator: FrozenPriceEstimator | None,
    effects: EffectBatchExecutor | None = None,
    clock=time.time,
) -> ReActDriver:
    """Public hard-policy builder; every authority must be explicit and frozen."""

    if not isinstance(limits, TerminationLimits):
        raise TypeError("limits must use TerminationLimits")
    if not isinstance(budget_policy, BudgetPolicy):
        raise TypeError("budget_policy must use BudgetPolicy")
    if estimator is not None and not isinstance(estimator, FrozenPriceEstimator):
        raise TypeError("estimator must use FrozenPriceEstimator or None")
    provider_fingerprint = budget_policy_fingerprint(budget_policy, estimator)
    policy_payload = {
        "limits": {
            "max_consecutive_same_tool": limits.max_consecutive_same_tool,
            "max_cost_micros": limits.max_cost_micros,
            "max_tool_calls": limits.max_tool_calls,
            "max_turns": limits.max_turns,
            "max_wall_seconds": limits.max_wall_seconds,
        },
        "protocol": "react-hard-policy-v1",
        "provider_budget_fingerprint": provider_fingerprint,
    }
    policy_fingerprint = hashlib.sha256(
        canonical_json(policy_payload).encode("utf-8")
    ).hexdigest()
    return ReActDriver(
        collaborator=AgentLoopCollaborator(limits=limits),
        effects=effects,
        clock=clock,
        policy_fingerprint=policy_fingerprint,
        provider_budget_fingerprint=provider_fingerprint,
    )


__all__ = ("ReActDriver", "build_react_driver")
