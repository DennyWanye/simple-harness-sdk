# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``RuntimeAuthorizationAdapter``: the only caller of the real evaluation point (INTERFACES §2).

The adapter never decides anything itself.  It hands a ``TrustedRuntimeRequest``
(identity from upstream authentication / runtime bindings, never from a model
payload) to the deployment's real evaluator and records the evaluated policy,
scope, input hash and the evaluator's own decision receipt as an
``AuthorizationReceipt``.  An evaluator that cannot name the policy it evaluated
(``AllowAllAuthorization`` and friends) is refused for ARP-configured runtimes;
legacy runtimes keep their behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .codec import check
from .errors import ArpError
from .pins import Pin
from .strict import digest

PURPOSES = (
    "CONTEXT_DISCLOSE",
    "HISTORY_READ",
    "SKILL_LOAD",
    "TOOL_EXECUTE",
    "SESSION_DESTROY",
    "REGISTRY_MUTATE",
)
DECISIONS = ("ALLOW", "DENY", "NEEDS_APPROVAL", "UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class TrustedRuntimeRequest:
    request_key: str
    session_id: str
    agent_id: str
    caller_ref: Pin
    service_owner_ref: Pin
    owner_contract_ref: Pin
    purpose: str
    target_refs: tuple[Pin, ...]
    input_hash: str
    expected_policy_refs: tuple[Pin, ...] = ()

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            raise ArpError("ENUM", field_path="purpose")
        self.caller_ref.require_kind("principal")
        self.service_owner_ref.require_kind("principal")
        self.owner_contract_ref.require_kind("task", "policy")
        for ref in self.expected_policy_refs:
            ref.require_kind("policy")
        if type(self.input_hash) is not str or len(self.input_hash) != 64:
            raise ArpError("STRING_PATTERN", field_path="input_hash")

    def to_json(self) -> dict[str, Any]:
        return {
            "request_key": self.request_key,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "caller_ref": self.caller_ref.to_json(),
            "service_owner_ref": self.service_owner_ref.to_json(),
            "owner_contract_ref": self.owner_contract_ref.to_json(),
            "purpose": self.purpose,
            "target_refs": [ref.to_json() for ref in self.target_refs],
            "input_hash": self.input_hash,
            "expected_policy_refs": [ref.to_json() for ref in self.expected_policy_refs],
        }


@dataclass(frozen=True, slots=True)
class OriginalDecision:
    """What the deployment's real evaluation point returns (its own receipt included)."""

    decision: str
    policy_ref: Pin
    scope_ref: Pin
    decision_receipt_ref: Pin
    expires_at_ms: int
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ArpError("ENUM", field_path="decision")
        self.policy_ref.require_kind("policy")
        self.scope_ref.require_kind("scope")
        self.decision_receipt_ref.require_kind("receipt")


class OriginalEvaluator(Protocol):
    """The deployment's existing policy evaluation point, adapted to ARP identities."""

    #: Stable identity of the evaluator implementation (for the receipt / diagnostics).
    evaluator_id: str

    async def evaluate(self, request: TrustedRuntimeRequest) -> OriginalDecision: ...


def is_allow_all(port: object) -> bool:
    """True for evaluation ports that allow everything without a policy source."""

    name = type(port).__name__
    if name in {"AllowAllAuthorization", "AllowAllAdmission", "AllowAllEvaluator"}:
        return True
    return bool(getattr(port, "allow_all", False))


@dataclass(slots=True)
class RuntimeAuthorizationAdapter:
    evaluator: OriginalEvaluator
    clock_ms: Callable[[], int]
    recorded: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if is_allow_all(self.evaluator):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "AllowAll evaluators are refused for ARP runtimes")
        if not callable(getattr(self.evaluator, "evaluate", None)):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "evaluator has no evaluate()")

    async def evaluate(self, request: TrustedRuntimeRequest) -> Mapping[str, Any]:
        """Evaluate once and return the validated ``AuthorizationReceipt`` body."""

        decision = await self.evaluator.evaluate(request)
        if not isinstance(decision, OriginalDecision):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "evaluator returned no original decision")
        if request.expected_policy_refs and decision.policy_ref not in request.expected_policy_refs:
            raise ArpError("POLICY_CONFLICT", "evaluated policy differs from the expected policy pin")
        observed = self.clock_ms()
        receipt = {
            "schema_version": 1,
            "authorization_id": "auth-" + digest({"request": request.to_json(), "at": observed})[:32],
            "request_key": request.request_key,
            "session_id": request.session_id,
            "agent_id": request.agent_id,
            "caller_ref": request.caller_ref.to_json(),
            "owner_contract_ref": request.owner_contract_ref.to_json(),
            "purpose": request.purpose,
            "subject_refs": [ref.to_json() for ref in request.target_refs],
            "input_hash": request.input_hash,
            "decision": decision.decision,
            "scope_ref": decision.scope_ref.to_json(),
            "policy_ref": decision.policy_ref.to_json(),
            "original_decision_receipt_ref": decision.decision_receipt_ref.to_json(),
            "observed_at_ms": observed,
            "expires_at_ms": max(decision.expires_at_ms, observed),
        }
        check("AuthorizationReceipt", receipt)
        self.recorded.append(receipt)
        return receipt

    async def require_allow(self, request: TrustedRuntimeRequest) -> Mapping[str, Any]:
        receipt = await self.evaluate(request)
        if receipt["decision"] != "ALLOW":
            code = {
                "DENY": "ACCESS_DENIED",
                "NEEDS_APPROVAL": "AUTHORIZATION_REQUIRED",
                "UNAVAILABLE": "AUTHORITY_SOURCE_MISSING",
            }[receipt["decision"]]
            raise ArpError(code, f"{request.purpose} not allowed", detail={"receipt": receipt})
        return receipt


def receipt_pin(receipt: Mapping[str, Any]) -> Pin:
    return Pin("receipt", str(receipt["authorization_id"]), 0, digest(receipt))


__all__ = (
    "DECISIONS",
    "OriginalDecision",
    "OriginalEvaluator",
    "PURPOSES",
    "RuntimeAuthorizationAdapter",
    "TrustedRuntimeRequest",
    "is_allow_all",
    "receipt_pin",
)
