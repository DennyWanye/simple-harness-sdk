# SPDX-License-Identifier: Apache-2.0
"""Unified method proposals use the existing synthesis admission and registry."""
from __future__ import annotations

from typing import Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.htn import TaskRef
from ..planning.htn.registry import MethodProposal
from ..planning.unknown_fields import decode_dropping_unknown
from ..planning.htn.synthesis import MethodSynthesizer


def prepare_method(dispatch: Any, mission_id: str, payload: Any, subject: Any) -> tuple[Any, Any, Any]:
    world = dispatch.require_planning_world()
    proposal = decode_dropping_unknown(  # planner-authored (user decision 2026-09-26)
        MethodProposal.from_json, dict(payload.method_proposal), root_names=("method_proposal",))
    binding = dispatch.network(mission_id).binding_for_task(TaskRef(subject["task_id"]))
    goal_type = world.catalog.resolve(proposal.method.goal_type_ref)
    if goal_type is None or goal_type.goal_signature != binding.goal_signature:
        raise ContractError("proposed method does not serve the bound planning subject")
    # Candidate admission mutates only an isolated registry. Persist/install the
    # accepted definition after the caller rechecks the current grant and request.
    candidate = world.registry.fork()
    text = "<method_proposal>" + canonical_json(dict(payload.method_proposal)) + "</method_proposal>"
    receipt = MethodSynthesizer(candidate, world.catalog).accept_response(
        text, policy=dispatch._admission_policy(mission_id))
    if not receipt.admitted or receipt.method_ref is None:
        raise ContractError(f"method proposal refused: {receipt}")
    return receipt, candidate.definition(receipt.method_ref), candidate.registration(receipt.method_ref)


def persist_method(dispatch: Any, receipt: Any, contract: Any, registration: Any) -> dict[str, Any]:
    dispatch.semantics().register_method(contract, registration)
    return {"method_ref": receipt.method_ref.to_json(), "registry_status": str(registration.status)}
