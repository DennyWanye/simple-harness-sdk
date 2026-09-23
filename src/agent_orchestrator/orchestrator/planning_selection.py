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


def local_decision(package: Mapping[str, Any]) -> dict[str, Any] | None:
    if package.get("repair_requests") or package.get("rejected_refinements"):
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
