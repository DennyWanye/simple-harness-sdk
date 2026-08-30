# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import ContentBlock, Message, MessageRole, RequestId, canonical_json
from simple_harness.execution.budget import BudgetPolicy
from simple_harness.execution.dispatch import provider_binding_fingerprint
from simple_harness.execution.provider_invocations import (
    provider_response_from_json,
    provider_response_json,
)
from simple_harness.providers import ProviderResponse
from simple_harness.providers.base import (
    ProviderContinuationCapability,
    ProviderContinuationMode,
)


def _response(*blocks: ContentBlock, metadata=None, opaque_ref=None) -> ProviderResponse:
    return ProviderResponse(
        RequestId("request-1"),
        Message(MessageRole.ASSISTANT, blocks, metadata=metadata or {}),
        opaque_continuation_ref=opaque_ref,
    )


def test_reasoning_disabled_rejects_hidden_reasoning_before_durable_encoding() -> None:
    response = _response(
        ContentBlock("output_text", {"text": "visible"}),
        ContentBlock("reasoning", {"text": "HIDDEN_COT_CANARY"}),
    )
    with pytest.raises(ValueError, match="hidden reasoning"):
        provider_response_json(response)


def test_opaque_continuation_persists_public_ref_but_never_hidden_reasoning() -> None:
    capability = ProviderContinuationCapability(ProviderContinuationMode.OPAQUE_REFERENCE)
    payload = provider_response_json(
        _response(
            ContentBlock("output_text", {"text": "visible"}),
            ContentBlock("reasoning", {"text": "HIDDEN_COT_CANARY"}),
            metadata={"provider_private": "CREDENTIAL_CANARY"},
            opaque_ref="provider-item-123",
        ),
        capability=capability,
    )
    encoded = canonical_json(payload)
    assert "visible" in encoded
    assert "provider-item-123" in encoded
    assert "HIDDEN_COT_CANARY" not in encoded
    assert "CREDENTIAL_CANARY" not in encoded
    restored = provider_response_from_json(payload, expected_capability=capability)
    assert restored.opaque_continuation_ref == "provider-item-123"


def test_replay_rejects_changed_continuation_capability() -> None:
    payload = provider_response_json(_response(ContentBlock("output_text", {"text": "ok"})))
    with pytest.raises(ValueError, match="another continuation capability"):
        provider_response_from_json(
            payload,
            expected_capability=ProviderContinuationCapability(
                ProviderContinuationMode.PUBLIC_STATELESS
            ),
        )


def test_opaque_reference_is_bounded_public_identifier() -> None:
    with pytest.raises(ValueError, match="bounded public identifier"):
        _response(ContentBlock("output_text", {"text": "ok"}), opaque_ref="x" * 1025)


def test_frozen_provider_binding_changes_with_continuation_capability() -> None:
    policy = BudgetPolicy()
    disabled = provider_binding_fingerprint(
        policy, None, ProviderContinuationCapability()
    )
    opaque = provider_binding_fingerprint(
        policy,
        None,
        ProviderContinuationCapability(ProviderContinuationMode.OPAQUE_REFERENCE),
    )
    assert disabled != opaque
