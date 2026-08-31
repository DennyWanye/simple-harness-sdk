# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static consumer fixture for cognitive evidence authority contracts."""

from simple_harness import (
    CONVERSATION_EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION,
    AdmittedEvidenceAuthority,
    ConversationEvidenceAuthorityVerifierPort,
    ConversationEvidenceMetadata,
    ConversationEvidenceRegistration,
    ConversationEvidenceRegistrationRef,
    EvidenceAuthorityVerifierPort,
    EvidenceItemAuthority,
    EvidenceSpanRef,
    InformationAttribute,
    PrivacyClass,
    ProposedTypedObservationRef,
    TypedObservationAuthorityReceipt,
    authorize_conversation_public_text,
    verify_evidence_span,
)


class StructuralEvidenceVerifier:
    async def resolve_admitted_evidence(self, span: EvidenceSpanRef) -> AdmittedEvidenceAuthority:
        raise NotImplementedError(span)

    async def resolve_typed_observation(
        self, reference: ProposedTypedObservationRef
    ) -> TypedObservationAuthorityReceipt:
        raise NotImplementedError(reference)


class StructuralConversationVerifier:
    async def resolve_conversation_registration(
        self, reference: ConversationEvidenceRegistrationRef
    ) -> ConversationEvidenceRegistration:
        raise NotImplementedError(reference)


EVIDENCE_VERIFIER: EvidenceAuthorityVerifierPort = StructuralEvidenceVerifier()
CONVERSATION_VERIFIER: ConversationEvidenceAuthorityVerifierPort = StructuralConversationVerifier()
PRIVACY_CLASS_TYPE: type[PrivacyClass] = PrivacyClass
INFORMATION_ATTRIBUTE_TYPE: type[InformationAttribute] = InformationAttribute
EVIDENCE_ITEM_AUTHORITY_SCHEMA: int = EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION
CONVERSATION_EVIDENCE_SCHEMA: int = CONVERSATION_EVIDENCE_SCHEMA_VERSION


async def resolve_verified_authority(span: EvidenceSpanRef) -> EvidenceItemAuthority:
    return await verify_evidence_span(span, EVIDENCE_VERIFIER)


def derive_recall_metadata(
    metadata: ConversationEvidenceMetadata,
    admitted: AdmittedEvidenceAuthority,
) -> ConversationEvidenceMetadata:
    return authorize_conversation_public_text(metadata, admitted)
