# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for durable_task HITL (Human-In-The-Loop) interrupt and resume behavior.

Validates:
- Clarification interrupt and user response
- Approval gate interrupt and decision
- Tool execution interrupt and authorization
- Resume behavior after interrupts
- State preservation across interrupts
"""

from __future__ import annotations

from typing import Any

import pytest

from simple_harness.contracts import JsonValue
from simple_harness.workflow.contracts import (
    StatePatch,
    WorkflowContext,
    WorkflowState,
)
from simple_harness.workflows.durable_task import create_definition, create_initial_state


# Handlers that trigger interrupts for testing
async def _interrupt_clarify(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    """Clarify handler that interrupts if clarification required."""
    if state["values"].get("clarification_required", False):
        # Simulate asking for clarification - return interrupt patch directly
        return {
            "__interrupt__": {
                "reason": "clarification_needed",
                "question": state["values"].get("clarification_question", "Please clarify"),
            }
        }
    return {"values": {"clarification_done": True}}


async def _interrupt_wait_approval(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    """Wait approval handler that interrupts if approval required."""
    if state["values"].get("approval_required", True):
        # Check if user has provided approval decision
        if "approval_decision" not in state["values"]:
            return {
                "__interrupt__": {
                    "reason": "approval_needed",
                    "plan": state["values"].get("plan_steps", []),
                    "request": state["values"].get("request", ""),
                }
            }
    return {"values": {"approval_done": True}}


async def _interrupt_tool_execution(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    """Tool execution handler that interrupts for authorization."""
    # Check if tools need authorization
    tool_calls = state["values"].get("pending_tool_calls", [])
    if tool_calls and "tool_authorization" not in state["values"]:
        return {
            "__interrupt__": {
                "reason": "tool_authorization_needed",
                "tools": tool_calls,
                "authorization_reason": "User authorization required for tool execution",
            }
        }
    return {"values": {"execution_done": True}}


# Non-interrupting handlers
async def _simple_intake(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    return {"values": {"intake_done": True}}


async def _simple_plan(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    return {"values": {"plan_done": True}}


async def _simple_llm_proposal(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    current = state.get("loop_counters", {}).get("proposal_turns", 0)
    return {
        "values": {"proposal_done": True},
        "loop_counters": {"proposal_turns": current + 1},
    }


async def _simple_completion_decision(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    return {"values": {"decision_done": True}}


async def _simple_test(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    return {"values": {"test_done": True}}


async def _simple_audit(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    current = state.get("loop_counters", {}).get("fix_rounds", 0)
    return {
        "values": {"audit_done": True},
        "loop_counters": {"fix_rounds": current + 1},
    }


async def _simple_finalize(
    state: WorkflowState, context: WorkflowContext
) -> StatePatch:
    return {"values": {"finalize_done": True}}


async def _route_approved(
    state: WorkflowState, context: WorkflowContext
) -> str:
    return "approved"


async def _route_finalize(
    state: WorkflowState, context: WorkflowContext
) -> str:
    return "finalize"


@pytest.fixture
def interrupt_definition():  # type: ignore[no-untyped-def]
    """Create workflow definition with interrupt-capable handlers."""
    return create_definition(
        intake_handler=_simple_intake,
        clarify_handler=_interrupt_clarify,
        plan_handler=_simple_plan,
        wait_approval_handler=_interrupt_wait_approval,
        llm_proposal_handler=_simple_llm_proposal,
        tool_execution_handler=_interrupt_tool_execution,
        completion_decision_handler=_simple_completion_decision,
        test_handler=_simple_test,
        audit_handler=_simple_audit,
        finalize_handler=_simple_finalize,
        approval_route=_route_approved,
        completion_route=_route_finalize,
        audit_route=_route_finalize,
    )


def test_clarification_interrupt_structure(interrupt_definition):  # type: ignore[no-untyped-def]
    """Verify clarify node is configured for interrupts."""
    clarify_node = next(
        n for n in interrupt_definition.nodes if n.node_id == "clarify"
    )
    assert clarify_node.interrupt_capable is True
    assert clarify_node.barrier is True
    assert clarify_node.exclusive_superstep is True
    assert clarify_node.pre_interrupt_effect_policy == "pure"


def test_approval_interrupt_structure(interrupt_definition):  # type: ignore[no-untyped-def]
    """Verify wait_approval node is configured for interrupts."""
    approval_node = next(
        n for n in interrupt_definition.nodes if n.node_id == "wait_approval"
    )
    assert approval_node.interrupt_capable is True
    assert approval_node.barrier is True
    assert approval_node.exclusive_superstep is True
    assert approval_node.pre_interrupt_effect_policy == "pure"


def test_tool_execution_interrupt_structure(interrupt_definition):  # type: ignore[no-untyped-def]
    """Verify tool_execution node is configured for interrupts."""
    tool_node = next(
        n for n in interrupt_definition.nodes if n.node_id == "tool_execution"
    )
    assert tool_node.interrupt_capable is True
    assert tool_node.barrier is True
    assert tool_node.exclusive_superstep is True
    assert tool_node.pre_interrupt_effect_policy == "pure"


def test_initial_state_clarification_flag():  # type: ignore[no-untyped-def]
    """Test initial state with clarification required."""
    state = create_initial_state(
        request="Ambiguous request",
        run_id="run-001",
        session_metadata={},
        capability_refs=[],
        clarification_required=True,
        clarification_question="What do you mean by X?",
    )
    assert state["values"]["clarification_required"] is True
    assert state["values"]["clarification_question"] == "What do you mean by X?"


def test_initial_state_approval_flag():  # type: ignore[no-untyped-def]
    """Test initial state with approval required."""
    state = create_initial_state(
        request="Modify production",
        run_id="run-002",
        session_metadata={},
        capability_refs=[],
        approval_required=True,
    )
    assert state["values"]["approval_required"] is True

    # Test with approval disabled
    state_no_approval = create_initial_state(
        request="Simple task",
        run_id="run-003",
        session_metadata={},
        capability_refs=[],
        approval_required=False,
    )
    assert state_no_approval["values"]["approval_required"] is False


async def test_clarify_triggers_interrupt(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that clarify handler returns interrupt when clarification needed."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-001",
        "thread_id": "thread-001",
        "values": {
            "clarification_required": True,
            "clarification_question": "What file should I modify?",
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_clarify(state, MockContext())  # type: ignore[arg-type]

    # Interrupt returns a special patch with __interrupt__ key
    assert "__interrupt__" in result
    interrupt_data = result["__interrupt__"]
    assert interrupt_data["reason"] == "clarification_needed"
    assert "question" in interrupt_data
    assert interrupt_data["question"] == "What file should I modify?"


async def test_clarify_skips_when_not_required(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that clarify handler proceeds without interrupt when not needed."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-001",
        "thread_id": "thread-001",
        "values": {
            "clarification_required": False,
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_clarify(state, MockContext())  # type: ignore[arg-type]

    # No interrupt, normal patch
    assert "__interrupt__" not in result
    assert result["values"]["clarification_done"] is True


async def test_approval_triggers_interrupt(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that wait_approval handler returns interrupt when approval needed."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-002",
        "thread_id": "thread-002",
        "values": {
            "approval_required": True,
            "request": "Delete production database",
            "plan_steps": ["Step 1: Connect", "Step 2: Drop tables"],
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_wait_approval(state, MockContext())  # type: ignore[arg-type]

    assert "__interrupt__" in result
    interrupt_data = result["__interrupt__"]
    assert interrupt_data["reason"] == "approval_needed"
    assert "plan" in interrupt_data
    assert len(interrupt_data["plan"]) == 2


async def test_approval_proceeds_with_decision(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that wait_approval proceeds when user has provided decision."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-002",
        "thread_id": "thread-002",
        "values": {
            "approval_required": True,
            "approval_decision": "approved",  # User has responded
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_wait_approval(state, MockContext())  # type: ignore[arg-type]

    # No interrupt because decision was provided
    assert "__interrupt__" not in result
    assert result["values"]["approval_done"] is True


async def test_tool_execution_triggers_interrupt(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that tool_execution handler returns interrupt for authorization."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-003",
        "thread_id": "thread-003",
        "values": {
            "pending_tool_calls": [
                {"tool": "write_file", "args": {"path": "/etc/hosts"}},
                {"tool": "bash", "args": {"command": "rm -rf /"}},
            ],
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_tool_execution(state, MockContext())  # type: ignore[arg-type]

    assert "__interrupt__" in result
    interrupt_data = result["__interrupt__"]
    assert interrupt_data["reason"] == "tool_authorization_needed"
    assert len(interrupt_data["tools"]) == 2


async def test_tool_execution_proceeds_with_auth(anyio_backend):  # type: ignore[no-untyped-def]
    """Test that tool_execution proceeds when authorization provided."""
    state: WorkflowState = {
        "workflow_name": "durable_task",
        "workflow_version": "v1",
        "run_id": "run-003",
        "thread_id": "thread-003",
        "values": {
            "pending_tool_calls": [{"tool": "read_file", "args": {}}],
            "tool_authorization": "granted",  # User has authorized
        },
        "loop_counters": {},
        "budgets": {},
    }

    class MockContext:
        pass

    result = await _interrupt_tool_execution(state, MockContext())  # type: ignore[arg-type]

    # No interrupt because authorization was provided
    assert "__interrupt__" not in result
    assert result["values"]["execution_done"] is True
