# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for personal_v1 workflow execution with mock runtime port.

Validates:
- Execute node handler invocation
- Selection deserialization from state
- Runtime port interaction
- Input/output mapping
- Error handling (missing port, execution failures)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from simple_harness.workflow.contracts import WorkflowContext
from simple_harness.workflows.personal_v1.definition import (
    _execute_handler,
    create_initial_state,
)
from simple_harness.workflows.personal_v1.selection import (
    PersonalWorkflowSelectionV1,
    personal_workflow_query_hash,
)


def _make_test_selection() -> PersonalWorkflowSelectionV1:
    """Create test selection with simple graph."""
    return PersonalWorkflowSelectionV1.issue(
        owner_key="test-owner",
        pack_id="test-pack",
        version="1.0.0",
        manifest_hash="a" * 64,
        binding_generation=1,
        graph={
            "schema_version": 1,
            "name": "test-workflow",
            "description": "Test workflow",
            "entry_node": "start",
            "nodes": [
                {
                    "id": "start",
                    "type": "output",
                    "bindings": {},
                    "config": {},
                }
            ],
            "outputs": {"/result": "/nodes/start"},
            "max_steps": 1,
        },
        graph_hash="b" * 64,
        query_hash=personal_workflow_query_hash("test query"),
        run_catalog_content_stamp="stamp-123",
        lease_entries=[{"lease_id": "lease-1"}],
        effect_topology={"policy": "read_only"},
        tool_bindings={},
    )


@pytest.mark.anyio
async def test_execute_handler_calls_runtime_port() -> None:
    """Test execute handler invokes runtime port with correct arguments."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-123",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={"input_key": "input_value"},
    )

    # Mock runtime port
    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(return_value={"output_key": "output_value"})

    execution_identity = object()
    context = WorkflowContext(
        ports={"personal_workflow_runtime": mock_runtime},
        identity=execution_identity,  # type: ignore[arg-type]
    )

    # Execute handler
    patch = await _execute_handler(state, context)

    # Verify runtime.execute was called
    mock_runtime.execute.assert_called_once()
    call_kwargs = mock_runtime.execute.call_args.kwargs

    assert call_kwargs["child_run_id"] == "run-123"
    assert isinstance(call_kwargs["selection"], PersonalWorkflowSelectionV1)
    assert call_kwargs["selection"].selection_id == selection.selection_id
    assert call_kwargs["inputs"] == {"input_key": "input_value"}
    assert call_kwargs["execution_identity"] is execution_identity

    # Verify patch includes outputs
    patch_dict = patch.to_dict()
    assert "values" in patch_dict
    assert patch_dict["values"]["outputs"] == {"output_key": "output_value"}
    assert patch_dict["values"]["terminal_status"] == "success"


@pytest.mark.anyio
async def test_execute_handler_preserves_existing_values() -> None:
    """Test execute handler preserves existing values in state."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-456",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={"key": "value"},
    )

    # Add extra value to state
    state["values"]["extra_field"] = "extra_value"

    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(return_value={"result": "done"})

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    patch = await _execute_handler(state, context)

    # Verify extra_field is preserved
    patch_dict = patch.to_dict()
    assert patch_dict["values"]["extra_field"] == "extra_value"
    assert patch_dict["values"]["outputs"] == {"result": "done"}


@pytest.mark.anyio
async def test_execute_handler_missing_port() -> None:
    """Test execute handler raises TypeError when runtime port missing."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-789",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    context = WorkflowContext()

    with pytest.raises(TypeError, match="personal workflow runtime port is unavailable"):
        await _execute_handler(state, context)


@pytest.mark.anyio
async def test_execute_handler_port_not_callable() -> None:
    """Test execute handler raises TypeError when port.execute not callable."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-abc",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    mock_runtime = Mock()
    mock_runtime.execute = "not-callable"  # Not a function

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    with pytest.raises(TypeError, match="personal workflow runtime port is unavailable"):
        await _execute_handler(state, context)


@pytest.mark.anyio
async def test_execute_handler_with_complex_inputs() -> None:
    """Test execute handler passes complex input structures."""
    selection = _make_test_selection()
    complex_inputs = {
        "string": "value",
        "number": 42,
        "nested": {"deep": {"key": "value"}},
        "array": [1, 2, 3],
        "null": None,
    }

    state = create_initial_state(
        run_id="run-complex",
        personal_workflow_selection=selection.to_child_payload(),
        inputs=complex_inputs,
    )

    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(return_value={"status": "ok"})

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    await _execute_handler(state, context)

    call_kwargs = mock_runtime.execute.call_args.kwargs
    assert call_kwargs["inputs"] == complex_inputs


@pytest.mark.anyio
async def test_execute_handler_with_empty_inputs() -> None:
    """Test execute handler works with empty inputs."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-empty",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(return_value={"default": "output"})

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    patch = await _execute_handler(state, context)

    call_kwargs = mock_runtime.execute.call_args.kwargs
    assert call_kwargs["inputs"] == {}
    patch_dict = patch.to_dict()
    assert patch_dict["values"]["outputs"] == {"default": "output"}


@pytest.mark.anyio
async def test_execute_handler_runtime_execution_failure() -> None:
    """Test execute handler propagates runtime execution failures."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-fail",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(side_effect=RuntimeError("Execution failed"))

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    with pytest.raises(RuntimeError, match="Execution failed"):
        await _execute_handler(state, context)


@pytest.mark.anyio
async def test_execute_handler_selection_deserialization() -> None:
    """Test execute handler correctly deserializes selection from state."""
    selection = _make_test_selection()
    state = create_initial_state(
        run_id="run-deser",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    mock_runtime = Mock()
    mock_runtime.execute = AsyncMock(return_value={})

    context = WorkflowContext(ports={"personal_workflow_runtime": mock_runtime})

    await _execute_handler(state, context)

    call_kwargs = mock_runtime.execute.call_args.kwargs
    deserialized_selection = call_kwargs["selection"]

    # Verify deserialized selection matches original
    assert deserialized_selection.selection_id == selection.selection_id
    assert deserialized_selection.selection_fingerprint == selection.selection_fingerprint
    assert deserialized_selection.graph_hash == selection.graph_hash
    assert deserialized_selection.owner_key == selection.owner_key
