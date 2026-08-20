# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned schema v1 descriptor for SDK execution persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def migrations() -> tuple[Migration, ...]:
    values = []
    for version, name in ((1, "0001_initial"), (2, "0002_context_authority")):
        sql = (
            files("simple_harness.execution.sqlite.migrations")
            .joinpath(f"{name}.sql")
            .read_text(encoding="utf-8")
        )
        values.append(
            Migration(version, name, sql, hashlib.sha256(sql.encode()).hexdigest())
        )
    return tuple(values)


def initial_migration() -> Migration:
    return migrations()[0]


__all__ = ("SCHEMA_VERSION", "Migration", "initial_migration", "migrations")
