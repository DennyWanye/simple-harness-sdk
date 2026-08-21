# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness.testing.contracts import CaseDefinition

CASES = (
    CaseDefinition(
        "conversation.contract",
        "conversation",
        "Typed conversation DTO, projection, and stable status contract",
        "conversation_contract",
    ),
    CaseDefinition(
        "conversation.schema_identity",
        "conversation",
        "Fresh execution schema v3 identity and reopen",
        "conversation_schema_identity",
    ),
    CaseDefinition(
        "conversation.outbox_recovery",
        "conversation",
        "Atomic Memory outbox and idempotent recovery",
        "conversation_outbox_recovery",
    ),
)

__all__ = ("CASES",)
