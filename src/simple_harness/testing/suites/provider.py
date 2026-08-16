# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from simple_harness.testing.contracts import CaseDefinition

CASES = (
    CaseDefinition("provider.physical_request", "provider", "Physical request and response correlation", "physical_request"),
    CaseDefinition("provider.typed_error", "provider", "Typed transport and server failures", "typed_error"),
    CaseDefinition("provider.usage", "provider", "Trusted usage and unknown usage handling", "usage"),
    CaseDefinition("provider.redaction", "provider", "Secret and raw response redaction", "redaction"),
)

__all__ = ("CASES",)
