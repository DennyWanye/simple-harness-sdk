# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Model Router (§9.3, theory 13-3, ORCH-BUILD §8.2, plan D6-4 / D6-4').

A *runtime profile* is this build's name (plan §6.1) for one physical target: one
provider, one model name, its own price table and its own SDK execution library.
Routing picks the profile for an Attempt by role, then by task kind, then the
default; §9.3's escalation ladder ("便宜模型先尝试 → 失败或低置信 → 换更强模型") moves a
Task to ``escalate[profile]`` after ``escalate_after_failures`` failed Attempts on
that profile; a profile in its unavailability cooldown is replaced by
``fallback[profile]`` or the Task waits (bounded, plan D6-5').  The decision is frozen
into the dispatch intent and echoed back by the provider — the echo is what proves
the physical route (S6-03), never the label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Attempt

ROUTER_VERSION = "model-router-v1"
DEFAULT_PROFILE = "default"
# review P1-8: the error classification table.  Kinds are the SDK audit vocabulary
# (``simple_harness.execution.audit``); they are matched inside the turn's error record.
UNAVAILABLE_KINDS = frozenset(
    {
        "provider_server_error",
        "provider_rate_limited",
        "provider_timeout",
        "provider_transport_error",
    }
)
# neither a health signal nor a reason to escalate: the SDK already retried inside the turn,
# or the turn was cancelled on purpose
NEUTRAL_KINDS = frozenset(
    {"provider_empty_response", "provider_cancelled", "provider_cancelled_after_handoff"}
)
CODE_KEYS = frozenset({"error_code", "code", "kind"})
ESCALATION_REASONS = frozenset(
    {"verification_failed", "outcome_failure", "envelope_invalid", "turn_failed"}
)


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """profile → actual provider / model / price / output caps (ORCH §8.2 model_router row)."""

    profile_id: str
    provider: Any
    model: str
    tier: int = 0  # higher = stronger; the escalation ladder only climbs
    price_table: Any | None = None  # runtime.assembly.PriceTable; None = unpriced
    default_max_output_tokens: int | None = None
    max_output_tokens_ceiling: int | None = None
    provider_kind: str = "fixtures"

    def __post_init__(self) -> None:
        if not self.profile_id or not self.model:
            raise ValueError("a runtime profile needs a profile_id and a model")
        if not callable(getattr(self.provider, "invoke", None)):
            raise TypeError(f"profile {self.profile_id}: provider must implement invoke")

    @property
    def unpriced(self) -> bool:
        return self.price_table is None

    def to_json(self) -> dict[str, Any]:  # never the provider object (no credentials leak)
        return {
            "profile_id": self.profile_id,
            "model": self.model,
            "tier": self.tier,
            "provider_kind": self.provider_kind,
            "priced": self.price_table is not None,
            "default_max_output_tokens": self.default_max_output_tokens,
            "max_output_tokens_ceiling": self.max_output_tokens_ceiling,
        }


@dataclass(frozen=True, slots=True)
class RoutingRules:
    default: str = DEFAULT_PROFILE
    by_role: Mapping[str, str] = field(default_factory=dict)
    by_task_kind: Mapping[str, str] = field(default_factory=dict)
    escalate: Mapping[str, str] = field(default_factory=dict)
    escalate_after_failures: int = 1
    fallback: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "default": self.default,
            "by_role": dict(self.by_role),
            "by_task_kind": dict(self.by_task_kind),
            "escalate": dict(self.escalate),
            "escalate_after_failures": self.escalate_after_failures,
            "fallback": dict(self.fallback),
            "version": ROUTER_VERSION,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    profile_id: str
    model: str
    reason: str
    escalated_from: str | None = None
    fallback_from: str | None = None
    version: str = ROUTER_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model": self.model,
            "reason": self.reason,
            "escalated_from": self.escalated_from,
            "fallback_from": self.fallback_from,
            "router_version": self.version,
        }


class RoutingUnavailable(RuntimeError):
    """No profile can take the work now: the candidate is cooling down and has no fallback."""

    def __init__(self, profile_id: str, until: float | None) -> None:
        super().__init__(f"runtime profile {profile_id} is unavailable")
        self.profile_id = profile_id
        self.until = until


def classify_turn_error(error: Mapping[str, Any] | None) -> str:
    """``provider_unavailable`` (feeds profile health), ``provider_error`` (feeds the
    escalation ladder, e.g. tool_parse protocol errors — L4-4) or ``other``."""

    if not error:
        return "other"
    codes = _error_codes(error)
    if codes & UNAVAILABLE_KINDS:
        return "provider_unavailable"
    if any(code.startswith("provider_") and code not in NEUTRAL_KINDS for code in codes):
        return "provider_error"
    return "other"


def _error_codes(value: Any) -> set[str]:
    """Exact error codes anywhere in the SDK's turn error record (review P2-1: no substring
    matching over free text)."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in CODE_KEYS and isinstance(item, str):
                found.add(item)
            else:
                found |= _error_codes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _error_codes(item)
    return found


class ModelRouter:
    def __init__(self, profiles: Mapping[str, RuntimeProfile], rules: RoutingRules) -> None:
        if not profiles:
            raise ValueError("at least one runtime profile is required")
        self._profiles = dict(profiles)
        self._rules = rules
        for name, target in (
            ("default", rules.default),
            *((f"by_role[{k}]", v) for k, v in rules.by_role.items()),
            *((f"by_task_kind[{k}]", v) for k, v in rules.by_task_kind.items()),
            *((f"escalate[{k}]", v) for k, v in rules.escalate.items()),
            *((f"fallback[{k}]", v) for k, v in rules.fallback.items()),
        ):
            if target not in self._profiles:
                raise ValueError(f"routing rule {name} names an unknown profile {target!r}")

    @property
    def profiles(self) -> Mapping[str, RuntimeProfile]:
        return self._profiles

    @property
    def rules(self) -> RoutingRules:
        return self._rules

    def profile(self, profile_id: str) -> RuntimeProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise KeyError(f"runtime profile {profile_id!r} is not configured") from error

    def base_profile(self, *, role: str, task_kind: str | None) -> tuple[str, str]:
        if role in self._rules.by_role:
            return self._rules.by_role[role], f"by_role:{role}"
        if task_kind is not None and task_kind in self._rules.by_task_kind:
            return self._rules.by_task_kind[task_kind], f"by_task_kind:{task_kind}"
        return self._rules.default, "default"

    def route(
        self,
        *,
        role: str,
        task_kind: str | None = None,
        previous_attempts: Sequence[Attempt] = (),
        unavailable_until: Mapping[str, float] | None = None,
        now: float = 0.0,
    ) -> RoutingDecision:
        profile_id, reason = self.base_profile(role=role, task_kind=task_kind)
        escalated_from: str | None = None
        # §9.3 escalation: climb the ladder while the current rung has failed enough times
        seen: set[str] = set()
        while profile_id in self._rules.escalate and profile_id not in seen:
            seen.add(profile_id)
            failures = sum(
                1
                for attempt in previous_attempts
                if attempt.runtime_profile_id == profile_id and _counts_for_escalation(attempt)
            )
            if failures < self._rules.escalate_after_failures:
                break
            escalated_from = profile_id
            profile_id = self._rules.escalate[profile_id]
            reason = f"escalate:{escalated_from}->{profile_id}:failures={failures}"
        fallback_from: str | None = None
        health = unavailable_until or {}
        until = health.get(profile_id)
        if until is not None and now < until:
            if profile_id in self._rules.fallback:
                fallback_from = profile_id
                profile_id = self._rules.fallback[profile_id]
                reason = f"fallback:unavailable:{fallback_from}->{profile_id}"
                target_until = health.get(profile_id)  # review P2-3: the fallback may be down too
                if target_until is not None and now < target_until:
                    raise RoutingUnavailable(profile_id, target_until)
            else:
                raise RoutingUnavailable(profile_id, until)
        return RoutingDecision(
            profile_id=profile_id,
            model=self.profile(profile_id).model,
            reason=reason,
            escalated_from=escalated_from,
            fallback_from=fallback_from,
        )


def _counts_for_escalation(attempt: Attempt) -> bool:
    failure = attempt.failure or {}
    reason = str(failure.get("reason", ""))
    if reason not in ESCALATION_REASONS:
        return False
    if reason == "turn_failed":
        # review P2-1: only a provider-side error of the turn (e.g. tool_parse) climbs the
        # ladder; unavailability is a health matter and an empty answer was already retried
        return str(failure.get("error_kind", "")) == "provider_error"
    return True


__all__ = (
    "DEFAULT_PROFILE",
    "ESCALATION_REASONS",
    "ROUTER_VERSION",
    "UNAVAILABLE_KINDS",
    "ModelRouter",
    "RoutingDecision",
    "RoutingRules",
    "RoutingUnavailable",
    "RuntimeProfile",
    "classify_turn_error",
)
