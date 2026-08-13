# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Typed immutable event envelopes for SDK consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Mapping

from .errors import ContractValidationError, ErrorCode
from .identity import CorrelationIds, EventId
from .json import JsonValue, freeze_json, thaw_json


class EventKind(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_PROGRESS = "run.progress"
    RUN_WAITING = "run.waiting"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    PROVIDER_INVOCATION = "provider.invocation"
    TOOL_EFFECT = "tool.effect"
    CHILD_SIGNAL = "child.signal"
    DELIVERY = "delivery"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: EventId
    kind: EventKind | str
    correlation: CorrelationIds
    sequence: int
    occurred_at: float
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, EventId):
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "event_id must use EventId"
            )
        try:
            kind = EventKind(self.kind)
        except (TypeError, ValueError) as error:
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "event kind is not supported"
            ) from error
        if not isinstance(self.correlation, CorrelationIds):
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "event correlation must use CorrelationIds"
            )
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "event sequence must be a positive integer"
            )
        if (
            not isinstance(self.occurred_at, (int, float))
            or isinstance(self.occurred_at, bool)
            or not math.isfinite(float(self.occurred_at))
            or self.occurred_at < 0
        ):
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT,
                "event occurred_at must be finite and non-negative",
            )
        if self.schema_version != 1:
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "EventEnvelope schema_version must be 1"
            )
        if not isinstance(self.payload, dict):
            raise ContractValidationError(
                ErrorCode.INVALID_EVENT, "event payload must be a JSON object"
            )
        frozen = freeze_json(self.payload)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "occurred_at", float(self.occurred_at))
        object.__setattr__(self, "payload", frozen)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id.value,
            "kind": self.kind.value,
            "correlation": self.correlation.to_dict(),
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "payload": thaw_json(self.payload),  # type: ignore[arg-type]
        }


__all__ = ("EventKind", "EventEnvelope")

