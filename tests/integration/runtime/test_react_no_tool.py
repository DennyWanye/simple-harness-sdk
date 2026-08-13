# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

from simple_harness.contracts import RequestId, RunId
from simple_harness.providers import CancelToken
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


def test_no_tool_completes_after_one_durable_provider_turn() -> None:
    provider = ScriptedProviderCoordinator([response("done")])
    tools = RecordingEffectExecutor()
    context = MemoryContext()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(3, 3, 30, 1000, 2)),
        effects=Batch(tools),
        clock=lambda: 1.0,
    )
    result = asyncio.run(
        loop.run(
            ReActRunInput(RunId("run-1"), RequestId("request-1"), max_output_tokens=10),
            services=services(provider, tools, context),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert result.response.message.content == "done"
    assert result.termination.turns == 1
    assert len(provider.calls) == 1
    assert tools.calls == []
