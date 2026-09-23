# SPDX-License-Identifier: Apache-2.0
"""Durable H4 triggers from original results, validity and requirements records.

Requests carry no guessed model action. Impact is calculated under the same writer
snapshot as the request; the original PlanningDecision pipeline chooses and commits.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..contracts.semantic_base import content_hash_of
from ..planning.htn.repair_adapter import RepairEventAdapter
from ..planning.htn.repair_decision import analyze_impact
from .hierarchical_dispatch import append_hierarchical_event
from .repair_impact import read_repair_impact_indexes

REQUESTED = "PlanningRepairRequested"


def record_request(dispatch: Any, mission_id: str, *, event_type: str,
                   trigger_refs: tuple[str, ...], source_key: str,
                   detail: dict[str, Any]) -> bool:
    store = dispatch.store
    with store.transaction():
        if any(e.type == REQUESTED and e.payload.get("source_key") == source_key
               for e in store.iter_events(mission_id)):
            return False
        network = dispatch.network(mission_id)
        request = RepairEventAdapter.request_from_event(
            {"type": event_type, "trigger_refs": trigger_refs,
             "payload": {"context": detail}}, mission_id=mission_id,
            plan_revision=int(network.plan_revision))
        impact = analyze_impact(request, **read_repair_impact_indexes(store, network, mission_id))
        append_hierarchical_event(store, REQUESTED, mission_id, key=source_key,
            payload={"source_key": source_key, "request_id": request.request_id,
                     "request": request.to_json(), "impact": impact.to_json()})
    return True


def collect_triggers(handler: Any, mission: Any) -> bool:
    dispatch = handler._new_mode(mission)
    if dispatch is None:
        return False
    from .planning_protocol_binding import planning_protocol_for_mission
    binding = planning_protocol_for_mission(handler.store, mission.id)
    if binding is None or int(binding["package_version"]) < 6 or binding["protocol_version"] != "planning-decision-v1":
        return False
    store = handler.store
    produced = False
    active_tasks = {str(spec.task_id) for spec in dispatch.network(mission.id).occurrences}
    h4 = int(binding["package_version"]) >= 7
    seen = {e.payload.get("source_key") for e in store.iter_events(mission.id) if e.type == REQUESTED}
    sources = {"ResultRejected": "WorkerRejected", "VerificationFailed": "VerifierAcceptanceRejected",
               "HierarchicalRootReviewRejected": "VerifierAcceptanceRejected"}
    if h4:
        sources.update({"AttemptLost": "WorkerRejected", "AttemptTimedOut": "WorkerRejected"})
    for event in tuple(store.iter_events(mission.id)):
        source_key = "event:" + event.idempotency_key
        if event.type not in sources or source_key in seen:
            continue
        if h4 and event.task_id and event.task_id not in active_tasks:
            continue
        if (event.type in {"AttemptLost", "AttemptTimedOut"}
                and event.payload.get("reason") in {"runtime_unavailable", "provider_outcome_unknown"}):
            # The authoritative failure row below produces the runtime request.
            continue
        refs = tuple(dict.fromkeys(str(x) for x in (event.attempt_id, event.task_id) if x)) or (mission.id,)
        produced |= record_request(dispatch, mission.id, event_type=sources[event.type],
            trigger_refs=refs, source_key=source_key,
            detail={"source_event": event.idempotency_key, "event_type": event.type, "detail": dict(event.payload)})
    htn = dispatch.semantics()
    for state in ("PENDING", "RECHECKING"):
        for dirty in htn.list_dirty(mission.id, state=state):
            # A committed repair already revoked these execution rights. Its
            # internal recheck marker is not a new evidence failure to replan.
            if h4 and dirty.reason == "dispatch_generation_revoked":
                continue
            source_key = "dirty:" + content_hash_of({k: v for k, v in asdict(dirty).items() if k != "state"})
            if source_key not in seen:
                produced |= record_request(dispatch, mission.id, event_type="EvidenceInvalidated",
                    trigger_refs=(dirty.subject_id,), source_key=source_key, detail=asdict(dirty))
    # Internal leaf/composition requirements snapshots are not user amendments.
    # Only a persisted revision carrying its amendment credential opens this trigger.
    for revision in htn.list_requirements_revisions(mission.id):
        source_key = "requirements:" + str(revision.revision_id)
        if revision.amendment_credential_ref and source_key not in seen:
            produced |= record_request(dispatch, mission.id, event_type="RequirementsUpdated",
                trigger_refs=(mission.id,), source_key=source_key,
                detail={"requirements": revision.to_json(), "content_hash": content_hash_of(revision.to_json())})
    for task in store.list_tasks(mission.id):
        if h4 and task.id not in active_tasks:
            continue
        for attempt in store.list_attempts(task.id):
            failure = attempt.failure
            source_key = "runtime:" + attempt.id
            if failure and failure.get("reason") in {"runtime_unavailable", "provider_outcome_unknown"} and source_key not in seen:
                produced |= record_request(dispatch, mission.id, event_type="RuntimeUnavailable",
                    trigger_refs=(attempt.id,), source_key=source_key, detail=dict(failure))
    # A committed retry is bound to the exact task/plan/input/operation read.
    # If it becomes stale before dispatch, reopen a system request; silently
    # retaining the old addressed trigger would leave the Task blocked forever.
    if int(binding["package_version"]) >= 7:
        from .planning_retry import RETRY_AUTHORIZED, pending_retry_permit, retry_decision_required
        permits = [e for e in store.iter_events(mission.id) if e.type == RETRY_AUTHORIZED]
        for task in store.list_tasks(mission.id):
            if task.id not in active_tasks or not retry_decision_required(store, mission.id, task.id):
                continue
            latest = max(store.list_attempts(task.id), key=lambda item: item.ordinal)
            previous = next((e for e in reversed(permits) if e.payload.get("task_id") == task.id
                             and e.payload.get("failed_attempt_id") == latest.id), None)
            if previous is None or pending_retry_permit(store, mission.id, task.id) is not None:
                continue
            from ..runtime.planning_operations import StoreOperationReader, build_operation_snapshot, SourceUnavailable
            try:
                operations = build_operation_snapshot(mission.id, reader=StoreOperationReader(store))
                operation_state = operations.read_digest
            except SourceUnavailable as error:
                operation_state = error.reason
            current = htn.task_semantics_of(mission.id, task.id)
            retry_state = {"previous_decision": previous.payload["decision_id"], "attempt_id": latest.id,
                     "task_version": task.version, "plan_revision": int(dispatch.network(mission.id).plan_revision),
                     "binding_hash": None if current is None else content_hash_of(current.to_json()),
                     "operation_state": operation_state}
            source_key = "retry_stale:" + content_hash_of(retry_state)
            if source_key not in seen:
                produced |= record_request(dispatch, mission.id, event_type="WorkerRejected",
                    trigger_refs=(latest.id, task.id), source_key=source_key,
                    detail={"reason": "committed_retry_binding_changed", **retry_state})
    return produced


def pending_requests(store: Any, mission_id: str) -> list[dict[str, Any]]:
    events = tuple(store.iter_events(mission_id))
    handled = {request_id for e in events if e.type == "PlanningRepairAddressed"
               for request_id in e.payload.get("repair_request_ids", ())}
    return [dict(e.payload) for e in events if e.type == REQUESTED and e.payload["request_id"] not in handled]


def repair_goal_occurrences(store: Any, network: Any) -> tuple[str, ...]:
    """Expose alternatives for adopted compounds affected by an original H4 trigger.

    Visibility is not a repair authorization. The decision compiler still checks
    the current instance, impact, accepted work, and operation state at admission.
    """
    from ..contracts.htn import TaskForm
    from .planning_protocol_binding import planning_protocol_for_mission

    mission_id = str(network.mission_id)
    protocol = planning_protocol_for_mission(store, mission_id)
    if protocol is None or int(protocol["package_version"]) < 7:
        return ()
    affected = {str(item) for row in pending_requests(store, mission_id)
                for group in ("revalidate", "supersede", "new_work")
                for item in row["impact"].get(group, ())}
    return tuple(sorted(str(spec.occurrence_id) for spec in network.occurrences
                        if spec.form is TaskForm.COMPOUND
                        and affected.intersection((str(spec.occurrence_id), str(spec.task_id)))))


def address_requests(store: Any, mission_id: str, *, package: Any,
                     decision_id: str, decision_type: str, status: str,
                     subject_key: str) -> None:
    """Only a committed plan change for the affected subject consumes a trigger.

    Evidence, human questions, proposals and WAIT preserve the request so the
    resumed planner can still see the failure that opened the service call.
    """
    if status != "COMMITTED" or decision_type not in {"REFINE", "REPAIR", "BIND_EXISTING_GOAL"} or not isinstance(package, dict):
        return
    subject = next((s for s in package.get("planning_subjects", ())
                    if s.get("subject_key") == subject_key), None)
    if subject is None:
        return
    targets = {str(subject[k]) for k in ("occurrence_id", "task_id", "obligation_id") if subject.get(k)}
    addressed = []
    for request in package.get("repair_requests", ()):
        impact = request.get("impact", {})
        affected = {str(item) for group in ("revalidate", "supersede", "new_work")
                    for item in impact.get(group, ())}
        if targets & affected:
            addressed.append(request["request_id"])
    if addressed:
        append_hierarchical_event(store, "PlanningRepairAddressed", mission_id, key=decision_id,
            payload={"decision_id": decision_id, "decision_type": decision_type, "status": status,
                     "subject_key": subject_key, "repair_request_ids": addressed})
