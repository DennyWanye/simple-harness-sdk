# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Activation-bound Provider and Effect adapters for Workflow nodes."""

from __future__ import annotations

import time
from collections.abc import Callable

from simple_harness.contracts import EffectId, RunId
from simple_harness.execution.dispatch import ProviderInvocationCoordinator
from simple_harness.providers import CancelToken, ProviderRequest, ProviderResponse
from simple_harness.tools.contracts import ToolCall, ToolContext
from simple_harness.tools.executor import EffectExecution, EffectExecutor

from .execution_ports import WorkflowActivation


class WorkflowProviderAdapter:
    """Bind every Provider invocation to one immutable Workflow activation.

    Ensures Provider calls carry the exact execution/run/workflow lease
    state captured at activation time, preventing lease drift.
    """

    __slots__ = ("_activation", "_clock", "_coordinator")

    def __init__(
        self,
        coordinator: ProviderInvocationCoordinator,
        activation: WorkflowActivation,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._coordinator = coordinator
        self._activation = activation
        self._clock = clock

    async def invoke(
        self, request: ProviderRequest, *, cancel: CancelToken
    ) -> ProviderResponse:
        """Invoke Provider with activation's immutable lease state."""
        return await self.invoke_for_run(
            RunId(self._activation.execution_lease.run_id), request, cancel=cancel
        )

    async def invoke_for_run(
        self,
        run_id: RunId,
        request: ProviderRequest,
        *,
        cancel: CancelToken,
    ) -> ProviderResponse:
        """Invoke Provider for explicit run_id, validating against activation."""
        if run_id.value != self._activation.execution_lease.run_id:
            raise ValueError(
                f"Workflow Provider request run_id {run_id.value} does not match "
                f"activation run_id {self._activation.execution_lease.run_id}"
            )
        self._clock()
        return await self._coordinator.invoke(
            run_id,
            request,
            cancel=cancel,
            execution_lease=self._activation.execution_lease,
            run_fence=self._activation.run_fence,
            workflow_lease=self._activation.workflow_lease,
        )


class WorkflowEffectAdapter:
    """Bind every Tool execution to one immutable Workflow activation.

    Ensures Effect calls carry the exact execution/run/workflow lease
    state captured at activation time, preventing lease drift.
    """

    __slots__ = ("_activation", "_clock", "_executor")

    def __init__(
        self,
        executor: EffectExecutor,
        activation: WorkflowActivation,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._executor = executor
        self._activation = activation
        self._clock = clock

    async def execute(
        self,
        *,
        effect_id: EffectId,
        call: ToolCall,
        context: ToolContext,
        raw_call_id: str | None = None,
        turn_ordinal: int = 0,
        call_ordinal: int = 0,
    ) -> EffectExecution:
        """Execute Tool with activation's immutable lease state."""
        if context.run_id.value != self._activation.execution_lease.run_id:
            raise ValueError(
                f"Workflow Tool context run_id {context.run_id.value} does not match "
                f"activation run_id {self._activation.execution_lease.run_id}"
            )
        self._clock()
        return await self._executor.execute(
            effect_id=effect_id,
            call=call,
            context=context,
            execution_lease=self._activation.execution_lease,
            run_fence=self._activation.run_fence,
            workflow_lease=self._activation.workflow_lease,
            raw_call_id=raw_call_id,
            turn_ordinal=turn_ordinal,
            call_ordinal=call_ordinal,
        )


__all__ = ("WorkflowEffectAdapter", "WorkflowProviderAdapter")
