# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Official ReAct RuntimeDriver."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.dispatch import ProviderInvocationUnknownError
from simple_harness.execution.recovery import RecoveryKind, WaitBlockerSpec
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderToolSpec
from simple_harness.runtime.kernel import DriverInvocation, DriverResult
from simple_harness.tools.errors import UnknownToolError

from ..termination import TerminationBudgetExceeded
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
    ) -> None:
        self._loop = ReActLoop(
            collaborator=collaborator or AgentLoopCollaborator(),
            effects=effects or EffectBatchExecutor(),
            clock=clock,
        )

    async def start(
        self, invocation: DriverInvocation, *, context, cancel
    ) -> DriverResult:
        if context is not invocation.services.context:
            raise ValueError("Runtime context service mismatch")
        input_value = cast(Mapping[str, object], invocation.start.input)
        initial = _messages(input_value.get("messages"))
        tools = _tools(
            input_value.get("capability_snapshot"), invocation.services.tools
        )
        try:
            result = await self._loop.run(
                ReActRunInput(
                    RunId(invocation.run.run_id),
                    RequestId(invocation.run.request_id),
                    tools=tools,
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
                RunState.WAITING,
                {"raw_failures": [{"error_code": str(error.code)}]},
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
        return DriverResult(
            RunState.COMPLETED,
            {"message": result.response.message.content},
        )


def _messages(value: object) -> tuple[Message, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("ReAct input messages are required")
    messages: list[Message] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("ReAct input message must be an object")
        messages.append(Message(MessageRole(str(item["role"])), str(item["content"])))
    return tuple(messages)


def _tools(value: object, executor) -> tuple[ProviderToolSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise TypeError("capability_snapshot must be an object")
    names = value.get("tools", ())
    if not isinstance(names, (list, tuple)) or not all(
        isinstance(name, str) for name in names
    ):
        raise TypeError("capability_snapshot.tools must contain strings")
    return executor.provider_tool_specs(tuple(names))


__all__ = ("ReActDriver",)
