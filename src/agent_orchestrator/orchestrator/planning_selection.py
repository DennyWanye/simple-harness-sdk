# SPDX-License-Identifier: Apache-2.0
"""H3 deterministic selection uses the original Decision admission and Commit lane."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.planning_decisions import PlanningDecisionEnvelopeV1
from .hierarchical_dispatch import append_hierarchical_event, METHOD_SELECTION_CALL_CLAIMED


def selection_context(dispatch: Any, mission_id: str, reports: Any, *, policy: str = "MODEL_ON_MULTIPLE") -> list[dict[str, Any]]:
    """Read only: assembling a package must not spend a selection call."""
    from ..planning.htn.method_selection import MethodSelectionPolicyV1, SelectionPolicyMode
    decisions = dispatch.select_method_candidates(mission_id, reports=reports, persist_claims=False,
        policy=MethodSelectionPolicyV1(mode=SelectionPolicyMode(policy)))
    network = dispatch.network(mission_id)
    return [{"occurrence_id": occurrence, "route": str(result.route),
             "identity": result.identity.to_json(),
             "applicable": [item.to_json() for item in sorted(result.applicable,
                 key=lambda item: (item.method_id != result.selected_method_id, item.method_id))[:12]],
             "selected_method_id": result.selected_method_id,
             "applicable_count": len(result.applicable),
             "omitted_count": max(0, len(result.applicable) - 12),
             "bindings": dict(network.binding_for_occurrence(occurrence).typed_parameters)}
            for occurrence, result in sorted(decisions.items(), key=lambda item:
                (item[1].route == "SELECTION_ALREADY_ATTEMPTED", item[0]))]


# 2026-09-28 用户决定：执行被打断（进程重启、调用结果无法核对）时由系统原样重做该步，
# 不交给规划器——规划器面对"结果不明"时既不能重试也无路可走，真机两局因此失败。
# 只在失败的是第 1–4 次执行时这样做，之后仍交给规划器（服务一直坏着不会无限重做）。
NATIVE_RUNTIME_RETRY_MAX_ORDINAL = 4


INTERRUPTED_REVIEW = "Assurance review awaits original-call reconciliation"


def _lost_execution(request: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return (request.get("trigger_source") == "RUNTIME_UNAVAILABLE"
            and context.get("reason") == "provider_outcome_unknown")


def _interrupted_review(request: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """The step's only failure is its content review whose call was interrupted.

    The strict review rules cannot open a second review while the interrupted call is
    still unreconciled (a DeepSeek call never becomes reconcilable), so the step is
    redone instead — the user's intent "an interruption must not end the step".
    """
    if (request.get("trigger_source") != "VERIFIER_ACCEPTANCE_REJECT"
            or context.get("event_type") != "VerificationFailed"):
        return False
    failures = (context.get("detail") or {}).get("failures") or ()
    return bool(failures) and all(
        f.get("layer") == "critic_review" and f.get("status") == "ERROR"
        and INTERRUPTED_REVIEW in str(f.get("summary", "")) for f in failures)


def _runtime_lost_retry(package: Mapping[str, Any]) -> dict[str, Any] | None:
    requests = package.get("repair_requests") or ()
    if len(requests) != 1 or package.get("rejected_refinements") or package.get("planning_rejected"):
        return None
    item = requests[0]
    request = item.get("request") or {}
    context = request.get("context") or {}
    if not (_lost_execution(request, context) or _interrupted_review(request, context)):
        return None
    if (item.get("impact") or {}).get("unresolved_operations"):
        return None
    protocol = package.get("planning_protocol") or {}
    if "RETRY_SAME_METHOD" not in (protocol.get("enabled_repair_kinds") or ()):
        return None
    refs = [str(r) for r in (request.get("trigger_refs") or ()) if ":attempt-" in str(r)]
    if len(refs) != 1:
        return None
    failed = refs[0]
    task_id, marker, ordinal = failed.rpartition(":attempt-")
    if not marker or not ordinal.isdigit() or int(ordinal) > NATIVE_RUNTIME_RETRY_MAX_ORDINAL:
        return None
    subject = next((s for s in package.get("planning_subjects", ()) if s.get("task_id") == task_id), None)
    if subject is None:
        return None
    occurrence = subject["occurrence_id"]
    instance_ids = {
        str(child.get("instance_id"))
        for instance in package.get("active_method_instances", ())
        for child in instance.get("child_bindings", ())
        if occurrence in (child.get("occurrence_id"), child.get("goal_occurrence_id"))
    }
    if len(instance_ids) != 1:
        return None
    instance_id = next(iter(instance_ids))
    ref = [r for r in package.get("visible_refs", ())
           if r.get("kind") == "method_instance" and r.get("id") == instance_id]
    if len(ref) != 1:
        return None
    document: dict[str, Any] = {"schema_version": 1, "subject_key": subject["subject_key"],
        "decision_type": "REPAIR", "reason_refs": [ref[0]], "assumptions": [], "uncertainties": [],
        "alternatives": [], "replan_triggers": [],
        "rationale": ("上一次执行或它的审阅被中断，模型调用的结果无法核对（未记录的回复不会执行任何工具）；"
                      "系统按规则原样重做该步，原调用的用量按上限记账。"),
        "payload": {"repair_kind": "RETRY_SAME_METHOD", "failed_attempt_id": failed,
                    "method_instance_ref": ref[0]}}
    return PlanningDecisionEnvelopeV1.from_json(document).to_json()


def local_decision(package: Mapping[str, Any]) -> dict[str, Any] | None:
    retry = _runtime_lost_retry(package)
    if retry is not None:
        return retry
    # 2026-09-28 真机：同一原生决定被准入拒绝后，下一轮原样再生成、再被拒，三轮耗尽规划
    # 次数。这一问已有被拒记录时交给模型（它能看到拒绝原因），不再原样重复。
    if package.get("repair_requests") or package.get("rejected_refinements") or package.get("planning_rejected"):
        return None
    choices = package.get("method_selection", ())
    if not choices:
        return None
    selected = choices[0]
    route = selected["route"]
    if route not in {"DETERMINISTIC", "SELECTION_ALREADY_ATTEMPTED"}:
        return None
    subject = next(s for s in package["planning_subjects"] if s["occurrence_id"] == selected["occurrence_id"])
    document: dict[str, Any] = {"schema_version": 1, "subject_key": subject["subject_key"],
        "reason_refs": [], "assumptions": [], "uncertainties": [], "alternatives": [], "replan_triggers": []}
    if route == "DETERMINISTIC":
        applicable = selected["applicable"]
        candidate = next((item for item in applicable
                          if item["method_id"] == selected["selected_method_id"]), None)
        if candidate is None:
            raise ContractError("native policy selection is missing from its applicability snapshot")
        refs = [r for r in package["visible_refs"] if r["kind"] == "method"
                and r["id"] == candidate["method_id"]
                and r["semantic_revision"] == candidate["method_version"]
                and r["content_hash"] == candidate["method_content_hash"]]
        if len(refs) != 1:
            raise ContractError("native method selection lacks its authoritative visible reference")
        document.update(decision_type="REFINE", rationale="The frozen applicability snapshot and deployment policy selected this method deterministically.",
                        payload={"method_ref": refs[0], "bindings": selected["bindings"]})
    else:
        document.update(decision_type="NO_CHANGE", rationale="This candidate set already reserved its one model selection call.",
                        payload={"reason": "selection identity already attempted; await a plan or evidence change"})
    return PlanningDecisionEnvelopeV1.from_json(document).to_json()


def reserve_selection(store: Any, intent: Any) -> None:
    package = intent.config.get("planning_package", {})
    if intent.config.get("native_planning_decision") is not None or package.get("repair_requests"):
        return
    choices = package.get("method_selection", ())
    if not choices or choices[0]["route"] != "MODEL_REFINE":
        return
    choice = choices[0]
    identity = choice["identity"]
    for event in store.iter_events(intent.mission_id):
        if event.type == METHOD_SELECTION_CALL_CLAIMED and event.payload.get("identity") == identity:
            if event.payload.get("call_id") != intent.intent_id:
                raise ContractError("selection call already reserved for this plan/evidence/candidate identity")
            return
    append_hierarchical_event(store, METHOD_SELECTION_CALL_CLAIMED, intent.mission_id,
        key=f"{identity['plan_revision']}:{identity['evidence_epoch']}:{identity['candidate_set_digest']}",
        payload={"occurrence_id": choice["occurrence_id"], "identity": identity,
                 "call_id": intent.intent_id, "state": "RESERVED"})


async def dispatch_local(handler: Any, intent: Any) -> bool:
    document = intent.config.get("native_planning_decision")
    if not isinstance(document, Mapping):
        return False
    mission = handler.store.get_mission(intent.mission_id)
    dispatch = None if mission is None else handler._new_mode(mission)
    if mission is None or dispatch is None or intent.kind != "plan":
        raise ContractError("native selection lost its original hierarchical Mission")
    text = "<planning_decision>" + canonical_json(dict(document)) + "</planning_decision>"
    append_hierarchical_event(handler.store, "NativePlanningDecisionPrepared", mission.id,
        key=intent.intent_id, payload={"intent_id": intent.intent_id,
            "origin": "deterministic_method_selection", "decision": dict(document)})
    # No Agent/turn/provider receipt is fabricated. This method already accepts a
    # locally resumed decision and reads the original request's durable authority.
    await handler._collect_plan_decision(intent, None, mission, text, dispatch)
    return True


def awaits_authority(store: Any, intent: Any) -> bool:
    """A new request waits for a real issuer binding before any dispatch.

    Expired/revoked/stale bindings go through original admission and its normal
    refusal path; they cannot turn into an indefinite wait for an immutable row
    to be rewritten. Only absence is an external input still to be supplied.
    """
    if intent.kind != "plan":
        return False
    from ..storage.planning_decision_store import PlanningDecisionStore
    from ..storage.planning_admission_store import PlanningAdmissionStore
    with store.read_view():
        decisions = PlanningDecisionStore(store)
        protocol = decisions.get_mission_protocol(intent.mission_id)
        if (protocol is None or protocol["protocol_version"] != "planning-decision-v1"
                or int(protocol["package_version"]) < 6):
            return False
        request = decisions.get_planning_request_for_intent(intent.intent_id)
        return request is not None and PlanningAdmissionStore(store).get_request_binding(request.request_id) is None
