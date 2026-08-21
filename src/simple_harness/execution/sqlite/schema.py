# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned fresh schema v3 descriptor for SDK execution persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def fresh_descriptor() -> Migration:
    resources = files("simple_harness.execution.sqlite.migrations")
    initial = resources.joinpath("0001_initial.sql").read_text(encoding="utf-8")
    initial = initial.replace(
        "CREATE TABLE execution_sessions (\n"
        "    session_id TEXT PRIMARY KEY,\n"
        "    created_at REAL NOT NULL CHECK(created_at >= 0)\n"
        ") STRICT;",
        "CREATE TABLE execution_users (\n"
        "    user_id TEXT PRIMARY KEY,\n"
        "    created_at REAL NOT NULL CHECK(created_at >= 0)\n"
        ") STRICT;\n\n"
        "CREATE TABLE execution_sessions (\n"
        "    session_id TEXT PRIMARY KEY,\n"
        "    user_id TEXT NOT NULL DEFAULT 'harness-system' "
        "REFERENCES execution_users(user_id),\n"
        "    created_at REAL NOT NULL CHECK(created_at >= 0),\n"
        "    UNIQUE(session_id, user_id)\n"
        ") STRICT;",
        1,
    )
    context = resources.joinpath("0002_context_authority.sql").read_text(
        encoding="utf-8"
    )
    conversation = resources.joinpath("0003_fresh.sql").read_text(encoding="utf-8")
    sql = "\n".join((initial, context, conversation))
    return Migration(
        SCHEMA_VERSION,
        "0003_fresh",
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
