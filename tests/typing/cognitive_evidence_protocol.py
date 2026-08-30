# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Static consumer fixture for cognitive evidence authority contracts."""

from simple_harness import (
    AdmittedEvidenceAuthority,
    ConversationEvidenceAuthorityVerifierPort,
    ConversationEvidenceRegistration,
    ConversationEvidenceRegistrationRef,
    EvidenceAuthorityVerifierPort,
    EvidenceSpanRef,
    ProposedTypedObservationRef,
    TypedObservationAuthorityReceipt,
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
