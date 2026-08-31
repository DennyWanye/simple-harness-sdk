# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace

import pytest

import simple_harness
from simple_harness.contracts import fingerprint_json
from simple_harness.runtime import (
    ContextAssemblyBudget,
    ContextAssemblyDecision,
    ContextAssemblyReasonCode,
    ContextFragment,
    ContextFragmentBindingV2,
    ContextFragmentType,
    DeliveryRecipient,
    DisclosureContext,
    DisclosureGeneration,
    DisclosurePurpose,
    DisclosureReasonCode,
    DisclosureSource,
    DisclosureTrust,
    EvidenceRef,
    InformationAttribute,
    IntendedAudience,
    LongTermMemoryType,
    PrivacyClass,
    RecallBudget,
    RecallCandidateCountStage,
    RecallConfirmationGroupV4,
    RecallConfirmationMemberV4,
    RecallContext,
    RecallContextUseAuthorizationRequestV1,
    RecallContextUseReceiptV1,
    RecallDecision,
    RecallDecisionOutcome,
    RecallFragmentAuthorityBindingV1,
    RecallItemBindingV1,
    RecallItemKind,
    RecallPlan,
    RecallReasonCode,
    RecallResultPageRequestV1,
    RecallResultPageV1,
    RecallRetrievalMode,
    RecallSelectedItemV4,
    RecallSelectorDomain,
    RecallSourceKind,
    TypedRecallConfirmationGroupV1,
    TypedRecallConfirmationMemberV1,
    TypedRecallResultItemV1,
    TypedRecallResultV1,
)


def _disclosure() -> DisclosureContext:
    return DisclosureContext(
        "run-1",
        "user-1",
        DeliveryRecipient.USER_SELF,
        "user-1",
        IntendedAudience.USER_SELF,
        DisclosurePurpose.PERSONALIZATION,
        DisclosureSource.AUTHENTICATED_HOST,
        DisclosureTrust.TRUSTED_AUTHORITY,
        DisclosureGeneration.CURRENT,
        "disclosure-1",
        (DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


def _ref() -> EvidenceRef:
    return EvidenceRef("evidence-1", "e" * 64, 1)


def _context() -> RecallContext:
    return RecallContext(
        "run-1",
        "user-1",
        "turn-1",
        1,
        100.0,
        "How should I answer?",
        None,
        (LongTermMemoryType.SEMANTIC,),
        True,
        (RecallSelectorDomain.MEMORY_TYPE, RecallSelectorDomain.SHORT_HORIZON),
        (RecallRetrievalMode.FULL_TEXT, RecallRetrievalMode.VECTOR),
        (),
        (),
        None,
        None,
        (),
        (),
        (),
        _disclosure(),
        (_ref(),),
        RecallBudget(8, 16_384, 2_048, 1_000),
    )


def _plan(context: RecallContext) -> RecallPlan:
    return RecallPlan(
        "plan-1",
        context.run_id,
        context.subject,
        context.context_hash,
        context.context_revision,
        context.query,
        context.available_memory_types,
        True,
        context.allowed_selector_domains,
        context.allowed_retrieval_modes,
        (),
        (),
        None,
        None,
        (),
        (),
        (),
        context.disclosure_context,
        context.evidence_refs,
        context.budget,
        "idem-1",
        (RecallReasonCode.USER_FACT_DEPENDENCY,),
    )


def _selected_items() -> tuple[RecallSelectedItemV4, RecallSelectedItemV4]:
    semantic_payload = {
        "subject_entity": "user:self",
        "predicate": "style",
        "object_value": "concise",
        "qualifiers": [],
    }
    short_payload = {"content": "Use a concise answer", "occurred_at": 40.0}
    return (
        RecallSelectedItemV4(
            "item-long",
            1,
            RecallItemKind.SELECTED,
            RecallSourceKind.COGNITIVE_MEMORY,
            "memory-1",
            "a" * 64,
            fingerprint_json(semantic_payload),
            LongTermMemoryType.SEMANTIC,
            3,
            None,
        ),
        RecallSelectedItemV4(
            "item-short",
            2,
            RecallItemKind.SELECTED,
            RecallSourceKind.SHORT_HORIZON,
            "chunk-1",
            "b" * 64,
            fingerprint_json(short_payload),
            None,
            None,
            "chunk-1",
        ),
    )


def _decision(context: RecallContext, plan: RecallPlan) -> RecallDecision:
    return RecallDecision(
        "decision-1",
        context.run_id,
        context.subject,
        context.context_hash,
        context.context_revision,
        plan.plan_id,
        plan.plan_hash,
        RecallDecisionOutcome.RECALL,
        _selected_items(),
        (),
        2,
        RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES,
        context.disclosure_context,
        context.evidence_refs,
        (RecallReasonCode.USER_FACT_DEPENDENCY,),
        50.0,
    )


def _result(decision: RecallDecision) -> TypedRecallResultV1:
    semantic_payload = {
        "subject_entity": "user:self",
        "predicate": "style",
        "object_value": "concise",
        "qualifiers": [],
    }
    short_payload = {"content": "Use a concise answer", "occurred_at": 40.0}
    items = (
        TypedRecallResultItemV1(
            decision.selected_items[0],
            semantic_payload,
            PrivacyClass.PERSONAL,
            (InformationAttribute.PREFERENCE,),
            0.9,
            "c" * 64,
            ("task-old",),
            "task-current",
            True,
        ),
        TypedRecallResultItemV1(
            decision.selected_items[1],
            short_payload,
            PrivacyClass.PERSONAL,
            (InformationAttribute.PREFERENCE,),
            0.8,
            "d" * 64,
            (),
            None,
            False,
        ),
    )
    return TypedRecallResultV1(
        "result-1",
        decision.decision_id,
        decision.decision_hash,
        2,
        "f" * 64,
        50.0,
        90.0,
        items,
        (),
        False,
        (RecallReasonCode.USER_FACT_DEPENDENCY,),
    )


def test_v4_mixed_and_short_only_decisions_round_trip_and_v3_wire_rejects() -> None:
    context = _context()
    plan = _plan(context)
    mixed = _decision(context, plan)
    mixed.validate_bindings(context, plan, current_time=50.0)
    assert RecallDecision.from_json(mixed.to_json()) == mixed

    short_plan = replace(
        plan,
        requested_memory_types=(),
        selector_domains=(RecallSelectorDomain.SHORT_HORIZON,),
    )
    short = replace(
        mixed,
        plan_hash=short_plan.plan_hash,
        selected_items=(replace(mixed.selected_items[1], ordinal=1),),
        filtered_candidate_count=1,
        reason_codes=(RecallReasonCode.SHORT_HORIZON_DEPENDENCY,),
    )
    short.validate_bindings(context, short_plan, current_time=50.0)

    old_wire = mixed.to_json()
    old_wire["schema_version"] = 3
    with pytest.raises(ValueError, match="unsupported RecallDecisionV4"):
        RecallDecision.from_json(old_wire)
    with pytest.raises(ValueError, match="fields differ"):
        RecallDecision.from_json({"memory_ref": "memory-1"})


def test_typed_result_page_and_context_use_are_hash_bound_without_naked_refs() -> None:
    context = _context()
    plan = _plan(context)
    decision = _decision(context, plan)
    result = _result(decision)
    result.validate_decision(decision)
    assert TypedRecallResultV1.from_json(result.to_json()) == result

    bindings = tuple(
        RecallItemBindingV1(item.selected_item.item_id, item.result_item_hash)
        for item in result.items
    )
    page_request = RecallResultPageRequestV1(
        result.result_id, result.result_hash, 1, 0, 8, 16_384, 51.0
    )
    page = RecallResultPageV1(
        "page-1",
        page_request.result_id,
        page_request.result_hash,
        1,
        0,
        bindings,
        512,
        True,
    )
    assert RecallResultPageV1.from_json(page.to_json()) == page
    assert "source_ref" not in page_request.to_json()

    fragment_payload = result.items[0].to_json()["public_payload"]
    authority = RecallFragmentAuthorityBindingV1(
        decision.decision_id,
        decision.decision_hash,
        result.result_id,
        result.result_hash,
        bindings[0].item_id,
        bindings[0].item_hash,
        None,
        None,
        page.page_id,
        page.page_hash,
        None,
        None,
        result.items[0].selected_item.public_payload_hash,
    )
    fragment = ContextFragment(
        "fragment-1",
        context.run_id,
        context.subject,
        ContextFragmentType.RECALLED_MEMORY,
        "result-bound:item-1",
        1,
        fragment_payload,
        result.items[0].selected_item.public_payload_hash,
        40,
        120,
        context.disclosure_context,
        context.evidence_refs,
        authority,
    )
    fragment_binding = ContextFragmentBindingV2(fragment.fragment_id, fragment.fragment_hash)
    assembly = ContextAssemblyDecision(
        "assembly-1",
        context.run_id,
        context.subject,
        (fragment_binding,),
        (),
        ("snapshot-1",),
        ContextAssemblyBudget(8_192, 65_536, 2_048, 512),
        40,
        120,
        context.disclosure_context,
        context.evidence_refs,
        (ContextAssemblyReasonCode.INCLUDED,),
        "assembly-idem-1",
    )
    assert assembly.selected_fragment_bindings == (fragment_binding,)

    snapshot_bindings = (fragment_binding,)
    manifest_hash = fingerprint_json([item.to_json() for item in snapshot_bindings])
    request = RecallContextUseAuthorizationRequestV1(
        context.subject,
        context.run_id,
        context.turn_id,
        "attempt-1",
        decision.decision_id,
        decision.decision_hash,
        result.result_id,
        result.result_hash,
        bindings,
        snapshot_bindings,
        manifest_hash,
        52.0,
    )
    receipt = RecallContextUseReceiptV1(
        "use-1",
        request.request_hash,
        request.subject,
        request.run_id,
        request.turn_id,
        request.provider_attempt_id,
        request.decision_id,
        request.decision_hash,
        request.result_id,
        request.result_hash,
        request.item_bindings,
        request.snapshot_manifest_hash,
        2,
        "f" * 64,
        52.0,
        80.0,
    )
    receipt.validate_request(request)
    assert RecallContextUseReceiptV1.from_json(receipt.to_json()) == receipt


def test_atomic_confirmation_result_cannot_page_a_partial_group() -> None:
    members = tuple(
        RecallConfirmationMemberV4(
            f"member-{ordinal}",
            ordinal,
            RecallItemKind.CONFIRMATION_MEMBER,
            "memory-1",
            ordinal,
            LongTermMemoryType.SEMANTIC,
            chr(96 + ordinal) * 64,
            fingerprint_json({"choice": ordinal}),
        )
        for ordinal in (1, 2)
    )
    group = RecallConfirmationGroupV4("group-1", "9" * 64, 1, members)
    typed_members = tuple(
        TypedRecallConfirmationMemberV1(
            member,
            {"choice": ordinal},
            PrivacyClass.PERSONAL,
            (InformationAttribute.PREFERENCE,),
            "8" * 64,
            (),
            None,
            False,
        )
        for ordinal, member in enumerate(members, 1)
    )
    typed_group = TypedRecallConfirmationGroupV1(group, typed_members)
    assert TypedRecallConfirmationGroupV1.from_json(typed_group.to_json()) == typed_group
    with pytest.raises(ValueError, match="complete or ordered"):
        TypedRecallConfirmationGroupV1(group, typed_members[:1])


def test_budget_maxima_capability_order_and_official_exports_are_frozen() -> None:
    for kwargs, field in (
        ({"max_items": 33, "max_bytes": 1, "max_tokens": 1, "deadline_ms": 1}, "max_items"),
        ({"max_items": 1, "max_bytes": 65_537, "max_tokens": 1, "deadline_ms": 1}, "max_bytes"),
        ({"max_items": 1, "max_bytes": 1, "max_tokens": 8_193, "deadline_ms": 1}, "max_tokens"),
        ({"max_items": 1, "max_bytes": 1, "max_tokens": 1, "deadline_ms": 2_001}, "deadline_ms"),
    ):
        with pytest.raises(ValueError, match=field):
            RecallBudget(**kwargs)

    context = _context()
    unsupported_context = replace(
        context,
        allowed_selector_domains=(RecallSelectorDomain.MEMORY_TYPE, RecallSelectorDomain.EVENT),
        event_constraint_refs=("event-1",),
        short_horizon_allowed=False,
        allowed_retrieval_modes=(RecallRetrievalMode.EXACT, RecallRetrievalMode.GRAPH),
    )
    unsupported_plan = RecallPlan(
        "unsupported",
        context.run_id,
        context.subject,
        unsupported_context.context_hash,
        unsupported_context.context_revision,
        context.query,
        context.available_memory_types,
        False,
        unsupported_context.allowed_selector_domains,
        unsupported_context.allowed_retrieval_modes,
        (),
        (),
        None,
        None,
        ("event-1",),
        (),
        (),
        context.disclosure_context,
        context.evidence_refs,
        context.budget,
        "unsupported-idem",
        (RecallReasonCode.USER_FACT_DEPENDENCY,),
    )
    assert unsupported_plan.unsupported_capabilities() == (
        "selector:event",
        "retrieval:exact",
        "retrieval:graph",
    )

    required = {
        "RecallDecisionV4",
        "RecallSelectedItemV4",
        "RecallConfirmationGroupV4",
        "TypedRecallResultV1",
        "RecallResultPageRequestV1",
        "RecallContextUseAuthorizationRequestV1",
        "RecallContextUseReceiptV1",
        "ContextFragmentV2",
        "ContextFragmentBindingV2",
    }
    assert required <= set(simple_harness.__all__)
