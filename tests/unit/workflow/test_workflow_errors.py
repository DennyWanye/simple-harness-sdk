# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.workflow.errors import (
    ERROR_DISPOSITIONS,
    AsyncOnlyWorkflowError,
    ErrorDisposition,
    InvalidStatePatch,
    LeaseLostError,
    StateMergeConflict,
    UnsupportedDeltaChannelError,
    WorkflowContractError,
    WorkflowDefinitionError,
    WorkflowDependencyUnavailable,
    WorkflowErrorCode,
    WorkflowNodeError,
)


def test_workflow_error_vocabulary_has_exact_codes_and_dispositions() -> None:
    assert [code.value for code in WorkflowErrorCode] == [
        "retryable_provider",
        "retryable_tool",
        "invalid_state",
        "checkpoint_corrupt",
        "permission_denied",
        "cancelled",
        "lease_lost",
        "effect_uncertain",
        "permanent",
    ]
    assert set(ERROR_DISPOSITIONS) == set(WorkflowErrorCode)
    assert all(isinstance(value, ErrorDisposition) for value in ERROR_DISPOSITIONS.values())
    assert ERROR_DISPOSITIONS[WorkflowErrorCode.RETRYABLE_PROVIDER].retryable is True
    assert ERROR_DISPOSITIONS[WorkflowErrorCode.PERMANENT].retryable is False


@pytest.mark.parametrize(
    ("error_type", "base_type"),
    [
        (WorkflowDefinitionError, WorkflowContractError),
        (InvalidStatePatch, WorkflowContractError),
        (StateMergeConflict, WorkflowContractError),
        (WorkflowDependencyUnavailable, RuntimeError),
        (AsyncOnlyWorkflowError, RuntimeError),
        (UnsupportedDeltaChannelError, RuntimeError),
        (LeaseLostError, RuntimeError),
    ],
)
def test_workflow_error_inheritance_is_stable(
    error_type: type[BaseException], base_type: type[BaseException]
) -> None:
    assert issubclass(error_type, base_type)


def test_contract_error_preserves_code_details_and_message() -> None:
    error = InvalidStatePatch("invalid_patch", "patch is invalid", details={"node": "n1"})
    assert str(error) == "patch is invalid"
    assert error.code == "invalid_patch"
    assert error.details == {"node": "n1"}


def test_node_error_default_disposition_and_envelope_are_exact() -> None:
    error = WorkflowNodeError(
        code=WorkflowErrorCode.RETRYABLE_TOOL,
        message_ref="workflow_node:tool:retryable_tool",
        node_id="tool",
    )
    assert error.to_envelope() == {
        "schema_version": 1,
        "code": "retryable_tool",
        "message_ref": "workflow_node:tool:retryable_tool",
        "retryable": True,
        "node_id": "tool",
    }


def test_node_error_accepts_string_code_and_explicit_retry_override() -> None:
    error = WorkflowNodeError(
        code="permanent", message_ref="workflow_node:n1:permanent", retryable=True
    )
    assert error.code is WorkflowErrorCode.PERMANENT
    assert error.to_envelope() == {
        "schema_version": 1,
        "code": "permanent",
        "message_ref": "workflow_node:n1:permanent",
        "retryable": True,
    }
