# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded JSON decoding using the SDK's existing canonical serialization."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from simple_harness.contracts import canonical_json

MAX_BYTES = 256 * 1024
# Historical records whose body is a whole provider request (a reviewer turn's input
# manifest / exposure: messages + provider_request for a 256K-token context).  Only the
# evidence-snapshot and exact-manifest reads use it; every other record keeps MAX_BYTES.
# The encoding is unchanged, so every existing hash stays byte-identical (2026-09-25
# desktop run: a 211 KB manifest row encoded to 275 KB and failed every review).
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_INTEGER = 9007199254740991


class AssuranceError(ValueError):
    def __init__(self, code: str, locator: str = "") -> None:
        self.code = code
        self.locator = locator
        super().__init__(f"{code}: {locator}" if locator else code)


def text(value: object, *, limit: int = 2048) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise AssuranceError("TEXT_INVALID")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AssuranceError("JSON_UNICODE") from exc
    return value


def integer(value: object, *, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AssuranceError("INTEGER_INVALID")
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AssuranceError("HASH_INVALID")
    return value


def fields(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys():
        raise AssuranceError("OBJECT_FIELDS_MISSING")
    if value.keys() - required - (optional or set()):
        raise AssuranceError("OBJECT_FIELDS_UNKNOWN")
    return value


def array(value: object, *, minimum: int = 0, maximum: int = 256) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AssuranceError("ARRAY_INVALID")
    return value


def one_of(value: object, choices: set[str] | frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise AssuranceError("ENUM_INVALID")
    return value


def unique_texts(value: object, *, maximum: int) -> tuple[str, ...]:
    result = tuple(text(v) for v in array(value, maximum=maximum))
    if len(set(result)) != len(result):
        raise AssuranceError("DUPLICATE_SET_MEMBER")
    return tuple(sorted(result))


def _validate_tree(value: object, depth: int = 0) -> None:
    # Depth counts JSON containers only, with the root container at zero.
    if isinstance(value, (dict, list)) and depth > 64:
        raise AssuranceError("JSON_DEPTH_LIMIT")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise AssuranceError("JSON_OBJECT_KEY")
            key.encode("utf-8", errors="strict")
            _validate_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_tree(child, depth + 1)
    elif isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif value is None or type(value) is bool:
        return
    elif type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise AssuranceError("JSON_INTEGER_OVERFLOW")
    elif type(value) is float:
        if not math.isfinite(value):
            raise AssuranceError("JSON_NUMBER_INVALID")
    else:
        raise AssuranceError("JSON_TYPE_INVALID")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise AssuranceError("JSON_DUPLICATE_KEY", key)
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise AssuranceError("JSON_NUMBER_INVALID", value)


def decode(raw: str | bytes, *, limit: int = MAX_BYTES) -> Any:
    try:
        encoded = raw.encode("utf-8", errors="strict") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes) or len(encoded) > limit:
            raise AssuranceError("JSON_BYTES_LIMIT")
        value = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
        _validate_tree(value)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise AssuranceError("JSON_INVALID") from exc


def canonical(value: Any, *, limit: int = MAX_BYTES) -> str:
    try:
        _validate_tree(value)
        result = canonical_json(value)
        if len(result.encode("utf-8")) > limit:
            raise AssuranceError("JSON_BYTES_LIMIT")
        return result
    except (UnicodeError, RecursionError) as exc:
        raise AssuranceError("JSON_INVALID") from exc


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
