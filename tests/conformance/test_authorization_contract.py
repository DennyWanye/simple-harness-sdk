# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.tools import (
    AuthorizationDecision,
    AuthorizationPort,
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
            return AuthorizationResult(
                AuthorizationDecision.ALLOW, receipt_ref="authorization:1"
            )

    port: AuthorizationPort = AllowAll()
    result = asyncio.run(port.authorize(_effect()))

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.receipt_ref == "authorization:1"


def test_authorization_result_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        AuthorizationResult(AuthorizationDecision.ALLOW)
    with pytest.raises(ValueError):
        AuthorizationResult(AuthorizationDecision.DENY)
    assert AuthorizationResult(
        AuthorizationDecision.REQUIRE_USER,
        reason_code="user_confirmation_required",
        public_message="Confirmation is required.",
    ).decision is AuthorizationDecision.REQUIRE_USER
