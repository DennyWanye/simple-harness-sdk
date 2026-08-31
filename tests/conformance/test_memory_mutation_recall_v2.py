# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

import pytest

import simple_harness
import simple_harness.runtime as runtime
from simple_harness.contracts import fingerprint_json
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
    EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
    EvidenceActorRole,
    EvidenceProvenance,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceSpanRef,
    EvidenceSupportKind,
    ProposedTypedObservationRef,
)
from simple_harness.runtime.memory_action_protocol import (
    MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION,
    MemoryActionAuthority,
    MemoryActionAuthorityRef,
    MemoryActionIntent,
    MemoryActionKind,
    issue_memory_action_authority,
    verify_memory_action_authority,
)
from simple_harness.runtime.memory_protocol import (
    MEMORY_MUTATION_SCHEMA_VERSION,
    RECALL_DECISION_SCHEMA_VERSION,
    ConflictStatus,
    CreatedByOperationTarget,
    EpisodeLifecycleState,
    EpisodeMemoryPayload,
    EpistemicStatus,
    ExistingMemoryTarget,
    InformationAttribute,
    LongTermMemoryType,
    MemoryActionConfirmationItem,
    MemoryMutationApplyMode,
    MemoryMutationApplyOutcome,
    MemoryMutationApplyReasonCode,
    MemoryMutationApplyReceipt,
    MemoryMutationApplyReceiptRef,
    MemoryMutationApplyResult,
    MemoryMutationKind,
    MemoryMutationOperation,
    MemoryMutationPlan,
    MemoryMutationPlanOutcome,
    PrivacyClass,
    ProcedureLifecycleState,
    ProcedureMemoryPayload,
    ProcedureRiskLevel,
    ProspectiveEventTrigger,
    ProspectiveLifecycleState,
    ProspectiveMemoryPayload,
    ProspectiveTimeTrigger,
    RecallBudget,
    RecallCandidateCountStage,
    RecallConfirmationItem,
    RecallContext,
    RecallDecision,
    RecallDecisionOutcome,
    RecallPlan,
    RecallReasonCode,
    RecallRetrievalMode,
    RecallSelectorDomain,
    SemanticLifecycleState,
    SemanticMemoryPayload,
    ValidTimeInterval,
    VerificationState,
    verify_memory_mutation_apply_receipt,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_cognitive_mutation_and_recall_contracts_are_public() -> None:
    names = (
        "MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION",
        "MEMORY_MUTATION_SCHEMA_VERSION",
        "EpisodeMemoryPayload",
        "SemanticMemoryPayload",
        "ProcedureMemoryPayload",
        "ProspectiveMemoryPayload",
        "MemoryMutationPlanOutcome",
        "MemoryMutationApplyMode",
        "MemoryMutationApplyReceipt",
        "MemoryMutationApplyReceiptRef",
        "MemoryActionAuthority",
        "MemoryActionAuthorityPort",
        "MemoryActionAuthorityRef",
        "MemoryActionConfirmationItem",
        "MemoryActionIntent",
        "MemoryActionKind",
        "MemoryMutationApplyOutcome",
        "MemoryMutationApplyReasonCode",
        "MemoryMutationApplyResult",
        "issue_memory_action_authority",
        "verify_memory_action_authority",
        "verify_memory_mutation_apply_receipt",
        "RecallSelectorDomain",
        "RecallRetrievalMode",
        "RecallConfirmationItem",
        "RECALL_DECISION_SCHEMA_VERSION",
        "PrivacyClass",
        "InformationAttribute",
    )
    for name in names:
        assert name in simple_harness.__all__
        assert name in runtime.__all__
        assert getattr(simple_harness, name) is getattr(runtime, name)


def _disclosure() -> DisclosureContext:
    return DisclosureContext(
        run_id="run-1",
        subject="user-1",
        recipient=DeliveryRecipient.USER_SELF,
        recipient_id="user-1",
        intended_audience=IntendedAudience.USER_SELF,
        purpose=DisclosurePurpose.PERSONALIZATION,
        source=DisclosureSource.AUTHENTICATED_HOST,
        trust=DisclosureTrust.TRUSTED_AUTHORITY,
        generation=DisclosureGeneration.CURRENT,
        authority_ref="host-disclosure-1",
        reason_codes=(DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


def _ref() -> EvidenceRef:
    return EvidenceRef("evidence-1", "b" * 64, 1)


def _typed_observation() -> ProposedTypedObservationRef:
    return ProposedTypedObservationRef(
        schema_id="com.simple-harness.observation",
        schema_version=1,
        registered_schema_hash="1" * 64,
        observation_receipt_id="observation-receipt-1",
        observation_receipt_hash="2" * 64,
        authority_issuer_id="host-observation-registry-1",
        json_pointer="/result/version",
        value_hash="3" * 64,
    )


def _span(
    *,
    source_kind: EvidenceSourceKind = EvidenceSourceKind.USER_MESSAGE,
    actor_role: EvidenceActorRole = EvidenceActorRole.USER,
    provenance: EvidenceProvenance = EvidenceProvenance.AUTHENTICATED_USER,
    support_kind: EvidenceSupportKind = EvidenceSupportKind.EXPLICIT_USER_ASSERTION,
    typed_observation: ProposedTypedObservationRef | None = None,
) -> EvidenceSpanRef:
    quote = "Python 3.12"
    return EvidenceSpanRef(
        span_id="span-1",
        evidence_id="evidence-1",
        envelope_hash="b" * 64,
        sanitized_hash="c" * 64,
        admission_receipt_id="receipt-1",
        admission_receipt_hash="d" * 64,
        source_kind=source_kind,
        item_ordinal=1,
        item_id="message-1",
        item_json_pointer="/public_text",
        start_byte=7,
        end_byte=18,
        exact_quote=quote,
        quote_hash=_sha(quote),
        source_hash="e" * 64,
        normalization_version=EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
        actor_role=actor_role,
        provenance=provenance,
        support_kind=support_kind,
        typed_observation=typed_observation,
    )


def _semantic_operation(
    operation_id: str,
    *,
    kind: MemoryMutationKind = MemoryMutationKind.CREATE,
    target: ExistingMemoryTarget | CreatedByOperationTarget | None = None,
    depends_on: tuple[str, ...] = (),
) -> MemoryMutationOperation:
    return MemoryMutationOperation(
        operation_id=operation_id,
        kind=kind,
        memory_type=LongTermMemoryType.SEMANTIC,
        payload=SemanticMemoryPayload(
            subject_entity="user:self",
            predicate="runtime_preference",
            object_value={"runtime": "python", "version": [3, 12]},
            qualifiers=("primary",),
        ),
        target=target,
        depends_on_operation_ids=depends_on,
        lifecycle_state=SemanticLifecycleState.ACTIVE,
        epistemic_status=EpistemicStatus.EXPLICIT_USER,
        conflict_status=(
            ConflictStatus.CONTESTED
            if kind is MemoryMutationKind.CONTEST
            else ConflictStatus.UNCONTESTED
        ),
        verification_state=VerificationState.SOURCE_BOUND,
        valid_time_interval=ValidTimeInterval(10.0, None),
        proposed_privacy_class=PrivacyClass.PERSONAL,
        proposed_information_attributes=(InformationAttribute.PREFERENCE,),
        evidence_spans=(_span(),),
        reason_code="explicit_user_assertion",
    )


def test_mutation_v4_target_union_no_mutation_and_order_independent_dag() -> None:
    create = _semantic_operation("create")
    revise = _semantic_operation(
        "revise",
        kind=MemoryMutationKind.CONTEST,
        target=CreatedByOperationTarget("create"),
        depends_on=("create",),
    )
    plan = MemoryMutationPlan(
        plan_id="mutation-1",
        run_id="run-1",
        turn_id="turn-1",
        subject="user-1",
        base_revision=4,
        outcome=MemoryMutationPlanOutcome.MUTATE,
        operations=(revise, create),
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        idempotency_key="mutation-idem-1",
    )
    assert [item.operation_id for item in plan.topological_operations()] == ["create", "revise"]
    ordered_plan = replace(plan, operations=(create, revise))
    assert ordered_plan.to_json() == plan.to_json()
    assert ordered_plan.plan_hash == plan.plan_hash
    assert MemoryMutationPlan.from_json(plan.to_json()) == plan
    assert plan.plan_hash != _sha("not-domain-bound")
    assert (
        revise.effective_privacy_class(
            PrivacyClass.PUBLIC,
            PrivacyClass.SENSITIVE,
            PrivacyClass.RESTRICTED,
        )
        is PrivacyClass.RESTRICTED
    )
    assert revise.effective_information_attributes(
        (InformationAttribute.HEALTH,),
        (InformationAttribute.FINANCIAL,),
    ) == (
        InformationAttribute.FINANCIAL,
        InformationAttribute.HEALTH,
        InformationAttribute.PREFERENCE,
    )
    with pytest.raises(ValueError, match="trusted floor"):
        revise.effective_privacy_class()

    no_mutation = MemoryMutationPlan(
        "mutation-2",
        "run-1",
        "turn-1",
        "user-1",
        4,
        MemoryMutationPlanOutcome.NO_MUTATION,
        (),
        _disclosure(),
        (_ref(),),
        "mutation-idem-2",
    )
    assert MemoryMutationPlan.from_json(no_mutation.to_json()) == no_mutation
    with pytest.raises(ValueError, match="no_mutation"):
        MemoryMutationPlan(
            "bad", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.NO_MUTATION,
            (create,), _disclosure(), (_ref(),), "bad-idem",
        )
    with pytest.raises(ValueError, match="unknown"):
        MemoryMutationPlan(
            "bad", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE,
            (_semantic_operation("x", depends_on=("missing",)),),
            _disclosure(), (_ref(),), "bad-idem",
        )

    suppress = replace(
        create,
        operation_id="suppress",
        kind=MemoryMutationKind.SUPPRESS,
        payload=None,
        target=ExistingMemoryTarget("memory-1", 8),
        lifecycle_state=SemanticLifecycleState.FORGOTTEN,
    )
    assert MemoryMutationOperation.from_json(suppress.to_json()) == suppress
    with pytest.raises(ValueError, match="cycle"):
        MemoryMutationPlan(
            "cycle", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE,
            (
                replace(
                    revise,
                    operation_id="a",
                    target=ExistingMemoryTarget("memory-a", 1),
                    depends_on_operation_ids=("b",),
                ),
                replace(
                    revise,
                    operation_id="b",
                    target=ExistingMemoryTarget("memory-b", 1),
                    depends_on_operation_ids=("a",),
                ),
            ),
            _disclosure(), (_ref(),), "cycle-idem",
        )
    with pytest.raises(ValueError, match="unique"):
        replace(create, depends_on_operation_ids=("x", "x"))


def test_mutation_epistemic_status_requires_exact_evidence_provenance() -> None:
    explicit = _semantic_operation("explicit")
    model_span = _span(
        source_kind=EvidenceSourceKind.ASSISTANT_MESSAGE,
        actor_role=EvidenceActorRole.ASSISTANT,
        provenance=EvidenceProvenance.MODEL_OUTPUT,
        support_kind=EvidenceSupportKind.MODEL_INFERENCE,
    )
    with pytest.raises(ValueError, match="explicit_user"):
        replace(explicit, evidence_spans=(model_span,))
    assert replace(
        explicit,
        epistemic_status=EpistemicStatus.LLM_INFERENCE,
        verification_state=VerificationState.UNVERIFIED,
        evidence_spans=(model_span,),
    ).epistemic_status is EpistemicStatus.LLM_INFERENCE
    with pytest.raises(ValueError, match="llm_inference"):
        replace(
            explicit,
            epistemic_status=EpistemicStatus.LLM_INFERENCE,
            verification_state=VerificationState.UNVERIFIED,
        )

    external_span = _span(
        source_kind=EvidenceSourceKind.PROVIDER_RECORD,
        actor_role=EvidenceActorRole.EXTERNAL,
        provenance=EvidenceProvenance.EXTERNAL_SOURCE,
        support_kind=EvidenceSupportKind.TYPED_OBSERVATION,
        typed_observation=_typed_observation(),
    )
    verified_external = replace(
        explicit,
        epistemic_status=EpistemicStatus.VERIFIED_EXTERNAL,
        verification_state=VerificationState.SOURCE_VERIFIED,
        evidence_spans=(external_span,),
    )
    assert verified_external.epistemic_status is EpistemicStatus.VERIFIED_EXTERNAL
    for untrusted in (
        _span(),
        model_span,
        _span(support_kind=EvidenceSupportKind.CONTEXT_ONLY),
    ):
        with pytest.raises(ValueError, match="external typed authority"):
            replace(verified_external, evidence_spans=(untrusted,))
    with pytest.raises(ValueError, match="source_verified"):
        replace(
            verified_external,
            verification_state=VerificationState.SOURCE_BOUND,
        )

    tool_span = _span(
        source_kind=EvidenceSourceKind.TOOL_RESULT,
        actor_role=EvidenceActorRole.TOOL,
        provenance=EvidenceProvenance.TRUSTED_TOOL,
        support_kind=EvidenceSupportKind.TYPED_OBSERVATION,
        typed_observation=_typed_observation(),
    )
    observed_tool = replace(
        explicit,
        epistemic_status=EpistemicStatus.OBSERVED_BEHAVIOR,
        verification_state=VerificationState.SOURCE_BOUND,
        evidence_spans=(tool_span,),
    )
    assert observed_tool.epistemic_status is EpistemicStatus.OBSERVED_BEHAVIOR
    runtime_span = _span(
        source_kind=EvidenceSourceKind.RUNTIME_EVENT,
        actor_role=EvidenceActorRole.RUNTIME,
        provenance=EvidenceProvenance.HOST_RUNTIME,
        support_kind=EvidenceSupportKind.RUNTIME_EVENT,
    )
    assert replace(observed_tool, evidence_spans=(runtime_span,)).evidence_spans
    with pytest.raises(ValueError, match="trusted Tool or Runtime"):
        replace(observed_tool, evidence_spans=(_span(),))

    with pytest.raises(ValueError, match="trusted typed observation"):
        replace(
            explicit,
            verification_state=VerificationState.SOURCE_VERIFIED,
        )


def test_mutation_plan_rejects_invalid_created_by_producer_and_uncovered_span() -> None:
    create = _semantic_operation("create")
    producer_not_create = replace(
        _semantic_operation("producer"),
        kind=MemoryMutationKind.REVISE,
        target=ExistingMemoryTarget("existing-1", 1),
    )
    consumer = _semantic_operation(
        "consumer",
        kind=MemoryMutationKind.CONTEST,
        target=CreatedByOperationTarget("producer"),
        depends_on=("producer",),
    )
    with pytest.raises(ValueError, match="exact ExistingMemoryTarget"):
        _semantic_operation(
            "unsafe-revise",
            kind=MemoryMutationKind.REVISE,
            target=CreatedByOperationTarget("producer"),
            depends_on=("producer",),
        )
    with pytest.raises(ValueError, match="producer must be a create"):
        MemoryMutationPlan(
            "bad-producer", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE, (producer_not_create, consumer),
            _disclosure(), (_ref(),), "bad-producer-idem",
        )

    episode_create = replace(
        create,
        operation_id="episode-create",
        memory_type=LongTermMemoryType.EPISODE,
        payload=EpisodeMemoryPayload(
            "decision", ("user:self",), ("ship",), ("test",), ("green",),
            ("released",), 10.0, 11.0, "thread-1",
        ),
        lifecycle_state=EpisodeLifecycleState.ACTIVE,
    )
    wrong_type_consumer = replace(
        consumer,
        target=CreatedByOperationTarget("episode-create"),
        depends_on_operation_ids=("episode-create",),
    )
    with pytest.raises(ValueError, match="same memory_type"):
        MemoryMutationPlan(
            "bad-type", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE,
            (episode_create, wrong_type_consumer), _disclosure(), (_ref(),),
            "bad-type-idem",
        )

    with pytest.raises(ValueError, match="cover every evidence span"):
        MemoryMutationPlan(
            "bad-evidence", "run-1", "turn-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE, (create,), _disclosure(),
            (EvidenceRef("evidence-1", "f" * 64, 1),), "bad-evidence-idem",
        )


class _ActionAuthority:
    def __init__(self, authority: MemoryActionAuthority) -> None:
        self.authority = authority
        self.calls = 0

    async def resolve_memory_action_authority(
        self, reference: MemoryActionAuthorityRef
    ) -> MemoryActionAuthority:
        del reference
        self.calls += 1
        return self.authority


def _authorized_revise_plan() -> tuple[
    MemoryMutationPlan,
    MemoryMutationOperation,
    MemoryActionIntent,
    MemoryActionAuthority,
    MemoryActionAuthorityRef,
]:
    operation = _semantic_operation(
        "revise-authorized",
        kind=MemoryMutationKind.REVISE,
        target=ExistingMemoryTarget("memory-1", 7),
    )
    proposal = MemoryMutationPlan(
        plan_id="action-plan-1",
        run_id="run-1",
        turn_id="turn-1",
        subject="user-1",
        base_revision=7,
        outcome=MemoryMutationPlanOutcome.MUTATE,
        operations=(operation,),
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        idempotency_key="action-plan-idem-1",
    )
    intent = proposal.action_intent(operation.operation_id)
    authority = issue_memory_action_authority(
        intent,
        authority_id="memory-action-authority-1",
        issued_at=10.0,
        expires_at=20.0,
        nonce="host-memory-action-nonce-1",
        issuer_ref="host-memory-action-authority:v1",
    )
    reference = MemoryActionAuthorityRef.from_authority(authority)
    operation_with_ref = replace(operation, action_authority_ref=reference)
    plan_with_ref = replace(proposal, operations=(operation_with_ref,))
    return plan_with_ref, operation_with_ref, intent, authority, reference


def test_memory_action_authority_is_host_resolved_once_and_non_circular() -> None:
    plan, operation, intent, authority, reference = _authorized_revise_plan()
    assert MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION == 2
    assert MEMORY_MUTATION_SCHEMA_VERSION == 4
    assert operation.operation_intent_hash == intent.operation_intent_hash
    assert plan.action_intent(operation.operation_id) == intent
    proposal_without_ref = replace(operation, action_authority_ref=None)
    authority_free_plan = replace(plan, operations=(proposal_without_ref,))
    assert proposal_without_ref.operation_intent_hash == operation.operation_intent_hash
    assert authority_free_plan.plan_intent_hash == plan.plan_intent_hash
    assert authority_free_plan.plan_hash != plan.plan_hash
    assert intent.plan_intent_hash == plan.plan_intent_hash
    assert intent.canonical_operation_index == 1

    port = _ActionAuthority(authority)
    verified = asyncio.run(
        verify_memory_action_authority(
            intent,
            reference,
            port,
            current_time=15.0,
        )
    )
    assert verified is authority
    assert port.calls == 1
    assert len(authority.replay_identity) == 64
    assert MemoryActionIntent.from_json(intent.to_json()) == intent
    assert MemoryActionAuthority.from_json(authority.to_json()) == authority
    assert MemoryActionAuthorityRef.from_json(reference.to_json()) == reference
    with pytest.raises(TypeError, match="reference"):
        asyncio.run(
            verify_memory_action_authority(
                intent,
                cast(MemoryActionAuthorityRef, authority),
                port,
                current_time=15.0,
            )
        )


@pytest.mark.parametrize(
    "changed",
    (
        {"subject": "other-user"},
        {"action": MemoryActionKind.SUPERSEDE},
        {"target_memory_id": "memory-2"},
        {"target_revision": 8},
        {"evidence_refs": (EvidenceRef("evidence-2", "c" * 64, 1),)},
        {"evidence_span_hashes": ("d" * 64,)},
        {"run_id": "run-2"},
        {"turn_id": "turn-2"},
        {"plan_id": "action-plan-2"},
        {"plan_intent_hash": "9" * 64},
        {"operation_id": "revise-other"},
        {"canonical_operation_index": 2},
        {"operation_intent_hash": "e" * 64},
    ),
)
def test_memory_action_authority_rejects_every_changed_intent(
    changed: dict[str, object],
) -> None:
    _plan, _operation, intent, authority, reference = _authorized_revise_plan()
    changed_intent = replace(intent, **cast(Any, changed))
    port = _ActionAuthority(authority)
    with pytest.raises(ValueError, match="intent differs"):
        asyncio.run(
            verify_memory_action_authority(
                changed_intent,
                reference,
                port,
                current_time=15.0,
            )
        )
    assert port.calls == 1


def test_memory_action_authority_binds_whole_plan_and_canonical_index() -> None:
    plan, operation, intent, authority, reference = _authorized_revise_plan()
    authority_free_operation = replace(operation, action_authority_ref=None)

    trailing_create = _semantic_operation("zz-extra-create")
    trailing_plan = replace(
        plan,
        operations=(authority_free_operation, trailing_create),
    )
    trailing_intent = trailing_plan.action_intent(operation.operation_id)
    assert trailing_intent.operation_intent_hash == intent.operation_intent_hash
    assert trailing_intent.canonical_operation_index == intent.canonical_operation_index
    assert trailing_intent.plan_intent_hash != intent.plan_intent_hash
    with pytest.raises(ValueError, match="intent differs"):
        asyncio.run(
            verify_memory_action_authority(
                trailing_intent,
                reference,
                _ActionAuthority(authority),
                current_time=15.0,
            )
        )

    changed_trailing = replace(
        trailing_create,
        reason_code="different_other_operation_reason",
    )
    changed_plan = replace(
        plan,
        operations=(authority_free_operation, changed_trailing),
    )
    assert changed_plan.plan_intent_hash != trailing_plan.plan_intent_hash
    with pytest.raises(ValueError, match="intent differs"):
        asyncio.run(
            verify_memory_action_authority(
                changed_plan.action_intent(operation.operation_id),
                reference,
                _ActionAuthority(authority),
                current_time=15.0,
            )
        )

    leading_create = _semantic_operation("create-before-authorized-revise")
    leading_plan = replace(
        plan,
        operations=(authority_free_operation, leading_create),
    )
    leading_intent = leading_plan.action_intent(operation.operation_id)
    assert leading_intent.operation_intent_hash == intent.operation_intent_hash
    assert leading_intent.canonical_operation_index == 2
    assert leading_intent.plan_intent_hash != intent.plan_intent_hash
    with pytest.raises(ValueError, match="intent differs"):
        asyncio.run(
            verify_memory_action_authority(
                leading_intent,
                reference,
                _ActionAuthority(authority),
                current_time=15.0,
            )
        )


@pytest.mark.parametrize(
    "changed",
    (
        {"authority_id": "other-authority"},
        {"authority_hash": "f" * 64},
        {"issuer_ref": "other-host-authority:v1"},
        {"replay_identity": "0" * 64},
    ),
)
def test_memory_action_authority_rejects_changed_reference(
    changed: dict[str, object],
) -> None:
    _plan, _operation, intent, authority, reference = _authorized_revise_plan()
    port = _ActionAuthority(authority)
    with pytest.raises(ValueError, match="differs from reference"):
        asyncio.run(
            verify_memory_action_authority(
                intent,
                replace(reference, **cast(Any, changed)),
                port,
                current_time=15.0,
            )
        )
    assert port.calls == 1


@pytest.mark.parametrize(
    "changed",
    (
        {"nonce": "different-host-nonce"},
        {"issuer_ref": "different-host-authority:v1"},
        {"issued_at": 11.0},
        {"expires_at": 21.0},
    ),
)
def test_memory_action_authority_rejects_changed_host_record(
    changed: dict[str, object],
) -> None:
    _plan, _operation, intent, authority, reference = _authorized_revise_plan()
    port = _ActionAuthority(replace(authority, **cast(Any, changed)))
    with pytest.raises(ValueError, match="differs from reference"):
        asyncio.run(
            verify_memory_action_authority(
                intent,
                reference,
                port,
                current_time=15.0,
            )
        )
    assert port.calls == 1


def test_memory_action_authority_expiry_and_strict_wire_fail_closed() -> None:
    plan, operation, intent, authority, reference = _authorized_revise_plan()
    for current_time, reason in ((9.0, "not yet valid"), (20.0, "expired")):
        port = _ActionAuthority(authority)
        with pytest.raises(ValueError, match=reason):
            asyncio.run(
                verify_memory_action_authority(
                    intent,
                    reference,
                    port,
                    current_time=current_time,
                )
            )
        assert port.calls == 1

    for decoder, original in (
        (MemoryActionIntent.from_json, intent.to_json()),
        (MemoryActionAuthority.from_json, authority.to_json()),
        (MemoryActionAuthorityRef.from_json, reference.to_json()),
    ):
        old = deepcopy(original)
        old["schema_version"] = MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION - 1
        with pytest.raises(ValueError, match="unsupported"):
            decoder(old)
        extra = deepcopy(original)
        extra["legacy_authority"] = True
        with pytest.raises(ValueError, match="fields differ"):
            decoder(extra)

    old_operation = operation.to_json()
    old_operation.pop("operation_intent_hash")
    old_operation.pop("action_authority_ref")
    with pytest.raises(ValueError, match="fields differ"):
        MemoryMutationOperation.from_json(old_operation)
    old_plan = plan.to_json()
    old_plan["schema_version"] = MEMORY_MUTATION_SCHEMA_VERSION - 1
    with pytest.raises(ValueError, match="unsupported"):
        MemoryMutationPlan.from_json(old_plan)
    tampered_plan_intent = plan.to_json()
    tampered_plan_intent["plan_intent_hash"] = "0" * 64
    with pytest.raises(ValueError, match="does not bind"):
        MemoryMutationPlan.from_json(tampered_plan_intent)
    receipt = MemoryMutationApplyReceipt(
        receipt_id="strict-wire-receipt",
        authority_ref="memory-apply-authority:v3",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        run_id=plan.run_id,
        subject=plan.subject,
        base_revision=plan.base_revision,
        committed_revision=plan.base_revision + 1,
        canonical_operation_ids=(operation.operation_id,),
        apply_mode=MemoryMutationApplyMode.STRICT_ATOMIC,
        committed_at=15.0,
    )
    old_receipt = receipt.to_json()
    old_receipt["schema_version"] = MEMORY_MUTATION_SCHEMA_VERSION - 1
    with pytest.raises(ValueError, match="unsupported"):
        MemoryMutationApplyReceipt.from_json(old_receipt)


def test_support_kind_and_contest_never_grant_memory_action_authority() -> None:
    plan, operation, intent, authority, reference = _authorized_revise_plan()
    correction = replace(
        operation.evidence_spans[0],
        support_kind=EvidenceSupportKind.EXPLICIT_USER_CORRECTION,
    )
    relabeled = replace(operation, evidence_spans=(correction,))
    relabeled_plan = replace(plan, operations=(relabeled,))
    assert relabeled.operation_intent_hash != operation.operation_intent_hash
    assert relabeled_plan.action_intent(relabeled.operation_id) != intent
    with pytest.raises(ValueError, match="intent differs"):
        asyncio.run(
            verify_memory_action_authority(
                relabeled_plan.action_intent(relabeled.operation_id),
                reference,
                _ActionAuthority(authority),
                current_time=15.0,
            )
        )

    with pytest.raises(ValueError, match="only valid"):
        replace(
            operation,
            kind=MemoryMutationKind.CONTEST,
            conflict_status=ConflictStatus.CONTESTED,
            action_authority_ref=reference,
        )


@pytest.mark.parametrize(
    ("memory_type", "payload", "terminal_lifecycle"),
    (
        *(
            (
                LongTermMemoryType.EPISODE,
                EpisodeMemoryPayload(
                    "contested episode",
                    ("user:self",),
                    ("decision",),
                    ("choose python",),
                    ("selected",),
                    ("use python",),
                    10.0,
                    11.0,
                    "thread-contest",
                ),
                lifecycle,
            )
            for lifecycle in (
                EpisodeLifecycleState.SUPERSEDED,
                EpisodeLifecycleState.REJECTED,
                EpisodeLifecycleState.FORGOTTEN,
            )
        ),
        *(
            (
                LongTermMemoryType.SEMANTIC,
                SemanticMemoryPayload(
                    "user:self",
                    "runtime_preference",
                    {"runtime": "python"},
                    ("primary",),
                ),
                lifecycle,
            )
            for lifecycle in (
                SemanticLifecycleState.SUPERSEDED,
                SemanticLifecycleState.REJECTED,
                SemanticLifecycleState.FORGOTTEN,
            )
        ),
        *(
            (
                LongTermMemoryType.PROCEDURE,
                ProcedureMemoryPayload(
                    "release checklist",
                    ("software release",),
                    ("test", "publish"),
                    ProcedureRiskLevel.HIGH,
                ),
                lifecycle,
            )
            for lifecycle in (
                ProcedureLifecycleState.INAPPLICABLE,
                ProcedureLifecycleState.SUPERSEDED,
                ProcedureLifecycleState.FORGOTTEN,
            )
        ),
        *(
            (
                LongTermMemoryType.PROSPECTIVE,
                ProspectiveMemoryPayload(
                    "publish release",
                    ProspectiveTimeTrigger(100.0, "Asia/Shanghai"),
                ),
                lifecycle,
            )
            for lifecycle in (
                ProspectiveLifecycleState.COMPLETED,
                ProspectiveLifecycleState.CANCELLED,
                ProspectiveLifecycleState.EXPIRED,
                ProspectiveLifecycleState.SUPERSEDED,
                ProspectiveLifecycleState.FORGOTTEN,
            )
        ),
    ),
)
def test_contest_rejects_destructive_terminal_lifecycle_for_every_memory_type(
    memory_type: LongTermMemoryType,
    payload: object,
    terminal_lifecycle: object,
) -> None:
    contest = _semantic_operation(
        "contest-terminal",
        kind=MemoryMutationKind.CONTEST,
        target=ExistingMemoryTarget("memory-contested", 3),
    )
    with pytest.raises(ValueError, match="destructive terminal"):
        replace(
            contest,
            memory_type=memory_type,
            payload=cast(Any, payload),
            lifecycle_state=cast(Any, terminal_lifecycle),
        )


def test_contest_requires_contested_conflict_status() -> None:
    contest = _semantic_operation(
        "contest-status",
        kind=MemoryMutationKind.CONTEST,
        target=ExistingMemoryTarget("memory-contested", 3),
    )
    with pytest.raises(ValueError, match="requires contested"):
        replace(contest, conflict_status=ConflictStatus.UNCONTESTED)
    with pytest.raises(ValueError, match="requires contested"):
        replace(contest, conflict_status=ConflictStatus.RESOLVED)


def test_memory_mutation_apply_result_has_strict_confirmation_matrix() -> None:
    plan, operation, _intent, _authority, _reference = _authorized_revise_plan()
    proposal = replace(plan, operations=(replace(operation, action_authority_ref=None),))
    item = MemoryActionConfirmationItem(proposal.action_intent(operation.operation_id))
    confirmation = MemoryMutationApplyResult(
        result_id="mutation-result-confirm-1",
        plan_id=proposal.plan_id,
        plan_hash=proposal.plan_hash,
        run_id=proposal.run_id,
        turn_id=proposal.turn_id,
        subject=proposal.subject,
        outcome=MemoryMutationApplyOutcome.NEEDS_USER_CONFIRMATION,
        receipt_ref=None,
        confirmation_items=(item,),
        reason_code=MemoryMutationApplyReasonCode.ACTION_AUTHORITY_REQUIRED,
        decided_at=15.0,
    )
    confirmation.validate_plan(proposal)
    assert MemoryMutationApplyResult.from_json(confirmation.to_json()) == confirmation

    receipt_ref = MemoryMutationApplyReceiptRef("receipt-1", "a" * 64)
    committed = MemoryMutationApplyResult(
        result_id="mutation-result-committed-1",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        run_id=plan.run_id,
        turn_id=plan.turn_id,
        subject=plan.subject,
        outcome=MemoryMutationApplyOutcome.COMMITTED,
        receipt_ref=receipt_ref,
        confirmation_items=(),
        reason_code=MemoryMutationApplyReasonCode.COMMITTED,
        decided_at=16.0,
    )
    committed.validate_plan(plan)
    unauthorized_committed = replace(committed, plan_hash=proposal.plan_hash)
    with pytest.raises(ValueError, match="every protected operation"):
        unauthorized_committed.validate_plan(proposal)
    rejected = replace(
        committed,
        result_id="mutation-result-rejected-1",
        outcome=MemoryMutationApplyOutcome.REJECTED,
        receipt_ref=None,
        reason_code=MemoryMutationApplyReasonCode.ACTION_AUTHORITY_REJECTED,
    )
    assert MemoryMutationApplyResult.from_json(rejected.to_json()) == rejected

    with pytest.raises(ValueError, match="requires confirmation_items"):
        replace(confirmation, confirmation_items=())
    with pytest.raises(ValueError, match="cannot carry receipt"):
        replace(rejected, receipt_ref=receipt_ref)
    with pytest.raises(ValueError, match="committed result requires"):
        replace(committed, confirmation_items=(item,))


@pytest.mark.parametrize(
    ("memory_type", "payload", "lifecycle"),
    (
        (
            LongTermMemoryType.EPISODE,
            EpisodeMemoryPayload(
                "runtime decision", ("user:self",), ("upgrade",), ("choose 3.12",),
                ("selected",), ("future tasks use 3.12",), 10.0, 11.0,
                "episode-thread-1",
            ),
            EpisodeLifecycleState.ACTIVE,
        ),
        (
            LongTermMemoryType.PROCEDURE,
            ProcedureMemoryPayload(
                "release checklist", ("software release",), ("test", "publish"),
                ProcedureRiskLevel.HIGH,
            ),
            ProcedureLifecycleState.DRAFT,
        ),
        (
            LongTermMemoryType.PROSPECTIVE,
            ProspectiveMemoryPayload(
                "publish release", ProspectiveTimeTrigger(100.0, "Asia/Shanghai")
            ),
            ProspectiveLifecycleState.PENDING,
        ),
        (
            LongTermMemoryType.PROSPECTIVE,
            ProspectiveMemoryPayload(
                "write changelog",
                ProspectiveEventTrigger(
                    "event-authority-1",
                    "release completed",
                    fingerprint_json("release completed"),
                ),
            ),
            ProspectiveLifecycleState.PENDING,
        ),
    ),
)
def test_nonsemantic_payloads_and_dedicated_lifecycles_are_strict(
    memory_type: LongTermMemoryType,
    payload: EpisodeMemoryPayload | ProcedureMemoryPayload | ProspectiveMemoryPayload,
    lifecycle: EpisodeLifecycleState | ProcedureLifecycleState | ProspectiveLifecycleState,
) -> None:
    operation = replace(
        _semantic_operation("typed"),
        memory_type=memory_type,
        payload=payload,
        lifecycle_state=lifecycle,
    )
    assert MemoryMutationOperation.from_json(operation.to_json()) == operation
    extra = operation.to_json()
    assert extra["payload"] is not None
    payload_json = cast(dict[str, object], extra["payload"])
    payload_json["free_form_claim"] = "forbidden"
    with pytest.raises(ValueError, match="extra"):
        MemoryMutationOperation.from_json(extra)
    with pytest.raises(ValueError, match="lifecycle_state"):
        replace(operation, lifecycle_state=SemanticLifecycleState.ACTIVE)


def _recall_context() -> RecallContext:
    return RecallContext(
        run_id="run-1",
        subject="user-1",
        turn_id="turn-1",
        context_revision=7,
        expires_at=100.0,
        query="How should I release this?",
        active_task_scope_id="task-1",
        available_memory_types=(LongTermMemoryType.PROCEDURE, LongTermMemoryType.SEMANTIC),
        short_horizon_allowed=True,
        allowed_selector_domains=(
            RecallSelectorDomain.MEMORY_TYPE,
            RecallSelectorDomain.TASK_SCOPE,
            RecallSelectorDomain.ENTITY,
            RecallSelectorDomain.TIME,
            RecallSelectorDomain.EVENT,
            RecallSelectorDomain.ENVIRONMENT,
            RecallSelectorDomain.TASK_PHASE,
            RecallSelectorDomain.SHORT_HORIZON,
        ),
        allowed_retrieval_modes=(RecallRetrievalMode.VECTOR, RecallRetrievalMode.EXACT),
        allowed_task_scope_ids=("task-1", "task-2"),
        allowed_entity_constraints=("user:self", "repo:memory"),
        earliest_occurred_at=10.0,
        latest_occurred_at=90.0,
        event_constraint_refs=("event-authority-1",),
        environment_constraint_refs=("environment-authority-1",),
        task_phase_authority_refs=("phase-authority-1",),
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        budget=RecallBudget(10, 20_000, 2_000, 2_000),
    )


def _recall_plan(context: RecallContext) -> RecallPlan:
    return RecallPlan(
        plan_id="recall-1",
        run_id="run-1",
        subject="user-1",
        context_hash=context.context_hash,
        context_revision=context.context_revision,
        query=context.query,
        requested_memory_types=(LongTermMemoryType.PROCEDURE,),
        include_short_horizon=True,
        selector_domains=(
            RecallSelectorDomain.MEMORY_TYPE,
            RecallSelectorDomain.TASK_SCOPE,
            RecallSelectorDomain.ENTITY,
            RecallSelectorDomain.TIME,
            RecallSelectorDomain.EVENT,
            RecallSelectorDomain.ENVIRONMENT,
            RecallSelectorDomain.TASK_PHASE,
            RecallSelectorDomain.SHORT_HORIZON,
        ),
        retrieval_modes=(RecallRetrievalMode.VECTOR,),
        task_scope_ids=("task-1",),
        entity_constraints=("user:self",),
        earliest_occurred_at=20.0,
        latest_occurred_at=80.0,
        event_constraint_refs=("event-authority-1",),
        environment_constraint_refs=("environment-authority-1",),
        task_phase_authority_refs=("phase-authority-1",),
        disclosure_context=context.disclosure_context,
        evidence_refs=context.evidence_refs,
        budget=RecallBudget(5, 10_000, 1_000, 1_000),
        idempotency_key="recall-idem-1",
        reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
    )


def test_recall_plan_binds_context_and_only_narrows_every_authority_dimension() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    plan.validate_narrowing(context, current_time=50.0)
    assert RecallPlan.from_json(plan.to_json()) == plan

    expanded = plan.to_json()
    expanded["budget"] = RecallBudget(11, 10_000, 1_000, 1_000).to_json()
    with pytest.raises(ValueError, match="budget"):
        RecallPlan.from_json(expanded).validate_narrowing(context, current_time=50.0)

    replayed = plan.to_json()
    replayed["context_hash"] = "f" * 64
    with pytest.raises(ValueError, match="context_hash"):
        RecallPlan.from_json(replayed).validate_narrowing(context, current_time=50.0)


@pytest.mark.parametrize(
    ("field", "expanded_value"),
    (
        ("requested_memory_types", ["episode"]),
        (
            "retrieval_modes", ["full_text"],
        ),
        ("task_scope_ids", ["task-3"]),
        ("entity_constraints", ["user:someone-else"]),
        ("earliest_occurred_at", 5.0),
        ("latest_occurred_at", 95.0),
        ("event_constraint_refs", ["event-authority-other"]),
        ("environment_constraint_refs", ["environment-authority-other"]),
        ("task_phase_authority_refs", ["phase-authority-other"]),
    ),
)
def test_recall_plan_rejects_each_selector_authority_expansion(
    field: str, expanded_value: object
) -> None:
    context = _recall_context()
    encoded = cast(dict[str, object], deepcopy(_recall_plan(context).to_json()))
    encoded[field] = expanded_value
    with pytest.raises(ValueError, match="expand"):
        RecallPlan.from_json(encoded).validate_narrowing(context, current_time=50.0)


@pytest.mark.parametrize(
    ("domain", "field"),
    (
        (RecallSelectorDomain.TASK_SCOPE, "task_scope_ids"),
        (RecallSelectorDomain.ENTITY, "entity_constraints"),
        (RecallSelectorDomain.EVENT, "event_constraint_refs"),
        (RecallSelectorDomain.ENVIRONMENT, "environment_constraint_refs"),
        (RecallSelectorDomain.TASK_PHASE, "task_phase_authority_refs"),
    ),
)
def test_recall_plan_cannot_drop_a_host_domain_with_its_constraint(
    domain: RecallSelectorDomain, field: str
) -> None:
    context = _recall_context()
    encoded = cast(dict[str, object], deepcopy(_recall_plan(context).to_json()))
    encoded[field] = []
    encoded["selector_domains"] = [
        item
        for item in cast(list[str], encoded["selector_domains"])
        if item != domain.value
    ]
    with pytest.raises(ValueError, match="mandatory Host selector"):
        RecallPlan.from_json(encoded).validate_narrowing(context, current_time=50.0)


def test_recall_plan_rejects_disclosure_and_short_horizon_expansion() -> None:
    context = _recall_context()
    encoded = cast(dict[str, object], deepcopy(_recall_plan(context).to_json()))
    disclosure = cast(dict[str, object], encoded["disclosure_context"])
    disclosure["authority_ref"] = "different-host-authority"
    with pytest.raises(ValueError, match="disclosure"):
        RecallPlan.from_json(encoded).validate_narrowing(context, current_time=50.0)

    no_short_context = replace(
        context,
        short_horizon_allowed=False,
        allowed_selector_domains=tuple(
            item
            for item in context.allowed_selector_domains
            if item is not RecallSelectorDomain.SHORT_HORIZON
        ),
    )
    short_plan = replace(
        _recall_plan(context),
        context_hash=no_short_context.context_hash,
        context_revision=no_short_context.context_revision,
    )
    with pytest.raises(ValueError, match="selector domains|short-horizon"):
        short_plan.validate_narrowing(no_short_context, current_time=50.0)


def test_recall_decision_binds_context_plan_and_post_gate_candidate_count() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    decision = RecallDecision(
        decision_id="decision-1",
        run_id="run-1",
        subject="user-1",
        context_hash=context.context_hash,
        context_revision=context.context_revision,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        outcome=RecallDecisionOutcome.RECALL,
        selected_memory_types=(LongTermMemoryType.PROCEDURE,),
        selected_memory_refs=("memory-1",),
        confirmation_items=(),
        filtered_candidate_count=2,
        candidate_count_stage=RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES,
        disclosure_context=_disclosure(),
        evidence_refs=(_ref(),),
        reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
        decided_at=50.0,
    )
    decision.validate_bindings(context, plan, current_time=50.0)
    assert RecallDecision.from_json(decision.to_json()) == decision

    with pytest.raises(ValueError, match="expired"):
        plan.validate_narrowing(context, current_time=context.expires_at)
    forged_evidence = replace(
        decision,
        evidence_refs=(EvidenceRef("evidence-other", "f" * 64, 1),),
    )
    with pytest.raises(ValueError, match="evidence_refs"):
        forged_evidence.validate_bindings(context, plan, current_time=50.0)


def _confirmation_decision(
    context: RecallContext,
    plan: RecallPlan,
    *,
    confirmation_items: tuple[RecallConfirmationItem, ...] | None = None,
    filtered_candidate_count: int = 2,
    disclosure_context: DisclosureContext | None = None,
) -> RecallDecision:
    return RecallDecision(
        decision_id="confirmation-decision-1",
        run_id=context.run_id,
        subject=context.subject,
        context_hash=context.context_hash,
        context_revision=context.context_revision,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        outcome=RecallDecisionOutcome.NEEDS_USER_CONFIRMATION,
        selected_memory_types=(),
        selected_memory_refs=(),
        confirmation_items=confirmation_items
        or (
            RecallConfirmationItem(
                "conflict-1", LongTermMemoryType.PROCEDURE, "memory-2"
            ),
            RecallConfirmationItem(
                "conflict-1", LongTermMemoryType.PROCEDURE, "memory-1"
            ),
        ),
        filtered_candidate_count=filtered_candidate_count,
        candidate_count_stage=RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES,
        disclosure_context=disclosure_context or context.disclosure_context,
        evidence_refs=plan.evidence_refs,
        reason_codes=(RecallReasonCode.NEEDS_USER_CONFIRMATION,),
        decided_at=50.0,
    )


def test_recall_confirmation_v3_is_canonical_and_binds_plan() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    decision = _confirmation_decision(context, plan)

    assert decision.schema_version == RECALL_DECISION_SCHEMA_VERSION == 3
    assert [item.memory_ref for item in decision.confirmation_items] == [
        "memory-1",
        "memory-2",
    ]
    decision.validate_bindings(context, plan, current_time=50.0)
    assert RecallDecision.from_json(decision.to_json()) == decision
    assert replace(
        decision,
        confirmation_items=tuple(reversed(decision.confirmation_items)),
    ).decision_hash == decision.decision_hash

    unordered = (
        RecallConfirmationItem(
            "conflict-b", LongTermMemoryType.SEMANTIC, "memory-4"
        ),
        RecallConfirmationItem(
            "conflict-a", LongTermMemoryType.SEMANTIC, "memory-2"
        ),
        RecallConfirmationItem(
            "conflict-b", LongTermMemoryType.PROCEDURE, "memory-3"
        ),
        RecallConfirmationItem(
            "conflict-a", LongTermMemoryType.PROCEDURE, "memory-1"
        ),
    )
    canonical = _confirmation_decision(
        context,
        plan,
        confirmation_items=unordered,
        filtered_candidate_count=4,
    )
    assert canonical.confirmation_items == tuple(
        sorted(
            unordered,
            key=lambda item: (
                item.conflict_group_ref,
                item.memory_type.value,
                item.memory_ref,
            ),
        )
    )
    assert replace(
        canonical,
        confirmation_items=tuple(reversed(unordered)),
    ).decision_hash == canonical.decision_hash


def test_recall_confirmation_v3_rejects_old_or_inexact_wire() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    encoded = _confirmation_decision(context, plan).to_json()

    encoded["schema_version"] = 2
    with pytest.raises(ValueError, match="unsupported RecallDecision schema_version"):
        RecallDecision.from_json(encoded)

    encoded = _confirmation_decision(context, plan).to_json()
    encoded["schema_version"] = 2
    del encoded["confirmation_items"]
    with pytest.raises(ValueError, match="unsupported RecallDecision schema_version"):
        RecallDecision.from_json(encoded)

    encoded = _confirmation_decision(context, plan).to_json()
    del encoded["confirmation_items"]
    with pytest.raises(ValueError, match="fields differ"):
        RecallDecision.from_json(encoded)


def test_recall_confirmation_v3_rejects_invalid_conflict_groups_and_counts() -> None:
    context = _recall_context()
    plan = _recall_plan(context)

    with pytest.raises(ValueError, match="at least two"):
        _confirmation_decision(
            context,
            plan,
            confirmation_items=(
                RecallConfirmationItem(
                    "conflict-1", LongTermMemoryType.PROCEDURE, "memory-1"
                ),
            ),
        )

    with pytest.raises(ValueError, match="globally unique"):
        _confirmation_decision(
            context,
            plan,
            confirmation_items=(
                RecallConfirmationItem(
                    "conflict-1", LongTermMemoryType.PROCEDURE, "memory-1"
                ),
                RecallConfirmationItem(
                    "conflict-1", LongTermMemoryType.PROCEDURE, "memory-2"
                ),
                RecallConfirmationItem(
                    "conflict-2", LongTermMemoryType.PROCEDURE, "memory-1"
                ),
                RecallConfirmationItem(
                    "conflict-2", LongTermMemoryType.PROCEDURE, "memory-3"
                ),
            ),
            filtered_candidate_count=4,
        )

    with pytest.raises(ValueError, match="candidate count"):
        _confirmation_decision(context, plan, filtered_candidate_count=1)


def test_recall_confirmation_v3_requires_exact_outcome_reason_and_safe_disclosure() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    confirmation = _confirmation_decision(context, plan)

    with pytest.raises(ValueError, match="confirmation reason"):
        replace(
            confirmation,
            reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
        )
    with pytest.raises(ValueError, match="confirmation items"):
        replace(confirmation, confirmation_items=())
    with pytest.raises(ValueError, match="non-recall"):
        replace(
            confirmation,
            selected_memory_types=(LongTermMemoryType.PROCEDURE,),
            selected_memory_refs=("selected-memory",),
            filtered_candidate_count=3,
        )
    with pytest.raises(ValueError, match="cannot require confirmation"):
        replace(
            confirmation,
            outcome=RecallDecisionOutcome.RECALL,
            selected_memory_types=(LongTermMemoryType.PROCEDURE,),
            selected_memory_refs=("selected-memory",),
            filtered_candidate_count=3,
            reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
        )
    with pytest.raises(ValueError, match="incompatible reason"):
        RecallDecision(
            decision_id="no-recall-with-confirmation-reason",
            run_id=context.run_id,
            subject=context.subject,
            context_hash=context.context_hash,
            context_revision=context.context_revision,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            outcome=RecallDecisionOutcome.NO_RECALL,
            selected_memory_types=(),
            selected_memory_refs=(),
            confirmation_items=(),
            filtered_candidate_count=0,
            candidate_count_stage=RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES,
            disclosure_context=context.disclosure_context,
            evidence_refs=plan.evidence_refs,
            reason_codes=(RecallReasonCode.NEEDS_USER_CONFIRMATION,),
            decided_at=50.0,
        )
    with pytest.raises(ValueError, match="outcome cannot include confirmation items"):
        replace(
            confirmation,
            outcome=RecallDecisionOutcome.REJECTED,
            reason_codes=(RecallReasonCode.INVALID_PLAN,),
        )

    unsafe_disclosure = replace(
        context.disclosure_context,
        recipient=DeliveryRecipient.EXTERNAL_PARTY,
    )
    unsafe_context = replace(context, disclosure_context=unsafe_disclosure)
    unsafe_plan = _recall_plan(unsafe_context)
    with pytest.raises(ValueError, match="recall rejects"):
        _confirmation_decision(
            unsafe_context,
            unsafe_plan,
            disclosure_context=unsafe_disclosure,
        )


def test_recall_confirmation_v3_rejects_unrequested_types_at_binding() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    decision = _confirmation_decision(
        context,
        plan,
        confirmation_items=(
            RecallConfirmationItem(
                "conflict-1", LongTermMemoryType.SEMANTIC, "memory-1"
            ),
            RecallConfirmationItem(
                "conflict-1", LongTermMemoryType.SEMANTIC, "memory-2"
            ),
        ),
    )
    with pytest.raises(ValueError, match="unrequested memory type"):
        decision.validate_bindings(context, plan, current_time=50.0)


def test_recall_decision_v3_freezes_outcome_reason_type_and_count_matrix() -> None:
    context = _recall_context()
    plan = _recall_plan(context)
    recall = RecallDecision(
        decision_id="matrix-recall",
        run_id=context.run_id,
        subject=context.subject,
        context_hash=context.context_hash,
        context_revision=context.context_revision,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        outcome=RecallDecisionOutcome.RECALL,
        selected_memory_types=(LongTermMemoryType.PROCEDURE,),
        selected_memory_refs=("memory-1",),
        confirmation_items=(),
        filtered_candidate_count=1,
        candidate_count_stage=RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES,
        disclosure_context=context.disclosure_context,
        evidence_refs=plan.evidence_refs,
        reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
        decided_at=50.0,
    )

    with pytest.raises(ValueError, match="selected memory types"):
        replace(recall, selected_memory_types=())
    with pytest.raises(ValueError, match="types exceed"):
        replace(
            recall,
            selected_memory_types=(
                LongTermMemoryType.PROCEDURE,
                LongTermMemoryType.SEMANTIC,
            ),
        )
    for reason in (
        RecallReasonCode.NO_ELIGIBLE_MEMORY,
        RecallReasonCode.BUDGET_EXHAUSTED,
        RecallReasonCode.DISCLOSURE_DENIED,
        RecallReasonCode.INVALID_PLAN,
        RecallReasonCode.NEEDS_USER_CONFIRMATION,
    ):
        with pytest.raises(ValueError, match="only dependency reasons"):
            replace(recall, reason_codes=(reason,))

    no_recall = replace(
        recall,
        decision_id="matrix-no-recall",
        outcome=RecallDecisionOutcome.NO_RECALL,
        selected_memory_types=(),
        selected_memory_refs=(),
        filtered_candidate_count=0,
        reason_codes=(RecallReasonCode.NO_ELIGIBLE_MEMORY,),
    )
    for reason in (
        RecallReasonCode.PROCEDURE_DEPENDENCY,
        RecallReasonCode.DISCLOSURE_DENIED,
        RecallReasonCode.INVALID_PLAN,
        RecallReasonCode.NEEDS_USER_CONFIRMATION,
    ):
        with pytest.raises(ValueError, match="no-recall.*incompatible reason"):
            replace(no_recall, reason_codes=(reason,))
    with pytest.raises(ValueError, match="cannot disclose candidate counts"):
        replace(no_recall, filtered_candidate_count=1)

    unsafe_disclosure = replace(
        context.disclosure_context,
        recipient=DeliveryRecipient.EXTERNAL_PARTY,
    )
    rejected = replace(
        no_recall,
        decision_id="matrix-rejected",
        outcome=RecallDecisionOutcome.REJECTED,
        disclosure_context=unsafe_disclosure,
        reason_codes=(RecallReasonCode.DISCLOSURE_DENIED,),
    )
    assert rejected.filtered_candidate_count == 0
    for reason in (
        RecallReasonCode.PROCEDURE_DEPENDENCY,
        RecallReasonCode.NO_ELIGIBLE_MEMORY,
        RecallReasonCode.NEEDS_USER_CONFIRMATION,
    ):
        with pytest.raises(ValueError, match="rejected.*incompatible reason"):
            replace(rejected, reason_codes=(reason,))
    with pytest.raises(ValueError, match="cannot disclose candidate counts"):
        replace(rejected, filtered_candidate_count=1)

    confirmation = _confirmation_decision(context, plan)
    with pytest.raises(ValueError, match="incompatible reason"):
        replace(
            confirmation,
            reason_codes=(
                RecallReasonCode.NEEDS_USER_CONFIRMATION,
                RecallReasonCode.NO_ELIGIBLE_MEMORY,
            ),
        )
    assert replace(
        confirmation,
        reason_codes=(
            RecallReasonCode.NEEDS_USER_CONFIRMATION,
            RecallReasonCode.PROCEDURE_DEPENDENCY,
        ),
    ).outcome is RecallDecisionOutcome.NEEDS_USER_CONFIRMATION


def test_recall_context_and_plan_require_domain_constraint_consistency() -> None:
    context = _recall_context()
    with pytest.raises(ValueError, match="entity and constraint differ"):
        replace(
            context,
            allowed_selector_domains=tuple(
                domain
                for domain in context.allowed_selector_domains
                if domain is not RecallSelectorDomain.ENTITY
            ),
        )
    plan = _recall_plan(context)
    with pytest.raises(ValueError, match="event and constraint differ"):
        replace(plan, event_constraint_refs=())


@pytest.mark.parametrize(
    "disclosure",
    (
        replace(_disclosure(), recipient=DeliveryRecipient.EXTERNAL_PARTY),
        replace(_disclosure(), intended_audience=IntendedAudience.EXTERNAL),
        replace(
            _disclosure(),
            purpose=DisclosurePurpose.UNKNOWN,
            reason_codes=(
                DisclosureReasonCode.MINIMUM_NECESSARY,
                DisclosureReasonCode.UNKNOWN_PURPOSE,
            ),
        ),
        replace(
            _disclosure(),
            source=DisclosureSource.LLM_PROPOSAL,
            trust=DisclosureTrust.UNTRUSTED_PROPOSAL,
        ),
        replace(
            _disclosure(),
            source=DisclosureSource.UNKNOWN,
            trust=DisclosureTrust.UNTRUSTED_PROPOSAL,
        ),
        replace(_disclosure(), generation=DisclosureGeneration.UNKNOWN),
        replace(_disclosure(), intended_audience=IntendedAudience.UNKNOWN),
    ),
)
def test_recall_outcome_defaults_to_deny_for_unsafe_disclosure(
    disclosure: DisclosureContext,
) -> None:
    context = replace(_recall_context(), disclosure_context=disclosure)
    plan = _recall_plan(context)
    with pytest.raises(ValueError, match="recall (requires|rejects)"):
        RecallDecision(
            decision_id="unsafe-decision",
            run_id=context.run_id,
            subject=context.subject,
            context_hash=context.context_hash,
            context_revision=context.context_revision,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            outcome=RecallDecisionOutcome.RECALL,
            selected_memory_types=(LongTermMemoryType.PROCEDURE,),
            selected_memory_refs=("memory-1",),
            confirmation_items=(),
            filtered_candidate_count=1,
            candidate_count_stage=(
                RecallCandidateCountStage.AFTER_ALL_ELIGIBILITY_GATES
            ),
            disclosure_context=disclosure,
            evidence_refs=plan.evidence_refs,
            reason_codes=(RecallReasonCode.PROCEDURE_DEPENDENCY,),
            decided_at=50.0,
        )


class _ApplyAuthority:
    def __init__(self, receipt: MemoryMutationApplyReceipt) -> None:
        self.receipt = receipt

    async def resolve_memory_mutation_apply_receipt(
        self, receipt_ref: MemoryMutationApplyReceiptRef
    ) -> MemoryMutationApplyReceipt:
        del receipt_ref
        return self.receipt


def test_strict_atomic_apply_receipt_binds_entire_canonical_plan() -> None:
    create = _semantic_operation("create")
    revise = _semantic_operation(
        "revise",
        kind=MemoryMutationKind.CONTEST,
        target=CreatedByOperationTarget("create"),
        depends_on=("create",),
    )
    plan = MemoryMutationPlan(
        "atomic-plan", "run-1", "turn-1", "user-1", 4,
        MemoryMutationPlanOutcome.MUTATE, (revise, create), _disclosure(),
        (_ref(),), "atomic-plan-idem",
    )
    assert plan.apply_mode is MemoryMutationApplyMode.STRICT_ATOMIC
    assert tuple(MemoryMutationApplyMode) == (MemoryMutationApplyMode.STRICT_ATOMIC,)
    receipt = MemoryMutationApplyReceipt(
        receipt_id="apply-receipt-1",
        authority_ref="memory-authority-1",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        run_id=plan.run_id,
        subject=plan.subject,
        base_revision=plan.base_revision,
        committed_revision=plan.base_revision + 1,
        canonical_operation_ids=("create", "revise"),
        apply_mode=MemoryMutationApplyMode.STRICT_ATOMIC,
        committed_at=60.0,
    )
    receipt.validate_plan(plan)
    assert MemoryMutationApplyReceipt.from_json(receipt.to_json()) == receipt
    ref = MemoryMutationApplyReceiptRef(receipt.receipt_id, receipt.receipt_hash)
    assert (
        asyncio.run(
            verify_memory_mutation_apply_receipt(
                plan, ref, _ApplyAuthority(receipt)
            )
        )
        == receipt
    )

    partial = replace(receipt, canonical_operation_ids=("create",))
    with pytest.raises(ValueError, match="all canonical operations"):
        partial.validate_plan(plan)
    wrong_revision = replace(receipt, committed_revision=plan.base_revision + 2)
    with pytest.raises(ValueError, match="base-to-committed"):
        wrong_revision.validate_plan(plan)
    partial_ref = MemoryMutationApplyReceiptRef(
        partial.receipt_id, partial.receipt_hash
    )
    with pytest.raises(ValueError, match="all canonical operations"):
        asyncio.run(
            verify_memory_mutation_apply_receipt(
                plan, partial_ref, _ApplyAuthority(partial)
            )
        )


def test_apply_receipt_rejects_protected_plan_without_action_authority() -> None:
    authorized_plan, operation, _intent, _authority, _reference = (
        _authorized_revise_plan()
    )
    unauthorized_plan = replace(
        authorized_plan,
        operations=(replace(operation, action_authority_ref=None),),
    )
    receipt = MemoryMutationApplyReceipt(
        receipt_id="unauthorized-apply-receipt",
        authority_ref="memory-apply-authority:v4",
        plan_id=unauthorized_plan.plan_id,
        plan_hash=unauthorized_plan.plan_hash,
        run_id=unauthorized_plan.run_id,
        subject=unauthorized_plan.subject,
        base_revision=unauthorized_plan.base_revision,
        committed_revision=unauthorized_plan.base_revision + 1,
        canonical_operation_ids=(operation.operation_id,),
        apply_mode=MemoryMutationApplyMode.STRICT_ATOMIC,
        committed_at=60.0,
    )
    with pytest.raises(ValueError, match="every protected operation"):
        receipt.validate_plan(unauthorized_plan)

    receipt_ref = MemoryMutationApplyReceiptRef(
        receipt.receipt_id,
        receipt.receipt_hash,
    )
    with pytest.raises(ValueError, match="every protected operation"):
        asyncio.run(
            verify_memory_mutation_apply_receipt(
                unauthorized_plan,
                receipt_ref,
                _ApplyAuthority(receipt),
            )
        )
