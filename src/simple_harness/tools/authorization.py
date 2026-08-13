# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned authorization seam for prepared Tool effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from simple_harness.contracts import EffectId, RunId

from .contracts import JsonObject, ToolCall, ToolSpec


class AuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_USER = "require_user"


@dataclass(frozen=True, slots=True)
class PreparedToolEffect:
    effect_id: EffectId
    run_id: RunId
    call: ToolCall
    spec: ToolSpec
    context_metadata: JsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, EffectId):
            raise TypeError("effect_id must use EffectId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: AuthorizationDecision
    receipt_ref: str | None = None
    reason_code: str | None = None
    public_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorizationDecision):
            object.__setattr__(self, "decision", AuthorizationDecision(self.decision))
        if self.decision is AuthorizationDecision.ALLOW:
            if not self.receipt_ref or not self.receipt_ref.strip():
                raise ValueError("allow authorization requires receipt_ref")
            if self.reason_code is not None:
                raise ValueError("allow authorization cannot have reason_code")
        elif not self.reason_code or not self.reason_code.strip():
            raise ValueError("deny/require_user authorization requires reason_code")


@runtime_checkable
class AuthorizationPort(Protocol):
    async def authorize(self, prepared: PreparedToolEffect) -> AuthorizationResult: ...


__all__ = (
    "AuthorizationDecision",
    "AuthorizationPort",
    "AuthorizationResult",
    "PreparedToolEffect",
)
