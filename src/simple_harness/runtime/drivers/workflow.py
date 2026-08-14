# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""SDK-owned bridge from RunKernel to the durable Workflow Runner."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from simple_harness.contracts import JsonValue, canonical_json, thaw_json
from simple_harness.execution.uow import RunState
from simple_harness.providers import CancelToken
from simple_harness.workflow.contracts import WorkflowContext, WorkflowRunStatus
from simple_harness.workflow.execution_ports import PrecreatedStartAction

if TYPE_CHECKING:
    from simple_harness.workflow.runner import WorkflowRunner

from ..context import ContextPort
from ..kernel import (
    DriverCancellationRecovery,
    DriverCancelOutcome,
    DriverInvocation,
    DriverResult,
)

WORKFLOW_DRIVER_KIND = "workflow"
WORKFLOW_DRIVER_IMPLEMENTATION_FINGERPRINT = hashlib.sha256(
    canonical_json(
        {
            "protocol": "simple-harness-workflow-runtime-driver-v1",
            "driver_kind": WORKFLOW_DRIVER_KIND,
            "start": "lifecycle.ensure_and_bind_precreated_start+runner.run_precreated",
            "cancel": "runner.request_cancel_precreated",
            "authority": "execution-lease+run-fence+workflow-projection",
        }
    ).encode()
).hexdigest()

_FACTORY_TOKEN = object()


class WorkflowRuntimeDriver:
    """Exact, non-subclassable official Workflow driver implementation."""

    driver_kind = WORKFLOW_DRIVER_KIND
    implementation_fingerprint = WORKFLOW_DRIVER_IMPLEMENTATION_FINGERPRINT

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("WorkflowRuntimeDriver cannot be subclassed")

    def __init__(self, runner: WorkflowRunner, *, _token: object) -> None:
        from simple_harness.workflow.runner import WorkflowRunner

        if _token is not _FACTORY_TOKEN:
            raise TypeError("WorkflowRuntimeDriver must be built by the SDK factory")
        if not isinstance(runner, WorkflowRunner):
            raise TypeError("workflow runner identity is invalid")
        self._runner = runner

    async def start(
        self,
        invocation: DriverInvocation,
        *,
        context: ContextPort,
        cancel: CancelToken,
    ) -> DriverResult:
        del context
        if cancel.is_cancelled:
            return DriverResult(
                RunState.WAITING,
                {"status": "cancel_requested"},
            )
        request = invocation.start.workflow_admission
        recovery_work = invocation.workflow_recovery_work
        if recovery_work is not None:
            if request is None:
                raise RuntimeError(
                    "workflow driver recovery requires durable admission"
                )
            recovered = await self._runner.recover_precreated(
                invocation.run.run_id,
                recovery_work=recovery_work,
                execution_lease=invocation.execution_lease,
                run_fence=invocation.run_fence,
                context=WorkflowContext(
                    trace_id=None,
                    request_id=invocation.run.request_id,
                ),
            )
            return self._driver_result(recovered)
        dispatch_claim = invocation.workflow_start_dispatch
        if request is None or dispatch_claim is None:
            raise RuntimeError(
                "workflow driver start requires durable admission and dispatch claim"
            )
        dispatch = await self._runner.start_precreated(
            request=request,
            execution_lease=invocation.execution_lease,
            run_fence=invocation.run_fence,
            dispatch_claim=dispatch_claim,
        )
        if dispatch.action is PrecreatedStartAction.SETTLED:
            outcome = (
                None
                if dispatch.serialized_outcome is None
                else thaw_json(dispatch.serialized_outcome)
            )
            if not isinstance(outcome, dict):
                raise RuntimeError("settled workflow start outcome is invalid")
            payload = cast(dict[str, JsonValue], outcome)
            status = payload.get("status")
            if not isinstance(status, str):
                raise RuntimeError("settled workflow start outcome is invalid")
            state = {
                "completed": RunState.COMPLETED,
                "failed": RunState.FAILED,
                "cancelled": RunState.COMPLETED,
            }.get(status)
            if state is None:
                raise RuntimeError("settled workflow start outcome is invalid")
            return DriverResult(state, cast(Mapping[str, JsonValue], payload))
        if dispatch.activation is None:
            raise RuntimeError("active workflow dispatch lacks activation")
        result = await self._runner.run_precreated(
            invocation.run.run_id,
            context=WorkflowContext(
                trace_id=None,
                request_id=invocation.run.request_id,
            ),
            activation=dispatch.activation,
        )
        return self._driver_result(result)

    @staticmethod
    def _driver_result(result) -> DriverResult:  # type: ignore[no-untyped-def]
        state = {
            WorkflowRunStatus.WAITING: RunState.WAITING,
            WorkflowRunStatus.COMPLETED: RunState.COMPLETED,
            WorkflowRunStatus.FAILED: RunState.FAILED,
            WorkflowRunStatus.BLOCKED: RunState.WAITING,
            WorkflowRunStatus.RETRYABLE: RunState.WAITING,
        }.get(result.status)
        if state is None:
            raise RuntimeError(
                f"workflow driver returned unsupported status: {result.status.value}"
            )
        output = result.output
        payload: Mapping[str, JsonValue]
        if isinstance(output, Mapping):
            payload = cast(Mapping[str, JsonValue], dict(output))
        else:
            payload = {"status": result.status.value}
        return DriverResult(state, payload)

    async def cancel(
        self,
        run,
        start_snapshot,
        *,
        reason: str,
        now: float,
        recovery: DriverCancellationRecovery,
    ) -> DriverCancelOutcome:
        del start_snapshot, now
        result = await self._runner.request_cancel_precreated(
            run.run_id,
            reason=reason,
            execution_lease=recovery.execution_lease,
            run_fence=recovery.run_fence,
        )
        output = result.output if isinstance(result.output, Mapping) else {}
        cancel_id = output.get("cancel_id")
        generation = output.get("generation")
        blocker_ids = output.get("blocker_ids", [])
        if (
            not isinstance(cancel_id, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not isinstance(blocker_ids, list)
            or not all(isinstance(item, str) for item in blocker_ids)
        ):
            raise RuntimeError("workflow cancel result lacks durable identity")
        return DriverCancelOutcome(
            cancel_id,
            generation,
            result.status.value,
            tuple(cast(list[str], blocker_ids)),
            result.status is WorkflowRunStatus.CANCELLED,
        )


def build_workflow_runtime_driver(runner: WorkflowRunner) -> WorkflowRuntimeDriver:
    """Build the only implementation accepted for the reserved driver key."""

    return WorkflowRuntimeDriver(runner, _token=_FACTORY_TOKEN)


__all__ = (
    "WORKFLOW_DRIVER_IMPLEMENTATION_FINGERPRINT",
    "WORKFLOW_DRIVER_KIND",
    "WorkflowRuntimeDriver",
    "build_workflow_runtime_driver",
)
