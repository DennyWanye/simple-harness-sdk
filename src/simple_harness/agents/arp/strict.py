# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict JSON bytes handling shared by every ARP codec (FIELD-CONTRACTS §1).

Rules: duplicate keys, non-finite numbers and depth above 24 are rejected; ``bool`` is
never an ``int``; canonical bytes are UTF-8 of ``sort_keys`` + compact separators
with ``allow_nan=False``.  ``digest`` is the SHA-256 hex of those canonical bytes.
This module deliberately re-implements the small rule set instead of importing a
random one of the several ad-hoc ``object_pairs_hook`` parsers in the tree, so that
the whole plane shares one documented behaviour.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from .errors import ArpError

MAX_JSON_DEPTH = 24
DEFAULT_MAX_BYTES = 256 * 1024


def plain(value: Any) -> Any:
    """Deep copy of a JSON-like value with every Mapping / tuple turned into dict / list."""

    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def canonical(value: object) -> bytes:
    """Canonical UTF-8 bytes; raises ``ArpError('NONFINITE_JSON')`` on NaN/Infinity."""

    try:
        return json.dumps(
            plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except ValueError as error:  # allow_nan=False raises ValueError
        raise ArpError("NONFINITE_JSON") from error
    except TypeError as error:
        raise ArpError("INVALID_JSON", str(error)) from error


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _no_duplicates(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in items:
        if key in out:
            raise ArpError("DUPLICATE_JSON_KEY", field_path=key)
        out[key] = value
    return out


def _bad_constant(name: str) -> Any:
    raise ArpError("NONFINITE_JSON", name)


def check_depth(value: object, *, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Reject containers nested deeper than ``max_depth`` and non-finite floats."""

    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise ArpError("JSON_TOO_DEEP")
        if type(item) is float and not math.isfinite(item):
            raise ArpError("NONFINITE_JSON")
        if type(item) is dict:
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)


def parse_strict(raw: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Any:
    """Parse ``raw`` under the strict rules; size is checked before decoding."""

    if not isinstance(raw, (bytes, bytearray)):
        raise ArpError("INVALID_JSON", "raw JSON must be bytes")
    if len(raw) > max_bytes:
        raise ArpError("DOCUMENT_TOO_LARGE")
    try:
        value = json.loads(
            bytes(raw).decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_bad_constant
        )
    except ArpError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ArpError("INVALID_JSON", str(error)) from error
    check_depth(value)
    return value


def nonnegative_int(value: object, code: str = "INVALID_BUDGET") -> int:
    if type(value) is not int or value < 0:
        raise ArpError(code)
    return value


__all__ = (
    "DEFAULT_MAX_BYTES",
    "MAX_JSON_DEPTH",
    "canonical",
    "check_depth",
    "digest",
    "digest_bytes",
    "nonnegative_int",
    "parse_strict",
)
