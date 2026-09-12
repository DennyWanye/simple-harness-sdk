# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Optional deployment admission before each physical provider handoff.

Input estimators describe a particular wire protocol, not a universal tokenizer
bound. The request is the final copy after tool-call restoration. No estimator
is supplied by default. Accounting and the physical slot belong to the caller.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True, slots=True)
class ProviderAdmissionFailure:
    """Safe durable denial data; requested/remaining describe incremental growth."""

    reason_code: str = "authority_rejected"
    account_id: str | None = None
    dimension: str | None = None
    requested: int | None = None
    remaining: int | None = None
    invocation_id: str | None = None
    handoff_ordinal: int | None = None
    mission_id: str | None = None
    subject_id: str | None = None
    bound_protocol: str | None = None
    request_tokens: int | None = None
    request_cost_micros: int | None = None

    def to_json(self) -> dict:
        return {"schema_version": 1, **asdict(self)}


class ProviderAdmissionDenied(ProviderRequestRejectedError):
    """No handoff was authorized. Retrying must obtain fresh admission."""

    error_code = "provider_admission_denied"

    def __init__(
        self,
        *,
        public_message: str | None = None,
        admission_detail: ProviderAdmissionFailure | None = None,
    ) -> None:
        super().__init__(public_message=public_message, retryable=False)
        self.admission_detail = admission_detail or ProviderAdmissionFailure()
        # Existing SDK failure persistence consumes Mapping-valued ``detail``.
        self.detail = self.admission_detail.to_json()


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
    "ProviderAdmissionFailure",
    "ProviderAdmissionPort",
    "ProviderAdmissionTicket",
    "TokenEstimatorPort",
)
