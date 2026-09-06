"""Test-owned Host S1 authority using package-root public DTOs only.

The seed construction follows the frozen public consumer fixture; no SDK product
helper, private Memory API, SQL, model, or expected-output-to-receipt synthesis.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import time
from typing import Any

import simple_harness_memory as memory

import simple_harness as harness


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _disclosure(subject: str) -> Any:
    return harness.DisclosureContext(
        run_id="relation-run-1",
        subject=subject,
        recipient=harness.DeliveryRecipient.USER_SELF,
        recipient_id=subject,
        intended_audience=harness.IntendedAudience.USER_SELF,
        purpose=harness.DisclosurePurpose.PERSONALIZATION,
        source=harness.DisclosureSource.AUTHENTICATED_HOST,
        trust=harness.DisclosureTrust.TRUSTED_AUTHORITY,
        generation=harness.DisclosureGeneration.CURRENT,
        authority_ref="host-disclosure-relation-1",
        reason_codes=(harness.DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


def _evidence(subject: str) -> tuple[Any, Any, Any]:
    payload = {
        "item_id": "message-relation-1",
        "public_text": "Use the evidence-first release workflow when publishing.",
    }
    envelope = harness.SanitizedEvidenceEnvelope(
        evidence_id="evidence-relation-1",
        run_id="relation-run-1",
        subject=subject,
        source_kind=harness.EvidenceSourceKind.USER_MESSAGE,
        source_ref="relation-turn-1/user",
        source_hash=_sha(payload["public_text"]),
        sanitized_payload=payload,
        sanitized_hash=harness.fingerprint_json(payload),
        filter_policy_version="credential-filter/v1",
        removed_spans=(),
        disclosure_context=_disclosure(subject),
        evidence_refs=(),
    )
    receipt = harness.SanitizedEvidenceReceipt(
        receipt_id="admission-relation-1",
        run_id=envelope.run_id,
        subject=envelope.subject,
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        source_hash=envelope.source_hash,
        sanitized_hash=envelope.sanitized_hash,
        filter_policy_version=envelope.filter_policy_version,
        accepted=True,
        reason_codes=(harness.EvidenceReasonCode.SANITIZED_AND_ACCEPTED,),
        disclosure_context=envelope.disclosure_context,
        evidence_refs=envelope.evidence_refs,
        admitted_at=1.0,
    )
    text = payload["public_text"]
    span = harness.EvidenceSpanRef(
        span_id="span-relation-1",
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        sanitized_hash=envelope.sanitized_hash,
        admission_receipt_id=receipt.receipt_id,
        admission_receipt_hash=receipt.receipt_hash,
        source_kind=envelope.source_kind,
        item_ordinal=1,
        item_id=payload["item_id"],
        item_json_pointer="/public_text",
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        exact_quote=text,
        quote_hash=_sha(text),
        source_hash=envelope.source_hash,
        normalization_version=harness.EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
        actor_role=harness.EvidenceActorRole.USER,
        provenance=harness.EvidenceProvenance.AUTHENTICATED_USER,
        support_kind=harness.EvidenceSupportKind.EXPLICIT_USER_ASSERTION,
        typed_observation=None,
    )
    return envelope, receipt, span


def _item_authority(span: Any) -> Any:
    return harness.EvidenceItemAuthority(
        schema_version=harness.EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION,
        authority_id="item-authority-relation-1",
        evidence_id=span.evidence_id,
        envelope_hash=span.envelope_hash,
        sanitized_hash=span.sanitized_hash,
        source_hash=span.source_hash,
        source_kind=span.source_kind,
        item_ordinal=span.item_ordinal,
        item_id=span.item_id,
        item_json_pointer=span.item_json_pointer,
        normalization_version=span.normalization_version,
        actor_role=span.actor_role,
        provenance=span.provenance,
        required_privacy_class=harness.PrivacyClass.PERSONAL,
        required_information_attributes=(),
        classification_authority_ref="host-classification-relation-1",
        issuer_ref="host-evidence-relation-1",
    )


def _shared_operation(span: Any, **overrides: Any) -> Any:
    values = {
        "target": None,
        "depends_on_operation_ids": (),
        "epistemic_status": harness.EpistemicStatus.EXPLICIT_USER,
        "conflict_status": harness.ConflictStatus.UNCONTESTED,
        "verification_state": harness.VerificationState.SOURCE_BOUND,
        "valid_time_interval": harness.ValidTimeInterval(1.0, None),
        "proposed_privacy_class": harness.PrivacyClass.PERSONAL,
        "evidence_spans": (span,),
        "reason_code": "explicit_user_relation",
    }
    values.update(overrides)
    return harness.MemoryMutationOperation(**values)


class PublicMemoryFixture:
    authority_scope_ref = "test-host-memory-store-1"

    def __init__(self, path):
        self.path = path
        self.principal = memory.MemoryPrincipal(
            "fixture-deployment", "fixture-household", "subject-1", "fixture-session"
        )
        self.admitted = {}
        self.created = []
        self.receipts = []
        self.requests = []
        self.after_authorize = None
        self.before_authorize = None
        self.run_id = "use-run-1"
        self.turn_id = "use-turn-1"

    async def open(self):
        self.manager = await memory.build_human_memory_v7(
            self.path,
            evidence_authority=self,
            classification_policy=memory.InformationClassificationPolicy(
                policy_id="fixture-policy",
                policy_version="1",
                authority_ref="fixture-classification",
                required_privacy_class=harness.PrivacyClass.PERSONAL,
                required_information_attributes=(),
            ),
        )
        return self

    async def resolve_admitted_evidence(self, span):
        authority = self.admitted[span.evidence_id]
        if (
            authority.envelope.envelope_hash != span.envelope_hash
            or authority.receipt.receipt_hash != span.admission_receipt_hash
        ):
            raise ValueError("fixture source binding differs")
        return authority

    async def resolve_typed_observation(self, ref):
        raise ValueError("fixture has no typed observation")

    async def seed(self):
        await self.manager.register_principal_owner(
            self.principal, memory.MemoryScope.personal(self.principal.actor_id)
        )
        self.seeds = [
            dict(
                subject_entity=subject,
                predicate="likes_season",
                object_value="autumn",
                qualifiers=[],
            )
            for subject in ("subject-1", "subject-2")
        ]
        for i, payload in enumerate(self.seeds, 1):
            text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            env, receipt, span = _evidence(self.principal.actor_id)
            sanitized = {"item_id": f"user-{i}", "public_text": text}
            env = dc.replace(
                env,
                evidence_id=f"evidence-{i}",
                source_ref=f"user-{i}",
                sanitized_payload=sanitized,
                sanitized_hash=harness.fingerprint_json(sanitized),
                source_hash=_sha(text),
            )
            receipt = dc.replace(
                receipt,
                receipt_id=f"admission-{i}",
                evidence_id=env.evidence_id,
                envelope_hash=env.envelope_hash,
                sanitized_hash=env.sanitized_hash,
                source_hash=env.source_hash,
            )
            span = dc.replace(
                span,
                span_id=f"span-{i}",
                evidence_id=env.evidence_id,
                envelope_hash=env.envelope_hash,
                sanitized_hash=env.sanitized_hash,
                source_hash=env.source_hash,
                admission_receipt_id=receipt.receipt_id,
                admission_receipt_hash=receipt.receipt_hash,
                item_id=f"user-{i}",
                exact_quote=text,
                end_byte=len(text.encode()),
                quote_hash=_sha(text),
            )
            item = dc.replace(_item_authority(span), authority_id=f"item-authority-{i}")
            self.admitted[env.evidence_id] = harness.AdmittedEvidenceAuthority(env, receipt, item)
            await self.manager.ingest_committed_evidence(env, receipt)
            op = _shared_operation(
                span,
                operation_id=f"create-{i}",
                kind=harness.MemoryMutationKind.CREATE,
                memory_type=harness.LongTermMemoryType.SEMANTIC,
                payload=harness.SemanticMemoryPayload(
                    payload["subject_entity"], payload["predicate"], payload["object_value"], ()
                ),
                lifecycle_state=harness.SemanticLifecycleState.ACTIVE,
                proposed_information_attributes=(),
            )
            plan = harness.MemoryMutationPlan(
                plan_id=f"seed-plan-{i}",
                run_id=env.run_id,
                turn_id=f"seed-turn-{i}",
                subject=self.principal.actor_id,
                base_revision=i,
                outcome=harness.MemoryMutationPlanOutcome.MUTATE,
                operations=(op,),
                disclosure_context=env.disclosure_context,
                evidence_refs=(harness.EvidenceRef(env.evidence_id, env.envelope_hash, 1),),
                idempotency_key=f"seed-{i}",
            )
            result = await self.manager.apply_memory_mutation_plan(
                principal=self.principal,
                scope=memory.MemoryScope.personal(self.principal.actor_id),
                plan=plan,
            )
            assert result.receipt_ref is not None, result.outcome
            materialized = await self.manager.get_memory_mutation_receipt_view(
                principal=self.principal, receipt_ref=result.receipt_ref
            )
            self.created.append(materialized.operations[0].memory_id)
        assert len(set(self.created)) == 2

    async def recall(self, run_id=None, turn_id=None):
        run_id, turn_id = run_id or self.run_id, turn_id or self.turn_id
        now = time.time()
        disclosure = dc.replace(_disclosure(self.principal.actor_id), run_id=run_id)
        refs = tuple(
            harness.EvidenceRef(eid, a.envelope.envelope_hash, i)
            for i, (eid, a) in enumerate(self.admitted.items(), 1)
        )
        budget = harness.RecallBudget(
            max_items=8, max_bytes=16384, max_tokens=2048, deadline_ms=2000
        )
        context = harness.RecallContext(
            run_id=run_id,
            subject=self.principal.actor_id,
            turn_id=turn_id,
            context_revision=1,
            expires_at=now + 300,
            query="likes_season",
            active_task_scope_id=None,
            available_memory_types=(harness.LongTermMemoryType.SEMANTIC,),
            short_horizon_allowed=False,
            allowed_selector_domains=(harness.RecallSelectorDomain.MEMORY_TYPE,),
            allowed_retrieval_modes=(harness.RecallRetrievalMode.FULL_TEXT,),
            allowed_task_scope_ids=(),
            allowed_entity_constraints=(),
            earliest_occurred_at=None,
            latest_occurred_at=None,
            event_constraint_refs=(),
            environment_constraint_refs=(),
            task_phase_authority_refs=(),
            procedure_applicability_fingerprints=(),
            disclosure_context=disclosure,
            evidence_refs=refs,
            budget=budget,
        )
        key = f"{run_id}-{turn_id}-recall"
        plan = harness.RecallPlan(
            plan_id=key,
            run_id=run_id,
            subject=context.subject,
            context_hash=context.context_hash,
            context_revision=1,
            query=context.query,
            requested_memory_types=context.available_memory_types,
            include_short_horizon=False,
            selector_domains=context.allowed_selector_domains,
            retrieval_modes=context.allowed_retrieval_modes,
            task_scope_ids=(),
            entity_constraints=(),
            earliest_occurred_at=None,
            latest_occurred_at=None,
            event_constraint_refs=(),
            environment_constraint_refs=(),
            task_phase_authority_refs=(),
            disclosure_context=disclosure,
            evidence_refs=refs,
            budget=budget,
            idempotency_key=key,
            reason_codes=(harness.RecallReasonCode.USER_FACT_DEPENDENCY,),
        )
        execution = await self.manager.execute_typed_recall(
            principal=self.principal, context=context, plan=plan, now=now
        )
        result, decision = execution.result, execution.decision
        assert len(result.items) == 2
        actual = [harness.thaw_json(item.public_payload) for item in result.items]
        assert sorted(json.dumps(p, sort_keys=True) for p in actual) == sorted(
            json.dumps(p, sort_keys=True) for p in self.seeds
        )
        fragments = []
        for i, item in enumerate(result.items):
            selected = item.selected_item
            request = harness.RecallResultPageRequestV1(
                result.result_id, result.result_hash, i + 1, i, 1, 16384, now
            )
            page = await self.manager.page_typed_recall_result(
                principal=self.principal, request=request
            )
            binding = harness.RecallFragmentAuthorityBindingV1(
                decision.decision_id,
                decision.decision_hash,
                result.result_id,
                result.result_hash,
                selected.item_id,
                item.result_item_hash,
                None,
                None,
                None,
                page.page_id,
                page.page_hash,
                None,
                None,
                selected.public_payload_hash,
            )
            size = len(
                json.dumps(
                    actual[i], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            )
            fragments.append(
                harness.ContextFragmentV2(
                    f"fragment-{i + 1}",
                    run_id,
                    self.principal.actor_id,
                    harness.ContextFragmentType.RECALLED_MEMORY,
                    selected.source_ref,
                    selected.source_revision,
                    actual[i],
                    selected.public_payload_hash,
                    size,
                    size,
                    disclosure,
                    refs,
                    binding,
                )
            )
        return tuple(fragments)

    async def forget(self):
        request = memory.SuppressionRequest(
            "forget-1",
            self.principal.actor_id,
            memory.SuppressionScopeKind.MEMORY,
            self.created[0],
            "user_forget",
            time.time(),
            purpose=None,
        )
        return await self.manager.suppress(principal=self.principal, request=request)

    async def authorize_recall_context_use(self, request):
        self.requests.append(request)
        if self.before_authorize is not None:
            await self.before_authorize()
        receipt = await self.manager.authorize_recall_context_use(
            principal=self.principal, request=request, now=time.time()
        )
        self.receipts.append(receipt)
        if self.after_authorize is not None:
            await self.after_authorize()
        return receipt

    async def close(self):
        await self.manager.close()
