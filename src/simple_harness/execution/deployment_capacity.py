# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Deployment capacity shared by SDK dispatch and direct provider callers.

This is physical-resource admission, independent of frozen Task token authority.
Only participants sharing the same local ledger/pool are coordinated.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from simple_harness.providers import ProviderRequest
from simple_harness.providers.errors import ProviderError

from .provider_admission import ProviderAdmissionDenied, ProviderAdmissionFailure
from .provider_invocations import provider_request_fingerprint
from .shared_capacity import CapacityLedger, CapacityLedgerError


@dataclass
class CapacityHandle:
    ledger: CapacityLedger
    ticket: Any
    handed_off: bool = False
    known_terminal: bool = False

    def handoff(self) -> None:
        if not self.handed_off:
            self.ledger.mark_handed_off(self.ticket)
            self.handed_off = True


def _known_usage(raw: Any) -> bool:
    if not isinstance(raw, Mapping):
        return False
    values = [raw.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")]
    return (
        all(type(x) is int and x >= 0 for x in values)
        and cast(int, values[0]) + cast(int, values[1]) == values[2]
    )


class DeploymentCapacity:
    def __init__(
        self,
        ledger: CapacityLedger,
        *,
        estimate: Callable[[ProviderRequest], int],
        wait_seconds: float = 900.0,
        call_seconds: float = 600.0,
    ) -> None:
        if not callable(estimate) or not 0 < wait_seconds <= 3600:
            raise ValueError("capacity requires a bound estimator and bounded wait")
        if type(call_seconds) not in (int, float) or not 0 < call_seconds <= 3600:
            raise ValueError("capacity requires a bounded physical call deadline")
        self.call_seconds = call_seconds
        self._response_waits: dict[str, tuple[str, float]] = {}
        self.ledger = ledger
        self.estimate = estimate
        self.wait_seconds = wait_seconds
        self.owner = uuid4().hex
        self._active: ContextVar[Any] = ContextVar("deployment_capacity", default=None)
        self._waiting: set[str] = set()

    @staticmethod
    def _namespace(uow: Any) -> str:
        path = getattr(getattr(uow, "database", None), "path", None)
        namespace = (
            str(Path(path).resolve())
            if path is not None
            else getattr(uow, "capacity_namespace", None)
        )
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("capacity requires a stable execution database namespace")
        return sha256(namespace.encode()).hexdigest()

    def record_key(self, uow: Any, record: Any) -> str:
        return f"sdk|{self._namespace(uow)}|{record.invocation_id}|{record.handoff_attempt + 1}"

    def recover(self, uow: Any) -> None:
        namespace = self._namespace(uow)
        for row in self.ledger.snapshot().rows:
            if row.state not in {"HANDED_OFF", "UNKNOWN"}:
                continue
            parts = row.key.split("|")
            if len(parts) != 4 or parts[:2] != ["sdk", namespace]:
                continue
            try:
                ordinal = int(parts[3])
            except ValueError:
                continue
            record = uow.read_provider_invocation(parts[2])
            if record is None:
                continue
            known = False
            if record.handoff_attempt == ordinal:
                usage = record.usage_json
                known = (
                    str(record.state) in {"succeeded", "failed"}
                    and isinstance(usage, Mapping)
                    and _known_usage(usage.get("usage"))
                )
                if (
                    str(record.state) == "failed"
                    and record.error_code == "provider_admission_denied"
                ):
                    known = True
                if not known:
                    resolution = uow.read_reconciliation_resolution(
                        kind="provider",
                        ledger_identity=record.invocation_id,
                        handoff_attempt=ordinal,
                    )
                    known = (
                        resolution is not None
                        and str(resolution.outcome) == "confirmed_not_started"
                    )
            elif (
                row.state == "UNKNOWN"
                and str(record.state) == "claimed"
                and record.handoff_attempt == ordinal - 1
            ):
                # Capacity marker committed, SDK handoff did not. SDK dispatch
                # cannot enter the transport before committing that handoff.
                known = True
            if known:
                proof = f"sdk:{namespace}:{record.invocation_id}:{ordinal}:{record.version}"
                self.ledger.reconcile(row.key, evidence_ref=proof)

    def response_waiting(self, request_prefix: str) -> bool:
        """A live physical await with a fixed deadline, never ledger-only liveness."""
        now = time.monotonic()
        return any(
            key.startswith(request_prefix) and now < deadline
            for key, deadline in self._response_waits.values()
        )

    def waiting(self, request_prefix: str) -> bool:
        return any(key.startswith(request_prefix) for key in self._waiting)

    @asynccontextmanager
    async def guard(self, request: ProviderRequest, *, cancel: Any, key: str | None = None):
        fingerprint = provider_request_fingerprint(request)
        active = self._active.get()
        task = asyncio.current_task()
        if active is not None and active[:2] == (task, fingerprint):
            yield active[2]
            return
        try:
            weight = self.estimate(request)
            if type(weight) is not int or weight <= 0:
                raise ValueError("invalid capacity estimate")
            identity = key or f"direct|{uuid4().hex}"
            ticket = self.ledger.enqueue(
                identity, weight=weight, owner=f"{self.owner}:{uuid4().hex}", pid=os.getpid()
            )
        except (ValueError, TypeError, CapacityLedgerError) as error:
            raise ProviderAdmissionDenied(
                admission_detail=ProviderAdmissionFailure(reason_code="capacity_unavailable")
            ) from error
        acquired = False
        handle = CapacityHandle(self.ledger, ticket)
        token = None
        self._waiting.add(request.request_id.value)
        try:
            deadline = time.monotonic() + self.wait_seconds
            while True:
                if cancel.is_cancelled:
                    raise asyncio.CancelledError()
                if time.monotonic() >= deadline:
                    raise ProviderAdmissionDenied(
                        admission_detail=ProviderAdmissionFailure(
                            reason_code="capacity_wait_timeout"
                        )
                    )
                if self.ledger.try_acquire(ticket):
                    acquired = True
                    break
                await asyncio.sleep(0.02)
            self._waiting.discard(request.request_id.value)
            token = self._active.set((task, fingerprint, handle))
            yield handle
        finally:
            self._waiting.discard(request.request_id.value)
            if token is not None:
                self._active.reset(token)
            if acquired:
                self.ledger.finish(ticket, known_terminal=handle.known_terminal)
            else:
                self.ledger.cancel_waiter(ticket)


class CapacityProvider:
    """Retain provider identity and hold capacity through the physical outcome."""

    def __init__(self, provider: Any, capacity: DeploymentCapacity) -> None:
        existing = getattr(provider, "deployment_capacity", None)
        if existing is not None and existing is not capacity:
            raise ValueError("provider already has a different deployment capacity binding")
        self.provider = provider
        self.deployment_capacity = capacity

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def invoke(self, request: ProviderRequest, *, cancel: Any):
        async with self.deployment_capacity.guard(request, cancel=cancel) as handle:
            if cancel.is_cancelled:
                raise asyncio.CancelledError()
            handle.handoff()
            waits = self.deployment_capacity._response_waits
            waits[handle.ticket.epoch] = (
                request.request_id.value, time.monotonic() + self.deployment_capacity.call_seconds
            )
            try:
                async with asyncio.timeout(self.deployment_capacity.call_seconds):
                    response = await self.provider.invoke(request, cancel=cancel)
            except ProviderError as error:
                detail = getattr(error, "detail", None)
                if isinstance(detail, Mapping) and _known_usage(detail.get("usage")):
                    handle.known_terminal = True
                raise
            finally:
                waits.pop(handle.ticket.epoch, None)
            usage = response.usage
            if usage is not None and _known_usage(
                {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                }
            ):
                handle.known_terminal = True
            return response
