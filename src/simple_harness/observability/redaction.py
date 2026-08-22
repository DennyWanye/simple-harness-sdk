# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Default-deny safe attribute construction."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TypeAlias

SafeScalar: TypeAlias = str | int | float | bool | None
SafeValue: TypeAlias = SafeScalar | tuple[SafeScalar, ...]

SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "accepted_children",
        "attempt",
        "budget_after",
        "budget_before",
        "candidate_count",
        "close_timeout",
        "content_length",
        "drop_reason",
        "duration_ms",
        "entity_id",
        "entity_kind",
        "error_code",
        "failed_children",
        "fingerprint",
        "from_state",
        "history_complete",
        "lease_epoch",
        "operation_kind",
        "overflow_count",
        "queue_capacity",
        "queue_depth",
        "recall_status",
        "recovery_result",
        "replayed",
        "retry_count",
        "run_id",
        "selected_count",
        "stage",
        "state_version",
        "to_state",
        "token_count",
    }
)

MAX_ATTRIBUTES = 32
MAX_STRING_LENGTH = 256
MAX_SEQUENCE_ITEMS = 16
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SECRET_MARKER = re.compile(
    r"(?:sk-|api[_-]?key|authorization|bearer|cookie|password|token)", re.IGNORECASE
)


class UnsafeAttributeError(ValueError):
    """Raised before an unknown or unsafe value can enter an event."""


def _safe_scalar(key: str, value: object) -> SafeScalar:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            raise UnsafeAttributeError("attribute string exceeds the safe bound")
        if isinstance(value, str) and (
            _SAFE_TEXT.fullmatch(value) is None or _SECRET_MARKER.search(value) is not None
        ):
            raise UnsafeAttributeError(f"{key} must use a stable non-secret identifier")
        if isinstance(value, float) and not math.isfinite(value):
            raise UnsafeAttributeError("attribute number must be finite")
        return value
    raise UnsafeAttributeError("attribute values must use safe scalar types")


def safe_attributes(attributes: Mapping[str, object] | None = None) -> Mapping[str, SafeValue]:
    """Validate and freeze a flat allowlisted attribute mapping.

    Unknown fields and nested containers are rejected instead of recursively
    inspecting values that may contain application text or credentials.
    """

    if attributes is None:
        return MappingProxyType({})
    if not isinstance(attributes, Mapping):
        raise UnsafeAttributeError("attributes must be a mapping")
    if len(attributes) > MAX_ATTRIBUTES:
        raise UnsafeAttributeError("too many attributes")
    output: dict[str, SafeValue] = {}
    for key, raw_value in attributes.items():
        if not isinstance(key, str) or key not in SAFE_ATTRIBUTE_KEYS:
            raise UnsafeAttributeError("attribute key is not allowlisted")
        if isinstance(raw_value, Mapping):
            raise UnsafeAttributeError("nested mappings are not allowed")
        if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes, bytearray)):
            if len(raw_value) > MAX_SEQUENCE_ITEMS:
                raise UnsafeAttributeError("attribute sequence exceeds the safe bound")
            output[key] = tuple(_safe_scalar(key, item) for item in raw_value)
        else:
            output[key] = _safe_scalar(key, raw_value)
    return MappingProxyType(output)


def thaw_attributes(attributes: Mapping[str, SafeValue]) -> dict[str, object]:
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in attributes.items()
    }


__all__ = (
    "MAX_ATTRIBUTES",
    "MAX_SEQUENCE_ITEMS",
    "MAX_STRING_LENGTH",
    "SAFE_ATTRIBUTE_KEYS",
    "SafeScalar",
    "SafeValue",
    "UnsafeAttributeError",
    "safe_attributes",
    "thaw_attributes",
)
