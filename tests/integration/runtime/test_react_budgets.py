# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetSnapshot
from simple_harness.providers import CancelToken, ProviderResponse, ProviderToolCall
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    ReActLoop,
    ReActRunInput,
)
from simple_harness.runtime.termination import (
    TerminationBudgetExceeded,
    TerminationLimits,
    TerminationReason,
)

from .react_fakes import (
    FENCE,
    LEASE,
    Batch,
    MemoryContext,
    RecordingEffectExecutor,
    ScriptedProviderCoordinator,
    response,
    services,
)


def test_cost_gate_runs_before_provider_dispatch() -> None:
    provider = ScriptedProviderCoordinator(
        [response("never")], BudgetSnapshot(committed_micros=100)
    )
    tools = RecordingEffectExecutor()
    context = MemoryContext()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(2, 2, 30, 100, 1)),
        effects=Batch(tools),
        clock=lambda: 1.0,
    )
    with pytest.raises(TerminationBudgetExceeded) as caught:
        asyncio.run(
            loop.run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=services(provider, tools, context),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert caught.value.reason is TerminationReason.COST
    assert provider.calls == []


def test_oversized_tool_batch_is_rejected_before_any_effect_is_prepared() -> None:
    batch = ProviderResponse(
        request_id=RequestId("fixture"),
        message=Message(role=MessageRole.ASSISTANT, content="batch"),
        tool_calls=(
            ProviderToolCall(CallId("call-1"), "a", {}),
            ProviderToolCall(CallId("call-2"), "b", {}),
        ),
        model="model-1",
    )
    provider = ScriptedProviderCoordinator([batch])
    tools = RecordingEffectExecutor()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(2, 1, 30, 100, 1)),
        effects=Batch(tools),
        clock=lambda: 1.0,
    )
    with pytest.raises(TerminationBudgetExceeded) as caught:
        asyncio.run(
            loop.run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=services(provider, tools, MemoryContext()),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert caught.value.reason is TerminationReason.MAX_TOOL_CALLS
    assert tools.calls == []
