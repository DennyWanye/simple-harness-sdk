# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

from simple_harness.contracts import RequestId, RunId
from simple_harness.providers import CancelToken, ProviderToolSpec
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    ReActLoop,
    ReActRunInput,
)
from simple_harness.runtime.termination import TerminationLimits

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


def test_tool_roundtrip_uses_effect_executor_then_same_driver() -> None:
    provider = ScriptedProviderCoordinator(
        [response("calling", tool=True), response("42")]
    )
    tools = RecordingEffectExecutor()
    context = MemoryContext()
    result = asyncio.run(
        ReActLoop(
            collaborator=AgentLoopCollaborator(limits=TerminationLimits(4, 4, 30, 1000, 2)),
            effects=Batch(tools),
            clock=lambda: 1.0,
        ).run(
            ReActRunInput(
                RunId("run-1"),
                RequestId("request-1"),
                tools=(
                    ProviderToolSpec("calculator", "Calculate", {"type": "object"}),
                ),
                max_output_tokens=10,
            ),
            services=services(provider, tools, context),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert result.response.message.content == "42"
    assert (result.termination.turns, result.termination.tool_calls) == (2, 1)
    assert len(provider.calls) == 2
    assert len(tools.calls) == 1
    assert provider.calls[1][1].messages[-1].role.value == "tool"
