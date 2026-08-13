# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Evidence-bound recovery observations for uncertain Provider invocations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .base import ProviderResponse

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


__all__ = (
    "ProviderReconciliationObservation",
    "ProviderReconciliationPort",
    "ProviderReconciliationState",
)
