# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Versioned search policy and selection identities; no model verdict is authority."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..contracts import ContractError, Task
from ..contracts.models import sha256_hex
from ..verification.assessments import mission_contract_revision, task_contract_revision

COMPARE = "COMPARE_THEN_SYNTHESIZE"
FIRST = "FIRST_VERIFIED"
SELECTION_VERSION = "candidate-selection-v1"
POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "max_candidates",
        "deadline_seconds",
        "tie_break",
        "synthesis_limit",
        "on_deadline",
        "synthesis_reserve",
        "synthesis_attempts_reserved",
    }
)


def validate_selection_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != POLICY_FIELDS:
        raise ContractError("selection policy must carry exactly the versioned fields")
    result = dict(value)
    for name, lo, hi in (
        ("schema_version", 1, 1),
        ("max_candidates", 1, 3),
        ("synthesis_limit", 0, 1),
        ("synthesis_attempts_reserved", 0, 1),
    ):
        if type(result[name]) is not int or not lo <= result[name] <= hi:
            raise ContractError(f"invalid selection {name}")
    if result["mode"] not in {FIRST, COMPARE}:
        raise ContractError("unknown selection mode")
    deadline = result["deadline_seconds"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
        raise ContractError("selection deadline must be finite and positive")
    if result["tie_break"] != "verified_rank_then_result_id-v1":
        raise ContractError("unknown selection tie break")
    if result["on_deadline"] != "best_complete_else_stop":
        raise ContractError("unknown selection deadline action")
    reserve = result["synthesis_reserve"]
    if not isinstance(reserve, Mapping) or set(reserve) != {"tokens", "cost_micros", "tool_calls"}:
        raise ContractError("invalid synthesis reserve")
    if any(type(v) is not int or v < 0 for v in reserve.values()):
        raise ContractError("invalid synthesis reserve amount")
    if result["synthesis_attempts_reserved"] != result["synthesis_limit"]:
        raise ContractError("synthesis must reserve its actual attempt slot")
    if result["synthesis_limit"] and (result["max_candidates"] < 2 or reserve["tokens"] <= 0):
        raise ContractError("synthesis needs two candidates and a positive tail reserve")
    result["synthesis_reserve"] = dict(reserve)
    return result


def task_contract(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "kind": task.kind,
        "goal": task.goal,
        "rationale": task.rationale,
        "success_criteria": list(task.success_criteria),
        "verification_policy": list(task.verification_policy),
        "outputs": list(task.outputs),
    }


def selection_revision(store: Any, task: Task) -> str:
    mission = store.get_mission(task.mission_id)
    return sha256_hex(
        {
            "version": SELECTION_VERSION,
            "task_contract_revision": task_contract_revision(task_contract(task)),
            "mission_contract_revision": mission_contract_revision(mission),
            "constraints": {
                "allowed_tools": list(task.allowed_tools),
                "dependencies": list(task.dependency_ids),
                "context": dict(task.context),
                "budget": task.budget.to_json(),
            },
            "domain": store.get_mission_domain(task.mission_id),
            "policy": store.get_mission_policy(task.mission_id),
        }
    )


def candidate_path(result_id: str, path: str) -> str:
    return f"candidate-inputs/{sha256_hex(result_id)}/{path}"


def selection_snapshot(store: Any, mission_id: str) -> dict[str, Any]:
    """Read-only public projection; old libraries have no selection records."""
    import json

    if not store.has_table("selection_rounds"):
        return {"binding": None, "rounds": [], "candidates": [], "decisions": []}
    binding = store.connection.execute(
        "SELECT json FROM search_bindings WHERE mission_id=?", (mission_id,)
    ).fetchone()
    return {
        "binding": None if binding is None else json.loads(binding[0]),
        "rounds": [
            json.loads(r[0])
            for r in store.connection.execute(
                "SELECT json FROM selection_rounds WHERE mission_id=? ORDER BY task_id",
                (mission_id,),
            )
        ],
        "candidates": [
            json.loads(r[0])
            for r in store.connection.execute(
                "SELECT c.json FROM selection_candidates c JOIN selection_rounds r "
                "ON c.round_id=r.round_id "
                "WHERE r.mission_id=? ORDER BY c.result_id",
                (mission_id,),
            )
        ],
        "decisions": [
            json.loads(row[0])
            for row in store.connection.execute(
                "SELECT c.receipt_json FROM commit_receipts c JOIN tasks t "
                "ON t.task_id=c.subject_id WHERE t.mission_id=? AND c.kind='selection_decision' "
                "ORDER BY c.applied_at,c.commit_id",
                (mission_id,),
            )
        ],
    }
