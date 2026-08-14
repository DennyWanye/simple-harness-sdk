# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Output contract validation for durable_task workflow.

Validates that workflow output meets completion criteria and evidence requirements.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from simple_harness.contracts import JsonValue

if TYPE_CHECKING:
    from .state import ProposalOutcomeV1, ProposalStateV1


# Tool classification for evidence validation
_WRITE_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "desktop_create_file",
        "edit_file",
        "file_write",
        "run_command",
        "run_shell",
        "write_file",
    }
)

_TEST_TOOL_NAMES = frozenset(
    {
        "godot_run",
        "godot_run_project",
        "godot_validate_project",
        "run_command",
        "run_shell",
    }
)

_DISCOVERY_ONLY_TOOL_NAMES = frozenset(
    {
        "capability_search",
        "list_directory",
        "memory_read",
        "memory_search",
        "read_file",
        "tool_describe",
        "tool_search",
        "workspace_prepare",
    }
)

# Request pattern detection
_WRITE_EXECUTION_REQUEST = re.compile(
    r"(?:"
    r"\b(?:copy|create|fix|repair|modify|update|write|edit|implement)\b|"
    r"复制|创建|生成|修复|修改|改写|实现"
    r")",
    re.IGNORECASE,
)

_NEGATED_WRITE_EXECUTION_REQUEST = re.compile(
    r"(?:"
    r"(?:不要|不需要|无需|不用|禁止|严禁|别|不).{0,16}"
    r"(?:复制|创建|生成|修复|修改|改写|实现|改动|写入|删除)|"
    r"\b(?:do\s+not|don't|without|must\s+not|never)\b.{0,24}"
    r"\b(?:copy|creat\w*|fix\w*|repair\w*|modif\w*|updat\w*|"
    r"writ\w*|edit\w*|implement\w*|chang\w*|delet\w*)\b"
    r")",
    re.IGNORECASE,
)

_TEST_EXECUTION_REQUEST = re.compile(
    r"(?:"
    r"\bpytest\b|\brun\s+(?:the\s+)?tests?\b|\b(?:launch|run|verify|validate)\b|"
    r"运行.{0,12}测试|执行.{0,12}测试|启动|运行|验证|确认"
    r")",
    re.IGNORECASE,
)

_EXPLICIT_TEST_EXECUTION_REQUEST = re.compile(
    r"(?:\bpytest\b|\brun\s+(?:the\s+)?tests?\b|"
    r"运行.{0,12}测试|执行.{0,12}测试)",
    re.IGNORECASE,
)

_EXPLICIT_TOOL_FREE_REQUEST = re.compile(
    r"(?:"
    r"只(?:需|要)?(?:回复|回答)|仅(?:回复|回答)|直接(?:回复|回答)|"
    r"不(?:要|需)?调用(?:任何)?(?:写)?工具|无需(?:调用)?工具|"
    r"\b(?:only|just)\s+(?:reply|respond|answer)\b|"
    r"\b(?:do\s+not|don't|without)\s+(?:(?:call|use|using)\s+)?(?:any\s+)?tools?\b"
    r")",
    re.IGNORECASE,
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


def _successful_receipts(
    proposal_state: ProposalStateV1,
) -> list[Mapping[str, JsonValue]]:
    """Extract successful tool results from proposal state."""
    return [
        result
        for result in proposal_state.committed_tool_results.values()
        if isinstance(result, Mapping) and _result_success(result)
    ]


def _execution_obligations(request: str) -> tuple[bool, bool]:
    """Determine write/test obligations from request text.

    Returns:
        (requires_write, requires_test) tuple

    Note:
        Negative prohibitions (禁止修改, without editing) are removed before
        detecting positive obligations. This prevents "do not write" from
        creating a write obligation.
    """
    positive_request = _NEGATED_WRITE_EXECUTION_REQUEST.sub(" ", request)
    requires_write = bool(_WRITE_EXECUTION_REQUEST.search(positive_request))
    requires_test = bool(_TEST_EXECUTION_REQUEST.search(positive_request))

    # Only keep test obligation if explicitly requested
    if not requires_write and not _EXPLICIT_TEST_EXECUTION_REQUEST.search(
        positive_request
    ):
        requires_test = False

    return requires_write, requires_test


def validate_tool_free_completion(
    outcome: ProposalOutcomeV1,
    proposal_state: ProposalStateV1,
) -> dict[str, JsonValue]:
    """Validate tool-free completion (read-only requests).

    Returns validation result with 'passed' boolean and optional 'reason'.
    """
    if (
        outcome.stop_reason != "end_turn"
        or outcome.prepared_calls
        or outcome.error is not None
        or not outcome.assistant_content.strip()
    ):
        return {
            "passed": False,
            "reason": "tool_free_completion_requires_clean_end_turn",
        }

    request = proposal_state.original_request.strip()

    # Accept if explicitly tool-free
    if _EXPLICIT_TOOL_FREE_REQUEST.search(request):
        return {"passed": True, "reason": "explicit_tool_free_request"}

    # Fail if write/test obligations exist
    requires_write, requires_test = _execution_obligations(request)
    if requires_write or requires_test:
        return {
            "passed": False,
            "reason": "tool_free_completion_with_execution_obligation",
        }

    return {"passed": True, "reason": "read_only_request"}


def validate_receipt_backed_completion(
    outcome: ProposalOutcomeV1,
    proposal_state: ProposalStateV1,
) -> dict[str, JsonValue]:
    """Validate receipt-backed completion (durable task contract).

    Requires:
    - Clean end_turn with assistant content
    - Successful tool receipts matching request obligations
    - For write+test: distinct receipts for each obligation

    Returns validation result with 'passed', 'reason', 'evidence_refs'.
    """
    if (
        outcome.stop_reason != "end_turn"
        or outcome.prepared_calls
        or outcome.error is not None
        or not outcome.assistant_content.strip()
    ):
        return {
            "passed": False,
            "reason": "receipt_backed_completion_requires_clean_end_turn",
            "evidence_refs": [],
        }

    request = proposal_state.original_request.strip()
    requires_write, requires_test = _execution_obligations(request)
    successful = _successful_receipts(proposal_state)

    # Tool-free requests pass only if explicitly allowed
    if not successful:
        if (
            not requires_write
            and not requires_test
            and _EXPLICIT_TOOL_FREE_REQUEST.search(request)
        ):
            return {"passed": True, "reason": "explicit_tool_free_request", "evidence_refs": []}
        return {
            "passed": False,
            "reason": "no_successful_tool_receipts",
            "evidence_refs": [],
        }

    # No obligations: any success is sufficient
    if not requires_write and not requires_test:
        evidence = [
            str(r.get("stable_call_id", ""))
            for r in successful
            if str(r.get("stable_call_id", ""))
        ]
        return {
            "passed": True,
            "reason": "no_obligations_with_receipts",
            "evidence_refs": evidence,
        }

    # Filter action receipts (exclude discovery-only)
    action_receipts = [
        result
        for result in successful
        if str(result.get("tool_name", "")) not in _DISCOVERY_ONLY_TOOL_NAMES
    ]

    write_receipts = [
        result
        for result in action_receipts
        if str(result.get("tool_name", "")) in _WRITE_TOOL_NAMES
    ]

    test_receipts = [
        result
        for result in action_receipts
        if str(result.get("tool_name", "")) in _TEST_TOOL_NAMES
    ]

    # Check write obligation
    if requires_write and not write_receipts:
        return {
            "passed": False,
            "reason": "write_obligation_not_satisfied",
            "evidence_refs": [],
        }

    # Check test obligation
    if requires_test and not test_receipts:
        return {
            "passed": False,
            "reason": "test_obligation_not_satisfied",
            "evidence_refs": [],
        }

    # Both obligations: require distinct receipts
    if requires_write and requires_test:
        distinct = any(
            str(write.get("stable_call_id", ""))
            and str(test.get("stable_call_id", ""))
            and str(write["stable_call_id"]) != str(test["stable_call_id"])
            for write in write_receipts
            for test in test_receipts
        )
        if not distinct:
            return {
                "passed": False,
                "reason": "write_and_test_obligations_require_distinct_receipts",
                "evidence_refs": [],
            }

    # All obligations satisfied
    evidence = [
        str(r.get("stable_call_id", ""))
        for r in action_receipts
        if str(r.get("stable_call_id", ""))
    ]

    return {
        "passed": True,
        "reason": "all_obligations_satisfied",
        "evidence_refs": evidence,
    }


def validate_output_contract(
    proposal_state: ProposalStateV1,
    outcome: ProposalOutcomeV1,
    *,
    contract_mode: str = "receipt_backed",
) -> dict[str, JsonValue]:
    """Validate workflow output against completion contract.

    Args:
        proposal_state: Current proposal state
        outcome: Latest proposal outcome
        contract_mode: Validation mode - 'receipt_backed' (durable task default)
                      or 'tool_free' (legacy compatibility)

    Returns:
        Validation result with 'passed', 'reason', 'mode', optional 'evidence_refs'
    """
    if contract_mode == "tool_free":
        result = validate_tool_free_completion(outcome, proposal_state)
    else:
        result = validate_receipt_backed_completion(outcome, proposal_state)

    result["mode"] = contract_mode
    return result


__all__ = [
    "validate_output_contract",
    "validate_receipt_backed_completion",
    "validate_tool_free_completion",
]
