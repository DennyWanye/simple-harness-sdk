# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Node handlers for durable_task workflow.

All handlers call through Port interfaces for capability access.
Product-specific logic removed; generic convergence/gate logic retained.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from simple_harness.contracts import JsonValue, validate_json_value
from simple_harness.workflow.contracts import (
    StatePatch,
    WorkflowContext,
    WorkflowState,
    canonical_json,
)
from simple_harness.workflow.control import workflow_interrupt
from simple_harness.workflow.errors import WorkflowErrorCode, WorkflowNodeError

from .state import (
    ConvergenceStateV1,
    GateConfigV1,
    GateStateV1,
    ProposalOutcomeV1,
    ProposalStateV1,
)

if TYPE_CHECKING:
    from .ports import (
        ArtifactPort,
        AuthorizationPort,
        CapabilityCatalogPort,
        ProposalPort,
        WorkspacePort,
    )


# Budget constants
DEFAULT_PROPOSAL_TURNS = 40
DEFAULT_FIX_ROUNDS = 8

# Tool classification
FORBIDDEN_DURABLE_TOOLS = frozenset(
    {"spawn_subagents", "await_subagents", "spawn_team", "todo_write"}
)
DYNAMIC_SOURCES = frozenset({"plugin", "mcp"})

# Request pattern detection
_EXPLICIT_TOOL_FREE_REQUEST = re.compile(
    r"(?:"
    r"只(?:需|要)?(?:回复|回答)|仅(?:回复|回答)|直接(?:回复|回答)|"
    r"不(?:要|需)?调用(?:任何)?(?:写)?工具|无需(?:调用)?工具|"
    r"\b(?:only|just)\s+(?:reply|respond|answer)\b|"
    r"\b(?:do\s+not|don't|without)\s+(?:(?:call|use|using)\s+)?(?:any\s+)?tools?\b"
    r")",
    re.IGNORECASE,
)
_APPROVAL_STEP = re.compile(
    r"(?:等待|等候).*(?:批准|确认)|(?:批准|确认).*(?:后|再)|"
    r"\bwait(?:ing)?\s+for\s+(?:approval|confirmation)\b|"
    r"\b(?:approve|approval|confirmation)\s+gate\b",
    re.IGNORECASE,
)

# Convergence limits
_MAX_CONSECUTIVE_DISCOVERY_SEARCHES = 3
_MAX_CONSECUTIVE_FAILED_DESCRIBES = 2
_REPEATABLE_DISCOVERY_SEARCH_TOOL_NAMES = frozenset({"capability_search", "tool_search"})

# Provider failure detection
_PROVIDER_BALANCE_FAILURE = re.compile(
    r"(?:\bstatus(?:_code)?[=:\s]+402\b|"
    r"\bhttp/\S+\s+402\b|"
    r"\"(?:status|code)\"\s*:\s*402\b|"
    r"insufficient[_ -]?balance|余额不足|费用不足)",
    re.IGNORECASE,
)


def _input_value(state: Mapping[str, object], name: str, default: object) -> object:
    """Extract value from state, preferring values channel."""
    values = state.get("values")
    if isinstance(values, Mapping) and name in values:
        return values[name]
    return state.get(name, default)


def _effective(state: Mapping[str, object]) -> dict[str, object]:
    """Flatten state with values channel merged."""
    effective = dict(state)
    values = state.get("values")
    if isinstance(values, Mapping):
        effective.update(values)
    return effective


def _merged_values(
    state: Mapping[str, object], updates: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Merge updates into state values channel."""
    current = state.get("values")
    merged = copy.deepcopy(dict(current)) if isinstance(current, Mapping) else {}
    merged.update(copy.deepcopy(dict(updates)))
    validate_json_value(merged)
    return merged


def _stable_id(*parts: object, prefix: str) -> str:
    """Generate stable ID from parts."""
    payload: list[JsonValue] = [str(part) for part in parts]
    digest = hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _interrupt(payload: Mapping[str, JsonValue]) -> JsonValue:
    """Signal HITL interrupt with payload."""
    return workflow_interrupt(copy.deepcopy(dict(payload)))


def _proposal_state(effective: Mapping[str, object]) -> ProposalStateV1:
    """Extract ProposalStateV1 from effective state."""
    raw = effective.get("proposal_state")
    if not isinstance(raw, Mapping):
        raise TypeError("proposal_state is missing from workflow checkpoint")
    return ProposalStateV1.from_dict(raw)


def _outcome(effective: Mapping[str, object]) -> ProposalOutcomeV1:
    """Extract ProposalOutcomeV1 from effective state."""
    raw = effective.get("proposal_outcome")
    if not isinstance(raw, Mapping):
        raise TypeError("proposal_outcome is missing from workflow checkpoint")
    return ProposalOutcomeV1.from_dict(raw)


def _todo_items(effective: Mapping[str, object]) -> list[dict[str, JsonValue]]:
    """Extract todo items from effective state."""
    raw = effective.get("todos", [])
    if not isinstance(raw, list):
        raise TypeError("todos must be an array")
    return [copy.deepcopy(dict(item)) for item in raw]


def _todo_intents(
    run_id: str, todos: Sequence[Mapping[str, JsonValue]]
) -> list[dict[str, JsonValue]]:
    """Generate delivery intents for todo updates."""
    return [
        {
            "intent_id": f"{run_id}:todo:{item['workflow_step_id']}",
            "kind": "todo_upsert",
            "workflow_run_id": run_id,
            "workflow_step_id": str(item["workflow_step_id"]),
            "payload": copy.deepcopy(dict(item)),
        }
        for item in todos
    ]


def _provider_failure_is_retryable(exc: BaseException) -> bool:
    """Classify transport failures without retrying explicit 402."""
    current: BaseException | None = exc
    seen: set[int] = set()
    retryable = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # Check for 402 status code patterns
        if _PROVIDER_BALANCE_FAILURE.search(str(current)):
            return False
        # Check exception types that indicate retryable failures
        exc_name = type(current).__name__
        if exc_name in {
            "ProviderDispatchNotSentError",
            "ProviderDispatchUnknownError",
            "TransportError",
            "ConnectError",
            "TimeoutException",
        }:
            retryable = True
        current = current.__cause__ or current.__context__
    return retryable


def _provider_failure_message_ref(exc: BaseException) -> str:
    """Reduce provider exception chain to safe, user-renderable code."""
    safe_runtime_codes = {
        "tool_activation_revision_conflict",
        "tool_activation_scope_conflict",
    }
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        error_class = str(getattr(current, "error_class", "") or "")
        status_code = getattr(current, "status_code", None)
        if error_class == "insufficient_balance" or status_code == 402:
            return "provider:insufficient_balance"
        if error_class in {"relay_key_invalid", "empty_api_key"}:
            return f"provider:{error_class}"
        runtime_code = str(current).strip()
        if runtime_code in safe_runtime_codes:
            return f"workflow_node:llm_proposal:{runtime_code}"
        current = current.__cause__ or current.__context__
    return "workflow_node:llm_proposal:provider_failure"


# === Node Handlers ===


async def intake_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Initialize workflow state from input.

    Validates request, sets up proposal state with gate config and convergence.
    """
    effective = _effective(state)
    request = str(effective.get("request", "")).strip()
    if not request:
        raise ValueError("durable_task request must not be empty")

    # Extract initialization parameters
    proposal_budget = int(effective.get("proposal_budget", DEFAULT_PROPOSAL_TURNS))
    fix_budget = int(effective.get("fix_budget", DEFAULT_FIX_ROUNDS))
    messages = effective.get("messages")
    if not isinstance(messages, list):
        messages = [{"role": "user", "content": request}]

    # Get clock time from context
    clock_port = context.ports.get("clock")
    if clock_port is not None and hasattr(clock_port, "time"):
        started_at = float(await clock_port.time())
    else:
        started_at = float(effective.get("started_at", 0.0) or 0.0)

    # Initialize proposal state
    proposal_state = ProposalStateV1(
        messages=messages,
        original_request=request,
        request_id=str(context.request_id or effective.get("request_id", "")),
        turn_id=str(context.turn_id or effective.get("turn_id", "")),
        system_prompt_ref=(
            str(effective["system_prompt_ref"]) if effective.get("system_prompt_ref") else None
        ),
        prompt_ref=(str(effective["prompt_ref"]) if effective.get("prompt_ref") else None),
        skill_refs=list(effective.get("skill_refs", [])),
        compaction_summary=(
            str(effective["compaction_summary"]) if effective.get("compaction_summary") else None
        ),
        compaction_ref=(
            str(effective["compaction_ref"]) if effective.get("compaction_ref") else None
        ),
        token_estimate=int(effective.get("token_estimate", 0)),
        iteration=0,
        proposal_turns_used=0,
        fix_rounds_used=0,
        tools_used=0,
        active_plan_id=None,
        active_step_id=None,
        active_todo_ids=[],
        tool_signature_repeat_window=[],
        completion_attempts=0,
        verify_attempts=0,
        self_check_attempts=0,
        completion_outcomes=[],
        verify_outcomes=[],
        self_check_outcomes=[],
        evidence_refs=[],
        provider_snapshot=dict(effective.get("provider_snapshot", {})),
        model_snapshot=dict(effective.get("model_snapshot", {})),
        fallback_attempts=[],
        last_error=None,
        pending_tool_results={},
        committed_tool_results={},
        gate_config=GateConfigV1(max_turns=min(proposal_budget, DEFAULT_PROPOSAL_TURNS)),
        gate_state=GateStateV1(started_at=started_at),
        convergence=ConvergenceStateV1(),
    )

    return StatePatch(
        {
            "values": {
                **copy.deepcopy(dict(state.get("values", {}))),
                "request": request,
                "proposal_state": proposal_state.to_dict(),
                "phase": "execution",
                "workflow_status": "running",
            },
            "loop_counters": {"proposal_turns": 0, "fix_rounds": 0},
            "budgets": {
                "proposal_turns": min(DEFAULT_PROPOSAL_TURNS, max(1, proposal_budget)),
                "fix_rounds": min(DEFAULT_FIX_ROUNDS, max(0, fix_budget)),
            },
        }
    )


async def clarify_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Request clarification from user if needed (HITL interrupt point)."""
    del context
    effective = _effective(state)

    if not bool(effective.get("clarification_required", False)):
        return StatePatch(
            {"values": _merged_values(state, {"clarification_status": "not_required"})}
        )

    response = effective.get("clarification_response")
    if response is None:
        # Trigger HITL interrupt
        response = _interrupt(
            {
                "kind": "clarification",
                "question": str(
                    effective.get(
                        "clarification_question",
                        "Please clarify the requested task.",
                    )
                ),
                "options": ["continue", "cancel"],
            }
        )

    # Process response
    if isinstance(response, Mapping):
        if str(response.get("action", "continue")) == "cancel":
            return StatePatch(
                {
                    "values": _merged_values(
                        state,
                        {
                            "workflow_status": "cancelled",
                            "clarification_status": "cancelled",
                        },
                    )
                }
            )
        answer = str(response.get("answer", ""))
    else:
        answer = str(response)

    # Append clarification to messages
    proposal = _proposal_state(effective).to_dict()
    messages = list(proposal["messages"])
    messages.append({"role": "user", "content": answer, "message_id": "clarification-response"})
    proposal["messages"] = messages

    return StatePatch(
        {
            "values": _merged_values(
                state,
                {
                    "clarification_response": answer,
                    "clarification_status": "resolved",
                    "proposal_state": proposal,
                },
            )
        }
    )


async def plan_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Generate execution plan from request or provided steps."""
    del context
    effective = _effective(state)

    if effective.get("workflow_status") == "cancelled":
        return StatePatch(
            {"values": _merged_values(state, {"plan": {}, "todos": [], "todo_intents": []})}
        )

    run_id = str(state.get("run_id", ""))
    request = str(effective.get("request", ""))
    raw_steps = effective.get("plan_steps", [])

    # Generate steps
    if not isinstance(raw_steps, list) or not raw_steps:
        raw_steps = [request]

    steps: list[dict[str, JsonValue]] = []
    for index, raw in enumerate(raw_steps):
        title = str(raw.get("title", "")) if isinstance(raw, Mapping) else str(raw)
        step_id = _stable_id(run_id, index, title, prefix="step")
        steps.append(
            {
                "workflow_step_id": step_id,
                "index": index,
                "title": title,
                "status": "pending",
            }
        )

    plan_id = _stable_id(run_id, request, prefix="plan")

    # Update proposal state with plan
    proposal = _proposal_state(effective).to_dict()
    proposal["active_plan_id"] = plan_id
    proposal["active_step_id"] = str(steps[0]["workflow_step_id"])
    proposal["active_todo_ids"] = [str(item["workflow_step_id"]) for item in steps]

    return StatePatch(
        {
            "values": _merged_values(
                state,
                {
                    "plan": {"plan_id": plan_id, "steps": steps},
                    "todos": steps,
                    "todo_intents": _todo_intents(run_id, steps),
                    "proposal_state": proposal,
                },
            )
        }
    )


async def wait_approval_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Wait for user approval of plan (HITL interrupt point)."""
    del context
    effective = _effective(state)

    if effective.get("workflow_status") == "cancelled":
        return StatePatch({"values": _merged_values(state, {"approval_status": "cancelled"})})

    if not bool(effective.get("approval_required", True)):
        decision: object = {"approved": True}
    else:
        decision = effective.get("approval_response")
        if decision is None:
            # Trigger HITL interrupt
            decision = _interrupt(
                {
                    "kind": "plan_approval",
                    "plan": copy.deepcopy(effective.get("plan", {})),
                    "options": ["approve", "revise", "cancel"],
                }
            )

    # Process decision
    approved = (
        bool(decision.get("approved", False)) if isinstance(decision, Mapping) else bool(decision)
    )
    action = (
        str(decision.get("action", "approve" if approved else "cancel"))
        if isinstance(decision, Mapping)
        else ("approve" if approved else "cancel")
    )

    if approved or action == "approve":
        status = "approved"
        workflow_status = "running"
    elif action == "revise":
        status = "revision_required"
        workflow_status = "blocked"
    else:
        status = "cancelled"
        workflow_status = "cancelled"

    updates: dict[str, JsonValue] = {
        "approval_response": copy.deepcopy(decision),
        "approval_status": status,
        "workflow_status": workflow_status,
    }

    # Add execution discipline message if approved
    if status == "approved":
        proposal = _proposal_state(effective).to_dict()
        messages = list(proposal["messages"])
        messages.append(
            {
                "role": "system",
                "content": (
                    "The user approved the displayed plan. Continue executing the "
                    "remaining steps now; do not ask for plan approval again. Tool-level "
                    "permission prompts, if any, are handled separately by the host."
                ),
                "message_id": "workflow-plan-approved",
            }
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "Use the proposal-turn budget for execution, not repeated "
                    "discovery. Keep each discovery query short and specific. "
                    "After one useful search result, copy its exact capability_id "
                    "and immediately follow the returned lifecycle (describe, "
                    "activate, then call); do not repeat semantically equivalent "
                    "searches. Treat tool results as authoritative. Once relevant "
                    "inputs or files are located, perform the requested action and "
                    "then a real verification, reserving at least one turn for each. "
                    "Discovery and workspace-setup receipts do not prove task "
                    "completion."
                ),
                "message_id": "durable-task-execution-discipline",
            }
        )
        proposal["messages"] = messages

        # Mark approval steps completed
        todos = _todo_items(effective)
        for item in todos:
            if _APPROVAL_STEP.search(str(item.get("title", ""))):
                item["status"] = "completed"

        updates.update(
            {
                "proposal_state": proposal,
                "todos": todos,
                "todo_intents": _todo_intents(str(state.get("run_id", "")), todos),
            }
        )

    return StatePatch({"values": _merged_values(state, updates)})


async def approval_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Route based on approval decision."""
    del context
    return "approved" if _effective(state).get("approval_status") == "approved" else "finalize"


async def llm_proposal_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Generate LLM proposal with tool calls.

    Calls ProposalPort to generate next proposal.
    Enforces budget limits and filters forbidden/unsupported tools.
    """
    effective = _effective(state)
    proposal_state = _proposal_state(effective)
    budgets = state.get("budgets", {})
    proposal_budget = (
        int(budgets.get("proposal_turns", DEFAULT_PROPOSAL_TURNS))
        if isinstance(budgets, Mapping)
        else DEFAULT_PROPOSAL_TURNS
    )

    # Check budget
    if proposal_state.proposal_turns_used >= proposal_budget:
        return StatePatch(
            {
                "values": _merged_values(
                    state,
                    {
                        "workflow_status": "blocked",
                        "blocked_reason": "proposal_budget_exhausted",
                    },
                )
            }
        )

    # Call ProposalPort
    try:
        proposal_port: ProposalPort = context.port("proposal")
        if context.identity is not None and hasattr(proposal_port, "propose_for_execution"):
            outcome_raw = await proposal_port.propose_for_execution(
                proposal_state, execution_identity=context.identity
            )
        else:
            outcome_raw = await proposal_port.propose(proposal_state)
    except Exception as exc:
        if not _provider_failure_is_retryable(exc):
            raise WorkflowNodeError(
                code=WorkflowErrorCode.PERMANENT,
                message_ref=_provider_failure_message_ref(exc),
                node_id="llm_proposal",
            ) from exc
        raise WorkflowNodeError(
            code=WorkflowErrorCode.RETRYABLE_PROVIDER,
            message_ref="workflow_node:llm_proposal:retryable_provider",
            node_id="llm_proposal",
        ) from exc

    # Normalize outcome
    if not isinstance(outcome_raw, ProposalOutcomeV1):
        if not isinstance(outcome_raw, Mapping):
            raise TypeError("ProposalPort.propose must return ProposalOutcomeV1 or its JSON form")
        outcome = ProposalOutcomeV1.from_dict(outcome_raw)
    else:
        outcome = outcome_raw

    # Update proposal state
    proposal = proposal_state.to_dict()
    if outcome.compacted_messages is not None:
        proposal["messages"] = [dict(message) for message in outcome.compacted_messages]
        proposal["compaction_summary"] = outcome.compaction_summary
        proposal["compaction_ref"] = outcome.compaction_ref
    proposal["token_estimate"] = outcome.token_estimate
    proposal["iteration"] = proposal_state.iteration + 1
    proposal["proposal_turns_used"] = proposal_state.proposal_turns_used + 1
    proposal["fix_rounds_used"] = proposal_state.fix_rounds_used
    proposal["gate_state"] = {
        **proposal_state.gate_state.to_dict(),
        "turns_used": proposal_state.gate_state.turns_used + 1,
        "last_transition": "llm_proposal",
    }
    proposal["last_error"] = outcome.error.to_dict() if outcome.error else None

    # Filter forbidden/unsupported tools via CapabilityCatalogPort
    catalog_port: CapabilityCatalogPort | None = context.ports.get("capability_catalog")
    forbidden_ids: list[str] = []
    unsupported_ids: list[str] = []

    for call in outcome.prepared_calls:
        if call.tool_name in FORBIDDEN_DURABLE_TOOLS:
            forbidden_ids.append(call.stable_call_id)
            continue

        # Check if tool is in catalog
        if catalog_port is not None:
            is_supported = await catalog_port.is_capability_available(call.tool_name)
            if not is_supported:
                unsupported_ids.append(call.stable_call_id)

    # Reject forbidden/unsupported calls
    rejected = {
        call_id: {
            "stable_call_id": call_id,
            "status": "failed",
            "code": "unsupported_in_durable_workflow",
        }
        for call_id in forbidden_ids + unsupported_ids
    }
    proposal["committed_tool_results"] = {
        **dict(proposal["committed_tool_results"]),
        **rejected,
    }

    # Check for dynamic tools needing review
    review: list[dict[str, JsonValue]] = []
    for call in outcome.prepared_calls:
        if call.stable_call_id in rejected:
            continue
        # Check if dynamic source
        if catalog_port is not None:
            source = await catalog_port.get_capability_source(call.tool_name)
            if source in DYNAMIC_SOURCES or call.tool_name.startswith(("plugin:", "mcp:")):
                policy = await catalog_port.get_capability_policy(call.tool_name)
                # Check if policy is sufficient for safe execution
                if (
                    not policy
                    or not policy.get("lifecycle_hash")
                    or not policy.get("outcome_parser_hash")
                ):
                    review.append(
                        {
                            "stable_call_id": call.stable_call_id,
                            "tool_name": call.tool_name,
                            "source": source or "unknown_dynamic",
                            "reason": "unsupported_dynamic_tool",
                        }
                    )

    counters = {
        "proposal_turns": int(proposal["proposal_turns_used"]),
        "fix_rounds": int(proposal["fix_rounds_used"]),
    }

    return StatePatch(
        {
            "values": _merged_values(
                state,
                {
                    "proposal_state": proposal,
                    "proposal_outcome": outcome.to_dict(),
                    "dispatch_call_ids": [
                        call.stable_call_id
                        for call in outcome.prepared_calls
                        if call.stable_call_id not in rejected
                    ],
                    "dynamic_tool_review": review,
                },
            ),
            "loop_counters": counters,
        }
    )


def _result_success(value: object) -> bool:
    """Check if tool result indicates success."""
    if not isinstance(value, Mapping):
        return False
    return (
        bool(value.get("ok", False))
        or value.get("state") == "success"
        or value.get("status") in {"success", "completed", "committed"}
        or value.get("domain_status") == "success"
    )


def _consecutive_discovery_searches(proposal_state: ProposalStateV1) -> int:
    """Count consecutive discovery search tool calls."""
    count = 0
    for result in reversed(list(proposal_state.committed_tool_results.values())):
        if not isinstance(result, Mapping):
            break
        if str(result.get("tool_name", "")) not in _REPEATABLE_DISCOVERY_SEARCH_TOOL_NAMES:
            break
        count += 1
    return count


def _consecutive_failed_describes(proposal_state: ProposalStateV1) -> int:
    """Count consecutive failed tool_describe calls."""
    count = 0
    for result in reversed(list(proposal_state.committed_tool_results.values())):
        if not isinstance(result, Mapping):
            break
        if str(result.get("tool_name", "")) != "tool_describe":
            break
        if _result_success(result):
            break
        count += 1
    return count


def _drop_superseded_discovery_search_messages(
    messages: Sequence[Mapping[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    """Keep only newest search result to avoid quadratic prompt growth."""
    removed_call_ids: set[str] = set()
    compacted: list[dict[str, JsonValue]] = []

    for message in messages:
        copied = copy.deepcopy(dict(message))
        if str(copied.get("role") or "") == "assistant":
            raw_calls = copied.get("tool_calls")
            if isinstance(raw_calls, list) and raw_calls:
                names: set[str] = set()
                call_ids: set[str] = set()
                for raw_call in raw_calls:
                    if not isinstance(raw_call, Mapping):
                        names.add("")
                        continue
                    function = raw_call.get("function")
                    names.add(
                        str(function.get("name") or "") if isinstance(function, Mapping) else ""
                    )
                    call_ids.add(str(raw_call.get("id") or ""))
                if names and names.issubset(_REPEATABLE_DISCOVERY_SEARCH_TOOL_NAMES):
                    removed_call_ids.update(call_ids)
                    continue
        if (
            str(copied.get("role") or "") == "tool"
            and str(copied.get("tool_call_id") or "") in removed_call_ids
        ):
            continue
        compacted.append(copied)

    return compacted


async def tool_execution_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Execute approved tool calls (HITL interrupt point).

    Handles authorization, dynamic tool review, convergence limits,
    and tool result normalization.
    """
    effective = _effective(state)

    if effective.get("workflow_status") in {"blocked", "cancelled"}:
        return StatePatch({"values": _merged_values(state, {})})

    outcome = _outcome(effective)
    proposal_state = _proposal_state(effective)
    dispatch_ids = {str(value) for value in effective.get("dispatch_call_ids", [])}
    calls = [call for call in outcome.prepared_calls if call.stable_call_id in dispatch_ids]

    # Build authorization grants for approved mutating calls
    authorizations: dict[str, JsonValue] = {}
    if effective.get("approval_status") == "approved":
        for call in calls:
            if call.effect_type != "idempotent_read":
                authorizations[call.stable_call_id] = {"action": "allow_once_opaque"}

    # Handle dynamic tool review
    review = effective.get("dynamic_tool_review", [])
    if isinstance(review, list) and review:
        decision = _interrupt(
            {
                "kind": "unsupported_dynamic_tool",
                "tools": copy.deepcopy(review),
                "options": ["allow_once_opaque", "continue_without", "cancel"],
            }
        )
        action = (
            str(decision.get("action", "cancel"))
            if isinstance(decision, Mapping)
            else str(decision)
        )
        review_ids = {str(item["stable_call_id"]) for item in review}

        if action == "cancel":
            return StatePatch(
                {
                    "values": _merged_values(
                        state,
                        {
                            "workflow_status": "cancelled",
                            "cancel_reason": "dynamic_tool_rejected",
                        },
                    )
                }
            )
        if action == "continue_without":
            calls = [call for call in calls if call.stable_call_id not in review_ids]
            reviewed_by_id = {str(item["stable_call_id"]): item for item in review}
            skipped = {
                call_id: {
                    "stable_call_id": call_id,
                    "tool_name": str(reviewed_by_id[call_id]["tool_name"]),
                    "status": "failed",
                    "code": "unsupported_dynamic_tool",
                    "retryable": False,
                }
                for call_id in review_ids
            }
            proposal_payload = proposal_state.to_dict()
            proposal_payload["committed_tool_results"] = {
                **dict(proposal_payload["committed_tool_results"]),
                **skipped,
            }
            proposal_state = ProposalStateV1.from_dict(proposal_payload)
        elif action == "allow_once_opaque":
            authorizations.update(
                {
                    call_id: {
                        "action": "allow_once_opaque",
                        "effect_type": "opaque_manual",
                    }
                    for call_id in review_ids
                }
            )

    # Enforce convergence limits
    blocked_search_ids: set[str] = set()
    if (
        calls
        and all(call.tool_name in _REPEATABLE_DISCOVERY_SEARCH_TOOL_NAMES for call in calls)
        and _consecutive_discovery_searches(proposal_state) >= _MAX_CONSECUTIVE_DISCOVERY_SEARCHES
    ):
        blocked_search_ids = {call.stable_call_id for call in calls}

    blocked_describe_ids: set[str] = set()
    if (
        calls
        and all(call.tool_name == "tool_describe" for call in calls)
        and _consecutive_failed_describes(proposal_state) >= _MAX_CONSECUTIVE_FAILED_DESCRIBES
    ):
        blocked_describe_ids = {call.stable_call_id for call in calls}

    dispatchable_calls = [
        call
        for call in calls
        if call.stable_call_id not in blocked_search_ids
        and call.stable_call_id not in blocked_describe_ids
    ]

    # Generate error results for blocked calls
    raw_results: dict[str, JsonValue] = {
        call.stable_call_id: {
            "ok": False,
            "status": "failed",
            "code": "discovery_loop_blocked",
            "message": (
                "The durable workflow already used three consecutive search "
                "turns. Use an exact capability_id and next_action from the "
                "latest result, or execute an already discovered capability."
            ),
        }
        for call in calls
        if call.stable_call_id in blocked_search_ids
    }
    raw_results.update(
        {
            call.stable_call_id: {
                "ok": False,
                "status": "failed",
                "code": "capability_describe_loop_blocked",
                "message": (
                    "The durable workflow already made two consecutive failed "
                    "tool_describe calls. Stop guessing capability ids. Call "
                    "tool_search with a short query and copy one returned full "
                    "capability_id exactly."
                ),
            }
            for call in calls
            if call.stable_call_id in blocked_describe_ids
        }
    )

    # Dispatch executable calls via WorkspacePort
    if dispatchable_calls:
        workspace_port: WorkspacePort = context.port("workspace")
        auth_port: AuthorizationPort | None = context.ports.get("authorization")

        # Apply authorizations
        if auth_port is not None:
            for call_id, auth in authorizations.items():
                await auth_port.grant_authorization(call_id, auth)

        dispatched = await workspace_port.execute_tools(
            dispatchable_calls,
            workflow_step_id=str(proposal_state.active_step_id or ""),
            prior_results=proposal_state.committed_tool_results,
        )
        if not isinstance(dispatched, Mapping):
            raise TypeError(
                "WorkspacePort.execute_tools must return mapping keyed by stable_call_id"
            )
        raw_results.update(dispatched)

    # Normalize results
    call_by_id = {call.stable_call_id: call for call in calls}
    normalized: dict[str, JsonValue] = {}
    for call_id, raw in raw_results.items():
        call = call_by_id.get(str(call_id))
        if call is None:
            raise ValueError(f"dispatch returned unknown call id: {call_id}")
        payload = (
            copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {"result": copy.deepcopy(raw)}
        )
        payload.update(
            {
                "stable_call_id": call.stable_call_id,
                "tool_name": call.tool_name,
                "workflow_step_id": str(proposal_state.active_step_id or ""),
                "args_hash": call.args_hash,
            }
        )
        validate_json_value(payload)
        normalized[call.stable_call_id] = payload

    missing = [call.stable_call_id for call in calls if call.stable_call_id not in normalized]
    if missing:
        raise ValueError(f"dispatch omitted prepared call results: {', '.join(missing)}")

    # Update proposal state with results
    proposal = proposal_state.to_dict()
    committed = {**dict(proposal["committed_tool_results"]), **normalized}
    proposal["committed_tool_results"] = committed
    proposal["pending_tool_results"] = {}
    proposal["tools_used"] = proposal_state.tools_used + len(calls)
    proposal["gate_state"] = {
        **proposal_state.gate_state.to_dict(),
        "tools_used": proposal_state.gate_state.tools_used + len(calls),
        "last_transition": "tool_execution",
    }

    # Compact messages (drop superseded searches)
    messages = list(proposal["messages"])
    if (
        calls
        and not blocked_search_ids
        and all(call.tool_name in _REPEATABLE_DISCOVERY_SEARCH_TOOL_NAMES for call in calls)
    ):
        messages = _drop_superseded_discovery_search_messages(messages)

    # Append assistant + tool messages
    if calls:
        raw_by_id = {
            str(item.get("stable_call_id", "")): item for item in outcome.raw_tool_proposals
        }
        messages.append(
            {
                "role": "assistant",
                "content": outcome.assistant_content,
                "tool_calls": [
                    {
                        "id": call.stable_call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": canonical_json(
                                raw_by_id.get(call.stable_call_id, {}).get(
                                    "raw_params", call.arguments_json()
                                )
                            ),
                        },
                    }
                    for call in calls
                ],
            }
        )
    elif outcome.assistant_content:
        messages.append({"role": "assistant", "content": outcome.assistant_content})

    for call in calls:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.stable_call_id,
                "name": call.tool_name,
                "content": canonical_json(normalized[call.stable_call_id]),
            }
        )

    # Add error feedback if proposal rejected
    if outcome.error is not None:
        messages.append(
            {
                "role": "system",
                "content": canonical_json(
                    {
                        "type": "host_tool_proposal_rejected",
                        "error": outcome.error.to_dict(),
                        "instruction": (
                            "Do not repeat the rejected call unchanged. "
                            "Follow the error recovery details, then continue "
                            "the existing task."
                        ),
                    }
                ),
            }
        )

    proposal["messages"] = messages

    # Update todo status based on execution
    todos = _todo_items(effective)
    all_success = bool(calls) and all(
        _result_success(normalized[call.stable_call_id]) for call in calls
    )

    # Check for evidence-backed completion via ArtifactPort
    artifact_port: ArtifactPort | None = context.ports.get("artifact")
    evidence_backed_complete = False
    if artifact_port is not None:
        evidence_backed_complete = await artifact_port.check_completion_evidence(
            proposal_state, outcome
        )

    # Determine if current step is complete
    discovery_only_tools = {
        "capability_search",
        "tool_search",
        "list_directory",
        "read_file",
        "workspace_prepare",
    }
    step_receipt_backed = all_success and any(
        call.tool_name not in discovery_only_tools for call in calls
    )

    active_step = proposal_state.active_step_id
    if evidence_backed_complete:
        for item in todos:
            item["status"] = "completed"
        proposal["active_step_id"] = None
    elif step_receipt_backed and active_step:
        for item in todos:
            if item.get("workflow_step_id") == active_step:
                item["status"] = "completed"
                break
        next_todo = next((item for item in todos if item.get("status") != "completed"), None)
        proposal["active_step_id"] = str(next_todo["workflow_step_id"]) if next_todo else None

    run_id = str(state.get("run_id", ""))
    return StatePatch(
        {
            "values": _merged_values(
                state,
                {
                    "proposal_state": proposal,
                    "tool_results": normalized,
                    "todos": todos,
                    "todo_intents": _todo_intents(run_id, todos),
                    "dynamic_tool_review": [],
                },
            )
        }
    )


async def completion_decision_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Decide next action: loop/test/audit/finalize.

    Routes based on:
    - Workflow status (cancelled/blocked)
    - Proposal budget exhaustion
    - Pending tool calls
    - Incomplete todos
    """
    effective = _effective(state)
    proposal_state = _proposal_state(effective)
    outcome = _outcome(effective)
    todos = _todo_items(effective)
    incomplete = [
        str(item["workflow_step_id"]) for item in todos if item.get("status") != "completed"
    ]

    budgets = state.get("budgets", {})
    budget = (
        int(budgets.get("proposal_turns", DEFAULT_PROPOSAL_TURNS))
        if isinstance(budgets, Mapping)
        else DEFAULT_PROPOSAL_TURNS
    )

    # Determine route
    if effective.get("workflow_status") in {"cancelled", "blocked"}:
        decision: dict[str, JsonValue] = {
            "route": "finalize",
            "reason": str(effective.get("blocked_reason", "cancelled")),
            "incomplete_todo_ids": incomplete,
        }
    elif proposal_state.proposal_turns_used >= budget:
        decision = {
            "route": "audit",
            "reason": "proposal_budget_exhausted",
            "incomplete_todo_ids": incomplete,
        }
    elif outcome.prepared_calls:
        decision = {
            "route": "loop",
            "reason": "tool_results_available",
            "incomplete_todo_ids": incomplete,
        }
    elif incomplete:
        decision = {
            "route": "loop",
            "reason": "incomplete_todos",
            "incomplete_todo_ids": incomplete,
        }
    else:
        decision = {
            "route": "test",
            "reason": "proposal_complete",
            "incomplete_todo_ids": [],
        }

    # Allow ArtifactPort to override decision
    artifact_port: ArtifactPort | None = context.ports.get("artifact")
    if artifact_port is not None and hasattr(artifact_port, "completion_decision"):
        override = await artifact_port.completion_decision(copy.deepcopy(decision), proposal_state)
        if isinstance(override, Mapping):
            decision.update(copy.deepcopy(dict(override)))

    route = str(decision.get("route", "loop"))
    if incomplete and route == "test":
        route = "loop" if proposal_state.proposal_turns_used < budget else "audit"
        decision["route"] = route
        decision["reason"] = "incomplete_todos"

    # Update proposal state
    proposal = proposal_state.to_dict()
    proposal["completion_attempts"] = proposal_state.completion_attempts + 1
    proposal["completion_outcomes"] = [*proposal_state.completion_outcomes, decision]

    return StatePatch(
        {
            "values": _merged_values(
                state,
                {
                    "proposal_state": proposal,
                    "completion_decision": decision,
                    "next_route": route,
                },
            )
        }
    )


async def completion_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Route from completion_decision node."""
    del context
    route = str(_effective(state).get("next_route", "loop"))
    return route if route in {"loop", "test", "audit", "finalize"} else "loop"


async def test_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Run tests on workflow output.

    Uses ArtifactPort to verify output quality and evidence.
    """
    effective = _effective(state)
    proposal_state = _proposal_state(effective)

    # Run tests via ArtifactPort
    artifact_port: ArtifactPort | None = context.ports.get("artifact")
    if artifact_port is not None and hasattr(artifact_port, "run_tests"):
        raw = await artifact_port.run_tests(proposal_state)
        if not isinstance(raw, Mapping):
            raise TypeError("ArtifactPort.run_tests must return a JSON object")
        result = copy.deepcopy(dict(raw))
    else:
        # Default: check if we have successful tool receipts
        successful_receipts = [
            result
            for result in proposal_state.committed_tool_results.values()
            if isinstance(result, Mapping) and _result_success(result)
        ]
        result = {
            "passed": bool(successful_receipts),
            "evidence_refs": [
                str(receipt.get("stable_call_id", ""))
                for receipt in successful_receipts
                if str(receipt.get("stable_call_id", ""))
            ],
            "source": "durable_tool_receipts",
        }

    validate_json_value(result)
    evidence = [str(value) for value in result.get("evidence_refs", [])]

    # Update proposal state
    proposal = proposal_state.to_dict()
    proposal["verify_attempts"] = proposal_state.verify_attempts + 1
    proposal["verify_outcomes"] = [*proposal_state.verify_outcomes, result]
    proposal["evidence_refs"] = list(dict.fromkeys([*proposal_state.evidence_refs, *evidence]))

    return StatePatch(
        {"values": _merged_values(state, {"proposal_state": proposal, "test_result": result})}
    )


async def audit_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Audit output quality and decide fix or finalize.

    Checks:
    - Test results
    - Incomplete todos
    - Remaining budgets for fix rounds
    """
    effective = _effective(state)
    proposal_state = _proposal_state(effective)
    todos = _todo_items(effective)
    incomplete = [
        str(item["workflow_step_id"]) for item in todos if item.get("status") != "completed"
    ]

    test_result = effective.get("test_result", {})
    test_passed = isinstance(test_result, Mapping) and bool(test_result.get("passed", False))

    # Build audit result
    audit: dict[str, JsonValue] = {
        "passed": test_passed and not incomplete,
        "test_passed": test_passed,
        "incomplete_todo_ids": incomplete,
        "reason": (
            "complete"
            if test_passed and not incomplete
            else ("incomplete_todos" if incomplete else "tests_failed")
        ),
    }

    if isinstance(test_result, Mapping):
        if isinstance(test_result.get("output_contract"), Mapping):
            audit["output_contract"] = copy.deepcopy(dict(test_result["output_contract"]))
        if test_result.get("failure_code"):
            audit["failure_code"] = str(test_result["failure_code"])

    # Allow ArtifactPort to override audit
    artifact_port: ArtifactPort | None = context.ports.get("artifact")
    if artifact_port is not None and hasattr(artifact_port, "audit"):
        override = await artifact_port.audit(copy.deepcopy(audit), proposal_state)
        if isinstance(override, Mapping):
            audit.update(copy.deepcopy(dict(override)))

    # Force incomplete todos to fail audit
    if incomplete:
        audit["passed"] = False
        audit["reason"] = "incomplete_todos"

    # Check if we can fix
    budgets = state.get("budgets", {})
    proposal_budget = (
        int(budgets.get("proposal_turns", DEFAULT_PROPOSAL_TURNS))
        if isinstance(budgets, Mapping)
        else DEFAULT_PROPOSAL_TURNS
    )
    fix_budget = (
        int(budgets.get("fix_rounds", DEFAULT_FIX_ROUNDS))
        if isinstance(budgets, Mapping)
        else DEFAULT_FIX_ROUNDS
    )
    can_fix = (
        proposal_state.proposal_turns_used < proposal_budget
        and proposal_state.fix_rounds_used < fix_budget
    )

    if bool(audit.get("passed", False)):
        route = "finalize"
        workflow_status = "completed"
    elif can_fix:
        route = "fix"
        workflow_status = "running"
    else:
        route = "finalize"
        workflow_status = "blocked"
        audit["budget_exhausted"] = True

    # Update proposal state for fix round
    proposal = proposal_state.to_dict()
    if route == "fix":
        proposal["fix_rounds_used"] = proposal_state.fix_rounds_used + 1
        repair_step = next(
            (item for item in todos if item.get("status") != "completed"),
            todos[-1] if todos else None,
        )
        if repair_step is not None:
            repair_step["status"] = "pending"
            proposal["active_step_id"] = str(repair_step["workflow_step_id"])

    proposal["self_check_attempts"] = proposal_state.self_check_attempts + 1
    proposal["self_check_outcomes"] = [*proposal_state.self_check_outcomes, audit]

    patch: dict[str, JsonValue] = {
        "values": _merged_values(
            state,
            {
                "proposal_state": proposal,
                "audit_result": audit,
                "phase": "fix" if route == "fix" else effective.get("phase", "execution"),
                "workflow_status": workflow_status,
                "audit_route": route,
                "todos": todos,
                "todo_intents": _todo_intents(str(state.get("run_id", "")), todos),
            },
        )
    }

    if route == "fix":
        patch["loop_counters"] = {
            "proposal_turns": proposal_state.proposal_turns_used,
            "fix_rounds": proposal_state.fix_rounds_used + 1,
        }

    return StatePatch(patch)


async def audit_route(state: WorkflowState, context: WorkflowContext) -> str:
    """Route from audit node."""
    del context
    return "fix" if _effective(state).get("audit_route") == "fix" else "finalize"


async def finalize_handler(state: WorkflowState, context: WorkflowContext) -> StatePatch:
    """Prepare final output and delivery intents.

    Generates workflow report and final assistant message.
    """
    effective = _effective(state)
    run_id = (
        context.identity.run_id if context.identity is not None else str(state.get("run_id", ""))
    )
    if not run_id:
        raise ValueError("workflow run id is missing")

    workflow_status = str(effective.get("workflow_status", "blocked"))
    audit = copy.deepcopy(effective.get("audit_result", {}))
    todos = _todo_items(effective)

    # Get final assistant content
    outcome_raw = effective.get("proposal_outcome")
    assistant_content = ""
    if isinstance(outcome_raw, Mapping):
        assistant_content = str(outcome_raw.get("assistant_content", ""))

    if not assistant_content:
        assistant_content = {
            "completed": "The durable task completed and passed its audit.",
            "cancelled": "The durable task was cancelled before completion.",
        }.get(
            workflow_status,
            "The durable task stopped without claiming completion.",
        )

    # Build summary
    summary: dict[str, JsonValue] = {
        "status": workflow_status,
        "audit": audit if isinstance(audit, dict) else {},
        "todos": todos,
    }

    # Generate delivery intents
    intents: list[dict[str, JsonValue]] = [
        {
            "intent_id": f"{run_id}:workflow-report",
            "kind": "workflow_report",
            "channel": "workflow_report",
            "payload": copy.deepcopy(summary),
        },
        {
            "intent_id": f"{run_id}:final",
            "kind": "final_assistant",
            "channel": "final_assistant",
            "payload": {"text": assistant_content, "workflow": copy.deepcopy(summary)},
        },
    ]

    return StatePatch({"values": {"delivery_intents": intents}})


__all__ = [
    "DEFAULT_FIX_ROUNDS",
    "DEFAULT_PROPOSAL_TURNS",
    "FORBIDDEN_DURABLE_TOOLS",
    "approval_route",
    "audit_handler",
    "audit_route",
    "clarify_handler",
    "completion_decision_handler",
    "completion_route",
    "finalize_handler",
    "intake_handler",
    "llm_proposal_handler",
    "plan_handler",
    "test_handler",
    "tool_execution_handler",
    "wait_approval_handler",
]
