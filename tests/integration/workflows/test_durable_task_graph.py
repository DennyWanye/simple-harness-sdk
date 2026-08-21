# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for durable_task workflow graph transitions and state management.

Validates:
- Graph structure and node transitions
- Channel value propagation
- Loop counter increments
- Budget enforcement
- Conditional edge routing
- State initialization
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
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    create_definition,
    create_initial_state,
)


# Mock handlers for testing graph structure
async def _mock_intake(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock intake handler."""
    return {"values": {"intake_done": True}}


async def _mock_clarify(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock clarify handler."""
    clarification_required = state["values"].get("clarification_required", False)
    if clarification_required:
        return {"values": {"clarification_done": True}}
    return {"values": {"clarification_skipped": True}}


async def _mock_plan(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock plan handler."""
    return {"values": {"plan_done": True}}


async def _mock_wait_approval(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock wait_approval handler."""
    approval_required = state["values"].get("approval_required", True)
    if approval_required:
        return {"values": {"approval_done": True}}
    return {"values": {"approval_skipped": True}}


async def _mock_llm_proposal(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock llm_proposal handler."""
    # Increment proposal counter
    current = state.get("loop_counters", {}).get("proposal_turns", 0)
    return {
        "values": {"proposal_done": True},
        "loop_counters": {"proposal_turns": current + 1},
    }


async def _mock_tool_execution(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock tool_execution handler."""
    return {"values": {"execution_done": True}}


async def _mock_completion_decision(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock completion_decision handler."""
    return {"values": {"decision_done": True}}


async def _mock_test(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock test handler."""
    return {"values": {"test_done": True}}


async def _mock_audit(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock audit handler."""
    # Increment fix counter
    current = state.get("loop_counters", {}).get("fix_rounds", 0)
    return {
        "values": {"audit_done": True},
        "loop_counters": {"fix_rounds": current + 1},
    }


async def _mock_finalize(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Mock finalize handler."""
    return {"values": {"finalize_done": True}}


async def _mock_approval_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Mock approval route - choose based on test mode."""
    mode = state["values"].get("test_route_mode", "approved")
    if mode == "skip_approval":
        return "finalize"
    return "approved"


async def _mock_completion_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Mock completion route - choose based on test mode."""
    mode = state["values"].get("test_route_mode", "finalize")
    return mode  # "loop", "test", "audit", "finalize"


async def _mock_audit_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Mock audit route - choose based on test mode."""
    mode = state["values"].get("test_route_mode", "finalize")
    if mode == "fix":
        return "fix"
    return "finalize"


@pytest.fixture
def definition():  # type: ignore[no-untyped-def]
    """Create test workflow definition with mock handlers."""
    return create_definition(
        intake_handler=_mock_intake,
        clarify_handler=_mock_clarify,
        plan_handler=_mock_plan,
        wait_approval_handler=_mock_wait_approval,
        llm_proposal_handler=_mock_llm_proposal,
        tool_execution_handler=_mock_tool_execution,
        completion_decision_handler=_mock_completion_decision,
        test_handler=_mock_test,
        audit_handler=_mock_audit,
        finalize_handler=_mock_finalize,
        approval_route=_mock_approval_route,
        completion_route=_mock_completion_route,
        audit_route=_mock_audit_route,
    )


def test_definition_metadata(definition):  # type: ignore[no-untyped-def]
    """Verify workflow definition metadata."""
    assert definition.name == WORKFLOW_NAME
    assert definition.version == WORKFLOW_VERSION
    assert definition.entry_node == "intake"
    assert definition.recursion_limit == 256
    assert definition.max_supersteps == 192


def test_definition_nodes(definition):  # type: ignore[no-untyped-def]
    """Verify all nodes are defined correctly."""
    node_names = {node.node_id for node in definition.nodes}
    expected = {
        "intake",
        "clarify",
        "plan",
        "wait_approval",
        "llm_proposal",
        "tool_execution",
        "completion_decision",
        "test",
        "audit",
        "finalize",
    }
    assert node_names == expected

    # Check interrupt-capable nodes
    interrupt_nodes = {node.node_id for node in definition.nodes if node.interrupt_capable}
    assert interrupt_nodes == {"clarify", "wait_approval", "tool_execution"}

    # Check retry policies
    llm_node = next(n for n in definition.nodes if n.node_id == "llm_proposal")
    assert llm_node.retry_policy is not None
    assert llm_node.retry_policy.max_attempts == 3

    tool_node = next(n for n in definition.nodes if n.node_id == "tool_execution")
    assert tool_node.retry_policy is not None
    assert tool_node.retry_policy.max_attempts == 2


def test_definition_edges(definition):  # type: ignore[no-untyped-def]
    """Verify static edges."""
    edge_map = {(e.sources, e.target) for e in definition.edges}
    expected = {
        (("intake",), "clarify"),
        (("clarify",), "plan"),
        (("plan",), "wait_approval"),
        (("llm_proposal",), "tool_execution"),
        (("tool_execution",), "completion_decision"),
        (("test",), "audit"),
        (("finalize",), "__end__"),
    }
    assert edge_map == expected


def test_definition_conditional_edges(definition):  # type: ignore[no-untyped-def]
    """Verify conditional edges."""
    conditional_map = {ce.source: set(ce.routes.keys()) for ce in definition.conditional_edges}
    assert conditional_map == {
        "wait_approval": {"approved", "finalize"},
        "completion_decision": {"loop", "test", "audit", "finalize"},
        "audit": {"fix", "finalize"},
    }


def test_definition_channels(definition):  # type: ignore[no-untyped-def]
    """Verify channel specifications."""
    assert "values" in definition.channels
    assert "loop_counters" in definition.channels
    assert "budgets" in definition.channels


def test_definition_loop_budgets(definition):  # type: ignore[no-untyped-def]
    """Verify loop budget configuration."""
    assert definition.loop_budgets == {
        "proposal_turns": DEFAULT_PROPOSAL_TURNS,
        "fix_rounds": DEFAULT_FIX_ROUNDS,
    }
    assert definition.loop_budget_bindings == {
        "completion_decision->llm_proposal": "proposal_turns",
        "audit->llm_proposal": "fix_rounds",
    }


def test_create_initial_state_minimal():  # type: ignore[no-untyped-def]
    """Test initial state creation with minimal parameters."""
    state = create_initial_state(
        request="Test request",
        run_id="run-001",
        session_metadata={"user": "test"},
        capability_refs=["cap-1", "cap-2"],
    )

    assert state["workflow_name"] == WORKFLOW_NAME
    assert state["workflow_version"] == WORKFLOW_VERSION
    assert state["run_id"] == "run-001"
    assert state["thread_id"] == "run-001"  # defaults to run_id
    assert state["values"]["request"] == "Test request"
    assert state["values"]["capability_refs"] == ["cap-1", "cap-2"]
    assert state["loop_counters"] == {"proposal_turns": 0, "fix_rounds": 0}
    assert state["budgets"]["proposal_turns"] == DEFAULT_PROPOSAL_TURNS
    assert state["budgets"]["fix_rounds"] == DEFAULT_FIX_ROUNDS


def test_create_initial_state_with_overrides():  # type: ignore[no-untyped-def]
    """Test initial state creation with custom parameters."""
    state = create_initial_state(
        request="Custom request",
        run_id="run-002",
        session_metadata={"env": "prod"},
        capability_refs=["cap-3"],
        thread_id="thread-custom",
        session_id="session-123",
        plan_steps=["step1", "step2"],
        clarification_required=True,
        clarification_question="What do you mean?",
        approval_required=False,
        proposal_budget=20,
        fix_budget=3,
        started_at=1234567890.0,
        request_id="req-001",
        turn_id="turn-001",
    )

    assert state["thread_id"] == "thread-custom"
    assert state["session_id"] == "session-123"
    assert state["values"]["plan_steps"] == ["step1", "step2"]
    assert state["values"]["clarification_required"] is True
    assert state["values"]["clarification_question"] == "What do you mean?"
    assert state["values"]["approval_required"] is False
    assert state["budgets"]["proposal_turns"] == 20
    assert state["budgets"]["fix_rounds"] == 3
    assert state["values"]["started_at"] == 1234567890.0


def test_create_initial_state_budget_clamping():  # type: ignore[no-untyped-def]
    """Test budget values are clamped to valid ranges."""
    # Over-budget should clamp to defaults
    state = create_initial_state(
        request="Test",
        run_id="run-003",
        session_metadata={},
        capability_refs=[],
        proposal_budget=1000,  # Over DEFAULT_PROPOSAL_TURNS
        fix_budget=100,  # Over DEFAULT_FIX_ROUNDS
    )
    assert state["budgets"]["proposal_turns"] == DEFAULT_PROPOSAL_TURNS
    assert state["budgets"]["fix_rounds"] == DEFAULT_FIX_ROUNDS

    # Negative should clamp to minimums
    state = create_initial_state(
        request="Test",
        run_id="run-004",
        session_metadata={},
        capability_refs=[],
        proposal_budget=-5,
        fix_budget=-10,
    )
    assert state["budgets"]["proposal_turns"] == 1  # min(DEFAULT, max(1, -5)) = 1
    assert state["budgets"]["fix_rounds"] == 0  # min(DEFAULT, max(0, -10)) = 0


def test_initial_state_messages_default():  # type: ignore[no-untyped-def]
    """Test default message list initialization."""
    state = create_initial_state(
        request="Hello world",
        run_id="run-005",
        session_metadata={},
        capability_refs=[],
    )
    messages = state["values"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello world"


def test_initial_state_messages_custom():  # type: ignore[no-untyped-def]
    """Test custom message list initialization."""
    custom_messages = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
        {"role": "user", "content": "Third"},
    ]
    state = create_initial_state(
        request="Test",
        run_id="run-006",
        session_metadata={},
        capability_refs=[],
        messages=custom_messages,
    )
    messages = state["values"]["messages"]
    assert len(messages) == 3
    assert messages[0]["content"] == "First"
    assert messages[2]["content"] == "Third"


def test_initial_state_snapshots():  # type: ignore[no-untyped-def]
    """Test provider/model snapshot initialization."""
    state = create_initial_state(
        request="Test",
        run_id="run-007",
        session_metadata={},
        capability_refs=[],
        provider_snapshot={"provider": "test-provider", "version": "1.0"},
        model_snapshot={"model": "test-model", "params": {"temperature": 0.7}},
        output_contract={"mode": "receipt_backed", "strict": True},
    )
    assert state["values"]["provider_snapshot"] == {
        "provider": "test-provider",
        "version": "1.0",
    }
    assert state["values"]["model_snapshot"]["model"] == "test-model"
    assert state["values"]["output_contract"]["mode"] == "receipt_backed"
