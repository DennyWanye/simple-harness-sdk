# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable model-spawnable Profile catalog descriptors."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from simple_harness.contracts import canonical_json


def profile_descriptor_fingerprint(
    key: str,
    description: str,
    use_when: str,
    avoid_when: str,
    input_schema_ref: str,
    generation: int,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "key": key,
                "description": description,
                "use_when": use_when,
                "avoid_when": avoid_when,
                "input_schema_ref": input_schema_ref,
                "generation": generation,
            }
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProfileDescriptor:
    key: str
    description: str
    use_when: str
    avoid_when: str
    input_schema_ref: str
    generation: int
    fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "key",
            "description",
            "use_when",
            "avoid_when",
            "input_schema_ref",
            "fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        expected = profile_descriptor_fingerprint(
            self.key,
            self.description,
            self.use_when,
            self.avoid_when,
            self.input_schema_ref,
            self.generation,
        )
        if self.fingerprint != expected:
            raise ValueError("profile fingerprint does not match descriptor")


__all__ = ("ProfileDescriptor", "profile_descriptor_fingerprint")
