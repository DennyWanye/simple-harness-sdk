# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned authorization seam for prepared Tool effects."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from simple_harness.contracts import EffectId, JsonValue, RunId, canonical_json, freeze_json

from .contracts import JsonObject, ToolCall, ToolSpec
from .sidecar import Sidecar, ToolResource


class AuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_USER = "require_user"


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Frozen user-decision request.  The nonce is the public replay fence."""

    prompt: str
    nonce: str
    expires_at: float | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("prompt", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.expires_at is not None and (
            not isinstance(self.expires_at, (int, float))
            or isinstance(self.expires_at, bool)
            or not math.isfinite(float(self.expires_at))
            or self.expires_at < 0
        ):
            raise ValueError("expires_at must be finite and non-negative")
        metadata = dict(self.metadata)
        frozen = freeze_json(metadata)
        if not isinstance(frozen, dict):
            # freeze_json currently preserves Mapping, but keep this boundary explicit.
            from collections.abc import Mapping

            if not isinstance(frozen, Mapping):
                raise TypeError("authorization metadata must be an object")
        object.__setattr__(self, "metadata", frozen)


@dataclass(frozen=True, slots=True)
class AuthorizationReceipt:
    """Opaque Host receipt, cryptographically bound to one SDK receipt hash."""

    receipt_ref: str
    receipt_hash: str
    bound_sdk_receipt_hash: str

    def __post_init__(self) -> None:
        if not self.receipt_ref.strip():
            raise ValueError("receipt_ref is required")
        for name in ("receipt_hash", "bound_sdk_receipt_hash"):
            value = getattr(self, name)
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be lowercase SHA-256")


def sdk_authorization_receipt(kind: str, payload: dict[str, JsonValue]) -> AuthorizationReceipt:
    """Build the deterministic SDK side of a cross-store receipt handshake."""

    body = {"kind": kind, "payload": payload, "protocol": "authorization-receipt-v1"}
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return AuthorizationReceipt(
        receipt_ref=f"sdk:{kind}:{digest}",
        receipt_hash=digest,
        bound_sdk_receipt_hash=digest,
    )


def bind_authorization_receipts(
    sdk_receipt: AuthorizationReceipt, host_receipt: AuthorizationReceipt
) -> str:
    """Return one durable ref containing both receipts and their hash chain."""

    if host_receipt.bound_sdk_receipt_hash != sdk_receipt.receipt_hash:
        raise ValueError("Host receipt is not bound to the SDK receipt")
    payload = {
        "host_receipt_hash": host_receipt.receipt_hash,
        "host_receipt_ref": host_receipt.receipt_ref,
        "sdk_receipt_hash": sdk_receipt.receipt_hash,
        "sdk_receipt_ref": sdk_receipt.receipt_ref,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"authorization-binding-v1:{digest}:{canonical_json(payload)}"


@dataclass(frozen=True, slots=True)
class PreparedToolEffect:
    effect_id: EffectId
    run_id: RunId
    call: ToolCall
    spec: ToolSpec
    context_metadata: JsonObject
    sidecar: Sidecar | None = None
    resources: tuple[ToolResource, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, EffectId):
            raise TypeError("effect_id must use EffectId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if self.call.name != self.spec.name:
            raise ValueError("prepared Tool call and spec differ")
        expected = self.spec.sidecar
        if self.sidecar is None:
            object.__setattr__(self, "sidecar", expected)
        elif not isinstance(self.sidecar, Sidecar) or self.sidecar != expected:
            raise ValueError("prepared Tool sidecar differs from registry authority")
        resources = tuple(self.resources)
        if any(not isinstance(resource, ToolResource) for resource in resources):
            raise TypeError("resources must use ToolResource")
        if len(resources) != len(
            {(value.namespace, value.resource_id, value.actions) for value in resources}
        ):
            raise ValueError("resources contain duplicate claims")
        object.__setattr__(self, "resources", resources)


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: AuthorizationDecision
    receipt_ref: str | None = None
    reason_code: str | None = None
    public_message: str | None = None
    request: AuthorizationRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorizationDecision):
            object.__setattr__(self, "decision", AuthorizationDecision(self.decision))
        if self.decision is AuthorizationDecision.ALLOW:
            if not self.receipt_ref or not self.receipt_ref.strip():
                raise ValueError("allow authorization requires receipt_ref")
            if self.reason_code is not None:
                raise ValueError("allow authorization cannot have reason_code")
            if self.request is not None:
                raise ValueError("allow authorization cannot carry a user request")
        elif self.decision is AuthorizationDecision.REQUIRE_USER:
            if not self.reason_code or not self.reason_code.strip():
                raise ValueError("require-user authorization requires reason_code")
            if self.request is None:
                raise ValueError("require-user authorization requires a request")
        elif not self.reason_code or not self.reason_code.strip():
            raise ValueError("deny/require_user authorization requires reason_code")


@runtime_checkable
class AuthorizationPort(Protocol):
    async def prepare(self, prepared: PreparedToolEffect) -> AuthorizationResult: ...

    async def bind_decision(
        self,
        prepared: PreparedToolEffect,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        sdk_receipt: AuthorizationReceipt,
    ) -> AuthorizationReceipt: ...

    async def bind_effect_handoff(
        self,
        prepared: PreparedToolEffect,
        authorization_receipt_ref: str,
        sdk_receipt: AuthorizationReceipt,
    ) -> AuthorizationReceipt: ...


__all__ = (
    "AuthorizationDecision",
    "AuthorizationPort",
    "AuthorizationReceipt",
    "AuthorizationRequest",
    "AuthorizationResult",
    "PreparedToolEffect",
    "bind_authorization_receipts",
    "sdk_authorization_receipt",
)
