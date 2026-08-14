# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, EffectId, RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import ExecutionLease
from simple_harness.providers import CancelToken, ProviderRequest
from simple_harness.tools.contracts import ToolCall, ToolContext
from simple_harness.workflow.adapters import (
    WorkflowEffectAdapter,
    WorkflowProviderAdapter,
)
from simple_harness.workflow.execution_ports import WorkflowActivation
from simple_harness.workflow.lease import WorkflowLease


def _activation(run_id: str = "run-1") -> WorkflowActivation:
    """Create test WorkflowActivation with correct parameter types."""
    execution = ExecutionLease(run_id, "runtime.kernel", "owner-1", 4, 30.0)
    return WorkflowActivation(
        execution,
        RunFenceLease(RunId(run_id), epoch=7, owner_id="owner-1", runtime_lease_epoch=4),
        WorkflowLease(run_id, "owner-1", epoch=9, expires_at=30.0, runtime_lease_epoch=4),
    )


class _ProviderCoordinator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def invoke(self, run_id, request, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"run_id": run_id, "request": request, **kwargs})
        return "provider-response"


class _EffectExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        return "effect-result"


def test_workflow_provider_adapter_forwards_exact_activation() -> None:
    """WorkflowProviderAdapter passes activation leases to coordinator."""
    activation = _activation()
    coordinator = _ProviderCoordinator()
    adapter = WorkflowProviderAdapter(coordinator, activation, clock=lambda: 12.0)
    request = ProviderRequest(
        request_id=RequestId("request-1"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
    )

    response = asyncio.run(adapter.invoke(request, cancel=CancelToken()))

    assert response == "provider-response"
    assert len(coordinator.calls) == 1
    call = coordinator.calls[0]
    assert call["run_id"] == RunId("run-1")
    assert call["request"] is request
    assert call["execution_lease"] is activation.execution_lease
    assert call["run_fence"] is activation.run_fence
    assert call["workflow_lease"] is activation.workflow_lease


def test_workflow_provider_adapter_rejects_request_for_another_run() -> None:
    """WorkflowProviderAdapter raises ValueError when run_id mismatches."""
    adapter = WorkflowProviderAdapter(
        _ProviderCoordinator(), _activation(), clock=lambda: 12.0
    )
    request = ProviderRequest(
        request_id=RequestId("request-1"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
    )

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            adapter.invoke_for_run(RunId("run-2"), request, cancel=CancelToken())
        )


def test_workflow_effect_adapter_forwards_exact_activation() -> None:
    """WorkflowEffectAdapter passes activation leases to executor."""
    activation = _activation()
    executor = _EffectExecutor()
    adapter = WorkflowEffectAdapter(executor, activation, clock=lambda: 12.0)
    effect_id = EffectId("effect-1")
    call = ToolCall(CallId("call-1"), "tool-name", {})
    context = ToolContext(RunId("run-1"), RequestId("request-1"), {})

    result = asyncio.run(
        adapter.execute(
            effect_id=effect_id,
            call=call,
            context=context,
            raw_call_id="raw-1",
            turn_ordinal=2,
            call_ordinal=3,
        )
    )

    assert result == "effect-result"
    assert len(executor.calls) == 1
    ex_call = executor.calls[0]
    assert ex_call["effect_id"] == effect_id
    assert ex_call["call"] is call
    assert ex_call["context"] is context
    assert ex_call["execution_lease"] is activation.execution_lease
    assert ex_call["run_fence"] is activation.run_fence
    assert ex_call["workflow_lease"] is activation.workflow_lease
    assert ex_call["raw_call_id"] == "raw-1"
    assert ex_call["turn_ordinal"] == 2
    assert ex_call["call_ordinal"] == 3


def test_workflow_effect_adapter_rejects_context_for_another_run() -> None:
    """WorkflowEffectAdapter raises ValueError when context run_id mismatches."""
    adapter = WorkflowEffectAdapter(
        _EffectExecutor(), _activation(), clock=lambda: 12.0
    )
    call = ToolCall(CallId("call-1"), "tool-name", {})
    context = ToolContext(RunId("run-2"), RequestId("request-1"), {})

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            adapter.execute(
                effect_id=EffectId("effect-1"),
                call=call,
                context=context,
            )
        )
