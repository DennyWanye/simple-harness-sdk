from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.htn.repair_decision import (
    HumanRequestV1,
    ImpactAnalysis,
    RepairAction,
    RepairActionType,
    RepairCause,
    RepairDecision,
    RepairDecisionLedger,
    RepairDecisionStatus,
    RepairDiagnosis,
    RepairRequestV1,
    RepairTriggerSource,
    analyze_impact,
    validate_repair_decision,
)


def request(**kwargs: object) -> RepairRequestV1:
    return RepairRequestV1.from_trigger(
        RepairTriggerSource.WORKER_REJECT,
        trigger_refs=("task-a",),
        mission_id="m1",
        diagnosis=RepairCause.IMPLEMENTATION_DEFECT,
        **kwargs,
    )


def test_h4_action_vocabulary_is_exactly_eleven_values() -> None:
    assert {item.value for item in RepairActionType} == {
        "RETRY_SAME_METHOD",
        "REFINE_DEEPER",
        "REQUEST_EVIDENCE",
        "REPLACE_METHOD",
        "REBIND_INPUT",
        "BIND_EXISTING_GOAL",
        "CANCEL_BRANCH",
        "REQUEST_COMPENSATION",
        "DECLARE_RUNTIME_BLOCKED",
        "ESCALATE",
        "PROPOSE_SUCCESSOR",
    }


def test_request_round_trip_and_id_is_content_derived() -> None:
    original = request(affected_hints=("task-hint",), context={"reason": "reject"})
    decoded = RepairRequestV1.from_json(original.to_json())
    assert decoded == original
    assert original.request_id == original.idempotency_key
    assert (
        RepairRequestV1.from_trigger(
            RepairTriggerSource.WORKER_REJECT,
            trigger_refs=("task-a",),
            mission_id="m1",
            diagnosis=RepairCause.IMPLEMENTATION_DEFECT,
            affected_hints=("different",),
            context={"reason": "reject"},
        ).request_id
        != original.request_id
    )


def test_all_five_trigger_sources_share_one_request_shape() -> None:
    for source in RepairTriggerSource:
        item = RepairRequestV1.from_trigger(source, trigger_refs=("e-1",))
        assert item.trigger_source is source
        assert RepairRequestV1.from_json(item.to_json()) == item


def test_impact_analysis_uses_program_indexes_not_model_hints() -> None:
    impact = analyze_impact(
        trigger_refs=("task-a",),
        affected_hints=("unrelated",),
        all_items=("task-a", "task-b", "task-c"),
        reverse_data={"task-a": ("task-b",)},
        reverse_support={"task-b": ("task-c",)},
        operation_states={"op-unknown": "UNKNOWN"},
    )
    assert impact.revalidate == ("task-a", "task-b", "task-c")
    assert impact.supersede == ()
    assert impact.unresolved_operations == ("op-unknown",)
    assert "unrelated" not in impact.revalidate
    assert impact.unknown_coverage == ("op-unknown",)


def test_unknown_operation_defers_even_if_model_requests_replacement() -> None:
    request_value = request()
    impact = analyze_impact(trigger_refs=("task-a",), operation_states={"op-1": "UNKNOWN"})
    decision = RepairDecision(
        request=request_value,
        actions=(RepairAction(RepairActionType.REPLACE_METHOD, target_refs=("method-b",)),),
        impact=impact,
    )
    assert validate_repair_decision(decision) is RepairDecisionStatus.DEFERRED


def test_request_human_is_not_automatically_admitted() -> None:
    decision = RepairDecision(
        request=request(),
        actions=(RepairAction(RepairActionType.REQUEST_EVIDENCE),),
        impact=ImpactAnalysis(),
        status=RepairDecisionStatus.REQUEST_HUMAN,
        human_request=HumanRequestV1("Choose a recovery path", options=("retry", "escalate")),
    )
    assert validate_repair_decision(decision) is RepairDecisionStatus.REQUEST_HUMAN


def test_ledger_is_restart_safe_and_idempotent() -> None:
    decision = RepairDecision(
        request=request(),
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
        impact=ImpactAnalysis(),
    )
    first = RepairDecisionLedger().record(decision)
    restored = RepairDecisionLedger({decision.request.idempotency_key: decision})
    assert restored.record(decision) is decision
    assert restored.get(decision.request) == first
    changed = RepairDecision(
        request=request(context={"different": True}),
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
        impact=ImpactAnalysis(),
    )
    assert restored.get(changed.request) is None


def test_request_rejects_tampered_id_and_impact_overlap() -> None:
    item = request().to_json()
    item["request_id"] = "0" * 64
    with pytest.raises(ContractError):
        RepairRequestV1.from_json(item)
    with pytest.raises(ContractError):
        ImpactAnalysis(retained=("a",), revalidate=("a",))


def test_compact_ast_constructor_and_decision_round_trip() -> None:
    decision = RepairDecision(
        trigger_refs=("task-a",),
        diagnosis=RepairDiagnosis(RepairCause.INPUT_STALE, detail="manifest changed"),
        actions=(RepairAction(RepairActionType.REBIND_INPUT, target_refs=("input-1",)),),
    )
    assert RepairDecision.from_json(decision.to_json()) == decision


def test_restart_snapshot_restores_ledger() -> None:
    decision = RepairDecision(
        request=request(),
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
        impact=ImpactAnalysis(),
    )
    restored = RepairDecisionLedger.from_snapshot(
        RepairDecisionLedger({decision.request.idempotency_key: decision}).snapshot()
    )
    assert restored.get(decision.request) == decision


def test_unknown_coverage_without_unknown_operation_still_defers() -> None:
    decision = RepairDecision(
        request=request(),
        actions=(RepairAction(RepairActionType.REPLACE_METHOD),),
        impact=ImpactAnalysis(unknown_coverage=("unindexed-source",)),
    )
    assert validate_repair_decision(decision) is RepairDecisionStatus.DEFERRED


def test_same_request_cannot_replace_its_recorded_decision_after_restart() -> None:
    original = RepairDecision(
        request=request(),
        actions=(RepairAction(RepairActionType.RETRY_SAME_METHOD),),
    )
    ledger = RepairDecisionLedger()
    ledger.record(original)
    ledger = RepairDecisionLedger.from_snapshot(ledger.snapshot())
    changed = RepairDecision(
        request=original.request,
        actions=(RepairAction(RepairActionType.REPLACE_METHOD),),
    )
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="different repair decision"):
        ledger.record(changed)
    assert ledger.snapshot() == before
