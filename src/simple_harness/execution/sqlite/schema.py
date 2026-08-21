# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned fresh schema v4 descriptor for SDK execution persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 4


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def fresh_descriptor() -> Migration:
    resources = files("simple_harness.execution.sqlite.migrations")
    sql = resources.joinpath("0004_fresh.sql").read_text(encoding="utf-8")
    return Migration(
        SCHEMA_VERSION,
        "0004_fresh",
        sql,
        hashlib.sha256(sql.encode()).hexdigest(),
    )


def migrations() -> tuple[Migration, ...]:
    """Return only the accepted fresh descriptor, never legacy migrations."""

    return (fresh_descriptor(),)


def initial_migration() -> Migration:
    return fresh_descriptor()


__all__ = (
    "SCHEMA_VERSION",
    "Migration",
    "fresh_descriptor",
    "initial_migration",
    "migrations",
)
