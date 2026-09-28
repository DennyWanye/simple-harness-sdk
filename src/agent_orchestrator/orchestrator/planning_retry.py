# SPDX-License-Identifier: Apache-2.0
"""H4 retry commands bind one failed Attempt; execution remains create_attempt.

No graph, duty, usage, or Attempt row is copied or reset here. A permit becomes
usable only in the same transaction that records the original decision COMMITTED.
"""
from __future__ import annotations

from typing import Any

from ..contracts import ContractError, TERMINAL_ATTEMPT
from ..contracts.htn import TaskForm
from ..contracts.planning_decisions import RepairRetrySameMethodDecision
from ..contracts.semantic_base import content_hash_of
from ..runtime.planning_operations import StoreOperationReader, build_operation_snapshot
from ..storage.htn_store import HtnStore
from ..storage.planning_decision_store import PlanningDecisionStore

RETRY_AUTHORIZED = "PlanningRetryAuthorized"


def _enabled(store: Any, mission_id: str) -> bool:
    protocol = PlanningDecisionStore(store).get_mission_protocol(mission_id)
    return bool(protocol and protocol["protocol_version"] == "planning-decision-v1"
                and int(protocol["package_version"]) >= 7)


def retry_binding(store: Any, mission_id: str, task_id: str,
                  payload: RepairRetrySameMethodDecision, *, expected_plan_revision: int) -> dict[str, Any]:
    """Compile a retry from live, authoritative rows, within the caller transaction."""
    if not _enabled(store, mission_id):
        raise ContractError("RETRY_SAME_METHOD is not enabled for this Mission")
    task = store.get_task(task_id)
    if (task is None or task.mission_id != mission_id or task.accepted_result_id
            or str(task.status) not in {"READY", "ACTIVE", "VERIFYING"} or task.paused):
        raise ContractError("retry target is not an unaccepted, active Task")
    attempts = store.list_attempts(task_id)
    latest = max(attempts, key=lambda a: a.ordinal, default=None)
    if (latest is None or latest.id != payload.failed_attempt_id or latest.status not in TERMINAL_ATTEMPT
            or str(latest.status) in {"CANCELLED", "SUPERSEDED"} or not latest.failure
            or any(a.status not in TERMINAL_ATTEMPT for a in attempts)):
        raise ContractError("retry must name the latest failed Attempt with no open sibling")
    # An unknown *charge* on a call that definitely failed is not a reason to freeze:
    # user count rule (2026-09-24) — overcount, never undercount, never freeze.  Its
    # reservation stays held at the upper bound (ReservationHeld) and the retry is a
    # new Attempt with its own reservation.  2026-09-25 UI 全量点击: a worker turn that
    # failed on a malformed tool call left an unknown charge and the document Mission
    # stalled because every RETRY_SAME_METHOD was refused here.
    # 2026-09-28 用户决定：结果不明（进程重启打断、原调用无法核对）也可原样重做——未记录
    # 的回复不会执行任何工具；原调用的预留按上限保留（只可多算），新执行另行预留。
    if latest.failure.get("reason") == "runtime_unavailable":
        # Runtime failure is not permanently unrepairable. An explicit retry may
        # proceed after its profile recovered; an unknown charge keeps its hold at the
        # upper bound and the runtime block keeps execution suspended below.
        health = store.get_scheduler_state("profile_health") or {}
        profile = health.get("profiles", {}).get(latest.runtime_profile_id, {})
        until = profile.get("unavailable_until")
        if until is not None and float(until) > store.now:
            raise ContractError("runtime profile is still unavailable")
    htn = HtnStore(store)
    active = htn.active_plan_revision(mission_id)
    if active is None or int(active.revision) != expected_plan_revision:
        raise ContractError("REQUEST_BINDING_STALE: retry Plan changed")
    occurrences = [spec for spec in htn.list_plan_memberships(mission_id, expected_plan_revision)
                   if str(spec.task_id) == task_id and spec.form is TaskForm.PRIMITIVE]
    if len(occurrences) != 1:
        raise ContractError("retry requires one active primitive occurrence")
    ref = payload.method_instance_ref
    instance = htn.get_method_instance(mission_id, ref.id)
    occurrence = occurrences[0]
    if (htn.method_instance_state(mission_id, ref.id) != "ADOPTED"
            or max(1, int(instance.plan_revision)) != ref.semantic_revision
            or instance.parameters_digest() != ref.content_hash
            or not any((child.goal_occurrence_id or child.occurrence_id) == occurrence.occurrence_id
                       for child in instance.child_bindings)):
        raise ContractError("retry Method identity or occurrence membership changed")
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None or binding.obligation_id != occurrence.obligation_id:
        raise ContractError("retry target lost its stable Obligation")
    snapshot = build_operation_snapshot(mission_id, reader=StoreOperationReader(store))
    if snapshot.unresolved:
        raise ContractError("OPERATION_UNRESOLVED: reconcile before retry")
    return {"mission_id": mission_id, "task_id": task_id, "task_version": task.version,
            "occurrence_id": str(occurrence.occurrence_id), "plan_revision": expected_plan_revision,
            "failed_attempt_id": latest.id, "method_instance_ref": ref.to_json(),
            "binding_hash": content_hash_of(binding.to_json()), "operation_digest": snapshot.read_digest}


def pending_retry_permit(store: Any, mission_id: str, task_id: str) -> dict[str, Any] | None:
    """Return a current committed permit, or None. Missing evidence never authorizes."""
    from ..runtime.planning_operations import SourceUnavailable
    from ..storage.store import StoreError
    for event in reversed(tuple(store.iter_events(mission_id))):
        if event.type != RETRY_AUTHORIZED or event.payload.get("task_id") != task_id:
            continue
        data = dict(event.payload)
        row = PlanningDecisionStore(store).get_planning_decision(str(data["decision_id"]))
        if (row is None or row["status"] != "COMMITTED"
                or row["canonical_hash"] != data["canonical_hash"]
                or row["request_id"] != data["request_id"]):
            continue
        try:
            payload = RepairRetrySameMethodDecision.from_json({
                "repair_kind": "RETRY_SAME_METHOD", "failed_attempt_id": data["failed_attempt_id"],
                "method_instance_ref": data["method_instance_ref"]})
            current = retry_binding(store, mission_id, task_id, payload,
                                    expected_plan_revision=int(data["plan_revision"]))
        except (ContractError, StoreError, SourceUnavailable, KeyError):
            return None
        return data if all(data.get(key) == value for key, value in current.items()) else None
    return None


def retry_decision_required(store: Any, mission_id: str, task_id: str) -> bool:
    if not _enabled(store, mission_id):
        return False
    attempts = store.list_attempts(task_id)
    latest = max(attempts, key=lambda a: a.ordinal, default=None)
    return bool(latest is not None and latest.status in TERMINAL_ATTEMPT and latest.failure
                and str(latest.status) not in {"CANCELLED", "SUPERSEDED"})
