# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One shared, fail-closed physical provider meter for a single experiment run.

Construct once per RunContext and pass the same object to every role/profile.
Counters are cumulative lower bounds when unknown_usage_calls is nonzero; such
runs must fail, and no further physical calls are admitted. No estimate is
reported as actual usage. The runner owns durable receipt persistence.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from simple_harness.execution.provider_admission import (
    ProviderAdmissionDenied,
    ProviderAdmissionFailure,
)
from simple_harness.providers import (
    Provider,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)
from simple_harness.providers.errors import ProviderError, ProviderRequestRejectedError

from .experiment import ExecutionCounters, ExperimentBudget, RunContext


class ExperimentBudgetExhausted(ProviderRequestRejectedError):
    """The next physical request cannot fit the remaining declared allowance."""

    error_code = "experiment_budget_exhausted"

    def __init__(self, message: str, *, usage: ProviderUsage | None = None) -> None:
        super().__init__(public_message=message, retryable=False)
        # A local refusal has known zero physical usage, not an ambiguous lost
        # network response. An over-reserve physical response retains its charge.
        actual = usage or ProviderUsage(0, 0, 0)
        self.detail = {
            "usage": {
                "input_tokens": actual.input_tokens,
                "output_tokens": actual.output_tokens,
                "total_tokens": actual.total_tokens,
                "cache_tokens": actual.cache_tokens,
                "reasoning_tokens": actual.reasoning_tokens,
            }
        }


class RunWindowDenied(ExperimentBudgetExhausted):
    """A local window refusal before physical handoff has known zero usage."""

    error_code = "run_window_denied"


class _ExperimentBudgetAdmissionDenied(ExperimentBudgetExhausted, ProviderAdmissionDenied):
    """Canonical terminal admission while preserving evaluation exception catches."""

    error_code = "provider_admission_denied"

    def __init__(self, original: ExperimentBudgetExhausted) -> None:
        ProviderAdmissionDenied.__init__(
            self,
            public_message=original.public_message,
            admission_detail=ProviderAdmissionFailure(reason_code="budget_exhausted"),
        )


class UnknownProviderUsage(RuntimeError):
    """A physical call has no trustworthy actual token usage; admission stops."""


class ProviderIdentityMismatch(RuntimeError):
    """The physical target or echoed response model changed during the run."""


class MeteredProvider:
    """Provider port with shared call/token/deadline admission and physical slots.

    ``estimate_input_tokens`` must be bound to the actual provider's request
    serializer/model. Where hidden server-side tokens cannot be bounded (e.g.
    prior reasoning), the caller must supply an additional conservative reserve
    via ``extra_input_reserve``. A missing output cap or estimate is rejected.
    A provider failure/cancellation is counted as a call with unknown usage:
    whether it reached the wire or incurred tokens cannot be proven here.
    """

    def __init__(
        self,
        provider: Provider,
        context: RunContext,
        *,
        estimate_input_tokens: Callable[[ProviderRequest], int],
        extra_input_reserve: Callable[[ProviderRequest], int] | None = None,
        before_handoff: Callable[[float], None] | None = None,
        max_inflight_tokens: int | None = None,
    ) -> None:
        target = provider.target
        if not isinstance(target, ProviderTarget) or (target.provider_id, target.model) != (
            context.manifest.provider,
            context.manifest.model,
        ):
            raise ProviderIdentityMismatch("physical provider/model differs from manifest")
        if not callable(estimate_input_tokens):
            raise TypeError("a bound request input estimator is required")
        if extra_input_reserve is not None and not callable(extra_input_reserve):
            raise TypeError("extra input reserve must be callable")
        if before_handoff is not None and not callable(before_handoff):
            raise TypeError("before_handoff must be callable")
        if max_inflight_tokens is not None and (
            type(max_inflight_tokens) is not int or max_inflight_tokens < 1
        ):
            raise ValueError("max_inflight_tokens must be a positive integer")
        if (
            getattr(
                getattr(estimate_input_tokens, "__self__", None),
                "requires_prior_output_reserve",
                False,
            )
            and extra_input_reserve is None
        ):
            raise ValueError("counter requires an explicit prior-output reserve")
        self.provider = provider
        self.target = target
        self._budget: ExperimentBudget = context.manifest.budget
        self._report = context.report_usage
        self._estimate = estimate_input_tokens
        self._extra = extra_input_reserve
        self._before_handoff = before_handoff
        self._slots = asyncio.Semaphore(context.manifest.physical_slots)
        self._max_inflight_tokens = max_inflight_tokens
        self._capacity_condition = asyncio.Condition()
        self._capacity_waiters: deque[object] = deque()
        self._capacity_reserved = self._capacity_active = 0
        self._physical_slots = context.manifest.physical_slots
        self._deadline = time.monotonic() + self._budget.seconds
        self._input = self._output = self._calls = self._active = self._peak = 0
        self._reserved_input = self._reserved_output = 0
        self.observations: list[dict[str, Any]] = []
        self.admission_denials: list[dict[str, Any]] = []
        self.unknown_usage_calls = 0
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        # Keep provider-specific capabilities available to SDK consumers.
        return getattr(self.provider, name)

    @property
    def counters(self) -> ExecutionCounters:
        return ExecutionCounters(
            self.target.provider_id,
            self.target.model,
            self._input,
            self._output,
            self._input + self._output,
            self._calls,
            self._peak,
        )

    def _publish(self) -> None:
        self._report(self.counters)

    @staticmethod
    def _tokens(value: int, name: str) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    def _admit(self, input_reserve: int, output_reserve: int) -> None:
        b = self._budget
        if self.unknown_usage_calls:
            raise UnknownProviderUsage("unknown physical usage; run cannot continue")
        if self._closed:
            raise ExperimentBudgetExhausted("meter closed after physical violation")
        if time.monotonic() >= self._deadline:
            raise ExperimentBudgetExhausted("experiment deadline exhausted")
        if (
            self._calls >= b.calls
            or self._input + self._reserved_input + input_reserve > b.input_tokens
            or self._output + self._reserved_output + output_reserve > b.output_tokens
            or self._input
            + self._output
            + self._reserved_input
            + self._reserved_output
            + input_reserve
            + output_reserve
            > b.total_tokens
        ):
            raise ExperimentBudgetExhausted("experiment call/token allowance exhausted")

    @asynccontextmanager
    async def _physical_admission(self, weight: int, cancel: Any) -> AsyncIterator[None]:
        capacity = self._max_inflight_tokens
        if capacity is None:
            async with self._slots:
                yield
            return
        if weight > capacity:
            raise ExperimentBudgetExhausted("request exceeds in-flight token capacity")

        async def notify_cancellation() -> None:
            await cancel.wait()
            async with self._capacity_condition:
                self._capacity_condition.notify_all()

        watcher = asyncio.create_task(notify_cancellation())
        try:
            ticket = object()
            async with self._capacity_condition:
                self._capacity_waiters.append(ticket)
                try:
                    while True:
                        if cancel.is_cancelled:
                            raise asyncio.CancelledError()
                        if self.unknown_usage_calls:
                            raise UnknownProviderUsage(
                                "unknown physical usage; run cannot continue"
                            )
                        if self._closed:
                            raise ExperimentBudgetExhausted("meter closed after physical violation")
                        if (
                            self._capacity_waiters[0] is ticket
                            and self._capacity_active < self._physical_slots
                            and self._capacity_reserved + weight <= capacity
                        ):
                            self._capacity_waiters.popleft()
                            self._capacity_active += 1
                            self._capacity_reserved += weight
                            self._capacity_condition.notify_all()
                            break
                        await self._capacity_condition.wait()
                finally:
                    if ticket in self._capacity_waiters:
                        self._capacity_waiters.remove(ticket)
                        self._capacity_condition.notify_all()
            try:
                yield
            finally:
                async with self._capacity_condition:
                    self._capacity_active -= 1
                    self._capacity_reserved -= weight
                    self._capacity_condition.notify_all()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _settle(
        self, usage: ProviderUsage, row: dict[str, Any], input_reserve: int, output_reserve: int
    ) -> None:
        row.update(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_tokens=usage.cache_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )
        self._input += self._tokens(usage.input_tokens, "actual input")
        self._output += self._tokens(usage.output_tokens, "actual output")
        if usage.total_tokens != usage.input_tokens + usage.output_tokens:
            self.unknown_usage_calls += 1
            self._closed = True
            raise UnknownProviderUsage("provider total has unallocated tokens")
        if (
            self._input > self._budget.input_tokens
            or self._output > self._budget.output_tokens
            or self._input + self._output > self._budget.total_tokens
            or usage.input_tokens > input_reserve
            or usage.output_tokens > output_reserve
        ):
            self._closed = True
            raise ExperimentBudgetExhausted(
                "physical usage exceeded reservation/budget", usage=usage
            )

    async def invoke(self, request: ProviderRequest, *, cancel: Any) -> ProviderResponse:
        handoff = [False]
        started = time.monotonic()
        try:
            return await self._invoke(request, cancel=cancel, handoff=handoff)
        except (Exception, asyncio.CancelledError) as error:
            if not handoff[0]:
                # Admission is auditable even when an Agent catches a local refusal.
                # Keep it separate from physical observations/call ordinals.
                self.admission_denials.append({
                    "request_id": request.request_id.value,
                    "status": "denied_before_handoff",
                    "reason": type(error).__name__,
                    "physical_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "seconds": time.monotonic() - started,
                })
                self._publish()
                if isinstance(error, ExperimentBudgetExhausted) and not isinstance(
                    error, RunWindowDenied
                ):
                    raise _ExperimentBudgetAdmissionDenied(error) from error
            raise

    async def _invoke(
        self, request: ProviderRequest, *, cancel: Any, handoff: list[bool]
    ) -> ProviderResponse:
        if self.provider.target != self.target:
            self._closed = True
            raise ProviderIdentityMismatch("physical provider target changed")
        input_reserve = self._tokens(self._estimate(request), "input estimate")
        if self._extra is not None:
            input_reserve += self._tokens(self._extra(request), "extra input reserve")
        if request.max_output_tokens is None:
            raise ValueError("request must declare an output cap")
        output_reserve = self._tokens(request.max_output_tokens, "output cap")
        if not output_reserve:
            raise ValueError("request must declare a positive output cap")
        queued_at = time.monotonic()
        # All state changes below are synchronous on the runner's single event loop.
        async with asyncio.timeout_at(self._deadline):
            async with self._physical_admission(input_reserve + output_reserve, cancel):
                if self.provider.target != self.target:
                    self._closed = True
                    raise ProviderIdentityMismatch("physical provider target changed while queued")
                self._admit(input_reserve, output_reserve)
                if cancel.is_cancelled:
                    raise asyncio.CancelledError()
                if self._before_handoff is not None:
                    self._before_handoff(time.monotonic() - queued_at)
                handoff[0] = True
                self._calls += 1
                self._active += 1
                self._peak = max(self._peak, self._active)
                self._reserved_input += input_reserve
                self._reserved_output += output_reserve
                started = time.monotonic()
                row = {
                    "ordinal": self._calls,
                    "request_id": request.request_id.value,
                    "input_reserve": input_reserve,
                    "output_cap": output_reserve,
                    "queue_seconds": started - queued_at,
                    "status": "started",
                }
                self.observations.append(row)
                try:
                    self._publish()  # Persist admission before the physical side effect.
                    try:
                        response = await self.provider.invoke(request, cancel=cancel)
                    except asyncio.CancelledError:
                        row["status"] = "cancelled_unknown_usage"
                        self.unknown_usage_calls += 1
                        self._closed = True
                        raise
                    except Exception as error:
                        row["status"] = type(error).__name__
                        detail = (
                            getattr(error, "detail", None)
                            if isinstance(error, ProviderError)
                            else None
                        )
                        raw = detail.get("usage") if isinstance(detail, Mapping) else None
                        try:
                            known = ProviderUsage(**raw) if isinstance(raw, Mapping) else None
                        except (ValueError, TypeError):
                            known = None
                        if known is not None:
                            self._settle(known, row, input_reserve, output_reserve)
                            # Retain SDK length recovery and failed-call accounting.
                            raise
                        self.unknown_usage_calls += 1
                        self._closed = True
                        raise UnknownProviderUsage("physical call failed; usage unknown") from error
                    usage = response.usage
                    if usage is None:
                        self.unknown_usage_calls += 1
                        self._closed = True
                        raise UnknownProviderUsage("physical response omitted usage")
                    self._settle(usage, row, input_reserve, output_reserve)
                    if self.provider.target != self.target or response.model != self.target.model:
                        self._closed = True
                        raise ProviderIdentityMismatch("physical response target/model changed")
                    row["status"] = "completed"
                    return response
                finally:
                    row["seconds"] = time.monotonic() - started
                    if row["status"] == "started":
                        row["status"] = "failed"
                    self._active -= 1
                    self._reserved_input -= input_reserve
                    self._reserved_output -= output_reserve
                    self._publish()  # Includes failed/cancelled calls, even during unwind.


__all__ = (
    "ExperimentBudgetExhausted",
    "MeteredProvider",
    "ProviderIdentityMismatch",
    "RunWindowDenied",
    "UnknownProviderUsage",
)
