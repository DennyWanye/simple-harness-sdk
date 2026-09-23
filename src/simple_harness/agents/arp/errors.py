# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""ARP error type bound to the frozen ``error-catalogue.json`` (ARP-EXEC-1.1.1 §10).

Every failure raised by the native runtime plane carries a stable catalogue code in
``args[0]``; callers must never expose arbitrary exception text to a model or a Host
view.  ``entry(code)`` returns the catalogue row (stage / retry / HTTP / WS mapping)
and refuses codes that are not in the catalogue, so a typo cannot mint a new error.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Mapping


class ArpError(ValueError):
    """Stable catalogue code in ``args[0]``; optional field path and detail."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        field_path: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code
        self.field_path = field_path
        self.detail = MappingProxyType(dict(detail or {}))

    def __str__(self) -> str:
        if self.field_path:
            return f"{self.code} at {self.field_path}: {self.message}"
        return f"{self.code}: {self.message}"

    def to_error_json(self, *, stage: str | None = None) -> dict[str, Any]:
        """Render the public ``Error`` DTO (schema_version 1) for this failure."""

        row = entry(self.code)
        return {
            "schema_version": 1,
            "code": self.code,
            "stage": stage or str(row["stage"]),
            "retry": str(row["retry"]),
            "field_path": self.field_path,
            "message": str(row["public_message"]),
            "source_refs": [],
        }


@lru_cache(maxsize=1)
def catalogue() -> Mapping[str, Mapping[str, Any]]:
    raw = resources.files(__package__).joinpath("contracts/error-catalogue.json").read_bytes()
    table = json.loads(raw.decode("utf-8"))
    if not isinstance(table, dict) or not table:
        raise RuntimeError("ARP error catalogue is not a non-empty object")
    return MappingProxyType({key: MappingProxyType(dict(value)) for key, value in table.items()})


def entry(code: str) -> Mapping[str, Any]:
    """The catalogue row for ``code``; unknown codes are a programming error."""

    try:
        return catalogue()[code]
    except KeyError as error:
        raise RuntimeError(f"ARP error code {code!r} is not in the frozen catalogue") from error


def is_known(code: str) -> bool:
    return code in catalogue()


__all__ = ("ArpError", "catalogue", "entry", "is_known")
