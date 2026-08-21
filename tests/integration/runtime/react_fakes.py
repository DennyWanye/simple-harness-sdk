# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field

from simple_harness.contracts import CallId, RequestId, RunId, freeze_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import BudgetSnapshot
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import ExecutionLease, WorkflowCheckpoint
from simple_harness.providers import (
    ProviderReconciliationObservation,
    ProviderReconciliationState,
    ProviderResponse,
    ProviderToolCall,
)
from simple_harness.runtime.context import ContextSnapshot
from simple_harness.runtime.kernel import RuntimeServices
from simple_harness.tools import ToolResult
from simple_harness.tools.executor import EffectExecution

LEASE = ExecutionLease("run-1", "runtime.kernel", "worker-1", 1, 100.0)
FENCE = RunFenceLease(RunId("run-1"), 1, "worker-1", 1)


class MemoryContext:
    def __init__(self) -> None:
        self.messages = [Message(role=MessageRole.USER, content="hello")]
        self.revision = 1

    def load(self, run_id: RunId):
        del run_id
        return ContextSnapshot(self.revision, tuple(self.messages))

    def append(self, run_id, lease, expected_revision, append_id, entries):
        del run_id, lease, append_id
        assert expected_revision == self.revision
        self.messages.extend(entries)
        self.revision += 1
        return ContextSnapshot(self.revision, tuple(self.messages))


class ScriptedProviderCoordinator:
    def __init__(self, responses, budget: BudgetSnapshot | None = None) -> None:
        self.responses = list(responses)
        self.calls = []
        self.budget = budget or BudgetSnapshot()

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot:
        del run_id
        return self.budget

    async def invoke(self, run_id, request, *, cancel, execution_lease):
        self.calls.append((run_id, request, cancel, execution_lease))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return ProviderResponse(
            request_id=request.request_id,
            message=response.message,
            tool_calls=response.tool_calls,
            usage=response.usage,
            model=response.model,
            finish_reason=response.finish_reason,
        )


class RecordingEffectExecutor:
    def __init__(self, *, error=None) -> None:
        self.calls = []
        self.error = error

    async def execute(self, **values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        call = values["call"]
        return EffectExecution(
            effect=None,
            result=ToolResult.succeeded(call.call_id, {"answer": 42}),
        )


class Checkpoint:
    def __init__(self) -> None:
        self.value = None

    def read_react_checkpoint(self, run_id):
        del run_id
        return self.value

    def cas_react_checkpoint(
        self,
        *,
        run_id,
        lease,
        expected_version,
        checkpoint,
        checkpoint_hash,
        now,
        fault=None,
    ):
        del fault, now
        current = None if self.value is None else self.value.version
        assert expected_version == current
        version = 1 if current is None else current + 1
        self.value = WorkflowCheckpoint(
            run_id,
            "react.termination.v1",
            freeze_json(checkpoint),
            checkpoint_hash,
            lease.epoch,
            version,
        )
        return self.value


class Batch:
    def __init__(self, tools) -> None:
        self.tools = tools

    async def execute(self, calls, **values):
        return [await self.tools.execute(call=call, **values) for call in calls]


class StillUnknownProviderReconciliation:
    async def observe(self, invocation):
        return ProviderReconciliationObservation(
            ProviderReconciliationState.STILL_UNKNOWN,
            f"fixture:provider-still-unknown:{invocation.invocation_id}",
        )


@dataclass
class MutableBudget:
    snapshot: BudgetSnapshot = field(default_factory=BudgetSnapshot)


def services(provider, tools, context, *, checkpoint=None):
    noop = object()
    return RuntimeServices(
        provider=provider,
        tools=tools,
        authorization=noop,
        context=context,
        delivery=noop,
        tool_reconciliation=noop,
        reconciliation=noop,
        provider_reconciliation=StillUnknownProviderReconciliation(),
        react_checkpoint=checkpoint or Checkpoint(),
    )


def response(content: str, *, tool: bool = False) -> ProviderResponse:
    return ProviderResponse(
        request_id=RequestId("fixture-response"),
        message=Message(role=MessageRole.ASSISTANT, content=content),
        tool_calls=(ProviderToolCall(CallId("call-1"), "calculator", {"x": 1}),) if tool else (),
        model="model-1",
        finish_reason="tool_calls" if tool else "stop",
    )
