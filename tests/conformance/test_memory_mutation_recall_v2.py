# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import cast

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
)
from simple_harness.runtime.memory_protocol import (
    ConflictStatus,
    CreatedByOperationTarget,
    EpisodeLifecycleState,
    EpisodeMemoryPayload,
    EpistemicStatus,
    ExistingMemoryTarget,
    InformationAttribute,
    LongTermMemoryType,
    MemoryMutationApplyMode,
    MemoryMutationApplyReceipt,
    MemoryMutationApplyReceiptRef,
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
        "EpisodeMemoryPayload",
        "SemanticMemoryPayload",
        "ProcedureMemoryPayload",
        "ProspectiveMemoryPayload",
        "MemoryMutationPlanOutcome",
        "MemoryMutationApplyMode",
        "MemoryMutationApplyReceipt",
        "MemoryMutationApplyReceiptRef",
        "verify_memory_mutation_apply_receipt",
        "RecallSelectorDomain",
        "RecallRetrievalMode",
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


def _span() -> EvidenceSpanRef:
    quote = "Python 3.12"
    return EvidenceSpanRef(
        span_id="span-1",
        evidence_id="evidence-1",
        envelope_hash="b" * 64,
        sanitized_hash="c" * 64,
        admission_receipt_id="receipt-1",
        admission_receipt_hash="d" * 64,
        source_kind=EvidenceSourceKind.USER_MESSAGE,
        item_ordinal=1,
        item_id="message-1",
        item_json_pointer="/public_text",
        start_byte=7,
        end_byte=18,
        exact_quote=quote,
        quote_hash=_sha(quote),
        source_hash="e" * 64,
        normalization_version=EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
        actor_role=EvidenceActorRole.USER,
        provenance=EvidenceProvenance.AUTHENTICATED_USER,
        support_kind=EvidenceSupportKind.EXPLICIT_USER_ASSERTION,
        typed_observation=None,
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
        conflict_status=ConflictStatus.UNCONTESTED,
        verification_state=VerificationState.SOURCE_BOUND,
        valid_time_interval=ValidTimeInterval(10.0, None),
        proposed_privacy_class=PrivacyClass.PERSONAL,
        proposed_information_attributes=(InformationAttribute.PREFERENCE,),
        evidence_spans=(_span(),),
        reason_code="explicit_user_assertion",
    )


def test_mutation_v2_target_union_no_mutation_and_order_independent_dag() -> None:
    create = _semantic_operation("create")
    revise = _semantic_operation(
        "revise",
        kind=MemoryMutationKind.REVISE,
        target=CreatedByOperationTarget("create"),
        depends_on=("create",),
    )
    plan = MemoryMutationPlan(
        plan_id="mutation-1",
        run_id="run-1",
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
            "bad", "run-1", "user-1", 4, MemoryMutationPlanOutcome.NO_MUTATION,
            (create,), _disclosure(), (_ref(),), "bad-idem",
        )
    with pytest.raises(ValueError, match="unknown"):
        MemoryMutationPlan(
            "bad", "run-1", "user-1", 4, MemoryMutationPlanOutcome.MUTATE,
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
            "cycle", "run-1", "user-1", 4, MemoryMutationPlanOutcome.MUTATE,
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


def test_mutation_plan_rejects_invalid_created_by_producer_and_uncovered_span() -> None:
    create = _semantic_operation("create")
    producer_not_create = replace(
        _semantic_operation("producer"),
        kind=MemoryMutationKind.REVISE,
        target=ExistingMemoryTarget("existing-1", 1),
    )
    consumer = _semantic_operation(
        "consumer",
        kind=MemoryMutationKind.REVISE,
        target=CreatedByOperationTarget("producer"),
        depends_on=("producer",),
    )
    with pytest.raises(ValueError, match="producer must be a create"):
        MemoryMutationPlan(
            "bad-producer", "run-1", "user-1", 4,
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
            "bad-type", "run-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE,
            (episode_create, wrong_type_consumer), _disclosure(), (_ref(),),
            "bad-type-idem",
        )

    with pytest.raises(ValueError, match="cover every evidence span"):
        MemoryMutationPlan(
            "bad-evidence", "run-1", "user-1", 4,
            MemoryMutationPlanOutcome.MUTATE, (create,), _disclosure(),
            (EvidenceRef("evidence-1", "f" * 64, 1),), "bad-evidence-idem",
        )


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
        kind=MemoryMutationKind.REVISE,
        target=CreatedByOperationTarget("create"),
        depends_on=("create",),
    )
    plan = MemoryMutationPlan(
        "atomic-plan", "run-1", "user-1", 4,
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
