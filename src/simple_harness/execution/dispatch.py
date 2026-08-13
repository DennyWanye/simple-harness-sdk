# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Single-owner durable coordination around the stateless Provider port."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol, cast

from simple_harness.contracts import FrozenJsonValue, HarnessError, RunId, thaw_json
from simple_harness.providers import (
    CancelToken,
    Provider,
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderPaymentRequiredError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderRequestRejectedError,
    ProviderResponse,
    ProviderServerError,
)

from .budget import (
    BudgetCharge,
    BudgetPolicy,
    BudgetSnapshot,
    FrozenPriceEstimator,
)
from .provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
    provider_invocation_id,
    provider_request_fingerprint,
    provider_response_from_json,
    provider_response_json,
)

if TYPE_CHECKING:
    from .uow import ExecutionLease


class ProviderInvocationUnitOfWork(Protocol):
    """SQLite implementation owns every method and its transaction boundary."""

    def claim_provider_invocation(
        self,
        record: ProviderInvocationRecord,
        *,
        budget_policy: BudgetPolicy,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord: ...

    def read_provider_invocation(
        self, invocation_id: str
    ) -> ProviderInvocationRecord | None: ...

    def hand_off_provider_invocation(
        self,
        invocation_id: str,
        *,
        expected_version: int,
        handed_off_at: float,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord: ...

    def settle_provider_invocation(
        self, record: ProviderInvocationRecord, *, expected_version: int
    ) -> ProviderInvocationRecord: ...

    def list_incomplete_provider_invocations(
        self,
    ) -> tuple[ProviderInvocationRecord, ...]: ...

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot: ...


class ProviderInvocationUnknownError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            "provider_invocation_unknown",
            "Provider invocation outcome is unknown and cannot be replayed.",
            retryable=False,
        )


class ProviderInvocationFailedError(HarnessError):
    def __init__(self, error_code: str | None) -> None:
        super().__init__(
            "provider_invocation_failed",
            "Provider invocation already failed.",
            retryable=False,
        )
        self.provider_error_code = error_code


class ProviderInvocationConflictError(HarnessError):
    def __init__(
        self, public_message: str = "Provider invocation already handed off."
    ) -> None:
        super().__init__(
            "provider_invocation_conflict",
            public_message,
            retryable=False,
        )


_DEFINITE_PROVIDER_FAILURES = (
    ProviderAuthenticationError,
    ProviderPaymentRequiredError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderServerError,
)


class ProviderInvocationCoordinator:
    """Durably claim, hand off once, and CAS-settle one Provider request."""

    def __init__(
        self,
        *,
        uow: ProviderInvocationUnitOfWork,
        provider: Provider,
        budget_policy: BudgetPolicy,
        estimator: FrozenPriceEstimator | None,
        clock=time.time,
    ) -> None:
        self._uow = uow
        self._provider = provider
        self._budget_policy = budget_policy
        self._estimator = estimator
        self._clock = clock
        if estimator is not None:
            estimator.bind(provider.target)

    async def prepare_claim(
        self,
        run_id: RunId,
        request: ProviderRequest,
        *,
        execution_lease: ExecutionLease,
    ) -> ProviderInvocationRecord:
        if (
            execution_lease.run_id != run_id.value
            or execution_lease.namespace != "runtime.kernel"
        ):
            raise ProviderInvocationConflictError(
                "Provider invocation requires the canonical Run lease."
            )
        fingerprint = provider_request_fingerprint(request)
        invocation_id = provider_invocation_id(run_id, request.request_id)
        reservation = (
            BudgetCharge.unknown()
            if self._estimator is None
            else self._estimator.estimate_upper_bound(request)
        )
        record = ProviderInvocationRecord.claimed(
            invocation_id=invocation_id,
            run_id=run_id,
            request_id=request.request_id,
            request_fingerprint=fingerprint,
            target=self._provider.target,
            estimator_snapshot=(
                None if self._estimator is None else self._estimator.snapshot_json()
            ),
            estimator_digest=(
                None if self._estimator is None else self._estimator.snapshot_digest
            ),
            reservation=reservation,
            claimed_at=self._clock(),
        )
        claimed = self._uow.claim_provider_invocation(
            record,
            budget_policy=self._budget_policy,
            execution_lease=execution_lease,
        )
        if (
            claimed.run_id != run_id
            or claimed.request_id != request.request_id
            or claimed.request_fingerprint != fingerprint
            or claimed.target != self._provider.target
            or claimed.target_digest != record.target_digest
            or claimed.estimator_digest != record.estimator_digest
        ):
            raise ProviderInvocationConflictError(
                "Provider invocation identity conflict."
            )
        return claimed

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot:
        """Expose the durable budget authority without leaking the UoW."""

        return self._uow.read_provider_budget(run_id)

    async def invoke(
        self,
        run_id: RunId,
        request: ProviderRequest,
        *,
        cancel: CancelToken,
        execution_lease: ExecutionLease,
    ) -> ProviderResponse:
        if (
            execution_lease.run_id != run_id.value
            or execution_lease.namespace != "runtime.kernel"
        ):
            raise ProviderInvocationConflictError(
                "Provider invocation requires the canonical Run lease."
            )
        record = await self.prepare_claim(
            run_id, request, execution_lease=execution_lease
        )
        if record.state is ProviderInvocationState.SUCCEEDED:
            if record.response_json is None:
                raise ProviderInvocationUnknownError()
            return provider_response_from_json(
                thaw_json(cast(FrozenJsonValue, record.response_json))
            )
        if record.state is ProviderInvocationState.FAILED:
            raise ProviderInvocationFailedError(record.error_code)
        if record.state is ProviderInvocationState.UNKNOWN:
            raise ProviderInvocationUnknownError()
        if record.state is ProviderInvocationState.HANDED_OFF:
            raise ProviderInvocationConflictError()
        try:
            handed_off = self._uow.hand_off_provider_invocation(
                record.invocation_id,
                expected_version=record.version,
                handed_off_at=self._clock(),
                execution_lease=execution_lease,
            )
        except ValueError as exc:
            current = self._uow.read_provider_invocation(record.invocation_id)
            if (
                current is not None
                and current.state is ProviderInvocationState.HANDED_OFF
            ):
                raise ProviderInvocationConflictError() from exc
            raise

        try:
            response = await self._provider.invoke(request, cancel=cancel)
        except _DEFINITE_PROVIDER_FAILURES as exc:
            failed = handed_off.settle_failed(
                error_code=str(exc.code),
                at=self._clock(),
                expected_version=handed_off.version,
            )
            self._uow.settle_provider_invocation(
                failed, expected_version=handed_off.version
            )
            raise
        except (ProviderCancelledError, asyncio.CancelledError) as exc:
            await self._settle_unknown(handed_off, "provider_cancelled_after_handoff")
            raise ProviderInvocationUnknownError() from exc
        except BaseException as exc:
            await self._settle_unknown(handed_off, "provider_error_after_handoff")
            raise ProviderInvocationUnknownError() from exc

        charge = self._response_charge(response, handed_off.budget_charge)
        usage_json = {
            "usage": (
                None
                if response.usage is None
                else {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                }
            ),
            "budget": charge.to_json(),
        }
        succeeded = handed_off.settle_succeeded(
            response_json=provider_response_json(response),
            usage_json=usage_json,
            budget_charge=charge,
            at=self._clock(),
            expected_version=handed_off.version,
        )
        try:
            self._uow.settle_provider_invocation(
                succeeded, expected_version=handed_off.version
            )
        except BaseException as exc:
            current = self._uow.read_provider_invocation(record.invocation_id)
            if (
                current is not None
                and current.state is ProviderInvocationState.SUCCEEDED
            ):
                return provider_response_from_json(
                    thaw_json(cast(FrozenJsonValue, current.response_json))
                )
            if (
                current is not None
                and current.state is ProviderInvocationState.HANDED_OFF
            ):
                await self._settle_unknown(
                    current, "provider_settlement_commit_unknown"
                )
            raise ProviderInvocationUnknownError() from exc
        return response

    def _response_charge(
        self, response: ProviderResponse, reservation: BudgetCharge
    ) -> BudgetCharge:
        if (
            response.usage is not None
            and self._estimator is not None
            and response.model == self._provider.target.model
        ):
            return self._estimator.charge_usage(response.usage)
        if response.usage is None and not reservation.is_unknown:
            return reservation
        return BudgetCharge.unknown()

    async def _settle_unknown(
        self, handed_off: ProviderInvocationRecord, error_code: str
    ) -> None:
        unknown = handed_off.settle_unknown(
            error_code=error_code,
            at=self._clock(),
            expected_version=handed_off.version,
        )
        try:
            self._uow.settle_provider_invocation(
                unknown, expected_version=handed_off.version
            )
        except ValueError:
            current = self._uow.read_provider_invocation(handed_off.invocation_id)
            if current is None or current.state not in {
                ProviderInvocationState.UNKNOWN,
                ProviderInvocationState.SUCCEEDED,
                ProviderInvocationState.FAILED,
            }:
                raise

    async def reconcile_incomplete(self) -> int:
        """Fail closed stranded handoffs; claimed rows remain safe to resume."""

        settled = 0
        for record in self._uow.list_incomplete_provider_invocations():
            if record.state is not ProviderInvocationState.HANDED_OFF:
                continue
            await self._settle_unknown(record, "recovered_after_handoff")
            settled += 1
        return settled


__all__ = (
    "ProviderInvocationConflictError",
    "ProviderInvocationCoordinator",
    "ProviderInvocationFailedError",
    "ProviderInvocationUnitOfWork",
    "ProviderInvocationUnknownError",
)
