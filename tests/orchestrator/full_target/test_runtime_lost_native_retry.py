"""2026-09-28 用户决定：执行被打断、调用结果无法核对时由系统原样重做该步，不交给规划器。
真机两局：规划器面对"结果不明"不能重试，只能声明受阻（写了列表外的原因），任务失败。"""

import copy

from agent_orchestrator.contracts.planning_decisions import PlanningDecisionEnvelopeV1
from agent_orchestrator.orchestrator.planning_selection import local_decision

REF = {"kind": "method_instance", "id": "mi-bc2f82cc2378f8abde9b99dadbebb4b5",
       "semantic_revision": 1, "content_hash": "22e93841b021db7ae10a571eb248d35cbd646ac75b49d6155fe78361ced7087e"}
PACKAGE = {
    "repair_requests": [{
        "request": {"trigger_source": "RUNTIME_UNAVAILABLE",
                    "context": {"event_type": "RuntimeUnavailable", "reason": "provider_outcome_unknown"},
                    "trigger_refs": ["task-742189979604bf7b96ea9335250e4a2c:attempt-2"]},
        "impact": {"unresolved_operations": [], "unknown_coverage": []},
    }],
    "rejected_refinements": [],
    "method_selection": [],
    "planning_protocol": {"enabled_repair_kinds": ["RETRY_SAME_METHOD", "DECLARE_RUNTIME_BLOCKED"]},
    "planning_subjects": [
        {"task_id": "desktop-root-mission-1", "occurrence_id": "root", "subject_key": "subject-14d1cdca7132ea080c4c4f4aaf3c3b76"},
        {"task_id": "task-742189979604bf7b96ea9335250e4a2c", "occurrence_id": "occ-960093fa91af16b8a9d2eaf91f4cdd71",
         "subject_key": "subject-4a288d48b1869537acc9985ee11bba2f"},
    ],
    "active_method_instances": [{"child_bindings": [
        {"occurrence_id": "occ-960093fa91af16b8a9d2eaf91f4cdd71", "goal_occurrence_id": "occ-960093fa91af16b8a9d2eaf91f4cdd71",
         "instance_id": REF["id"]},
        {"occurrence_id": "occ-other", "goal_occurrence_id": "occ-other", "instance_id": REF["id"]},
    ]}],
    "visible_refs": [REF, {"kind": "method", "id": "m", "semantic_revision": 1, "content_hash": "b" * 64}],
}


def test_a_lost_attempt_is_retried_by_the_system_with_the_same_method():
    decision = local_decision(PACKAGE)
    assert decision is not None
    envelope = PlanningDecisionEnvelopeV1.from_json(decision)
    assert decision["decision_type"] == "REPAIR"
    assert decision["subject_key"] == "subject-4a288d48b1869537acc9985ee11bba2f"
    assert decision["payload"]["repair_kind"] == "RETRY_SAME_METHOD"
    assert decision["payload"]["failed_attempt_id"] == "task-742189979604bf7b96ea9335250e4a2c:attempt-2"
    assert decision["payload"]["method_instance_ref"] == REF
    assert envelope is not None


def _variant(**change):
    package = copy.deepcopy(PACKAGE)
    request = package["repair_requests"][0]
    for key, value in change.items():
        if key == "reason":
            request["request"]["context"]["reason"] = value
        elif key == "attempt":
            request["request"]["trigger_refs"] = [value]
        elif key == "unresolved":
            request["impact"]["unresolved_operations"] = value
        elif key == "kinds":
            package["planning_protocol"]["enabled_repair_kinds"] = value
    return package


def test_anything_else_still_goes_to_the_planner():
    assert local_decision(_variant(reason="executor_stalled")) is None
    assert local_decision(_variant(attempt="task-742189979604bf7b96ea9335250e4a2c:attempt-5")) is None
    assert local_decision(_variant(unresolved=["op-1"])) is None
    assert local_decision(_variant(kinds=["DECLARE_RUNTIME_BLOCKED"])) is None


def test_an_interrupted_review_redoes_the_step_but_a_real_rejection_does_not():
    package = copy.deepcopy(PACKAGE)
    package["repair_requests"][0]["request"] = {
        "trigger_source": "VERIFIER_ACCEPTANCE_REJECT",
        "context": {"event_type": "VerificationFailed", "detail": {"failures": [
            {"layer": "critic_review", "status": "ERROR",
             "summary": "critic verdict unusable: Assurance review awaits original-call reconciliation"}]}},
        "trigger_refs": ["task-742189979604bf7b96ea9335250e4a2c:attempt-1", "task-742189979604bf7b96ea9335250e4a2c"],
    }
    decision = local_decision(package)
    assert decision is not None
    assert decision["payload"]["failed_attempt_id"] == "task-742189979604bf7b96ea9335250e4a2c:attempt-1"
    rejected = copy.deepcopy(package)
    rejected["repair_requests"][0]["request"]["context"]["detail"]["failures"] = [
        {"layer": "critic_review", "status": "FAIL", "summary": "README lacks a real sample output"}]
    assert local_decision(rejected) is None
    mixed = copy.deepcopy(package)
    mixed["repair_requests"][0]["request"]["context"]["detail"]["failures"].append(
        {"layer": "code_test", "status": "FAIL", "summary": "pytest failed"})
    assert local_decision(mixed) is None


def test_a_refused_native_decision_is_not_repeated():
    refused = copy.deepcopy(PACKAGE)
    refused["planning_rejected"] = [{"reason": "proposal_not_grounded", "detail": {}}]
    assert local_decision(refused) is None
    selection = {"repair_requests": [], "rejected_refinements": [], "method_selection": [{"route": "DETERMINISTIC"}],
                 "planning_rejected": [{"reason": "proposal_not_grounded", "detail": {}}]}
    assert local_decision(selection) is None
