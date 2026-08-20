# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Single-owner durable coordination around the stateless Provider port."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
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
    ProviderReconciliationPort,
    ProviderReconciliationState,
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
    budget_policy_fingerprint,
)
from .provider_invocations import (
    ProviderInvocationRecord,
    ProviderInvocationState,
    provider_invocation_id,
    provider_request_fingerprint,
    provider_request_json,
    provider_response_from_json,
    provider_response_json,
)
from .recovery import ResolutionOutcome

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from simple_harness.workflow.lease import WorkflowLease

    from .recovery import ReconciliationResolution
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
        workflow_lease: WorkflowLease | None = None,
    ) -> ProviderInvocationRecord: ...

    def settle_provider_invocation(
        self, record: ProviderInvocationRecord, *, expected_version: int
    ) -> ProviderInvocationRecord: ...

    def list_incomplete_provider_invocations(
        self,
    ) -> tuple[ProviderInvocationRecord, ...]: ...

    def read_provider_budget(self, run_id: RunId) -> BudgetSnapshot: ...

    def record_provider_reconciliation(
        self,
        record: ProviderInvocationRecord,
        *,
        outcome: ResolutionOutcome,
        response_json: object | None,
        usage_json: object | None,
        budget_charge: BudgetCharge,
        evidence_ref: str,
        now: float,
        fault: Callable[[str], None] | None = None,
    ) -> ProviderInvocationRecord: ...

    def read_reconciliation_resolution(
        self, *, kind: str, ledger_identity: str, handoff_attempt: int
    ) -> ReconciliationResolution | None: ...

    def reauthorize_provider_not_started(
        self,
        record: ProviderInvocationRecord,
        *,
        resolution,
        execution_lease: ExecutionLease,
        now: float,
    ) -> ProviderInvocationRecord: ...


class ProviderInvocationUnknownError(HarnessError):
    def __init__(self, invocation: ProviderInvocationRecord | None = None) -> None:
        super().__init__(
            "provider_invocation_unknown",
            "Provider invocation outcome is unknown and cannot be replayed.",
            retryable=False,
        )
        self.invocation = invocation


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

    @property
    def budget_policy_fingerprint(self) -> str:
        return budget_policy_fingerprint(self._budget_policy, self._estimator)

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
            request_json=provider_request_json(request),
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
        workflow_lease: WorkflowLease | None = None,
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
                raise ProviderInvocationUnknownError(record)
            return provider_response_from_json(
                thaw_json(cast(FrozenJsonValue, record.response_json))
            )
        if record.state is ProviderInvocationState.FAILED:
            raise ProviderInvocationFailedError(record.error_code)
        if record.state is ProviderInvocationState.UNKNOWN:
            resolution = self._uow.read_reconciliation_resolution(
                kind="provider",
                ledger_identity=record.invocation_id,
                handoff_attempt=record.handoff_attempt,
            )
            if (
                resolution is None
                or resolution.outcome is not ResolutionOutcome.CONFIRMED_NOT_STARTED
            ):
                raise ProviderInvocationUnknownError(record)
            record = self._uow.reauthorize_provider_not_started(
                record,
                resolution=resolution,
                execution_lease=execution_lease,
                now=self._clock(),
            )
        if record.state is ProviderInvocationState.HANDED_OFF:
            raise ProviderInvocationConflictError()
        try:
            handed_off = self._uow.hand_off_provider_invocation(
                record.invocation_id,
                expected_version=record.version,
                handed_off_at=self._clock(),
                execution_lease=execution_lease,
                workflow_lease=workflow_lease,
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
            unknown = await self._settle_unknown(
                handed_off, "provider_cancelled_after_handoff"
            )
            raise ProviderInvocationUnknownError(unknown) from exc
        except BaseException as exc:
            unknown = await self._settle_unknown(
                handed_off, "provider_error_after_handoff"
            )
            raise ProviderInvocationUnknownError(unknown) from exc

        charge = self._response_charge(response, handed_off.budget_charge)
        usage_json = {
            "usage": (
                None
                if response.usage is None
                else {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cache_tokens": response.usage.cache_tokens,
                    "reasoning_tokens": response.usage.reasoning_tokens,
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
            current = self._uow.read_provider_invocation(record.invocation_id)
            raise ProviderInvocationUnknownError(current) from exc
        logger.info(
            "provider.invoked",
            extra={
                "model": response.model,
                "input_tokens": (response.usage.input_tokens if response.usage else None),
                "output_tokens": (response.usage.output_tokens if response.usage else None),
                "total_tokens": (response.usage.total_tokens if response.usage else None),
            },
        )
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
        if response.usage is not None:
            logger.warning(
                "provider.usage_untrusted",
                extra={
                    "target_model": self._provider.target.model,
                    "response_model": response.model,
                },
            )
        logger.warning("provider.charge_unknown", extra={"model": response.model})
        return BudgetCharge.unknown()

    async def _settle_unknown(
        self, handed_off: ProviderInvocationRecord, error_code: str
    ) -> ProviderInvocationRecord:
        logger.warning("reconcile.unknown_settled", extra={"error_code": error_code})
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
        current = self._uow.read_provider_invocation(handed_off.invocation_id)
        assert current is not None
        return current

    async def reconcile_incomplete(
        self, *, provider_reconciliation: ProviderReconciliationPort | None = None
    ) -> int:
        """Observe uncertain handoffs without replaying their physical request."""

        settled = 0
        for record in self._uow.list_incomplete_provider_invocations():
            if record.state is ProviderInvocationState.CLAIMED:
                continue
            if record.state is ProviderInvocationState.HANDED_OFF:
                await self._settle_unknown(record, "recovered_after_handoff")
                current = self._uow.read_provider_invocation(record.invocation_id)
                assert current is not None
                record = current
                settled += 1
            if provider_reconciliation is None:
                continue
            observation = await provider_reconciliation.observe(record)
            state = ProviderReconciliationState(observation.state)
            if state is ProviderReconciliationState.STILL_UNKNOWN:
                continue
            if state is ProviderReconciliationState.COMPLETED:
                response = observation.response
                if not isinstance(response, ProviderResponse):
                    raise ProviderInvocationConflictError(
                        "Completed reconciliation requires ProviderResponse."
                    )
                if response.request_id != record.request_id:
                    raise ProviderInvocationConflictError(
                        "Reconciled Provider response belongs to another request."
                    )
                charge = self._response_charge_for_record(response, record)
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
                self._uow.record_provider_reconciliation(
                    record,
                    outcome=ResolutionOutcome.COMPLETED,
                    response_json=provider_response_json(response),
                    usage_json=usage_json,
                    budget_charge=charge,
                    evidence_ref=observation.evidence_ref,
                    now=self._clock(),
                )
                logger.info(
                    "provider.invoked",
                    extra={
                        "reconcile": True,
                        "model": response.model,
                        "input_tokens": (
                            response.usage.input_tokens if response.usage else None
                        ),
                        "output_tokens": (
                            response.usage.output_tokens if response.usage else None
                        ),
                        "total_tokens": (
                            response.usage.total_tokens if response.usage else None
                        ),
                    },
                )
            else:
                self._uow.record_provider_reconciliation(
                    record,
                    outcome=ResolutionOutcome.CONFIRMED_NOT_STARTED,
                    response_json=None,
                    usage_json=None,
                    budget_charge=record.budget_charge,
                    evidence_ref=observation.evidence_ref,
                    now=self._clock(),
                )
            settled += 1
        return settled

    def _response_charge_for_record(
        self, response: ProviderResponse, record: ProviderInvocationRecord
    ) -> BudgetCharge:
        if response.usage is None:
            return (
                record.budget_charge
                if not record.budget_charge.is_unknown
                else BudgetCharge.unknown()
            )
        snapshot = record.estimator_snapshot
        if not isinstance(snapshot, Mapping):
            return BudgetCharge.unknown()
        estimator = FrozenPriceEstimator(
            snapshot_id=str(snapshot["snapshot_id"]),
            pricing_key=str(snapshot["pricing_key"]),
            input_micros_per_million_tokens=int(
                snapshot["input_micros_per_million_tokens"]
            ),
            output_micros_per_million_tokens=int(
                snapshot["output_micros_per_million_tokens"]
            ),
            fixed_request_overhead_tokens=int(
                snapshot["fixed_request_overhead_tokens"]
            ),
            per_message_overhead_tokens=int(snapshot["per_message_overhead_tokens"]),
            per_tool_overhead_tokens=int(snapshot["per_tool_overhead_tokens"]),
        )
        estimator.bind(record.target)
        return estimator.charge_usage(response.usage)


__all__ = (
    "ProviderInvocationConflictError",
    "ProviderInvocationCoordinator",
    "ProviderInvocationFailedError",
    "ProviderInvocationUnitOfWork",
    "ProviderInvocationUnknownError",
)
