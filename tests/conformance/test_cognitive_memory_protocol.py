# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import fields, replace
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
    CONVERSATION_EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION,
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
    authorize_conversation_public_text,
    verify_conversation_evidence_registration,
    verify_evidence_span,
)
from simple_harness.runtime.information_classification_protocol import (
    InformationAttribute,
    PrivacyClass,
)
from simple_harness.runtime.memory_protocol import (
    InformationAttribute as MemoryInformationAttribute,
)
from simple_harness.runtime.memory_protocol import PrivacyClass as MemoryPrivacyClass


def test_cognitive_evidence_contract_is_on_official_public_surfaces() -> None:
    names = (
        "COGNITIVE_MEMORY_SCHEMA_VERSION",
        "CONVERSATION_EVIDENCE_SCHEMA_VERSION",
        "EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION",
        "EvidenceSpanRef",
        "EvidenceAuthorityVerifierPort",
        "EvidenceItemAuthority",
        "PrivacyClass",
        "InformationAttribute",
        "ProposedTypedObservationRef",
        "ConversationEvidenceMetadata",
        "ConversationEvidenceRegistration",
        "ConversationEvidenceAuthorityVerifierPort",
        "verify_evidence_span",
        "verify_conversation_evidence_registration",
        "authorize_conversation_public_text",
    )
    for name in names:
        assert name in simple_harness.__all__
        assert name in runtime.__all__
        assert getattr(simple_harness, name) is getattr(runtime, name)
    assert MemoryPrivacyClass is PrivacyClass
    assert MemoryInformationAttribute is InformationAttribute


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
        schema_version=EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION,
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
        required_privacy_class=PrivacyClass.PERSONAL,
        required_information_attributes=(InformationAttribute.PREFERENCE,),
        classification_authority_ref="host-classification-policy-1",
        issuer_ref="host-evidence-store-1",
    )


def _typed_authority(
    envelope: SanitizedEvidenceEnvelope,
    receipt: SanitizedEvidenceReceipt,
) -> tuple[TypedObservationAuthorityReceipt, ProposedTypedObservationRef]:
    authority = TypedObservationAuthorityReceipt(
        receipt_id="observation-receipt-1",
        evidence_id=envelope.evidence_id,
        envelope_hash=envelope.envelope_hash,
        sanitized_hash=envelope.sanitized_hash,
        admission_receipt_id=receipt.receipt_id,
        admission_receipt_hash=receipt.receipt_hash,
        item_ordinal=1,
        item_id="message-1",
        item_json_pointer="/public_text",
        schema_id="com.simple-harness.provider-record",
        schema_version=2,
        registered_schema_hash="1" * 64,
        json_pointer="/result/version",
        value_hash=_sha_json("3.12"),
        accepted=True,
        issuer_ref="host-observation-registry-1",
    )
    proposal = ProposedTypedObservationRef(
        schema_id=authority.schema_id,
        schema_version=authority.schema_version,
        registered_schema_hash=authority.registered_schema_hash,
        observation_receipt_id=authority.receipt_id,
        observation_receipt_hash=authority.receipt_hash,
        authority_issuer_id=authority.issuer_ref,
        json_pointer=authority.json_pointer,
        value_hash=authority.value_hash,
    )
    return authority, proposal


class _EvidenceVerifier:
    def __init__(
        self,
        admitted: AdmittedEvidenceAuthority,
        observation: TypedObservationAuthorityReceipt | None = None,
    ) -> None:
        self.admitted = admitted
        self.observation = observation
        self.admitted_resolve_count = 0

    async def resolve_admitted_evidence(self, span: EvidenceSpanRef) -> AdmittedEvidenceAuthority:
        self.admitted_resolve_count += 1
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
    verifier = _EvidenceVerifier(admitted)
    verified = asyncio.run(verify_evidence_span(span, verifier))
    assert verified is admitted.item_authority
    assert verifier.admitted_resolve_count == 1
    assert verified.required_privacy_class is PrivacyClass.PERSONAL
    assert verified.required_information_attributes == (
        InformationAttribute.PREFERENCE,
    )
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


def test_evidence_item_classification_v3_is_required_canonical_and_hash_bound() -> None:
    envelope, receipt = _admitted()
    span = _span(envelope, receipt)
    authority = _item_authority(span)
    init_values = {
        item.name: getattr(authority, item.name)
        for item in fields(EvidenceItemAuthority)
        if item.init
    }

    for missing in (
        "schema_version",
        "required_privacy_class",
        "required_information_attributes",
        "classification_authority_ref",
    ):
        incomplete = dict(init_values)
        del incomplete[missing]
        with pytest.raises(TypeError):
            EvidenceItemAuthority(**incomplete)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="schema_version"):
        replace(authority, schema_version=2)
    with pytest.raises(ValueError, match="classification_authority_ref"):
        replace(authority, classification_authority_ref="")
    with pytest.raises(ValueError, match="unique"):
        replace(
            authority,
            required_information_attributes=(
                InformationAttribute.HEALTH,
                InformationAttribute.HEALTH,
            ),
        )
    with pytest.raises(ValueError, match="item limit"):
        replace(
            authority,
            required_information_attributes=tuple(
                InformationAttribute.PREFERENCE for _ in range(33)
            ),
        )

    first = replace(
        authority,
        required_information_attributes=(
            InformationAttribute.PREFERENCE,
            InformationAttribute.FINANCIAL,
            InformationAttribute.HEALTH,
        ),
    )
    second = replace(
        authority,
        required_information_attributes=(
            InformationAttribute.HEALTH,
            InformationAttribute.PREFERENCE,
            InformationAttribute.FINANCIAL,
        ),
    )
    assert first.required_information_attributes == (
        InformationAttribute.FINANCIAL,
        InformationAttribute.HEALTH,
        InformationAttribute.PREFERENCE,
    )
    assert first.authority_hash == second.authority_hash
    assert (
        first.to_json()["schema_version"]
        == EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION
        == 3
    )
    assert not hasattr(EvidenceItemAuthority, "from_json")

    object.__setattr__(authority, "required_privacy_class", PrivacyClass.RESTRICTED)
    with pytest.raises(ValueError, match="hash differs"):
        asyncio.run(
            verify_evidence_span(
                span,
                _EvidenceVerifier(
                    AdmittedEvidenceAuthority(envelope, receipt, authority)
                ),
            )
        )


def test_admitted_evidence_requires_exact_host_authority_types() -> None:
    envelope, receipt = _admitted()
    span = _span(envelope, receipt)
    authority = _item_authority(span)

    class _AuthoritySubclass(EvidenceItemAuthority):
        pass

    subclass = _AuthoritySubclass(
        **{
            item.name: getattr(authority, item.name)
            for item in fields(EvidenceItemAuthority)
            if item.init
        }
    )
    with pytest.raises(TypeError, match="item_authority"):
        AdmittedEvidenceAuthority(envelope, receipt, subclass)

    class _DuckAuthority:
        pass

    with pytest.raises(TypeError, match="item_authority"):
        AdmittedEvidenceAuthority(
            envelope,
            receipt,
            _DuckAuthority(),  # type: ignore[arg-type]
        )

    class _AdmittedSubclass(AdmittedEvidenceAuthority):
        pass

    admitted_subclass = _AdmittedSubclass(envelope, receipt, authority)
    with pytest.raises(TypeError, match="exact AdmittedEvidenceAuthority"):
        asyncio.run(
            verify_evidence_span(
                span,
                _EvidenceVerifier(admitted_subclass),
            )
        )

    class _DuckAdmitted:
        pass

    duck_admitted = _DuckAdmitted()
    duck_admitted.envelope = envelope
    duck_admitted.receipt = receipt
    duck_admitted.item_authority = authority

    with pytest.raises(TypeError, match="exact AdmittedEvidenceAuthority"):
        asyncio.run(
            verify_evidence_span(
                span,
                _EvidenceVerifier(duck_admitted),  # type: ignore[arg-type]
            )
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


def test_external_typed_observation_requires_exact_external_provenance_and_receipt() -> None:
    envelope, receipt = _admitted(
        "external observed version 3.12",
        source_kind=EvidenceSourceKind.PROVIDER_RECORD,
    )
    authority_receipt, proposal = _typed_authority(envelope, receipt)
    external_span = _span(
        envelope,
        receipt,
        quote="external observed version 3.12",
        source_kind=EvidenceSourceKind.PROVIDER_RECORD,
        actor_role=EvidenceActorRole.EXTERNAL,
        provenance=EvidenceProvenance.EXTERNAL_SOURCE,
        support_kind=EvidenceSupportKind.TYPED_OBSERVATION,
        observation=proposal,
    )
    admitted = AdmittedEvidenceAuthority(
        envelope,
        receipt,
        _item_authority(external_span),
    )
    assert asyncio.run(
        verify_evidence_span(
            external_span,
            _EvidenceVerifier(admitted, authority_receipt),
        )
    ) is admitted.item_authority
    with pytest.raises(ValueError, match="not registered"):
        asyncio.run(
            verify_evidence_span(external_span, _EvidenceVerifier(admitted))
        )

    for actor_role, provenance in (
        (EvidenceActorRole.USER, EvidenceProvenance.AUTHENTICATED_USER),
        (EvidenceActorRole.ASSISTANT, EvidenceProvenance.MODEL_OUTPUT),
        (EvidenceActorRole.EXTERNAL, EvidenceProvenance.TRUSTED_TOOL),
        (EvidenceActorRole.TOOL, EvidenceProvenance.EXTERNAL_SOURCE),
    ):
        spoofed = replace(
            external_span,
            actor_role=actor_role,
            provenance=provenance,
        )
        spoofed_admitted = AdmittedEvidenceAuthority(
            envelope,
            receipt,
            _item_authority(spoofed),
        )
        with pytest.raises(ValueError, match="typed observation.*provenance"):
            asyncio.run(
                verify_evidence_span(
                    spoofed,
                    _EvidenceVerifier(spoofed_admitted, authority_receipt),
                )
            )


def _registration(
    *, secondary: bool = False
) -> tuple[ConversationEvidenceRegistration, ConversationEvidenceRegistrationRef]:
    envelope, admission = _admitted()
    item_authority = _item_authority(_span(envelope, admission))
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
    metadata = authorize_conversation_public_text(
        metadata,
        AdmittedEvidenceAuthority(envelope, admission, item_authority),
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
        recall_item_authority=item_authority,
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
    assert metadata.has_authorized_public_text
    assert metadata.schema_version == CONVERSATION_EVIDENCE_SCHEMA_VERSION
    assert metadata.public_text_json_pointer == "/public_text"
    assert metadata.public_text_hash == _sha_text(
        str(registration.envelope.sanitized_payload["public_text"])
    )
    assert metadata.effective_privacy_class is PrivacyClass.PERSONAL
    assert metadata.information_attributes == (InformationAttribute.PREFERENCE,)
    assert (
        metadata.classification_authority_ref
        == "host-classification-policy-1"
    )
    assert ConversationEvidenceMetadata.from_json(metadata.to_json()) == metadata
    assert (
        ConversationEvidenceMetadataReceipt.from_json(registration.metadata_receipt.to_json())
        == registration.metadata_receipt
    )
    with pytest.raises(ValueError, match="unsupported.*schema_version"):
        replace(registration.metadata_receipt, schema_version=2)
    with pytest.raises(ValueError, match="unsupported.*schema_version"):
        replace(registration, schema_version=2)
    assert set(registration.to_json()) == {
        "schema_version",
        "registration_id",
        "evidence_id",
        "envelope_hash",
        "admission_receipt_id",
        "admission_receipt_hash",
        "metadata",
        "metadata_receipt",
        "recall_item_authority",
    }
    assert "conversation_metadata" not in registration.envelope.to_json()

    bad_group = metadata.to_json()
    bad_group["item_ordinal"] = 2
    with pytest.raises(ValueError, match="group_item_count"):
        ConversationEvidenceMetadata.from_json(bad_group)
    extra = metadata.to_json()
    extra["model_claimed_primary"] = True
    with pytest.raises(ValueError, match="extra"):
        ConversationEvidenceMetadata.from_json(extra)
    legacy = metadata.to_json()
    legacy["schema_version"] = 2
    with pytest.raises(ValueError, match="unsupported.*schema_version"):
        ConversationEvidenceMetadata.from_json(legacy)
    with pytest.raises(ValueError, match="all present or all absent"):
        replace(metadata, public_text_hash=None)
    with pytest.raises(ValueError, match="RFC 6901"):
        replace(metadata, public_text_json_pointer="/bad~2escape")

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
            recall_item_authority=registration.recall_item_authority,
        )


def test_conversation_recall_text_is_host_derived_and_absence_denies_indexing() -> None:
    registration, reference = _registration()
    metadata = registration.metadata
    forged = replace(metadata, public_text_json_pointer="/result/version")
    with pytest.raises(ValueError, match="differs from item authority"):
        ConversationEvidenceRegistration(
            "forged-pointer-registration",
            registration.envelope,
            registration.admission_receipt,
            forged,
            replace(
                registration.metadata_receipt,
                metadata_hash=forged.metadata_hash,
            ),
            recall_item_authority=registration.recall_item_authority,
        )

    for field_name, forged_value in (
        ("public_text_hash", "f" * 64),
        ("effective_privacy_class", PrivacyClass.PUBLIC),
        ("information_attributes", (InformationAttribute.IDENTITY,)),
        ("classification_authority_ref", "model-selected-policy"),
    ):
        forged_classification = replace(metadata, **{field_name: forged_value})
        with pytest.raises(ValueError, match="differs from item authority"):
            ConversationEvidenceRegistration(
                f"forged-{field_name}",
                registration.envelope,
                registration.admission_receipt,
                forged_classification,
                replace(
                    registration.metadata_receipt,
                    metadata_hash=forged_classification.metadata_hash,
                ),
                recall_item_authority=registration.recall_item_authority,
            )

    absent = replace(
        metadata,
        public_text_json_pointer=None,
        public_text_hash=None,
        public_text_normalization_version=None,
        evidence_item_authority_id=None,
        evidence_item_authority_hash=None,
        effective_privacy_class=None,
        information_attributes=None,
        classification_authority_ref=None,
    )
    durable_but_not_indexable = ConversationEvidenceRegistration(
        "unindexed-registration",
        registration.envelope,
        registration.admission_receipt,
        absent,
        replace(registration.metadata_receipt, metadata_hash=absent.metadata_hash),
    )
    assert not durable_but_not_indexable.short_horizon_eligible
    absent_ref = replace(
        reference,
        registration_id=durable_but_not_indexable.registration_id,
        registration_hash=durable_but_not_indexable.registration_hash,
    )
    with pytest.raises(ValueError, match="no authorized public_text"):
        asyncio.run(
            verify_conversation_evidence_registration(
                absent_ref, _ConversationVerifier(durable_but_not_indexable)
            )
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
