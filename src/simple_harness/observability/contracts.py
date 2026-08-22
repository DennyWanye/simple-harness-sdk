# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable version-one observability wire contract."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .correlation import CorrelationContext
from .redaction import SafeValue, safe_attributes, thaw_attributes

SCHEMA_VERSION = 1
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_LABEL = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    DEGRADED = "degraded"
    TERMINAL = "terminal"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class ObservabilityEventV1:
    event_name: str
    occurred_at: float
    severity: Severity
    component: str
    operation: str
    outcome: Outcome
    correlation: CorrelationContext
    attributes: Mapping[str, SafeValue] = field(default_factory=dict)
    sequence: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("ObservabilityEventV1 schema_version must be 1")
        if not isinstance(self.event_name, str) or _EVENT_NAME.fullmatch(self.event_name) is None:
            raise ValueError("event_name must use a stable dotted identifier")
        if not isinstance(self.occurred_at, (int, float)) or isinstance(self.occurred_at, bool):
            raise ValueError("occurred_at must be numeric")
        if not math.isfinite(float(self.occurred_at)) or self.occurred_at < 0:
            raise ValueError("occurred_at must be finite and non-negative")
        if not isinstance(self.component, str) or _LABEL.fullmatch(self.component) is None:
            raise ValueError("component must use a stable bounded identifier")
        if not isinstance(self.operation, str) or _LABEL.fullmatch(self.operation) is None:
            raise ValueError("operation must use a stable bounded identifier")
        if not isinstance(self.correlation, CorrelationContext):
            raise TypeError("correlation must use CorrelationContext")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer")
        object.__setattr__(self, "occurred_at", float(self.occurred_at))
        object.__setattr__(self, "severity", Severity(self.severity))
        object.__setattr__(self, "outcome", Outcome(self.outcome))
        object.__setattr__(self, "attributes", safe_attributes(self.attributes))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_name": self.event_name,
            "occurred_at": self.occurred_at,
            "sequence": self.sequence,
            "severity": self.severity.value,
            "component": self.component,
            "operation": self.operation,
            "outcome": self.outcome.value,
            "correlation": self.correlation.to_dict(),
            "attributes": thaw_attributes(self.attributes),
        }


def validate_event_dict(value: Mapping[str, object]) -> None:
    """Validate known V1 fields while allowing future top-level fields."""

    required = {
        "schema_version",
        "event_name",
        "occurred_at",
        "sequence",
        "severity",
        "component",
        "operation",
        "outcome",
        "correlation",
        "attributes",
    }
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise ValueError("event is missing required V1 fields")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported observability schema version")
    correlation = value["correlation"]
    attributes = value["attributes"]
    if not isinstance(correlation, Mapping) or not isinstance(attributes, Mapping):
        raise ValueError("correlation and attributes must be mappings")
    ObservabilityEventV1(
        event_name=value["event_name"],  # type: ignore[arg-type]
        occurred_at=value["occurred_at"],  # type: ignore[arg-type]
        sequence=value["sequence"],  # type: ignore[arg-type]
        severity=value["severity"],  # type: ignore[arg-type]
        component=value["component"],  # type: ignore[arg-type]
        operation=value["operation"],  # type: ignore[arg-type]
        outcome=value["outcome"],  # type: ignore[arg-type]
        correlation=CorrelationContext(
            trace_id=correlation.get("trace_id"),  # type: ignore[arg-type]
            root_id=correlation.get("root_id"),  # type: ignore[arg-type]
            operation_id=correlation.get("operation_id"),  # type: ignore[arg-type]
            parent_id=correlation.get("parent_id"),  # type: ignore[arg-type]
        ),
        attributes=attributes,  # type: ignore[arg-type]
    )


__all__ = (
    "SCHEMA_VERSION",
    "ObservabilityEventV1",
    "Outcome",
    "Severity",
    "validate_event_dict",
)
