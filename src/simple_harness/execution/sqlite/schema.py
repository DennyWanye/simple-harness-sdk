# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned schema v1 descriptor for SDK execution persistence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files


SCHEMA_VERSION = 1
MIGRATION_NAME = "0001_initial"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def initial_migration() -> Migration:
    sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0001_initial.sql")
        .read_text(encoding="utf-8")
    )
    return Migration(
        version=SCHEMA_VERSION,
        name=MIGRATION_NAME,
        sql=sql,
        checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
    )


__all__ = ("SCHEMA_VERSION", "Migration", "initial_migration")

