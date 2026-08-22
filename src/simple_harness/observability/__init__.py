# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Import-pure, local-only observability contracts and adapters."""

from .contracts import (
    SCHEMA_VERSION,
    ObservabilityEventV1,
    Outcome,
    Severity,
    validate_event_dict,
)
from .correlation import CorrelationContext
from .redaction import SAFE_ATTRIBUTE_KEYS, UnsafeAttributeError, safe_attributes
from .runtime import EmitterCounters, ObservabilityRuntime, SafeEmitter
from .sinks import (
    CompositeSink,
    JsonlSink,
    LoggingSink,
    NoopSink,
    ObservabilitySink,
    RecordingSink,
    RingBufferSink,
)
from .snapshot import DiagnosticsSnapshotV1

__all__ = (
    "SCHEMA_VERSION",
    "SAFE_ATTRIBUTE_KEYS",
    "CompositeSink",
    "CorrelationContext",
    "DiagnosticsSnapshotV1",
    "EmitterCounters",
    "JsonlSink",
    "LoggingSink",
    "NoopSink",
    "ObservabilityEventV1",
    "ObservabilityRuntime",
    "ObservabilitySink",
    "Outcome",
    "RecordingSink",
    "RingBufferSink",
    "SafeEmitter",
    "Severity",
    "UnsafeAttributeError",
    "safe_attributes",
    "validate_event_dict",
)
