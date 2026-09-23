# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Frozen deployment policy for the registered Assurance 1.1 profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codec import AssuranceError, canonical, fields, integer

_FIXED = {
    "schema_version": 1,
    "mode": "assurance-exec-v1.1",
    "max_json_bytes": 262144,
    "max_json_depth": 64,
    "max_formula_nodes": 512,
    "max_criteria": 256,
    "max_ground_literals": 10000,
    "max_rules": 20000,
    "statement_time_unit": "UTC_MS",
    "require_independent_verifier": True,
    "unknown_effect_closeout": "BLOCK",
    "cross_mission_evidence": "EXPLICIT_EXPORT_ONLY",
}


@dataclass(frozen=True, slots=True)
class AssurancePolicy:
    format_retries: int = 1

    def __post_init__(self) -> None:
        integer(self.format_retries, maximum=1)

    def to_json(self) -> dict[str, Any]:
        return {**_FIXED, "format_retries": self.format_retries}

    @classmethod
    def from_json(cls, value: object) -> AssurancePolicy:
        row = fields(value, set(_FIXED) | {"format_retries"})
        for key, expected in _FIXED.items():
            if type(row[key]) is not type(expected) or row[key] != expected:
                raise AssuranceError("ASSURANCE_POLICY_UNREGISTERED", key)
        canonical(row)
        return cls(row["format_retries"])
