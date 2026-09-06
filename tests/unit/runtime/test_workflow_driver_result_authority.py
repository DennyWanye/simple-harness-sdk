# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.execution.uow import RunState
from simple_harness.runtime.drivers.workflow import WorkflowRuntimeDriver
from simple_harness.runtime.kernel import DriverResult
from simple_harness.workflow.contracts import WorkflowRunStatus
from simple_harness.workflow.execution_ports import (
    StartMode,
    WorkflowRetryWake,
    WorkflowTerminalOutcome,
)
from simple_harness.workflow.runner import WorkflowRunResult


TERMINAL = WorkflowTerminalOutcome(
    "terminal-1", "run-1", "checkpoint-1", "completed", "event-1", (), "a" * 64
)
RETRY = WorkflowRetryWake(
    "run-1", "resume-1", 2, StartMode.PRECREATED, 20.0, "event-2", 8, "b" * 64
)


def test_driver_result_exact_workflow_authority_matrix() -> None:
    terminal = DriverResult(
        RunState.COMPLETED, workflow_terminal=TERMINAL, driver_kind="workflow"
    )
    retry = DriverResult(
        RunState.WAITING, workflow_retry_wake=RETRY, driver_kind="workflow"
    )
    assert terminal.workflow_terminal is TERMINAL
    assert retry.workflow_retry_wake is RETRY

    invalid = (
        (RunState.WAITING, TERMINAL, None, "workflow"),
        (RunState.COMPLETED, None, RETRY, "workflow"),
        (RunState.COMPLETED, TERMINAL, None, "react"),
        (RunState.WAITING, None, RETRY, "react"),
        (RunState.COMPLETED, TERMINAL, RETRY, "workflow"),
    )
    for state, terminal_outcome, retry_wake, driver_kind in invalid:
        with pytest.raises(ValueError):
            DriverResult(
                state,
                workflow_terminal=terminal_outcome,
                workflow_retry_wake=retry_wake,
                driver_kind=driver_kind,
            )


def test_workflow_driver_passes_terminal_and_retry_carriers_unchanged() -> None:
    terminal_result = WorkflowRunResult(
        "run-1", WorkflowRunStatus.COMPLETED, workflow_terminal=TERMINAL
    )
    retry_result = WorkflowRunResult(
        "run-1", WorkflowRunStatus.RETRYABLE, workflow_retry_wake=RETRY
    )

    terminal = WorkflowRuntimeDriver._driver_result(terminal_result)
    retry = WorkflowRuntimeDriver._driver_result(retry_result)

    assert terminal.workflow_terminal is TERMINAL
    assert retry.workflow_retry_wake is RETRY
