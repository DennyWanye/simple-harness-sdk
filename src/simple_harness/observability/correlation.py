# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Opaque, immutable correlation identities for observability events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_id(name: str, value: str | None, *, required: bool) -> None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded opaque identifier")


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Diagnostic identity only; it never grants authority."""

    trace_id: str
    root_id: str
    operation_id: str
    parent_id: str | None = None

    def __post_init__(self) -> None:
        _validate_id("trace_id", self.trace_id, required=True)
        _validate_id("root_id", self.root_id, required=True)
        _validate_id("operation_id", self.operation_id, required=True)
        _validate_id("parent_id", self.parent_id, required=False)

    @classmethod
    def new_root(cls) -> CorrelationContext:
        trace_id = uuid4().hex
        operation_id = uuid4().hex
        return cls(trace_id=trace_id, root_id=operation_id, operation_id=operation_id)

    def child(self) -> CorrelationContext:
        return CorrelationContext(
            trace_id=self.trace_id,
            root_id=self.root_id,
            parent_id=self.operation_id,
            operation_id=uuid4().hex,
        )

    def to_dict(self) -> dict[str, str]:
        value = {
            "trace_id": self.trace_id,
            "root_id": self.root_id,
            "operation_id": self.operation_id,
        }
        if self.parent_id is not None:
            value["parent_id"] = self.parent_id
        return value


__all__ = ("CorrelationContext",)
