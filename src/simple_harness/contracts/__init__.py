# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Public immutable contracts for Simple Harness SDK."""

from .errors import ContractValidationError, ErrorCode, HarnessError
from .events import EventEnvelope, EventKind
from .identity import (
    CallId,
    CorrelationIds,
    EffectId,
    EventId,
    ExecutionSessionId,
    RequestId,
    RunId,
)
from .json import (
    FrozenJsonValue,
    JsonPrimitive,
    JsonValue,
    canonical_json,
    fingerprint_json,
    freeze_json,
    thaw_json,
    validate_json_value,
)
from .messages import Message, MessageRole


__all__ = (
    "JsonPrimitive",
    "JsonValue",
    "FrozenJsonValue",
    "validate_json_value",
    "canonical_json",
    "freeze_json",
    "thaw_json",
    "fingerprint_json",
    "ExecutionSessionId",
    "RunId",
    "RequestId",
    "CallId",
    "EffectId",
    "EventId",
    "CorrelationIds",
    "MessageRole",
    "Message",
    "EventKind",
    "EventEnvelope",
    "ErrorCode",
    "HarnessError",
    "ContractValidationError",
)

