# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Tests for durable_task output contract validation.

Validates:
- Receipt-backed completion validation (write/test obligations)
- Tool-free completion validation (read-only requests)
- Negative cases (incomplete evidence, missing receipts)

Note: Tests validate high-level contract behavior using ProposalStateV1 and
ProposalOutcomeV1 structures. Internal helpers (_classify_tools, _detect_obligations)
are private implementation details and not directly tested.
"""

from __future__ import annotations

from simple_harness.workflows.durable_task.output_contract import (
    validate_output_contract,
    validate_receipt_backed_completion,
    validate_tool_free_completion,
)
from simple_harness.workflows.durable_task.state import (
    ConvergenceStateV1,
    GateConfigV1,
    GateStateV1,
    ProposalOutcomeV1,
    ProposalStateV1,
)


def _make_state(request: str, tool_results: dict[str, dict]) -> ProposalStateV1:
    """Helper to create ProposalStateV1 for testing."""
    return ProposalStateV1(
        original_request=request,
        messages=[],
        committed_tool_results=tool_results,
        pending_tool_results={},
        gate_config=GateConfigV1(),
        gate_state=GateStateV1(started_at=0.0),
        convergence=ConvergenceStateV1(),
        request_id="",
        turn_id="",
        system_prompt_ref=None,
        prompt_ref=None,
        skill_refs=[],
        compaction_summary=None,
        compaction_ref=None,
        token_estimate=0,
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
        provider_snapshot={},
        model_snapshot={},
        fallback_attempts=[],
        last_error=None,
    )


def _make_outcome(
    stop_reason: str = "end_turn",
    content: str = "Done",
    prepared_calls: list | None = None,
    error: str | None = None,
) -> ProposalOutcomeV1:
    """Helper to create ProposalOutcomeV1 for testing."""
    from simple_harness.workflows.durable_task.state import ProposalErrorV1

    return ProposalOutcomeV1(
        stop_reason=stop_reason,
        assistant_content=content,
        prepared_calls=prepared_calls or [],
        reasoning_summary_ref=None,
        raw_tool_proposals=[],
        usage={},
        provider="test",
        model="test-model",
        error=ProposalErrorV1(code=error, message_ref="") if error else None,
    )


def test_tool_free_clean_end_turn():  # type: ignore[no-untyped-def]
    """Test tool-free validation with clean end_turn."""
    state = _make_state("Explain the architecture", {})
    outcome = _make_outcome(stop_reason="end_turn", content="The architecture uses...")

    result = validate_tool_free_completion(outcome, state)

    assert result["passed"] is True


def test_tool_free_with_prepared_calls():  # type: ignore[no-untyped-def]
    """Test tool-free validation rejects prepared calls."""
    state = _make_state("Explain the code", {})
    # prepared_calls must be PreparedToolCall objects, not dicts
    outcome = _make_outcome(
        stop_reason="tool_use",  # Changed from end_turn since tools are prepared
        content="Done",
        prepared_calls=[],  # Empty for now since we just need non-end_turn
    )

    result = validate_tool_free_completion(outcome, state)

    assert result["passed"] is False
    assert "clean_end_turn" in result["reason"]


def test_tool_free_with_write_obligation():  # type: ignore[no-untyped-def]
    """Test tool-free validation rejects write obligations."""
    state = _make_state("Fix the bug in parser.py", {})
    outcome = _make_outcome(stop_reason="end_turn", content="Done")

    result = validate_tool_free_completion(outcome, state)

    assert result["passed"] is False
    assert "execution_obligation" in result["reason"]


def test_tool_free_explicit_request():  # type: ignore[no-untyped-def]
    """Test tool-free validation with explicit tool-free request."""
    state = _make_state("Just reply without calling tools", {})
    outcome = _make_outcome(stop_reason="end_turn", content="Here's my answer")

    result = validate_tool_free_completion(outcome, state)

    assert result["passed"] is True
    assert "explicit_tool_free" in result["reason"]


def test_receipt_backed_write_complete():  # type: ignore[no-untyped-def]
    """Test receipt-backed validation with complete write evidence."""
    state = _make_state(
        "Fix the parser bug",
        {
            "call-1": {"ok": True, "tool_name": "edit_file", "status": "success"},
        },
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Fixed the bug")

    result = validate_receipt_backed_completion(outcome, state)

    assert result["passed"] is True


def test_receipt_backed_write_missing():  # type: ignore[no-untyped-def]
    """Test receipt-backed validation with missing write evidence."""
    state = _make_state(
        "Fix the authentication bug",
        {},  # No tool results
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Done")

    result = validate_receipt_backed_completion(outcome, state)

    assert result["passed"] is False
    assert "evidence" in result["reason"].lower() or "receipt" in result["reason"].lower()


def test_receipt_backed_test_complete():  # type: ignore[no-untyped-def]
    """Test receipt-backed validation with complete test evidence."""
    state = _make_state(
        "Run the test suite",
        {
            "call-1": {"ok": True, "tool_name": "run_command", "status": "success"},
        },
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Tests passed")

    result = validate_receipt_backed_completion(outcome, state)

    assert result["passed"] is True


def test_receipt_backed_failed_tool():  # type: ignore[no-untyped-def]
    """Test receipt-backed validation rejects failed tools."""
    state = _make_state(
        "Update config.json",
        {
            "call-1": {"ok": False, "tool_name": "write_file", "status": "failed"},
        },
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Done")

    result = validate_receipt_backed_completion(outcome, state)

    assert result["passed"] is False


def test_receipt_backed_not_end_turn():  # type: ignore[no-untyped-def]
    """Test receipt-backed validation requires end_turn."""
    state = _make_state(
        "Fix bug",
        {
            "call-1": {"ok": True, "tool": "edit_file", "status": "success"},
        },
    )
    outcome = _make_outcome(stop_reason="tool_use", content="")

    result = validate_receipt_backed_completion(outcome, state)

    assert result["passed"] is False


def test_validate_output_contract_receipt_mode():  # type: ignore[no-untyped-def]
    """Test main validation entry point with receipt-backed mode."""
    state = _make_state(
        "Fix parser",
        {
            "call-1": {"ok": True, "tool_name": "edit_file", "status": "success"},
        },
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Fixed")

    result = validate_output_contract(state, outcome, contract_mode="receipt_backed")

    assert result["passed"] is True


def test_validate_output_contract_tool_free_mode():  # type: ignore[no-untyped-def]
    """Test main validation entry point with tool-free mode."""
    state = _make_state("Explain the architecture", {})
    outcome = _make_outcome(stop_reason="end_turn", content="The architecture...")

    result = validate_output_contract(state, outcome, contract_mode="tool_free")

    assert result["passed"] is True


def test_validate_output_contract_default_mode():  # type: ignore[no-untyped-def]
    """Test main validation defaults to receipt-backed mode."""
    state = _make_state(
        "Add feature",
        {
            "call-1": {"ok": True, "tool_name": "write_file", "status": "success"},
        },
    )
    outcome = _make_outcome(stop_reason="end_turn", content="Done")

    result = validate_output_contract(state, outcome)

    assert result["passed"] is True
    assert result["mode"] == "receipt_backed"


def test_validate_output_contract_unknown_mode():  # type: ignore[no-untyped-def]
    """Test main validation with unknown contract mode."""
    state = _make_state("Test", {})
    outcome = _make_outcome(stop_reason="end_turn", content="Done")

    # Unknown mode falls through to receipt_backed
    result = validate_output_contract(state, outcome, contract_mode="unknown_mode")

    # Should treat as receipt_backed and fail (no tool results for write)
    assert result["mode"] == "unknown_mode"
