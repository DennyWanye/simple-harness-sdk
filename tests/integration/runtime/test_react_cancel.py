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


def test_pre_cancel_performs_no_provider_or_tool_side_effect() -> None:
    provider = ScriptedProviderCoordinator([response("never")])
    tools = RecordingEffectExecutor()
    token = CancelToken()
    token.cancel()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(2, 2, 30, 100, 1)),
        effects=Batch(tools),
        clock=lambda: 1.0,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            loop.run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=services(provider, tools, MemoryContext()),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=token,
                initial_messages=(),
            )
        )
    assert provider.calls == [] and tools.calls == []
