# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.effects import (
    EffectRecord,
    EffectState,
    effect_request_hash,
)
from simple_harness.providers import CancelToken, ProviderResponse, ProviderToolCall
from simple_harness.runtime.context import ContextSnapshot
from simple_harness.runtime.drivers.react_loop import (
    AgentLoopCollaborator,
    EffectBatchExecutor,
    ReActLoop,
    ReActRunInput,
)
from simple_harness.runtime.termination import TerminationLimits
from simple_harness.tools import ToolResult
from simple_harness.tools.executor import EffectExecution

from .react_fakes import (
    FENCE,
    LEASE,
    Checkpoint,
    MemoryContext,
    ScriptedProviderCoordinator,
    services,
)


class CrashAfterToolBatchCheckpoint(Checkpoint):
    def __init__(self) -> None:
        super().__init__()
        self.crash = True

    def cas_react_checkpoint(self, **values):
        stored = super().cas_react_checkpoint(**values)
        if self.crash and values["checkpoint"]["phase"] == "tool_batch_reserved":
            self.crash = False
            raise RuntimeError("crash-after-tool-batch-checkpoint")
        return stored


class CrashAtDurableWrite(Checkpoint):
    def __init__(self, phase: str, *, progress: int | None = None) -> None:
        super().__init__()
        self.phase = phase
        self.progress = progress
        self.crash = True

    def cas_react_checkpoint(self, **values):
        stored = super().cas_react_checkpoint(**values)
        checkpoint = values["checkpoint"]
        if (
            self.crash
            and checkpoint["phase"] == self.phase
            and (self.progress is None or checkpoint["tool_result_progress"] == self.progress)
        ):
            self.crash = False
            raise RuntimeError(f"crash-after-{self.phase}-{self.progress}")
        return stored


class ReceiptContext(MemoryContext):
    def __init__(self, *, crash_suffix: str | None = None) -> None:
        super().__init__()
        self.receipts: dict[str, ContextSnapshot] = {}
        self.crash_suffix = crash_suffix

    def append(self, run_id, lease, expected_revision, append_id, entries):
        if append_id in self.receipts:
            return self.receipts[append_id]
        snapshot = super().append(run_id, lease, expected_revision, append_id, entries)
        self.receipts[append_id] = snapshot
        if self.crash_suffix is not None and append_id.endswith(self.crash_suffix):
            self.crash_suffix = None
            raise RuntimeError("crash-after-context-append")
        return snapshot


class LedgerFirstProvider(ScriptedProviderCoordinator):
    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.completed = {}
        self.physical_calls = 0

    async def invoke(self, run_id, request, *, cancel, execution_lease):
        existing = self.completed.get(request.request_id.value)
        if existing is not None:
            self.calls.append((run_id, request, cancel, execution_lease))
            return existing
        self.physical_calls += 1
        response = await super().invoke(
            run_id, request, cancel=cancel, execution_lease=execution_lease
        )
        self.completed[request.request_id.value] = response
        return response


class DurableEffects:
    def __init__(self) -> None:
        self.records: dict[str, EffectRecord] = {}
        self.calls: list[dict[str, object]] = []

    async def execute(self, **values):
        self.calls.append(values)
        effect_id = values["effect_id"]
        call = values["call"]
        existing = self.records.get(effect_id.value)
        if existing is not None:
            assert existing.result is not None
            return EffectExecution(existing, existing.result)
        result = ToolResult.succeeded(call.call_id, {"ok": True})
        record = EffectRecord(
            effect_id=effect_id,
            run_id=values["context"].run_id,
            call_id=call.call_id,
            tool_name=call.name,
            request_hash=effect_request_hash(tool_name=call.name, arguments=dict(call.arguments)),
            arguments=call.arguments,
            state=EffectState.SUCCEEDED,
            version=2,
            fence_epoch=values["run_fence"].epoch,
            authorization_receipt_ref="auth:h13",
            handoff_receipt_ref="handoff:h13",
            evidence_ref="result:h13",
            result=result,
            raw_call_id=values["raw_call_id"],
            turn_ordinal=values["turn_ordinal"],
            call_ordinal=values["call_ordinal"],
            handoff_attempt=1,
        )
        self.records[effect_id.value] = record
        return EffectExecution(record, result)


def _tool_response(content: str) -> ProviderResponse:
    return ProviderResponse(
        RequestId("fixture"),
        Message(MessageRole.ASSISTANT, content),
        (ProviderToolCall(CallId("same-raw-call"), "calculator", {"x": 1}),),
    )


def test_tool_batch_checkpoint_reopen_never_replays_provider_and_raw_ids_are_turn_scoped() -> None:
    provider = ScriptedProviderCoordinator(
        [
            _tool_response("turn-1"),
            _tool_response("turn-2"),
            ProviderResponse(RequestId("fixture"), Message(MessageRole.ASSISTANT, "done")),
        ]
    )
    effects = DurableEffects()
    context = MemoryContext()
    checkpoint = CrashAfterToolBatchCheckpoint()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(6, 6, 30, 1000, 3)),
        effects=EffectBatchExecutor(),
        clock=lambda: 1.0,
    )
    runtime_services = services(provider, effects, context, checkpoint=checkpoint)
    value = ReActRunInput(RunId("run-1"), RequestId("request-1"))
    with pytest.raises(RuntimeError, match="crash-after-tool-batch-checkpoint"):
        asyncio.run(
            loop.run(
                value,
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert len(provider.calls) == 1 and effects.calls == []

    result = asyncio.run(
        loop.run(
            value,
            services=runtime_services,
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert result.response.message.content == "done"
    assert len(provider.calls) == 3
    assert len(effects.records) == 2
    records = tuple(effects.records.values())
    assert records[0].raw_call_id == records[1].raw_call_id == "same-raw-call"
    assert records[0].effect_id != records[1].effect_id
    tool_messages = [m for m in context.messages if m.role is MessageRole.TOOL]
    assert [m.call_id for m in tool_messages] == [
        CallId("same-raw-call"),
        CallId("same-raw-call"),
    ]


@pytest.mark.parametrize(
    ("checkpoint", "context_crash"),
    [
        (CrashAtDurableWrite("provider_reserved"), None),
        (CrashAtDurableWrite("tool_batch_reserved"), None),
        (CrashAtDurableWrite("tool_batch_reserved", progress=1), None),
        (Checkpoint(), ":assistant"),
        (Checkpoint(), ":context"),
    ],
)
def test_each_react_write_phase_reopens_without_duplicate_physical_effect(
    checkpoint, context_crash
) -> None:
    provider = LedgerFirstProvider(
        [
            _tool_response("turn-1"),
            ProviderResponse(RequestId("fixture"), Message(MessageRole.ASSISTANT, "done")),
        ]
    )
    effects = DurableEffects()
    context = ReceiptContext(crash_suffix=context_crash)
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(4, 4, 30, 1000, 3)),
        effects=EffectBatchExecutor(),
        clock=lambda: 1.0,
    )
    runtime_services = services(provider, effects, context, checkpoint=checkpoint)
    value = ReActRunInput(RunId("run-1"), RequestId("request-1"))
    with pytest.raises(RuntimeError, match="crash-after"):
        asyncio.run(
            loop.run(
                value,
                services=runtime_services,
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    result = asyncio.run(
        loop.run(
            value,
            services=runtime_services,
            execution_lease=LEASE,
            run_fence=FENCE,
            cancel=CancelToken(),
            initial_messages=(),
        )
    )
    assert result.response.message.content == "done"
    assert provider.physical_calls == 2
    assert len(effects.records) == 1
    assert len([m for m in context.messages if m.role is MessageRole.TOOL]) == 1


def test_duplicate_raw_call_id_within_one_provider_turn_is_rejected() -> None:
    duplicate = ProviderResponse(
        RequestId("fixture"),
        Message(MessageRole.ASSISTANT, "bad"),
        (
            ProviderToolCall(CallId("duplicate"), "calculator", {"x": 1}),
            ProviderToolCall(CallId("duplicate"), "calculator", {"x": 2}),
        ),
    )
    provider = ScriptedProviderCoordinator([duplicate])
    effects = DurableEffects()
    loop = ReActLoop(
        collaborator=AgentLoopCollaborator(limits=TerminationLimits(2, 2, 30, 1000, 3)),
        effects=EffectBatchExecutor(),
        clock=lambda: 1.0,
    )
    with pytest.raises(RuntimeError, match="duplicate raw Provider call ID"):
        asyncio.run(
            loop.run(
                ReActRunInput(RunId("run-1"), RequestId("request-1")),
                services=services(provider, effects, ReceiptContext()),
                execution_lease=LEASE,
                run_fence=FENCE,
                cancel=CancelToken(),
                initial_messages=(),
            )
        )
    assert effects.calls == []
