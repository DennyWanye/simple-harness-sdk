# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Optional deployment admission before each physical provider handoff.

Input estimators describe a particular wire protocol, not a universal tokenizer
bound. The request is the final copy after tool-call restoration. No estimator
is supplied by default. Accounting and the physical slot belong to the caller.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from simple_harness.providers import CancelToken, ProviderRequest
from simple_harness.providers.errors import ProviderRequestRejectedError

from .provider_invocations import ProviderInvocationRecord


class TokenEstimatorPort(Protocol):
    fingerprint: str
    bound_protocol: str
    requires_prior_output_reserve: bool

    def estimate_input_tokens(self, request: ProviderRequest) -> int:
        """Public-wire input allowance; the guard adds prior output when required."""
        ...


class ProviderAdmissionDenied(ProviderRequestRejectedError):
    """No handoff was authorized. Retrying must obtain fresh admission."""

    error_code = "provider_admission_denied"


@dataclass(frozen=True, slots=True)
class ProviderAdmissionTicket:
    invocation_id: str
    handoff_ordinal: int
    wire_fingerprint: str
    authority_fingerprint: str


class ProviderAdmissionPort(Protocol):
    fingerprint: str

    def waiting_for_slot(self, *, agent_id: str, turn_id: str) -> bool:
        """Whether this executor is actually queued, with no physical handoff."""
        ...

    async def acquire(
        self,
        *,
        request: ProviderRequest,
        record: ProviderInvocationRecord,
        cancel: CancelToken,
        uow: object,
        execution_lease: object,
    ) -> ProviderAdmissionTicket:
        """Wait outside transactions; atomically reserve tokens and a shared slot."""
        ...

    def handoff(
        self,
        ticket: ProviderAdmissionTicket,
        *,
        request: ProviderRequest,
        cancel: CancelToken,
    ) -> AbstractContextManager[None]:
        """Fence public cancellation around synchronous SDK handoff; never await."""
        ...

    def observe(
        self,
        ticket: ProviderAdmissionTicket,
        *,
        record: ProviderInvocationRecord | None,
    ) -> None:
        """Keep uncertain handoffs held; settle only durable terminal usage."""
        ...

    def recover(self, uow: object) -> None:
        """Reconcile durable grants against this pool's actual SDK records."""
        ...


__all__ = (
    "ProviderAdmissionDenied",
    "ProviderAdmissionPort",
    "ProviderAdmissionTicket",
    "TokenEstimatorPort",
)
