# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

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


def test_tool_crash_propagates_without_an_extra_provider_turn() -> None:
    provider = ScriptedProviderCoordinator(
        [response("calling", tool=True), response("must not run")]
    )
    tools = RecordingEffectExecutor(error=RuntimeError("crash"))
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(3, 3, 30, 1000, 2)),
        effects=Batch(tools),
        clock=lambda: 1.0,
    )
    with pytest.raises(RuntimeError, match="crash"):
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
    assert len(provider.calls) == 1
    assert len(tools.calls) == 1
