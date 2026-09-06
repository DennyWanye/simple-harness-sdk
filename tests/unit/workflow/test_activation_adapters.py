# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, EffectId, RequestId, RunId
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
    execution = ExecutionLease(run_id, "runtime.kernel", "owner-1", 4, 30.0)
    return WorkflowActivation(
        execution,
        RunFenceLease(RunId(run_id), "owner-1", 4, 7),
        WorkflowLease(run_id, "native", "owner-1", 9, 4, 30.0),
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
    activation = _activation()
    coordinator = _ProviderCoordinator()
    adapter = WorkflowProviderAdapter(coordinator, activation, clock=lambda: 12.0)
    request = ProviderRequest(RequestId("request-1"), (), ())

    response = asyncio.run(adapter.invoke(request, cancel=CancelToken()))

    assert response == "provider-response"
    assert coordinator.calls == [
        {
            "run_id": RunId("run-1"),
            "request": request,
            "cancel": coordinator.calls[0]["cancel"],
            "execution_lease": activation.execution_lease,
            "run_fence": activation.run_fence,
            "workflow_lease": activation.workflow_lease,
        }
    ]


def test_workflow_provider_adapter_rejects_request_for_another_run() -> None:
    adapter = WorkflowProviderAdapter(
        _ProviderCoordinator(), _activation(), clock=lambda: 12.0
    )
    request = ProviderRequest(RequestId("request-1"), (), ())

    with pytest.raises(ValueError, match="another Run"):
        asyncio.run(
            adapter.invoke_for_run(
                RunId("run-2"), request, cancel=CancelToken()
            )
        )


def test_workflow_effect_adapter_forwards_exact_activation() -> None:
    activation = _activation()
    executor = _EffectExecutor()
    adapter = WorkflowEffectAdapter(executor, activation, clock=lambda: 12.0)
    call = ToolCall(CallId("call-1"), "demo", {})
    context = ToolContext(
        RunId("run-1"), RequestId("request-1"), CancelToken(), {}
    )

    result = asyncio.run(
        adapter.execute(
            effect_id=EffectId("effect-1"),
            call=call,
            context=context,
            raw_call_id="raw-1",
            turn_ordinal=2,
            call_ordinal=3,
        )
    )

    assert result == "effect-result"
    assert executor.calls == [
        {
            "effect_id": EffectId("effect-1"),
            "call": call,
            "context": context,
            "execution_lease": activation.execution_lease,
            "run_fence": activation.run_fence,
            "workflow_lease": activation.workflow_lease,
            "raw_call_id": "raw-1",
            "turn_ordinal": 2,
            "call_ordinal": 3,
        }
    ]


def test_workflow_effect_adapter_rejects_context_for_another_run() -> None:
    adapter = WorkflowEffectAdapter(_EffectExecutor(), _activation(), clock=lambda: 12.0)
    context = ToolContext(
        RunId("run-2"), RequestId("request-1"), CancelToken(), {}
    )

    with pytest.raises(ValueError, match="another Run"):
        asyncio.run(
            adapter.execute(
                effect_id=EffectId("effect-1"),
                call=ToolCall(CallId("call-1"), "demo", {}),
                context=context,
            )
        )
