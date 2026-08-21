# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationPort,
    AuthorizationReceipt,
    AuthorizationRequest,
    AuthorizationResult,
    PreparedToolEffect,
    ToolCall,
    ToolSpec,
)


def _effect() -> PreparedToolEffect:
    spec = ToolSpec(
        "read_summary",
        "Read a summary.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    return PreparedToolEffect(
        effect_id=EffectId("effect-1"),
        run_id=RunId("run-1"),
        call=ToolCall(CallId("call-1"), spec.name, {"path": "."}),
        spec=spec,
        context_metadata={},
    )


def test_host_authorization_port_receives_prepared_effect() -> None:
    class AllowAll:
        async def authorize(self, prepared: PreparedToolEffect) -> AuthorizationResult:
            assert prepared.effect_id == EffectId("effect-1")
            return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref="authorization:1")

    port: AuthorizationPort = AllowAll()
    result = asyncio.run(port.authorize(_effect()))

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.receipt_ref == "authorization:1"


def test_authorization_result_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        AuthorizationResult(AuthorizationDecision.ALLOW)
    with pytest.raises(ValueError):
        AuthorizationResult(AuthorizationDecision.DENY)
    assert (
        AuthorizationResult(
            AuthorizationDecision.REQUIRE_USER,
            reason_code="user_confirmation_required",
            public_message="Confirmation is required.",
            request=AuthorizationRequest("Confirm read.", "nonce-1"),
        ).decision
        is AuthorizationDecision.REQUIRE_USER
    )


def test_host_receipt_must_bind_the_sdk_receipt_hash() -> None:
    from simple_harness.tools import (
        bind_authorization_receipts,
        sdk_authorization_receipt,
    )

    sdk = sdk_authorization_receipt("decision", {"decision_id": "decision-1"})
    host = AuthorizationReceipt("host:1", "a" * 64, sdk.receipt_hash)
    assert bind_authorization_receipts(sdk, host).startswith("authorization-binding-v1:")
    with pytest.raises(ValueError):
        bind_authorization_receipts(sdk, AuthorizationReceipt("host:2", "b" * 64, "c" * 64))
