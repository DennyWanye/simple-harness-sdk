# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from simple_harness.contracts import JsonValue, RequestId, RunId
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
    Checkpoint,
    MemoryContext,
    RecordingEffectExecutor,
    ScriptedProviderCoordinator,
    response,
    services,
)


class Exposure:
    def __init__(self) -> None:
        self.activated = False
        self.restored: list[JsonValue | None] = []

    def restore(self, run_id: RunId, checkpoint: JsonValue | None) -> None:
        assert run_id == RunId("run-1")
        self.restored.append(checkpoint)
        if isinstance(checkpoint, dict):
            self.activated = checkpoint.get("activated") is True

    def provider_specs(self, run_id: RunId) -> tuple[ProviderToolSpec, ...]:
        assert run_id == RunId("run-1")
        direct = (ProviderToolSpec("calculator", "Calculate", {"type": "object"}),)
        if not self.activated:
            return direct
        return (*direct, ProviderToolSpec("late_tool", "Late", {"type": "object"}))

    def observe_tool_result(
        self, run_id: RunId, tool_name: str, result: Mapping[str, object]
    ) -> None:
        assert run_id == RunId("run-1")
        if tool_name == "calculator" and result.get("answer") == 42:
            self.activated = True

    def checkpoint(self, run_id: RunId) -> JsonValue:
        assert run_id == RunId("run-1")
        return {"activated": self.activated}


def _loop() -> ReActLoop:
    return ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(4, 4, 30, 1000, 2)),
        effects=Batch(RecordingEffectExecutor()),
        clock=lambda: 1.0,
    )


def test_ready_attempt_reprojects_tools_after_durable_tool_result() -> None:
    provider = ScriptedProviderCoordinator([response("calling", tool=True), response("done")])
    effects = RecordingEffectExecutor()
    exposure = Exposure()
    result = asyncio.run(
        ReActLoop(
            collaborator=AgentLoopCollaborator(
                limits=TerminationLimits(4, 4, 30, 1000, 2)
            ),
            effects=Batch(effects),
            clock=lambda: 1.0,
        ).run(
            ReActRunInput(
                RunId("run-1"), RequestId("request-1"), tool_exposure=exposure
            ),
            services=services(provider, effects, MemoryContext()),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert result.response.message.content == "done"
    assert [[tool.name for tool in call[1].tools] for call in provider.calls] == [
        ["calculator"],
        ["calculator", "late_tool"],
    ]
    assert result.termination.tool_exposure_state == {"activated": True}


def test_provider_reserved_reopen_replays_frozen_tool_projection() -> None:
    checkpoint = Checkpoint()
    context = MemoryContext()
    effects = RecordingEffectExecutor()
    exposure = Exposure()
    first = ScriptedProviderCoordinator([RuntimeError("provider crashed")])
    with pytest.raises(RuntimeError, match="provider crashed"):
        asyncio.run(
            _loop().run(
                ReActRunInput(
                    RunId("run-1"), RequestId("request-1"), tool_exposure=exposure
                ),
                services=services(first, effects, context, checkpoint=checkpoint),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )

    exposure.activated = True
    reopened = ScriptedProviderCoordinator([response("replayed")])
    asyncio.run(
        _loop().run(
            ReActRunInput(
                RunId("run-1"), RequestId("request-1"), tool_exposure=exposure
            ),
            services=services(reopened, effects, context, checkpoint=checkpoint),
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert [tool.name for tool in reopened.calls[0][1].tools] == ["calculator"]
