# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable bounded diagnostics snapshot base."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .contracts import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DiagnosticsSnapshotV1:
    sdk_version: str
    lifecycle: str
    health: str
    counters: Mapping[str, int]
    queue_depth: int
    queue_capacity: int
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        bounded = dict(self.counters)
        if len(bounded) > 32 or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in bounded.items()
        ):
            raise ValueError("snapshot counters must be bounded non-negative integers")
        object.__setattr__(self, "counters", MappingProxyType(bounded))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sdk_version": self.sdk_version,
            "lifecycle": self.lifecycle,
            "health": self.health,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_capacity,
            "counters": dict(self.counters),
        }


__all__ = ("DiagnosticsSnapshotV1",)
