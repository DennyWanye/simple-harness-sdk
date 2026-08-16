# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Generic terminal-delivery contracts and retry-only dispatcher."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
)
from simple_harness.execution.uow import RunRecord


class DeliveryState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    FAILED = "failed"
    RELEASED = "released"


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DeliverySpec:
    delivery_id: str
    sink_kind: str
    idempotency_key: str
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        for name in ("delivery_id", "sink_kind", "idempotency_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if not isinstance(self.payload, Mapping):
            raise TypeError("delivery payload must be a mapping")
        frozen = freeze_json(dict(self.payload))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    run_id: str
    sink_kind: str
    idempotency_key: str
    payload: FrozenJsonValue
    state: DeliveryState
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", DeliveryState(self.state))


@dataclass(frozen=True, slots=True)
class TerminalCommitResult:
    run: RunRecord
    deliveries: tuple[DeliveryRecord, ...]


class DeliveryConflictError(RuntimeError):
    code = "delivery_conflict"


class DeliverySink(Protocol):
    async def deliver(
        self, payload: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> None: ...


class DeliveryUnitOfWork(Protocol):
    def claim_delivery(
        self,
        *,
        sink_kinds: Sequence[str],
        now: float,
        claim_ttl_seconds: float,
    ) -> DeliveryRecord | None: ...

    def complete_delivery(
        self, delivery_id: str, *, expected_version: int, now: float
    ) -> DeliveryRecord: ...

    def release_delivery(
        self, delivery_id: str, *, expected_version: int, now: float
    ) -> DeliveryRecord: ...


class DeliveryDispatcher:
    """Deliver one claimed item; sink failure only releases that item."""

    def __init__(
        self,
        uow: DeliveryUnitOfWork,
        sinks: Mapping[str, DeliverySink],
        *,
        clock: Callable[[], float] = time.time,
        claim_ttl_seconds: float = 30.0,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._uow = uow
        self._sinks = dict(sinks)
        if not self._sinks or any(not key.strip() for key in self._sinks):
            raise ValueError("delivery sinks must have non-empty unique keys")
        self._clock = clock
        self._fault = fault
        if not math.isfinite(claim_ttl_seconds) or claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be finite and positive")
        self._claim_ttl_seconds = float(claim_ttl_seconds)

    async def run_once(self) -> bool:
        record = self._uow.claim_delivery(
            sink_kinds=tuple(self._sinks),
            now=self._now(),
            claim_ttl_seconds=self._claim_ttl_seconds,
        )
        if record is None:
            return False
        sink = self._sinks.get(record.sink_kind)
        if sink is None:
            self._uow.release_delivery(
                record.delivery_id,
                expected_version=record.version,
                now=self._now(),
            )
            return True
        payload = _mapping(record.payload)
        try:
            await sink.deliver(payload, idempotency_key=record.idempotency_key)
        except Exception:  # noqa: BLE001 - sink failures are retryable delivery facts
            self._uow.release_delivery(
                record.delivery_id,
                expected_version=record.version,
                now=self._now(),
            )
        else:
            if self._fault is not None:
                self._fault("delivery.sink_succeeded.before_complete")
            self._uow.complete_delivery(
                record.delivery_id,
                expected_version=record.version,
                now=self._now(),
            )
        return True

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value) or value < 0:
            raise ValueError("delivery clock must be finite and non-negative")
        return value


def delivery_payload_json(value: Mapping[str, JsonValue]) -> str:
    return canonical_json(dict(value))


def _mapping(value: FrozenJsonValue) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("stored delivery payload must be an object")
    return cast(Mapping[str, JsonValue], value)


__all__ = (
    "DeliveryConflictError",
    "DeliveryDispatcher",
    "DeliveryRecord",
    "DeliverySink",
    "DeliverySpec",
    "DeliveryState",
    "DeliveryUnitOfWork",
    "TerminalCommitResult",
    "delivery_payload_json",
)
