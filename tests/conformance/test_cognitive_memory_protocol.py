# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

import simple_harness
import simple_harness.runtime as runtime
from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    fingerprint_json,
)
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
    AdmittedEvidenceAuthority,
    ConversationEvidenceMetadata,
    ConversationEvidenceMetadataReceipt,
    ConversationEvidenceRegistration,
    ConversationEvidenceRegistrationRef,
    ConversationEvidenceRole,
    ConversationToolCausalLink,
    EvidenceActorRole,
    EvidenceItemAuthority,
    EvidenceProvenance,
    EvidenceReasonCode,
    EvidenceSourceKind,
    EvidenceSpanRef,
    EvidenceSupportKind,
    ProposedTypedObservationRef,
    SanitizedEvidenceEnvelope,
    SanitizedEvidenceReceipt,
    TypedObservationAuthorityReceipt,
    verify_conversation_evidence_registration,
    verify_evidence_span,
)


def test_cognitive_evidence_contract_is_on_official_public_surfaces() -> None:
    names = (
        "COGNITIVE_MEMORY_SCHEMA_VERSION",
        "EvidenceSpanRef",
        "EvidenceAuthorityVerifierPort",
        "ProposedTypedObservationRef",
        "ConversationEvidenceMetadata",
        "ConversationEvidenceRegistration",
        "ConversationEvidenceAuthorityVerifierPort",
        "verify_evidence_span",
        "verify_conversation_evidence_registration",
    )
    for name in names:
        assert name in simple_harness.__all__
        assert name in runtime.__all__
        assert getattr(simple_harness, name) is getattr(runtime, name)


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()  # type: ignore[arg-type]


def _disclosure() -> DisclosureContext:
    return DisclosureContext(
        run_id="run-1",
        subject="actor-1",
        recipient=DeliveryRecipient.USER_SELF,
        recipient_id="actor-1",
        intended_audience=IntendedAudience.USER_SELF,
        purpose=DisclosurePurpose.PERSONALIZATION,
        source=DisclosureSource.AUTHENTICATED_HOST,
        trust=DisclosureTrust.TRUSTED_AUTHORITY,
        generation=DisclosureGeneration.CURRENT,
        authority_ref="host-disclosure-1",
        reason_codes=(DisclosureReasonCode.MINIMUM_NECESSARY,),
    )


def _admitted(
    text: str = "用户现在常用 Python 3.12 🙂",
    *,
    source_kind: EvidenceSourceKind = EvidenceSourceKind.USER_MESSAGE,
    evidence_id: str = "evidence-1",
) -> tuple[SanitizedEvidenceEnvelope, SanitizedEvidenceReceipt]:
    payload: dict[str, JsonValue] = {
        "item_id": "message-1",
        "public_text": text,
        "result": {"version": "3.12"},
    }
    frozen_payload = cast(Mapping[str, FrozenJsonValue], payload)
    envelope = SanitizedEvidenceEnvelope(
        evidence_id=evidence_id,
        run_id="run-1",
        subject="actor-1",
        source_kind=source_kind,
        source_ref="turn-1/user",
        source_hash="a" * 64,
        sanitized_payload=frozen_payload,
        sanitized_hash=fingerprint_json(payload),
        filter_policy_version="credential-filter/v1",
        removed_spans=(),
        disclosure_context=_disclosure(),
        evidence_refs=(),
    )
    receipt = SanitizedEvidenceReceipt(
        receipt_id="admission-1",
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
        evidence_refs=(),
        admitted_at=10.0,
    )
    return envelope, receipt


def _span(
    envelope: SanitizedEvidenceEnvelope,
    receipt: SanitizedEvidenceReceipt,
    *,
    span_id: str = "span-1",
    quote: str = "Python 3.12 🙂",
    start_adjustment: int = 0,
    source_kind: EvidenceSourceKind = EvidenceSourceKind.USER_MESSAGE,
    actor_role: EvidenceActorRole = EvidenceActorRole.USER,
    provenance: EvidenceProvenance = EvidenceProvenance.AUTHENTICATED_USER,
    support_kind: EvidenceSupportKind = EvidenceSupportKind.EXPLICIT_USER_ASSERTION,
    observation: ProposedTypedObservationRef | None = None,
) -> EvidenceSpanRef:
    text = str(envelope.sanitized_payload["public_text"])
    encoded = text.encode("utf-8")
    quote_encoded = quote.encode("utf-8")
    start = encoded.index(quote_encoded) + start_adjustment
    return EvidenceSpanRef(
        span_id=span_id,
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        sanitized_hash=envelope.sanitized_hash,
        admission_receipt_id=receipt.receipt_id,
        admission_receipt_hash=receipt.receipt_hash,
        source_kind=source_kind,
        item_ordinal=1,
        item_id="message-1",
        item_json_pointer="/public_text",
        start_byte=start,
        end_byte=start + len(quote_encoded),
        exact_quote=quote,
        quote_hash=_sha_text(quote),
        source_hash=envelope.source_hash,
        normalization_version=EVIDENCE_NORMALIZATION_IDENTITY_UTF8_V1,
        actor_role=actor_role,
        provenance=provenance,
        support_kind=support_kind,
        typed_observation=observation,
    )


def _item_authority(span: EvidenceSpanRef) -> EvidenceItemAuthority:
    return EvidenceItemAuthority(
        authority_id="item-authority-1",
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
        issuer_ref="host-evidence-store-1",
    )


class _EvidenceVerifier:
    def __init__(
        self,
        admitted: AdmittedEvidenceAuthority,
        observation: TypedObservationAuthorityReceipt | None = None,
    ) -> None:
        self.admitted = admitted
        self.observation = observation

    async def resolve_admitted_evidence(self, span: EvidenceSpanRef) -> AdmittedEvidenceAuthority:
        return self.admitted

    async def resolve_typed_observation(
        self, reference: ProposedTypedObservationRef
    ) -> TypedObservationAuthorityReceipt:
        if self.observation is None:
            raise ValueError("typed observation is not registered")
        return self.observation


def test_span_safe_path_resolves_admitted_evidence_and_checks_utf8() -> None:
    envelope, receipt = _admitted()
    span = _span(envelope, receipt)
    admitted = AdmittedEvidenceAuthority(envelope, receipt, _item_authority(span))
    asyncio.run(verify_evidence_span(span, _EvidenceVerifier(admitted)))
    assert EvidenceSpanRef.from_json(span.to_json()) == span

    second = _span(envelope, receipt, span_id="span-2", quote="3.12")
    assert second.evidence_id == span.evidence_id
    assert second.span_hash != span.span_hash

    split = _span(envelope, receipt, quote="用户", start_adjustment=1)
    split_admitted = AdmittedEvidenceAuthority(envelope, receipt, _item_authority(split))
    with pytest.raises(ValueError, match="UTF-8|exact_quote"):
        asyncio.run(verify_evidence_span(split, _EvidenceVerifier(split_admitted)))

    unknown = span.to_json()
    unknown["normalization_version"] = "unicode-nfc/v1"
    with pytest.raises(ValueError, match="normalization"):
        EvidenceSpanRef.from_json(unknown)

    wrong_receipt = replace(span, admission_receipt_hash="e" * 64)
    wrong_receipt_admitted = AdmittedEvidenceAuthority(
        envelope, receipt, _item_authority(wrong_receipt)
    )
    with pytest.raises(ValueError, match="admission receipt"):
        asyncio.run(verify_evidence_span(wrong_receipt, _EvidenceVerifier(wrong_receipt_admitted)))

    wrong_provenance = replace(span, provenance=EvidenceProvenance.MODEL_OUTPUT)
    wrong_provenance_admitted = AdmittedEvidenceAuthority(
        envelope, receipt, _item_authority(wrong_provenance)
    )
    with pytest.raises(ValueError, match="support kind"):
        asyncio.run(
            verify_evidence_span(wrong_provenance, _EvidenceVerifier(wrong_provenance_admitted))
        )


def test_forged_caller_objects_cannot_bypass_authority_resolver() -> None:
    envelope, receipt = _admitted()
    span = _span(envelope, receipt)
    forged_envelope, forged_receipt = _admitted("forged replacement text")
    forged = AdmittedEvidenceAuthority(forged_envelope, forged_receipt, _item_authority(span))
    with pytest.raises(ValueError, match="admitted envelope|receipt"):
        asyncio.run(verify_evidence_span(span, _EvidenceVerifier(forged)))

    extra = span.to_json()
    extra["hidden_reasoning"] = "never persist me"
    with pytest.raises(ValueError, match="extra"):
        EvidenceSpanRef.from_json(extra)


def test_typed_observation_is_only_proposal_until_authority_resolution() -> None:
    envelope, receipt = _admitted(
        "tool observed version 3.12", source_kind=EvidenceSourceKind.TOOL_RESULT
    )
    authority_receipt = TypedObservationAuthorityReceipt(
        receipt_id="observation-receipt-1",
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        sanitized_hash=envelope.sanitized_hash,
        admission_receipt_id=receipt.receipt_id,
        admission_receipt_hash=receipt.receipt_hash,
        item_ordinal=1,
        item_id="message-1",
        item_json_pointer="/public_text",
        schema_id="com.simple-harness.tool-result",
        schema_version=2,
        registered_schema_hash="1" * 64,
        json_pointer="/result/version",
        value_hash=_sha_json("3.12"),
        accepted=True,
        issuer_ref="host-observation-registry-1",
    )
    proposal = ProposedTypedObservationRef(
        schema_id=authority_receipt.schema_id,
        schema_version=authority_receipt.schema_version,
        registered_schema_hash=authority_receipt.registered_schema_hash,
        observation_receipt_id=authority_receipt.receipt_id,
        observation_receipt_hash=authority_receipt.receipt_hash,
        authority_issuer_id=authority_receipt.issuer_ref,
        json_pointer=authority_receipt.json_pointer,
        value_hash=authority_receipt.value_hash,
    )
    span = _span(
        envelope,
        receipt,
        quote="tool observed version 3.12",
        source_kind=EvidenceSourceKind.TOOL_RESULT,
        actor_role=EvidenceActorRole.TOOL,
        provenance=EvidenceProvenance.TRUSTED_TOOL,
        support_kind=EvidenceSupportKind.TYPED_OBSERVATION,
        observation=proposal,
    )
    admitted = AdmittedEvidenceAuthority(envelope, receipt, _item_authority(span))
    asyncio.run(verify_evidence_span(span, _EvidenceVerifier(admitted, authority_receipt)))
    with pytest.raises(ValueError, match="not registered"):
        asyncio.run(verify_evidence_span(span, _EvidenceVerifier(admitted)))
    mismatched_receipt = replace(authority_receipt, value_hash="9" * 64)
    with pytest.raises(ValueError, match="authority receipt"):
        asyncio.run(verify_evidence_span(span, _EvidenceVerifier(admitted, mismatched_receipt)))

    replay_envelope, replay_admission = _admitted(
        "tool observed version 3.12",
        source_kind=EvidenceSourceKind.TOOL_RESULT,
        evidence_id="evidence-2",
    )
    replay_span = _span(
        replay_envelope,
        replay_admission,
        quote="tool observed version 3.12",
        source_kind=EvidenceSourceKind.TOOL_RESULT,
        actor_role=EvidenceActorRole.TOOL,
        provenance=EvidenceProvenance.TRUSTED_TOOL,
        support_kind=EvidenceSupportKind.TYPED_OBSERVATION,
        observation=proposal,
    )
    replay_authority = AdmittedEvidenceAuthority(
        replay_envelope, replay_admission, _item_authority(replay_span)
    )
    with pytest.raises(ValueError, match="admitted evidence item"):
        asyncio.run(
            verify_evidence_span(
                replay_span, _EvidenceVerifier(replay_authority, authority_receipt)
            )
        )


def _registration(
    *, secondary: bool = False
) -> tuple[ConversationEvidenceRegistration, ConversationEvidenceRegistrationRef]:
    envelope, admission = _admitted()
    metadata = ConversationEvidenceMetadata(
        metadata_id="conversation-metadata-1",
        authority_issuer_id="host-conversation-registry-1",
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        admission_receipt_id=admission.receipt_id,
        admission_receipt_hash=admission.receipt_hash,
        run_id=envelope.run_id,
        subject=envelope.subject,
        source_hash=envelope.source_hash,
        sanitized_hash=envelope.sanitized_hash,
        conversation_id="conversation-secondary" if secondary else "conversation-primary",
        primary_conversation_id="conversation-primary",
        causal_group_id="causal-group-10",
        causal_group_sequence=10,
        item_ordinal=1,
        group_item_count=1,
        ordered_group_manifest_hash="4" * 64,
        role=ConversationEvidenceRole.USER,
        occurred_at=10.0,
        task_scope_id="task-1",
        tool_causal_link=None,
        entities=("python",),
    )
    metadata_receipt = ConversationEvidenceMetadataReceipt(
        receipt_id="conversation-metadata-receipt-1",
        metadata_id=metadata.metadata_id,
        authority_issuer_id=metadata.authority_issuer_id,
        evidence_id=metadata.evidence_id,
        envelope_hash=metadata.envelope_hash,
        admission_receipt_id=metadata.admission_receipt_id,
        admission_receipt_hash=metadata.admission_receipt_hash,
        run_id=metadata.run_id,
        subject=metadata.subject,
        source_hash=metadata.source_hash,
        sanitized_hash=metadata.sanitized_hash,
        metadata_hash=metadata.metadata_hash,
        issuer_ref=metadata.authority_issuer_id,
        accepted=True,
    )
    registration = ConversationEvidenceRegistration(
        "conversation-registration-1",
        envelope,
        admission,
        metadata,
        metadata_receipt,
    )
    reference = ConversationEvidenceRegistrationRef(
        registration.registration_id,
        registration.registration_hash,
        envelope.evidence_id,
        envelope.envelope_hash,
    )
    return registration, reference


class _ConversationVerifier:
    def __init__(self, registration: ConversationEvidenceRegistration) -> None:
        self.registration = registration

    async def resolve_conversation_registration(
        self, reference: ConversationEvidenceRegistrationRef
    ) -> ConversationEvidenceRegistration:
        return self.registration


def test_conversation_metadata_is_post_ingestion_authority_registration() -> None:
    registration, reference = _registration()
    metadata = asyncio.run(
        verify_conversation_evidence_registration(reference, _ConversationVerifier(registration))
    )
    assert metadata.belongs_to_primary_conversation
    assert ConversationEvidenceMetadata.from_json(metadata.to_json()) == metadata
    assert (
        ConversationEvidenceMetadataReceipt.from_json(registration.metadata_receipt.to_json())
        == registration.metadata_receipt
    )
    assert "conversation_metadata" not in registration.envelope.to_json()

    bad_group = metadata.to_json()
    bad_group["item_ordinal"] = 2
    with pytest.raises(ValueError, match="group_item_count"):
        ConversationEvidenceMetadata.from_json(bad_group)
    extra = metadata.to_json()
    extra["model_claimed_primary"] = True
    with pytest.raises(ValueError, match="extra"):
        ConversationEvidenceMetadata.from_json(extra)

    secondary, secondary_ref = _registration(secondary=True)
    with pytest.raises(ValueError, match="primary conversation"):
        asyncio.run(
            verify_conversation_evidence_registration(
                secondary_ref, _ConversationVerifier(secondary)
            )
        )

    forged_ref = replace(reference, registration_hash="f" * 64)
    with pytest.raises(ValueError, match="reference differs"):
        asyncio.run(
            verify_conversation_evidence_registration(
                forged_ref, _ConversationVerifier(registration)
            )
        )

    assistant_envelope, assistant_admission = _admitted()
    with pytest.raises(ValueError, match="source_kind"):
        ConversationEvidenceRegistration(
            "wrong-role-registration",
            assistant_envelope,
            assistant_admission,
            replace(metadata, role=ConversationEvidenceRole.ASSISTANT),
            replace(
                registration.metadata_receipt,
                metadata_hash=replace(
                    metadata, role=ConversationEvidenceRole.ASSISTANT
                ).metadata_hash,
            ),
        )


def test_tool_causal_parent_must_be_inside_group_and_precede_tool_item() -> None:
    registration, _ = _registration()
    metadata = registration.metadata
    valid_tool_link = ConversationToolCausalLink(
        tool_call_id="tool-call-1",
        tool_name="python",
        parent_item_ordinal=1,
        terminal_receipt_id="tool-terminal-1",
        terminal_receipt_hash="5" * 64,
    )
    valid = replace(
        metadata,
        role=ConversationEvidenceRole.TOOL,
        item_ordinal=2,
        group_item_count=2,
        tool_causal_link=valid_tool_link,
    )
    assert valid.tool_causal_link == valid_tool_link

    with pytest.raises(ValueError, match="within the group and earlier"):
        replace(
            valid,
            tool_causal_link=replace(valid_tool_link, parent_item_ordinal=999),
        )
    with pytest.raises(ValueError, match="within the group and earlier"):
        replace(
            valid,
            tool_causal_link=replace(valid_tool_link, parent_item_ordinal=2),
        )
