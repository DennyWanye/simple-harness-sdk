# SPDX-License-Identifier: Apache-2.0
"""Compile REQUEST_COMPENSATION into a human request, not an external action."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any

from ..contracts import ContractError
from ..contracts.planning_decisions import RepairCompensationRequestDecision, RequestHumanDecision
from ..contracts.semantic_base import content_hash_of


def prepare_request(store: Any, mission_id: str, payload: RepairCompensationRequestDecision,
                    package: Any) -> tuple[RequestHumanDecision, dict[str, Any]]:
    candidates = package.get("compensation_candidates", ()) if isinstance(package, Mapping) else ()
    if not any(row["action_key"] == payload.action_key and row["action_hash"] == payload.action_hash
               for row in candidates):
        raise ContractError("compensation action was not visible in this frozen request")
    action = store.get_action(payload.action_key)
    if (action is None or action["mission_id"] != mission_id or action["state"] != "SUCCEEDED"
            or content_hash_of(action) != payload.action_hash):
        raise ContractError("compensation requires the exact, still-successful original action")
    context = {"kind": "compensation_request", "action_key": payload.action_key,
               "action_hash": payload.action_hash, "connector": action["connector"],
               "operation": action["operation"], "target": action["target"],
               "execution": "separate_authenticated_compensation_proposal_required"}
    question = RequestHumanDecision.from_json({
        "question": f"请人工处理动作 {payload.action_key} 的补偿请求：{payload.reason}。回答仅记录处置意见；"
                    "执行补偿仍须通过独立补偿操作，选择产物并完成相应审批。",
        "options": [], "blocking": True})
    return question, context
