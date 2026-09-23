# SPDX-License-Identifier: Apache-2.0
"""Durable H4 runtime suspension. A changed source wakes planning, never execution."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from ..contracts import ContractError
from ..contracts.planning_decisions import RepairRuntimeBlockedDecision
from ..contracts.semantic_base import content_hash_of
from .hierarchical_dispatch import append_hierarchical_event

BLOCKED = "PlanningRuntimeBlocked"
WOKEN = "PlanningRuntimeBlockWoken"
RESOLVED = "PlanningRuntimeBlockResolved"


def pending_block(store: Any, mission_id: str) -> Any:
    events = tuple(store.iter_events(mission_id))
    resolved = {e.payload.get("block_decision_id") for e in events if e.type == RESOLVED}
    return next((e for e in reversed(events) if e.type == BLOCKED
                 and e.payload["decision_id"] not in resolved), None)


def last_wake(store: Any, mission_id: str, block: Any) -> Any:
    return next((e for e in reversed(tuple(store.iter_events(mission_id))) if e.type == WOKEN
                 and e.payload.get("block_decision_id") == block.payload["decision_id"]), None)


def planner_binding(store: Any, mission_id: str) -> dict[str, str]:
    block = pending_block(store, mission_id)
    if block is None or last_wake(store, mission_id, block) is None:
        return {}
    wake = last_wake(store, mission_id, block)
    return {"runtime_replan_block_id": str(block.payload["decision_id"]),
            "runtime_replan_wake_id": str(wake.payload["decision_id"])}


def blocks_intent(store: Any, intent: Any) -> bool:
    block = pending_block(store, intent.mission_id)
    if block is None or intent.kind == "critic":
        return False
    wake = last_wake(store, intent.mission_id, block)
    return not (intent.kind == "plan" and wake is not None
                and intent.config.get("runtime_replan_block_id") == block.payload["decision_id"]
                and intent.config.get("runtime_replan_wake_id") == wake.payload["decision_id"])


def resume_source_current(handler: Any, intent: Any, mission: Any) -> bool:
    """Fence a reply when the environment changed after its last wake snapshot."""
    block = pending_block(handler.store, mission.id)
    if block is None:
        return not intent.config.get("runtime_replan_block_id")
    if blocks_intent(handler.store, intent):
        return False
    wake = last_wake(handler.store, mission.id, block)
    sources = runtime_binding(handler, mission, tuple(block.payload["resumable_if"]))
    return bool(wake and content_hash_of(sources) == wake.payload["source_hash"])


def resolve_after_decision(handler: Any, intent: Any, *, status: str,
                           decision_type: str, decision_id: str) -> None:
    block = pending_block(handler.store, intent.mission_id)
    if (block is None or intent.config.get("runtime_replan_block_id") != block.payload["decision_id"]
            or not (status == "COMMITTED" or (status == "NO_STATE_CHANGE" and decision_type == "NO_CHANGE"))):
        return
    wake = last_wake(handler.store, intent.mission_id, block)
    if wake is None or intent.config.get("runtime_replan_wake_id") != wake.payload["decision_id"]:
        return
    mission = handler.store.get_mission(intent.mission_id)
    # A committed graph repair may itself change the Plan. The admission reader
    # checks the pre-commit sources; NO_CHANGE must still match them here.
    if status != "COMMITTED" and not resume_source_current(handler, intent, mission):
        return
    from ..runtime.planning_operations import SourceUnavailable, StoreOperationReader, build_operation_snapshot
    try:
        operations = build_operation_snapshot(intent.mission_id, reader=StoreOperationReader(handler.store))
    except SourceUnavailable:
        return
    if operations.unresolved or any(t.paused and t.pause_reason == "provider_admission:usage_unresolved"
                                   for t in handler.store.list_tasks(intent.mission_id)):
        return
    append_hierarchical_event(handler.store, RESOLVED, intent.mission_id, key=decision_id,
        payload={"block_decision_id": block.payload["decision_id"], "decision_id": decision_id,
                 "decision_type": decision_type, "operation_digest": operations.read_digest})


def runtime_binding(handler: Any, mission: Any, conditions: tuple[str, ...]) -> dict[str, Any]:
    """Only semantic facts, never a wall-clock timestamp, enter the wake digest."""
    dispatch = handler._new_mode(mission)
    if dispatch is None:
        raise ContractError("runtime suspension lost its hierarchical deployment")
    world = dispatch.require_planning_world()
    htn = dispatch.semantics()
    plan = htn.active_plan_revision(mission.id)
    requirements = htn.latest_requirements_revision(mission.id)
    # Revisions always matter: a suspension of an old plan cannot hold a new one.
    result: dict[str, Any] = {
        "plan_revision": 0 if plan is None else int(plan.revision),
        "requirements_revision": 0 if requirements is None else int(requirements.revision),
        "capabilities": sorted((asdict(c) for c in world.capabilities().records), key=lambda c: c["capability_id"]),
        "cooldowns": {key: until > handler.store.now
                      for key, until in sorted(handler.commit.unavailable_until().items())},
        "provider_holds": sorted(task.id for task in handler.store.list_tasks(mission.id)
                                 if task.paused and task.pause_reason == "provider_admission:usage_unresolved"),
    }
    if "evidence_updated" in conditions:
        snapshot = world.snapshot()
        result["evidence"] = [snapshot.scope_epoch, snapshot.support_revision]
    if "human_resolved" in conditions:
        from ..storage.planning_human_store import PlanningHumanStore
        result["human_answers"] = sorted(row["decision_id"] for row in PlanningHumanStore(handler.store).list(mission.id)
                                        if row["state"] == "ANSWERED")
    return result


def register_block(handler: Any, mission: Any, *, payload: RepairRuntimeBlockedDecision,
                   package: Any, decision_id: str, canonical_hash: str) -> dict[str, Any]:
    from .planning_repair_requests import pending_requests
    requests = [] if not isinstance(package, Mapping) else package.get("repair_requests", ())
    requested = next((r for r in requests if r["request_id"] == payload.repair_request_id), None)
    live = next((r for r in pending_requests(handler.store, mission.id)
                 if r["request_id"] == payload.repair_request_id), None)
    if (requested is None or live != requested
            or requested["request"]["trigger_source"] != "RUNTIME_UNAVAILABLE"):
        raise ContractError("runtime blockage needs an exact, pending runtime RepairRequest from this package")
    conditions = tuple(str(item) for item in payload.resumable_if)
    sources = runtime_binding(handler, mission, conditions)
    data = {"decision_id": decision_id, "canonical_hash": canonical_hash,
            "repair_request_id": payload.repair_request_id, "blockers": [b.to_json() for b in payload.blockers],
            "resumable_if": list(conditions), "sources": sources, "source_hash": content_hash_of(sources)}
    previous = pending_block(handler.store, mission.id)
    if previous is not None and previous.payload["decision_id"] != decision_id:
        append_hierarchical_event(handler.store, RESOLVED, mission.id, key="superseded:" + decision_id,
            payload={"block_decision_id": previous.payload["decision_id"], "decision_id": decision_id,
                     "reason": "superseded_by_new_runtime_block"})
    append_hierarchical_event(handler.store, BLOCKED, mission.id, key=decision_id, payload=data)
    return data


def wake_blocks(handler: Any) -> bool:
    progressed = False
    for mission in handler._active_missions():
        with handler.store.transaction():
            block = pending_block(handler.store, mission.id)
            if block is None:
                continue
            sources = runtime_binding(handler, mission, tuple(block.payload["resumable_if"]))
            digest = content_hash_of(sources)
            wake = last_wake(handler.store, mission.id, block)
            previous_digest = block.payload["source_hash"] if wake is None else wake.payload["source_hash"]
            if digest == previous_digest:
                continue
            # Include the previous wake identity: A -> B -> A -> B is four
            # transitions, not a replay of the first B event.
            wake_key = content_hash_of({"block": block.payload["decision_id"],
                "previous_wake": None if wake is None else wake.payload["decision_id"],
                "source_hash": digest})
            append_hierarchical_event(handler.store, WOKEN, mission.id,
                key=wake_key, payload={
                    "decision_id": "runtime-wake:" + wake_key,
                    "block_decision_id": block.payload["decision_id"], "previous_source_hash": previous_digest,
                    "source_hash": digest, "reason": "authoritative_runtime_or_resume_source_changed"})
            # An undispatched planner frozen before this transition must not
            # prevent the new wake from opening a replacement round. Submitted
            # calls retain their accounting and are refused by live admission.
            for intent in handler.store.list_intents("PENDING"):
                if (intent.mission_id == mission.id and intent.kind == "plan"
                        and blocks_intent(handler.store, intent)):
                    handler._settle_intent(intent, "FAILED")
                    handler._settle_service_if_known(intent.subject_id, mission.id)
            progressed = True
    return progressed
