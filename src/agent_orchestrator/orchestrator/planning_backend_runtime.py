# SPDX-License-Identifier: Apache-2.0
"""Durable solver results for the existing Planner decision/Commit transaction."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from ..contracts.models import ContractError
from ..planning.htn.backend_port import BackendStatus, CandidatePlanWitness, PlanningBackendResult
from .hierarchical_dispatch import append_hierarchical_event


def bind_deployment(handler: Any, mission_id: str) -> None:
    """Freeze native/solver selection before the first request, including recovery."""
    backend = handler._config.planning_backend
    limits = handler._config.planning_backend_limits
    if (backend is None) != (limits is None):
        raise ContractError("solver deployment requires both backend and limits")
    from ..planning.htn.method_selection import SelectionPolicyMode
    selection_policy = str(SelectionPolicyMode(handler._config.method_selection_policy))
    identity = {"backend_id": "native" if backend is None else backend.backend_id,
                "limits": None if limits is None else limits.to_json(),
                "repair_enabled": handler._config.hierarchical_repair_enabled,
                "method_selection_policy": selection_policy}
    with handler.store.transaction():
        bound = False
        for event in handler.store.iter_events(mission_id):
            if event.type == "PlanningDeploymentBound":
                original = dict(event.payload)
                # Older native deployments used this default before the field
                # was serialized. Compare with that default, preserving old bytes.
                original.setdefault("method_selection_policy", "MODEL_ON_MULTIPLE")
                if original != identity:
                    raise ContractError("planning deployment differs from this Mission's frozen deployment")
                bound = True
            if event.type == "PlanningBackendBound" and (
                event.payload.get("backend_id") != identity["backend_id"]
                or event.payload.get("limits") != identity["limits"]
            ):
                raise ContractError("planning backend differs from this Mission's frozen deployment")
        if not bound:
            append_hierarchical_event(handler.store, "PlanningDeploymentBound", mission_id,
                                      key=mission_id, payload=identity)


async def solve_decision(handler: Any, *, mission_id: str, decision_id: str,
                         lane: Any) -> tuple[Any, PlanningBackendResult]:
    bind_deployment(handler, mission_id)
    backend = handler._config.planning_backend
    limits = handler._config.planning_backend_limits
    if backend is None or limits is None:
        raise ContractError("solver deployment requires its backend and limits")
    identity = {"backend_id": backend.backend_id, "limits": limits.to_json()}
    for event in handler.store.iter_events(mission_id):
        if event.type == "PlanningBackendBound" and dict(event.payload) != identity:
            raise ContractError("planning backend differs from this Mission's frozen deployment")
    append_hierarchical_event(handler.store, "PlanningBackendBound", mission_id,
                              key=mission_id, payload=identity)
    snapshot = lane.snapshot()
    for event in handler.store.iter_events(mission_id):
        if event.type != "PlanningBackendReturned" or event.payload.get("decision_id") != decision_id:
            continue
        if event.payload["snapshot_digest"] != snapshot.digest:
            raise ContractError("solver decision snapshot changed on recovery")
        raw = event.payload.get("witness")
        witness = None if raw is None else CandidatePlanWitness(
            backend_id=raw["backend_id"], snapshot_digest=raw["snapshot_digest"],
            steps=tuple(raw["steps"]), plan_hash=raw["plan_hash"], hierarchy_json=raw["hierarchy_json"])
        return snapshot, PlanningBackendResult(BackendStatus(event.payload["status"]), snapshot.digest,
                                              witness, event.payload.get("detail", ""))
    result = await asyncio.to_thread(backend.solve, snapshot, limits)
    if result.snapshot_digest != snapshot.digest:
        raise ContractError("solver returned a result for another snapshot")
    append_hierarchical_event(handler.store, "PlanningBackendReturned", mission_id,
        key=decision_id, payload={"decision_id": decision_id, "snapshot_digest": snapshot.digest,
            "status": str(result.status), "detail": result.detail,
            "witness": None if result.witness is None else {
                **asdict(result.witness), "steps": list(result.witness.steps)}})
    return snapshot, result
