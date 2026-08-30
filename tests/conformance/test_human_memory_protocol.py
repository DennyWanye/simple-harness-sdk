# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simple_harness.contracts import canonical_json, fingerprint_json
from simple_harness.runtime.disclosure_protocol import (
    DeliveryRecipient,
    DisclosureContext,
    DisclosureGeneration,
    DisclosurePurpose,
    DisclosureReasonCode,
    DisclosureSource,
    DisclosureTrust,
    IntendedAudience,
)
from simple_harness.runtime.evidence_protocol import (
    AnalysisBudget,
    AnalysisValidationStatus,
    EvidenceReasonCode,
    EvidenceRef,
    EvidenceSourceKind,
    MemoryAnalysisReceipt,
    MemoryAnalysisRequest,
    MemoryAnalysisResult,
    RemovedSpanSummary,
    RemovedSpanType,
    SanitizedEvidenceEnvelope,
    SanitizedEvidenceReceipt,
)
from simple_harness.runtime.memory_protocol import (
    LongTermMemoryType,
    MemoryMutationKind,
    MemoryMutationOperation,
    MemoryMutationPlan,
    RecallBudget,
    RecallDecision,
    RecallDecisionOutcome,
    RecallPlan,
    RecallReasonCode,
    WorkingMemoryRole,
)
from simple_harness.runtime.task_scope_protocol import (
    TaskScopeMutationKind,
    TaskScopeMutationOperation,
    TaskScopeMutationOutcome,
    TaskScopeMutationPlan,
    TaskScopeProposal,
    TaskScopeReasonCode,
    TaskScopeRoute,
)


def _disclosure(
    *,
    recipient: DeliveryRecipient = DeliveryRecipient.USER_SELF,
    authority_ref: str = "host-decision-1",
) -> DisclosureContext:
    return DisclosureContext(
        run_id="run-1",
        subject="actor-1",
        recipient=recipient,
        recipient_id="actor-1",
        intended_audience=IntendedAudience.USER_SELF,
        purpose=DisclosurePurpose.PERSONALIZATION,
        source=DisclosureSource.AUTHENTICATED_HOST,
        trust=DisclosureTrust.TRUSTED_AUTHORITY,
        generation=DisclosureGeneration.CURRENT,
        authority_ref=authority_ref,
        reason_codes=(DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


def _ref() -> EvidenceRef:
    return EvidenceRef("evidence-1", "a" * 64, 1)


def test_disclosure_is_strict_canonical_and_authority_cannot_be_forged() -> None:
    first = _disclosure()
    second = DisclosureContext.from_json(first.to_json())
    assert first.context_hash == second.context_hash
    external = _disclosure(recipient=DeliveryRecipient.EXTERNAL_PARTY)
    assert first.context_hash != external.context_hash
    assert first.context_hash != _disclosure(authority_ref="host-decision-2").context_hash

    extra = dict(first.to_json())
    extra["model_claimed_permission"] = True
    with pytest.raises(ValueError, match="extra"):
        DisclosureContext.from_json(extra)
    unknown = dict(first.to_json())
    unknown["recipient"] = "close_friend"
    with pytest.raises(ValueError):
        DisclosureContext.from_json(unknown)
    with pytest.raises(ValueError, match="trusted disclosure authority"):
        DisclosureContext(
            run_id="run-1",
            subject="actor-1",
            recipient=DeliveryRecipient.EXTERNAL_PARTY,
            recipient_id="recipient-1",
            intended_audience=IntendedAudience.EXTERNAL,
            purpose=DisclosurePurpose.TASK_EXECUTION,
            source=DisclosureSource.LLM_PROPOSAL,
            trust=DisclosureTrust.TRUSTED_AUTHORITY,
            generation=DisclosureGeneration.CURRENT,
            authority_ref="llm-says-so",
            reason_codes=(),
        )


def test_unknown_disclosure_is_explicit_and_fail_closed() -> None:
    context = DisclosureContext(
        run_id="run-1",
        subject="actor-1",
        recipient=DeliveryRecipient.UNKNOWN,
        recipient_id=None,
        intended_audience=IntendedAudience.UNKNOWN,
        purpose=DisclosurePurpose.UNKNOWN,
        source=DisclosureSource.UNKNOWN,
        trust=DisclosureTrust.UNTRUSTED_PROPOSAL,
        generation=DisclosureGeneration.UNKNOWN,
        authority_ref=None,
        reason_codes=(
            DisclosureReasonCode.UNKNOWN_RECIPIENT,
            DisclosureReasonCode.UNKNOWN_PURPOSE,
        ),
    )
    assert context.recipient is DeliveryRecipient.UNKNOWN
    with pytest.raises(ValueError, match="unknown recipient"):
        DisclosureContext(
            run_id="run-1",
            subject="actor-1",
            recipient=DeliveryRecipient.UNKNOWN,
            recipient_id=None,
            intended_audience=IntendedAudience.UNKNOWN,
            purpose=DisclosurePurpose.UNKNOWN,
            source=DisclosureSource.UNKNOWN,
            trust=DisclosureTrust.UNTRUSTED_PROPOSAL,
            generation=DisclosureGeneration.UNKNOWN,
            authority_ref=None,
            reason_codes=(DisclosureReasonCode.UNKNOWN_PURPOSE,),
        )


def test_sanitized_evidence_hash_receipt_and_immutability() -> None:
    payload = {"public_text": "hello", "metadata": {"kind": "turn"}}
    sanitized_hash = fingerprint_json(payload)
    envelope = SanitizedEvidenceEnvelope(
        evidence_id="evidence-2",
        run_id="run-1",
        subject="actor-1",
        source_kind=EvidenceSourceKind.USER_MESSAGE,
        source_ref="turn-1/user",
        source_hash="b" * 64,
        sanitized_payload=payload,
        sanitized_hash=sanitized_hash,
        filter_policy_version="credential-filter/v1",
        removed_spans=(RemovedSpanSummary(RemovedSpanType.ACCESS_TOKEN, 1),),
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
    )
    payload["public_text"] = "mutated"
    assert envelope.sanitized_payload["public_text"] == "hello"
    with pytest.raises(TypeError):
        envelope.sanitized_payload["public_text"] = "bad"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        envelope.source_hash = "c" * 64  # type: ignore[misc]
    with pytest.raises(ValueError, match=EvidenceReasonCode.SANITIZED_HASH_MISMATCH.value):
        SanitizedEvidenceEnvelope(
            evidence_id="evidence-2",
            run_id="run-1",
            subject="actor-1",
            source_kind=EvidenceSourceKind.USER_MESSAGE,
            source_ref="turn-1/user",
            source_hash="b" * 64,
            sanitized_payload={"public_text": "different"},
            sanitized_hash=sanitized_hash,
            filter_policy_version="credential-filter/v1",
            removed_spans=(),
            disclosure_context=_disclosure(),
            evidence_refs=(_ref(),),
        )

    receipt = SanitizedEvidenceReceipt(
        receipt_id="receipt-1",
        run_id="run-1",
        subject="actor-1",
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        source_hash=envelope.source_hash,
        sanitized_hash=envelope.sanitized_hash,
        filter_policy_version=envelope.filter_policy_version,
        accepted=True,
        reason_codes=(EvidenceReasonCode.SANITIZED_AND_ACCEPTED,),
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        admitted_at=10.0,
    )
    receipt.verify(envelope)


def test_long_term_memory_enum_excludes_working_memory_and_plan_hash_is_stable() -> None:
    assert {item.value for item in LongTermMemoryType} == {
        "episode",
        "semantic",
        "procedure",
        "prospective",
    }
    assert all(item.value != "working" for item in LongTermMemoryType)
    assert WorkingMemoryRole.RECENT_CAUSAL_WINDOW.value == "recent_causal_window"
    budget = RecallBudget(8, 16_384, 2048, 1000)
    assert RecallBudget.from_json(budget.to_json()) == budget
    with pytest.raises(ValueError, match="extra"):
        RecallBudget.from_json({**budget.to_json(), "overflow": 1})

    plan = RecallPlan(
        plan_id="recall-plan-1",
        run_id="run-1",
        subject="actor-1",
        query="How should I format this?",
        requested_memory_types=(LongTermMemoryType.PROCEDURE, LongTermMemoryType.SEMANTIC),
        include_short_horizon=True,
        task_scope_ids=(),
        entity_constraints=("answer style",),
        earliest_occurred_at=None,
        latest_occurred_at=None,
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        budget=budget,
        idempotency_key="recall-plan-idem-1",
        reason_codes=(RecallReasonCode.USER_PREFERENCE_DEPENDENCY,),
    )
    assert plan.plan_hash == fingerprint_json(plan.to_json())
    changed = RecallPlan(
        plan_id=plan.plan_id,
        run_id=plan.run_id,
        subject=plan.subject,
        query=plan.query,
        requested_memory_types=plan.requested_memory_types,
        include_short_horizon=plan.include_short_horizon,
        task_scope_ids=plan.task_scope_ids,
        entity_constraints=plan.entity_constraints,
        earliest_occurred_at=plan.earliest_occurred_at,
        latest_occurred_at=plan.latest_occurred_at,
        disclosure_context=_disclosure(recipient=DeliveryRecipient.EXTERNAL_PARTY),
        evidence_refs=plan.evidence_refs,
        budget=plan.budget,
        idempotency_key=plan.idempotency_key,
        reason_codes=plan.reason_codes,
    )
    assert changed.plan_hash != plan.plan_hash


def test_recall_decision_and_memory_mutation_bind_evidence_and_revision() -> None:
    decision = RecallDecision(
        decision_id="decision-1",
        run_id="run-1",
        subject="actor-1",
        plan_id=None,
        plan_hash=None,
        outcome=RecallDecisionOutcome.NO_RECALL,
        selected_memory_types=(),
        selected_memory_refs=(),
        filtered_candidate_count=0,
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        reason_codes=(RecallReasonCode.NO_RECALL_CONTEXT_SUFFICIENT,),
        decided_at=11.0,
    )
    assert decision.decision_hash == fingerprint_json(decision.to_json())
    with pytest.raises(ValueError, match="no_recall"):
        RecallDecision(
            decision_id="decision-2",
            run_id="run-1",
            subject="actor-1",
            plan_id=None,
            plan_hash=None,
            outcome=RecallDecisionOutcome.NO_RECALL,
            selected_memory_types=(LongTermMemoryType.SEMANTIC,),
            selected_memory_refs=("memory-1",),
            filtered_candidate_count=0,
            disclosure_context=_disclosure(),
            evidence_refs=(_ref(),),
            reason_codes=(RecallReasonCode.NO_RECALL_CONTEXT_SUFFICIENT,),
            decided_at=11.0,
        )
    operation = MemoryMutationOperation(
        "operation-1",
        MemoryMutationKind.CREATE,
        LongTermMemoryType.SEMANTIC,
        None,
        "User prefers concise answers.",
        (_ref(),),
        "explicit_user_assertion",
    )
    mutation = MemoryMutationPlan(
        "mutation-plan-1",
        "run-1",
        "actor-1",
        1,
        (operation,),
        _disclosure(),
        (_ref(),),
        "mutation-idem-1",
    )
    assert mutation.plan_hash == fingerprint_json(mutation.to_json())


@pytest.mark.parametrize("route", tuple(TaskScopeRoute))
def test_task_scope_has_exact_five_routes(route: TaskScopeRoute) -> None:
    kwargs: dict[str, object] = {
        "proposal_id": f"proposal-{route.value}",
        "run_id": "run-1",
        "subject": "actor-1",
        "route": route,
        "target_task_scope_id": None,
        "goal": None,
        "project_hint": None,
        "confidence_millionths": 900_000,
        "disclosure_context": _disclosure(),
        "evidence_refs": (_ref(),),
        "idempotency_key": f"idem-{route.value}",
        "reason_codes": (TaskScopeReasonCode.SELF_CONTAINED,),
    }
    if route is TaskScopeRoute.CREATE_NEW:
        kwargs["goal"] = "Implement the project"
    elif route in {TaskScopeRoute.CONTINUE_ACTIVE, TaskScopeRoute.RESUME_EXISTING}:
        kwargs["target_task_scope_id"] = "task-1"
    proposal = TaskScopeProposal(**kwargs)  # type: ignore[arg-type]
    assert proposal.route is route


def test_task_scope_mutation_is_revisioned_and_no_mutation_is_explicit() -> None:
    operation = TaskScopeMutationOperation(
        "operation-1",
        TaskScopeMutationKind.PLAN_STEP_ADD,
        "Implement protocol DTOs",
        (_ref(),),
        "plan_step_added",
    )
    plan = TaskScopeMutationPlan(
        "task-mutation-1",
        "run-1",
        "actor-1",
        "task-1",
        3,
        TaskScopeMutationOutcome.MUTATE,
        (operation,),
        None,
        "turn-1",
        _disclosure(),
        (_ref(),),
        "task-mutation-idem-1",
    )
    assert plan.plan_hash == fingerprint_json(plan.to_json())
    no_change = TaskScopeMutationPlan(
        "task-mutation-2",
        "run-1",
        "actor-1",
        "task-1",
        3,
        TaskScopeMutationOutcome.NO_MUTATION,
        (),
        "Tool evidence does not change the semantic plan.",
        "turn-1",
        _disclosure(),
        (_ref(),),
        "task-mutation-idem-2",
    )
    assert no_change.operations == ()


def test_memory_analysis_contract_binds_model_configuration_usage_and_validator() -> None:
    request = MemoryAnalysisRequest(
        job_id="analysis-job-1",
        run_id="run-1",
        subject="actor-1",
        ordered_evidence_refs=(_ref(),),
        prompt_version="memory-analysis/v1",
        result_schema_version="memory-mutation/v1",
        policy_version="memory-policy/v1",
        provider_id="provider-1",
        model_id="model-1",
        model_config_hash="c" * 64,
        attempt=1,
        budget=AnalysisBudget(4096, 1024, 3000, 100_000),
        disclosure_context=_disclosure(),
        idempotency_key="analysis-idem-1",
    )
    result = MemoryAnalysisResult(
        job_id=request.job_id,
        run_id=request.run_id,
        request_hash=request.request_hash,
        provider_response_id="provider-response-1",
        structured_result={"outcome": "no_mutation", "operations": []},
        input_tokens=500,
        output_tokens=30,
        cost_microunits=200,
        latency_ms=250,
    )
    receipt = MemoryAnalysisReceipt(
        receipt_id="analysis-receipt-1",
        job_id=request.job_id,
        run_id=request.run_id,
        request_hash=request.request_hash,
        result_hash=result.result_hash,
        validator_version="memory-validator/v1",
        validation_status=AnalysisValidationStatus.ACCEPTED,
        reason_codes=(EvidenceReasonCode.VALIDATOR_ACCEPTED,),
        committed_revision=1,
        committed_at=12.0,
    )
    assert receipt.result_hash == result.result_hash
    assert canonical_json(result.to_json())
