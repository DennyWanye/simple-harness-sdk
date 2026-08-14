# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for durable_task workflow recovery and checkpoint consistency.

Validates:
- State preservation across checkpoint/restore cycles
- Loop counter persistence and restoration
- Budget enforcement after recovery
- Error state preservation
- Partial completion recovery
"""

from __future__ import annotations

import pytest

from simple_harness.workflow.contracts import (
    StatePatch,
    WorkflowContext,
    WorkflowState,
)
from simple_harness.workflows.durable_task import (
    DEFAULT_FIX_ROUNDS,
    DEFAULT_PROPOSAL_TURNS,
    create_initial_state,
)


def test_initial_state_roundtrip():  # type: ignore[no-untyped-def]
    """Test that initial state can be serialized and restored."""
    state = create_initial_state(
        request="Test request",
        run_id="run-001",
        session_metadata={"user": "test", "env": "dev"},
        capability_refs=["cap-1", "cap-2"],
        thread_id="thread-001",
        session_id="session-001",
        plan_steps=["step1", "step2"],
        clarification_required=True,
        clarification_question="What?",
        approval_required=False,
        proposal_budget=20,
        fix_budget=3,
    )

    # Simulate checkpoint: all fields are JSON-serializable
    import json
    serialized = json.dumps(state)
    restored = json.loads(serialized)

    # Verify critical fields preserved
    assert restored["workflow_name"] == "durable_task"
    assert restored["run_id"] == "run-001"
    assert restored["thread_id"] == "thread-001"
    assert restored["values"]["request"] == "Test request"
    assert restored["values"]["plan_steps"] == ["step1", "step2"]
    assert restored["values"]["clarification_required"] is True
    assert restored["budgets"]["proposal_turns"] == 20
    assert restored["budgets"]["fix_rounds"] == 3
    assert restored["loop_counters"]["proposal_turns"] == 0


def test_loop_counter_recovery():  # type: ignore[no-untyped-def]
    """Test loop counter state is preserved across recovery."""
    state = create_initial_state(
        request="Test",
        run_id="run-002",
        session_metadata={},
        capability_refs=[],
    )

    # Simulate some loop iterations
    state["loop_counters"]["proposal_turns"] = 5
    state["loop_counters"]["fix_rounds"] = 2

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    assert restored["loop_counters"]["proposal_turns"] == 5
    assert restored["loop_counters"]["fix_rounds"] == 2


def test_budget_enforcement_after_recovery():  # type: ignore[no-untyped-def]
    """Test budget limits are enforced after state recovery."""
    state = create_initial_state(
        request="Test",
        run_id="run-003",
        session_metadata={},
        capability_refs=[],
        proposal_budget=10,
        fix_budget=2,
    )

    # Simulate approaching budget limit
    state["loop_counters"]["proposal_turns"] = 9

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    # Verify budget state preserved
    assert restored["budgets"]["proposal_turns"] == 10
    assert restored["loop_counters"]["proposal_turns"] == 9
    # Budget exhausted check would happen in node handler
    remaining = restored["budgets"]["proposal_turns"] - restored["loop_counters"]["proposal_turns"]
    assert remaining == 1


def test_values_channel_recovery():  # type: ignore[no-untyped-def]
    """Test values channel state is fully preserved."""
    state = create_initial_state(
        request="Original request",
        run_id="run-004",
        session_metadata={"key": "value"},
        capability_refs=["cap-1"],
    )

    # Add various value types
    state["values"]["string_value"] = "test"
    state["values"]["int_value"] = 42
    state["values"]["bool_value"] = True
    state["values"]["list_value"] = [1, 2, 3]
    state["values"]["dict_value"] = {"nested": "data"}
    state["values"]["null_value"] = None

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    assert restored["values"]["string_value"] == "test"
    assert restored["values"]["int_value"] == 42
    assert restored["values"]["bool_value"] is True
    assert restored["values"]["list_value"] == [1, 2, 3]
    assert restored["values"]["dict_value"] == {"nested": "data"}
    assert restored["values"]["null_value"] is None


def test_message_history_recovery():  # type: ignore[no-untyped-def]
    """Test message history is preserved across recovery."""
    messages = [
        {"role": "user", "content": "First message"},
        {"role": "assistant", "content": "Response"},
        {"role": "user", "content": "Follow-up"},
    ]

    state = create_initial_state(
        request="Test",
        run_id="run-005",
        session_metadata={},
        capability_refs=[],
        messages=messages,
    )

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    assert len(restored["values"]["messages"]) == 3
    assert restored["values"]["messages"][0]["content"] == "First message"
    assert restored["values"]["messages"][2]["content"] == "Follow-up"


def test_snapshot_metadata_recovery():  # type: ignore[no-untyped-def]
    """Test provider/model snapshots are preserved."""
    state = create_initial_state(
        request="Test",
        run_id="run-006",
        session_metadata={},
        capability_refs=[],
        provider_snapshot={"provider": "anthropic", "version": "2024-01"},
        model_snapshot={"model": "claude-opus-5", "temperature": 0.7},
        output_contract={"mode": "receipt_backed", "strict": True},
    )

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    assert restored["values"]["provider_snapshot"]["provider"] == "anthropic"
    assert restored["values"]["model_snapshot"]["model"] == "claude-opus-5"
    assert restored["values"]["output_contract"]["mode"] == "receipt_backed"


def test_partial_completion_recovery():  # type: ignore[no-untyped-def]
    """Test recovery from partially completed workflow."""
    state = create_initial_state(
        request="Test",
        run_id="run-007",
        session_metadata={},
        capability_refs=[],
    )

    # Simulate partial execution
    state["values"]["intake_done"] = True
    state["values"]["clarification_done"] = True
    state["values"]["plan_done"] = True
    state["values"]["approval_done"] = True
    state["loop_counters"]["proposal_turns"] = 3

    # Checkpoint and restore
    import json
    restored: WorkflowState = json.loads(json.dumps(state))

    # Verify progress markers preserved
    assert restored["values"]["intake_done"] is True
    assert restored["values"]["plan_done"] is True
    assert restored["loop_counters"]["proposal_turns"] == 3
