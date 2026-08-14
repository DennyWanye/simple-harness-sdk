# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for personal_v1 workflow graph structure.

Validates:
- Definition metadata (name, version, schema)
- Node structure (single execute node)
- Edge structure (execute → END_NODE)
- Channel structure (values channel)
- Initial state construction
"""

from __future__ import annotations

import json

import pytest

from simple_harness.workflows.personal_v1.definition import (
    PERSONAL_WORKFLOW_V1_DEFINITION,
    PROFILE_KEY,
    STATE_SCHEMA_VERSION,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    create_initial_state,
)
from simple_harness.workflows.personal_v1.selection import (
    PersonalWorkflowSelectionV1,
    personal_workflow_query_hash,
)


def _make_minimal_selection() -> PersonalWorkflowSelectionV1:
    """Create minimal valid selection for testing."""
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
                    "type": "input",
                    "bindings": {},
                    "config": {},
                }
            ],
            "outputs": {},
            "max_steps": 1,
        },
        graph_hash="b" * 64,
        query_hash=personal_workflow_query_hash("test query"),
        run_catalog_content_stamp="stamp-123",
        lease_entries=[{"lease_id": "lease-1"}],
        effect_topology={"policy": "read_only"},
        tool_bindings={},
    )


def test_definition_metadata() -> None:
    """Test definition has correct metadata."""
    assert PERSONAL_WORKFLOW_V1_DEFINITION.name == WORKFLOW_NAME
    assert PERSONAL_WORKFLOW_V1_DEFINITION.version == WORKFLOW_VERSION
    assert PERSONAL_WORKFLOW_V1_DEFINITION.state_schema_version == STATE_SCHEMA_VERSION
    assert PERSONAL_WORKFLOW_V1_DEFINITION.entry_node == "execute"


def test_definition_nodes() -> None:
    """Test definition has single execute node."""
    nodes = PERSONAL_WORKFLOW_V1_DEFINITION.nodes
    assert len(nodes) == 1
    assert nodes[0].node_id == "execute"
    assert callable(nodes[0].handler)


def test_definition_edges() -> None:
    """Test definition has single edge to END_NODE."""
    edges = PERSONAL_WORKFLOW_V1_DEFINITION.edges
    assert len(edges) == 1
    # Edge.source is normalized to tuple in __post_init__
    assert edges[0].sources == ("execute",)
    assert edges[0].target == "__end__"


def test_definition_channels() -> None:
    """Test definition has values channel with correct reducer."""
    channels = PERSONAL_WORKFLOW_V1_DEFINITION.channels
    assert "values" in channels

    values_channel = channels["values"]
    assert values_channel.value_type.value == "object"
    assert values_channel.reducer.value == "single_writer"
    assert "execute" in values_channel.allowed_writers


def test_definition_prompt_manifest() -> None:
    """Test definition includes profile key in prompt manifest."""
    manifest = PERSONAL_WORKFLOW_V1_DEFINITION.prompt_manifest
    assert manifest is not None
    assert manifest.get("profile") == PROFILE_KEY


def test_definition_policy_manifest() -> None:
    """Test definition includes policy metadata."""
    policy = PERSONAL_WORKFLOW_V1_DEFINITION.policy_manifest
    assert policy is not None
    assert policy.get("implementation") == "personal-workflow-interpreter-v1"
    assert policy.get("selection_source") == "trusted-parent-start-snapshot-only"
    assert policy.get("effect_identity") == "child-selection-graph-node-v1"


def test_create_initial_state_minimal() -> None:
    """Test create_initial_state with minimal arguments."""
    selection = _make_minimal_selection()

    state = create_initial_state(
        run_id="run-123",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={},
    )

    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["workflow_name"] == WORKFLOW_NAME
    assert state["workflow_version"] == WORKFLOW_VERSION
    assert state["run_id"] == "run-123"
    assert state["thread_id"] == "run-123"  # Defaults to run_id
    assert state["session_id"] == ""
    assert state["status"] == "pending"
    assert state["active_nodes"] == []


def test_create_initial_state_with_overrides() -> None:
    """Test create_initial_state with thread_id and session_id."""
    selection = _make_minimal_selection()

    state = create_initial_state(
        run_id="run-456",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={"key": "value"},
        thread_id="thread-789",
        session_id="session-abc",
    )

    assert state["run_id"] == "run-456"
    assert state["thread_id"] == "thread-789"
    assert state["session_id"] == "session-abc"


def test_initial_state_values_structure() -> None:
    """Test initial state values contain selection and inputs."""
    selection = _make_minimal_selection()

    state = create_initial_state(
        run_id="run-test",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={"input1": "value1", "input2": 42},
    )

    values = state["values"]
    assert "personal_workflow_selection" in values
    assert "inputs" in values

    # Verify selection payload structure
    sel_payload = values["personal_workflow_selection"]
    assert sel_payload["schema_version"] == 1
    assert sel_payload["selection_id"] == selection.selection_id
    assert sel_payload["graph_hash"] == selection.graph_hash

    # Verify inputs are preserved
    inputs = values["inputs"]
    assert inputs["input1"] == "value1"
    assert inputs["input2"] == 42


def test_initial_state_serialization_roundtrip() -> None:
    """Test initial state can be serialized and deserialized."""
    selection = _make_minimal_selection()

    state = create_initial_state(
        run_id="run-roundtrip",
        personal_workflow_selection=selection.to_child_payload(),
        inputs={"key": "value"},
    )

    # Serialize to JSON and back
    serialized = json.dumps(state)
    deserialized = json.loads(serialized)

    assert deserialized["run_id"] == "run-roundtrip"
    assert deserialized["workflow_name"] == WORKFLOW_NAME
    assert deserialized["values"]["inputs"]["key"] == "value"


def test_initial_state_rejects_invalid_selection() -> None:
    """Test create_initial_state rejects invalid selection payload."""
    with pytest.raises(Exception):  # PersonalWorkflowSelectionError
        create_initial_state(
            run_id="run-bad",
            personal_workflow_selection={"invalid": "payload"},
            inputs={},
        )
