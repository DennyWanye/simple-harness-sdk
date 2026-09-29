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
ADDRESSED = "PlanningRepairAddressed"
#: 架构方案 B（2026-09-30）：一次资料变更没影响到任何在跑的尝试或引用它的已通过结果，
#: 只记这一条（不发修复请求），下一轮不再重算。
SOURCE_CHANGE_ASSESSED = "SourceChangeAssessed"
SOURCE_CHANGE_EVENTS = {"SourceSuperseded": "source_superseded", "SourceRevoked": "source_revoked"}


def source_change_triggers(handler: Any, dispatch: Any, mission: Any, *, seen: set[str],
                           active_tasks: set[str]) -> bool:
    """架构方案 B：资料换版本 / 撤销 → 只对**有证据**的受影响对象发一条"证据失效"修复请求。

    派发时冻结的 ``source_versions`` 是当时全部现行资料，不是这一步用了哪些，所以：
    已通过的结果只按引用（claims 的 citations）判定；拿着旧版还在跑的尝试等它跑完再评估
    （验收会对照当前资料，被拒走普通的验收失败请求）；还没派发的步骤不算（下次派发自动
    拿新版）。新登记的资料不发请求。影响范围由已有的影响分析沿依赖算到下游；请求以资料
    事件的幂等键去重。
    """
    from ..contracts.state_machines import TERMINAL_ATTEMPT

    store = handler.store
    events = tuple(store.iter_events(mission.id))
    assessed = {e.payload.get("source_key") for e in events if e.type == SOURCE_CHANGE_ASSESSED}
    produced = False
    for event in events:
        reason = SOURCE_CHANGE_EVENTS.get(event.type)
        source_key = "source:" + event.idempotency_key
        if reason is None or source_key in seen or source_key in assessed:
            continue
        rows = [dict(r) for r in (event.payload.get("sources") or ()) if isinstance(r, dict)]
        old = rows[0] if rows else {}
        path, old_hash = str(old.get("path") or ""), str(old.get("version_hash") or "")
        revoked = reason == "source_revoked"
        new_hash = None if revoked else next((str(r.get("version_hash")) for r in rows[1:]), None)
        # 拿着旧版跑的尝试：还在跑就**先不评估**——2026-09-30 真机：当轮发请求，规划器只会回 WAIT
        # 等它跑完，修复轮又拒绝 WAIT，白花两轮次数；它跑完后验收本来就对照当前资料（审阅员按
        # 现行版本判、引用按当前性判），被拒就走普通的验收失败请求。跑完再评估，把它们记在案。
        on_old_version: list[str] = []
        still_running = False
        for task_id in sorted(active_tasks):
            for attempt in store.list_attempts(task_id):
                frozen = handler._frozen_source_binding(attempt).get("source_versions", {})
                if path in frozen and (revoked or str(frozen[path]) == old_hash):
                    on_old_version.append(attempt.id)
                    still_running |= attempt.status not in TERMINAL_ATTEMPT
        if still_running:
            continue
        accepted: list[str] = []
        for result_id, task_id in store.connection.execute(
                "SELECT result_id, task_id FROM results WHERE mission_id=? AND verification_state='DONE'"
                " ORDER BY received_at", (mission.id,)).fetchall():
            if str(task_id) not in active_tasks:
                continue
            stored = store.get_result(str(result_id))
            if stored is None or str(stored.verdict or "").upper() != "PASS":
                continue
            cited = any(c.path == path and (revoked or c.version == old_hash)
                        for claim in stored.envelope.claims for c in claim.citations)
            if cited:
                accepted.append(str(result_id))
        detail = {"reason": reason, "path": path, "old_version": old_hash, "new_version": new_hash,
                  "source_event": event.idempotency_key,
                  "affected": {"attempts_on_old_version": on_old_version, "accepted_results": accepted}}
        refs = tuple(accepted)
        if not refs:
            append_hierarchical_event(store, SOURCE_CHANGE_ASSESSED, mission.id, key=source_key,
                                      payload={"source_key": source_key, **detail})
            continue
        produced |= record_request(dispatch, mission.id, event_type="EvidenceInvalidated",
                                   trigger_refs=refs, source_key=source_key, detail=detail)
    return produced


def settle_addressed_requests(handler: Any, dispatch: Any, mission: Any) -> bool:
    """架构方案 B 前置 2（用户 2026-09-29 决定）：修复请求由**系统**消费的出口。

    此前只有"提交了计划改动且目标与影响范围相交"才消费请求；规划器判断旧贡献不用重做时，
    请求永远挂着，让"还欠修复"一直为真，之后任何一轮被拒都逼着开新轮直到次数用完。现在：
    影响范围里没有新增工作、且每个受影响的叶子步骤都在请求之后重新验收通过（``TaskCompleted``
    晚于请求）时，系统记一条 ``PlanningRepairAddressed``（decision_type=SYSTEM_REVALIDATED）。
    影响范围含义务、操作、方法实例等只有计划改动才能了结的对象时，不由系统消费。
    """
    from ..contracts.htn import TaskForm

    store = handler.store
    events = tuple(store.iter_events(mission.id))
    handled = {rid for e in events if e.type == ADDRESSED for rid in e.payload.get("repair_request_ids", ())}
    network = dispatch.network(mission.id)
    leaf_of_occurrence = {str(s.occurrence_id): str(s.task_id) for s in network.occurrences if s.form is not TaskForm.COMPOUND}
    compound_occurrences = {str(s.occurrence_id) for s in network.occurrences if s.form is TaskForm.COMPOUND}
    leaf_tasks = set(leaf_of_occurrence.values())
    compound_tasks = {str(s.task_id) for s in network.occurrences if s.form is TaskForm.COMPOUND}
    # 影响分析沿"子步骤 → 所属复合目标 → 采纳的方法实例"把父级也算进来；父级经叶子了结。
    parents = compound_occurrences | compound_tasks | {str(i.instance_id) for i in network.method_instances}
    acceptances = {str(a.acceptance_id): str(a.task_id) for a in dispatch.semantics().list_acceptances(mission.id)}
    # 分层语义下叶子验收通过写 AcceptanceCommitted（带 task_id）；旧任务状态机同时写 TaskCompleted。
    completed: dict[str, tuple[int, str]] = {}
    for e in events:
        if e.type in {"AcceptanceCommitted", "TaskCompleted"} and e.task_id:
            completed[str(e.task_id)] = (int(e.seq or 0), e.idempotency_key)
    produced = False
    for e in events:
        if e.type != REQUESTED or e.payload["request_id"] in handled:
            continue
        impact = e.payload.get("impact", {})
        if impact.get("new_work"):
            continue
        tasks: set[str] = set()
        settleable = True
        for item in (str(i) for group in ("revalidate", "supersede") for i in impact.get(group, ())):
            if item == mission.id or item in parents:
                continue  # a parent is settled through its leaves
            if item in leaf_tasks:
                tasks.add(item)
            elif item in leaf_of_occurrence:
                tasks.add(leaf_of_occurrence[item])
            elif item in acceptances:
                tasks.add(acceptances[item])
            else:
                attempt = store.get_attempt(item)
                if attempt is not None:
                    tasks.add(str(attempt.task_id))
                    continue
                row = store.connection.execute("SELECT task_id FROM results WHERE result_id=?", (item,)).fetchone()
                if row is not None:
                    tasks.add(str(row[0]))
                    continue
                settleable = False  # obligations, operations, method instances: only a plan change settles them
                break
        if not settleable or not tasks:
            continue
        request_seq = int(e.seq or 0)
        settled = {t: completed[t][1] for t in sorted(tasks) if t in completed and completed[t][0] > request_seq}
        if len(settled) != len(tasks):
            continue
        append_hierarchical_event(store, ADDRESSED, mission.id, key="system:" + str(e.payload["request_id"]),
            payload={"decision_id": None, "decision_type": "SYSTEM_REVALIDATED", "status": "COMMITTED",
                     "subject_key": None, "repair_request_ids": [e.payload["request_id"]], "settled_by": settled})
        produced = True
    return produced


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
    from .failure_classes import classify_failure

    def failure_class(attempt_id: str | None) -> dict[str, str]:
        # 2026-09-28：请求里带上"谁的错"，非模型原因由系统原地重做（planning_selection）。
        attempt = store.get_attempt(attempt_id) if attempt_id else None
        return {} if attempt is None or not attempt.failure else {
            "failure_class": classify_failure(attempt.failure)}
    sources = {"ResultRejected": "WorkerRejected", "VerificationFailed": "VerifierAcceptanceRejected",
               "HierarchicalRootReviewRejected": "VerifierAcceptanceRejected"}
    if h4:
        sources.update({"AttemptLost": "WorkerRejected", "AttemptTimedOut": "WorkerRejected"})
    # 2026-09-29 真机第十、十一局：步骤如实报告"卡住"（缺上游文件）后尝试进"等重试"，却没人
    # 问规划器，几秒后判"没有可派发的工作"、整局失败。如实的卡住/失败/没进展报告也交给
    # 规划器（带上步骤自己的说明），由它决定重排、补步骤或重试。
    sources["OutcomeRecorded"] = "WorkerRejected"
    produced |= settle_addressed_requests(handler, dispatch, mission)
    produced |= source_change_triggers(handler, dispatch, mission, seen=seen, active_tasks=active_tasks)
    for event in tuple(store.iter_events(mission.id)):
        source_key = "event:" + event.idempotency_key
        if event.type not in sources or source_key in seen:
            continue
        if (event.type == "OutcomeRecorded"
                and event.payload.get("outcome") not in {"blocked", "failure", "no_progress"}):
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
            detail={"source_event": event.idempotency_key, "event_type": event.type,
                    "detail": dict(event.payload), **failure_class(event.attempt_id)})
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
                    trigger_refs=(attempt.id,), source_key=source_key,
                    detail={**dict(failure), **failure_class(attempt.id)})
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
