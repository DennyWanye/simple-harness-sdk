# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable task workflow graph definition.

Graph structure:
    intake → clarify? → plan → wait_approval? →
    llm_proposal → tool_execution → completion_decision →
    (test → audit → fix_round → llm_proposal) | finalize → END

HITL interrupt points: clarify, wait_approval, tool_execution
Loop budgets: proposal_turns, fix_rounds
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from simple_harness.contracts import JsonValue
from simple_harness.workflow.contracts import (
    ChannelSpec,
    JsonType,
    ReducerKind,
    RetryPolicy,
    WorkflowState,
)
from simple_harness.workflow.definition import (
    END_NODE,
    ConditionalEdge,
    Edge,
    NodeDefinition,
    WorkflowDefinition,
)

if TYPE_CHECKING:
    from simple_harness.workflow.contracts import NodeHandler, RouteSelector


WORKFLOW_NAME = "durable_task"
WORKFLOW_VERSION = "v1"
STATE_SCHEMA_VERSION = 1
RECURSION_LIMIT = 256
MAX_SUPERSTEPS = 192

# Default budgets
DEFAULT_PROPOSAL_TURNS = 40
DEFAULT_FIX_ROUNDS = 8

# Value-writing nodes (for channel reducer)
_VALUE_WRITERS = frozenset(
    {
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
)


def _single(value_type: JsonType, writers: frozenset[str]) -> ChannelSpec:
    """Create single-writer channel spec."""
    return ChannelSpec(
        value_type=value_type,
        reducer=ReducerKind.SINGLE_WRITER,
        allowed_writers=writers,
    )


def create_definition(
    *,
    intake_handler: NodeHandler,
    clarify_handler: NodeHandler,
    plan_handler: NodeHandler,
    wait_approval_handler: NodeHandler,
    llm_proposal_handler: NodeHandler,
    tool_execution_handler: NodeHandler,
    completion_decision_handler: NodeHandler,
    test_handler: NodeHandler,
    audit_handler: NodeHandler,
    finalize_handler: NodeHandler,
    approval_route: RouteSelector,
    completion_route: RouteSelector,
    audit_route: RouteSelector,
    max_proposal_turns: int = DEFAULT_PROPOSAL_TURNS,
    max_fix_rounds: int = DEFAULT_FIX_ROUNDS,
) -> WorkflowDefinition:
    """Create durable_task workflow definition with provided handlers.

    Args:
        intake_handler: Initialize workflow state from input
        clarify_handler: Request clarification from user (HITL)
        plan_handler: Generate execution plan
        wait_approval_handler: Wait for user approval (HITL)
        llm_proposal_handler: Generate LLM proposal with tool calls
        tool_execution_handler: Execute approved tool calls (HITL)
        completion_decision_handler: Decide next action (loop/test/audit/finalize)
        test_handler: Run tests on output
        audit_handler: Audit output quality
        finalize_handler: Prepare final output
        approval_route: Route from wait_approval (approved/finalize)
        completion_route: Route from completion_decision (loop/test/audit/finalize)
        audit_route: Route from audit (fix/finalize)
        max_proposal_turns: Maximum LLM proposal iterations
        max_fix_rounds: Maximum fix rounds after audit

    Returns:
        WorkflowDefinition for durable_task graph
    """
    return WorkflowDefinition(
        name=WORKFLOW_NAME,
        version=WORKFLOW_VERSION,
        state_schema_version=STATE_SCHEMA_VERSION,
        entry_node="intake",
        nodes=(
            NodeDefinition("intake", intake_handler),
            NodeDefinition(
                "clarify",
                clarify_handler,
                interrupt_capable=True,
                barrier=True,
                exclusive_superstep=True,
                pre_interrupt_effect_policy="pure",
            ),
            NodeDefinition("plan", plan_handler),
            NodeDefinition(
                "wait_approval",
                wait_approval_handler,
                interrupt_capable=True,
                barrier=True,
                exclusive_superstep=True,
                pre_interrupt_effect_policy="pure",
            ),
            NodeDefinition(
                "llm_proposal",
                llm_proposal_handler,
                retry_policy=RetryPolicy(
                    max_attempts=3,
                    retryable_codes=frozenset({"retryable_provider"}),
                ),
            ),
            NodeDefinition(
                "tool_execution",
                tool_execution_handler,
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    retryable_codes=frozenset(
                        {"retryable_network", "retryable_provider"}
                    ),
                ),
                interrupt_capable=True,
                barrier=True,
                exclusive_superstep=True,
                pre_interrupt_effect_policy="pure",
            ),
            NodeDefinition("completion_decision", completion_decision_handler),
            NodeDefinition("test", test_handler),
            NodeDefinition("audit", audit_handler),
            NodeDefinition("finalize", finalize_handler),
        ),
        channels={
            "values": _single(JsonType.OBJECT, _VALUE_WRITERS),
            "loop_counters": _single(
                JsonType.OBJECT, frozenset({"intake", "llm_proposal", "audit"})
            ),
            "budgets": _single(JsonType.OBJECT, frozenset({"intake"})),
        },
        edges=(
            Edge("intake", "clarify"),
            Edge("clarify", "plan"),
            Edge("plan", "wait_approval"),
            Edge("llm_proposal", "tool_execution"),
            Edge("tool_execution", "completion_decision"),
            Edge("test", "audit"),
            Edge("finalize", END_NODE),
        ),
        conditional_edges=(
            ConditionalEdge(
                "wait_approval",
                approval_route,
                {"approved": "llm_proposal", "finalize": "finalize"},
                selector_effect_policy="pure",
            ),
            ConditionalEdge(
                "completion_decision",
                completion_route,
                {
                    "loop": "llm_proposal",
                    "test": "test",
                    "audit": "audit",
                    "finalize": "finalize",
                },
                selector_effect_policy="pure",
            ),
            ConditionalEdge(
                "audit",
                audit_route,
                {"fix": "llm_proposal", "finalize": "finalize"},
                selector_effect_policy="pure",
            ),
        ),
        recursion_limit=RECURSION_LIMIT,
        max_supersteps=MAX_SUPERSTEPS,
        loop_budgets={
            "proposal_turns": max_proposal_turns,
            "fix_rounds": max_fix_rounds,
        },
        loop_budget_bindings={
            "completion_decision->llm_proposal": "proposal_turns",
            "audit->llm_proposal": "fix_rounds",
        },
        prompt_manifest={
            "profile": "workflow.durable_task",
            "proposal_contract": "ProposalStateV1->ProposalOutcomeV1",
            "nodes": [
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
            ],
        },
        policy_manifest={
            "implementation": "durable-task-graph-v1.0.0",
            "proposal_budget": max_proposal_turns,
            "fix_budget": max_fix_rounds,
            "proposal_effect_boundary": "separate-sync-checkpoints",
            "dynamic_tool_policy": "durable-hitl-allow-once-opaque-or-skip-or-cancel",
            "todo_projection": "graph-owned-stable-workflow-step-id",
            "terminal_contract": "delivery-intents-only",
            "product_mode": "none",
            "workspace_owner": "task_work_context",
        },
    )


def create_initial_state(
    *,
    request: str,
    run_id: str,
    session_metadata: Mapping[str, JsonValue],
    capability_refs: Sequence[str],
    thread_id: str | None = None,
    session_id: str = "",
    messages: Sequence[Mapping[str, JsonValue]] | None = None,
    plan_steps: Sequence[str] = (),
    clarification_required: bool = False,
    clarification_question: str = "",
    approval_required: bool = True,
    proposal_budget: int = DEFAULT_PROPOSAL_TURNS,
    fix_budget: int = DEFAULT_FIX_ROUNDS,
    started_at: float = 0.0,
    request_id: str = "",
    turn_id: str = "",
    provider_snapshot: Mapping[str, JsonValue] | None = None,
    model_snapshot: Mapping[str, JsonValue] | None = None,
    output_contract: Mapping[str, JsonValue] | None = None,
) -> WorkflowState:
    """Create initial workflow state for durable_task.

    Args:
        request: User request text
        run_id: Unique run identifier
        session_metadata: Session context metadata
        capability_refs: References to available capabilities
        thread_id: Optional thread identifier (defaults to run_id)
        session_id: Session identifier
        messages: Initial message history
        plan_steps: Initial plan steps
        clarification_required: Whether clarification is needed
        clarification_question: Clarification question text
        approval_required: Whether approval gate is required
        proposal_budget: Maximum proposal turns
        fix_budget: Maximum fix rounds
        started_at: Workflow start timestamp
        request_id: Request identifier
        turn_id: Turn identifier
        provider_snapshot: Provider configuration snapshot
        model_snapshot: Model configuration snapshot
        output_contract: Output validation contract

    Returns:
        WorkflowState ready for execution
    """
    values: dict[str, JsonValue] = {
        "request": request,
        "messages": [
            dict(value)
            for value in (messages or [{"role": "user", "content": request}])
        ],
        "plan_steps": list(plan_steps),
        "clarification_required": clarification_required,
        "clarification_question": clarification_question,
        "approval_required": approval_required,
        "proposal_budget": proposal_budget,
        "fix_budget": fix_budget,
        "started_at": started_at,
        "request_id": request_id,
        "turn_id": turn_id,
        "session_metadata": dict(session_metadata),
        "capability_refs": list(capability_refs),
        "provider_snapshot": dict(provider_snapshot or {}),
        "model_snapshot": dict(model_snapshot or {}),
        "output_contract": dict(output_contract or {}),
    }

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "workflow_name": WORKFLOW_NAME,
        "workflow_version": WORKFLOW_VERSION,
        "thread_id": thread_id or run_id,
        "run_id": run_id,
        "session_id": session_id,
        "active_nodes": [],
        "active_step_id": None,
        "status": "pending",
        "values": values,
        "blob_refs": [],
        "artifact_refs": [],
        "receipt_refs": [],
        "loop_counters": {"proposal_turns": 0, "fix_rounds": 0},
        "budgets": {
            "proposal_turns": min(DEFAULT_PROPOSAL_TURNS, max(1, proposal_budget)),
            "fix_rounds": min(DEFAULT_FIX_ROUNDS, max(0, fix_budget)),
        },
        "errors": [],
    }


__all__ = [
    "DEFAULT_FIX_ROUNDS",
    "DEFAULT_PROPOSAL_TURNS",
    "MAX_SUPERSTEPS",
    "RECURSION_LIMIT",
    "STATE_SCHEMA_VERSION",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "create_definition",
    "create_initial_state",
]
