# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Evidence-bound recovery observations for uncertain Provider invocations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from simple_harness.contracts import FrozenJsonValue, canonical_json, thaw_json

from .base import ProviderResponse, ProviderUsage

if TYPE_CHECKING:
    from simple_harness.execution.provider_invocations import ProviderInvocationRecord


class ProviderReconciliationState(StrEnum):
    COMPLETED = "completed"
    CONFIRMED_NOT_STARTED = "confirmed_not_started"
    STILL_UNKNOWN = "still_unknown"


@dataclass(frozen=True, slots=True)
class ProviderReconciliationObservation:
    state: ProviderReconciliationState
    evidence_ref: str
    response: ProviderResponse | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ProviderReconciliationState(self.state))
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise ValueError("evidence_ref is required")
        if self.state is ProviderReconciliationState.COMPLETED:
            if not isinstance(self.response, ProviderResponse):
                raise ValueError("completed observation requires ProviderResponse")
        elif self.response is not None:
            raise ValueError("only completed observation may carry ProviderResponse")


@runtime_checkable
class ProviderReconciliationPort(Protocol):
    async def observe(
        self, invocation: ProviderInvocationRecord
    ) -> ProviderReconciliationObservation: ...


@dataclass(frozen=True, slots=True)
class ProviderAccountingIdentity:
    """Authority of one original terminal call, not authority to run it again."""

    invocation_id: str
    run_id: str
    request_id: str
    handoff_attempt: int
    request_fingerprint: str
    target_digest: str
    estimator_digest: str | None
    response_digest: str
    original_state: str
    original_version: int

    def __post_init__(self) -> None:
        for name in ("run_id", "request_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"accounting {name} is required")
        for name in (
            "invocation_id",
            "request_fingerprint",
            "target_digest",
            "response_digest",
            "estimator_digest",
        ):
            value = getattr(self, name)
            if value is None and name == "estimator_digest":
                continue
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"accounting {name} must be a SHA-256 digest")
        if self.original_state not in {"succeeded", "failed"}:
            raise ValueError("accounting identity must refer to a terminal call")
        if any(
            type(value) is not int or value < 1
            for value in (
                self.handoff_attempt,
                self.original_version,
            )
        ):
            raise ValueError("accounting ordinal and version must be positive integers")

    @classmethod
    def from_record(cls, record: ProviderInvocationRecord) -> ProviderAccountingIdentity:
        return cls(
            record.invocation_id,
            record.run_id.value,
            record.request_id.value,
            record.handoff_attempt,
            record.request_fingerprint,
            record.target_digest,
            record.estimator_digest,
            sha256(
                canonical_json(thaw_json(cast(FrozenJsonValue, record.response_json))).encode()
            ).hexdigest(),
            record.state.value,
            record.version,
        )

    def to_json(self):
        return asdict(self)


class ProviderAccountingState(StrEnum):
    KNOWN = "known"
    STILL_UNKNOWN = "still_unknown"


@dataclass(frozen=True, slots=True)
class ProviderAccountingObservation:
    state: ProviderAccountingState
    identity: ProviderAccountingIdentity
    evidence_ref: str
    usage: ProviderUsage | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ProviderAccountingState(self.state))
        if not isinstance(self.identity, ProviderAccountingIdentity):
            raise TypeError("accounting requires original invocation authority")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise ValueError("accounting evidence_ref is required")
        if self.state is ProviderAccountingState.KNOWN:
            if not isinstance(self.usage, ProviderUsage):
                raise ValueError("known accounting requires actual ProviderUsage")
        elif self.usage is not None:
            raise ValueError("unknown accounting cannot supply usage")


@runtime_checkable
class ProviderAccountingPort(Protocol):
    """Optional companion to reconciliation; never supplies execution results."""

    async def observe_accounting(
        self, invocation: ProviderInvocationRecord
    ) -> ProviderAccountingObservation: ...


__all__ = (
    "ProviderAccountingIdentity",
    "ProviderAccountingObservation",
    "ProviderAccountingPort",
    "ProviderAccountingState",
    "ProviderReconciliationObservation",
    "ProviderReconciliationPort",
    "ProviderReconciliationState",
)
