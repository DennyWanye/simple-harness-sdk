# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict JSON values and defensive immutable snapshots."""

from __future__ import annotations

import json as _json
import math
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .errors import ContractValidationError, ErrorCode


JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    JsonPrimitive
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
)


def validate_json_value(value: object, *, path: str = "$") -> None:
    """Reject anything without a stable, standards-compliant JSON encoding."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(
                ErrorCode.INVALID_JSON,
                f"{path} must contain a finite JSON number",
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(
                    ErrorCode.INVALID_JSON,
                    f"{path} contains a non-string object key",
                )
            validate_json_value(item, path=f"{path}.{key}")
        return
    raise ContractValidationError(
        ErrorCode.INVALID_JSON,
        f"{path} contains unsupported JSON value type {type(value).__name__}",
    )


def canonical_json(value: JsonValue) -> str:
    validate_json_value(value)
    return _json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def freeze_json(value: JsonValue) -> FrozenJsonValue:
    """Validate and recursively snapshot mutable JSON containers."""

    validate_json_value(value)
    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: FrozenJsonValue) -> JsonValue:
    """Return a detached mutable JSON value suitable for serialization."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


__all__ = (
    "JsonPrimitive",
    "JsonValue",
    "FrozenJsonValue",
    "validate_json_value",
    "canonical_json",
    "freeze_json",
    "thaw_json",
)

