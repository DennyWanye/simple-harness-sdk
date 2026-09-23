# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit execution-library upgrade v10 → v11 (ARP-EXEC-1.1.1 additive descriptor).

Contract (sql/MIGRATION-CONTRACT.md): the original execution runner owns the
migration transaction; PRAGMAs are set and read back before ``BEGIN``; statements
are split on real boundaries (never ``split(';')`` / ``executescript``); failure
rolls the whole transaction back and never re-issues fresh DDL over a used library;
applying twice is a no-op that verifies the recorded checksum.  Session partitions
(``session_partition_v2.sql``) are separate per-session files created by the
partition owner, not by this upgrader.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from simple_harness.execution.sqlite.database import ExecutionSchemaIncompatible
from simple_harness.execution.sqlite.schema import (
    ARP_SCHEMA_VERSION,
    accepted_descriptor_rows,
    arp_descriptor,
    fresh_descriptor,
)

from .errors import ArpError
from .rules import sql_statements


@dataclass(frozen=True, slots=True)
class ExecutionArpUpgradeReceipt:
    from_version: int
    to_version: int
    prior_descriptor_hash: str
    new_descriptor_hash: str
    applied_now: bool


def assert_runtime_sqlite_pragmas(connection: sqlite3.Connection) -> None:
    """Set and read back the PRAGMAs every ARP writer connection needs (§11)."""

    if connection.in_transaction:
        raise ArpError("SQL_PRAGMA_UNSUPPORTED", "PRAGMAs must be set outside a transaction")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ArpError("SQL_PRAGMA_UNSUPPORTED", "foreign_keys")
    if connection.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
        raise ArpError("SQL_PRAGMA_UNSUPPORTED", "recursive_triggers")


def apply_ddl(connection: sqlite3.Connection, ddl: str) -> int:
    """Execute a DDL script statement by statement inside the caller's transaction."""

    if not connection.in_transaction:
        raise RuntimeError("apply_ddl requires the caller's open transaction")
    count = 0
    for statement in sql_statements(ddl):
        connection.execute(statement)
        count += 1
    return count


def partition_ddl() -> str:
    return (
        resources.files(__package__).joinpath("sql/session_partition_v2.sql").read_text("utf-8")
    )


def _descriptor_rows(connection: sqlite3.Connection) -> tuple[tuple[int, str, str], ...]:
    return tuple(
        (int(r[0]), str(r[1]), str(r[2]))
        for r in connection.execute(
            "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
        )
    )


def migrate_execution_to_v11(path: str | Path, *, timeout: float = 5.0) -> ExecutionArpUpgradeReceipt:
    """Apply the ARP additive descriptor to an accepted v10 library (idempotent)."""

    source = Path(path).expanduser()
    if source.is_symlink():
        raise ExecutionSchemaIncompatible("execution_arp_upgrade_symlink_forbidden")
    if not source.is_file():
        raise ExecutionSchemaIncompatible("execution_arp_upgrade_source_missing")
    descriptor = arp_descriptor()
    writer = sqlite3.connect(source, isolation_level=None, timeout=timeout)
    writer.row_factory = sqlite3.Row
    try:
        assert_runtime_sqlite_pragmas(writer)
        writer.execute("PRAGMA synchronous = FULL")
        writer.execute("BEGIN IMMEDIATE")
        try:
            rows = _descriptor_rows(writer)
        except sqlite3.DatabaseError as error:
            raise ExecutionSchemaIncompatible("execution_arp_upgrade_source_unavailable") from error
        if rows not in accepted_descriptor_rows():
            raise ExecutionSchemaIncompatible("execution_arp_upgrade_unknown_descriptor")
        if rows[-1][0] == ARP_SCHEMA_VERSION:
            if rows[-1] != (descriptor.version, descriptor.name, descriptor.checksum):
                raise ExecutionSchemaIncompatible("execution_arp_upgrade_checksum_differs")
            writer.execute("COMMIT")
            return ExecutionArpUpgradeReceipt(
                10, ARP_SCHEMA_VERSION, rows[-2][2], descriptor.checksum, False
            )
        if rows[-1][0] != 10 or rows[-1][2] != fresh_descriptor().checksum:
            raise ExecutionSchemaIncompatible("execution_arp_upgrade_requires_v10")
        tables = {
            str(r[0]) for r in writer.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        if any(name.startswith("arp_") for name in tables):
            raise ExecutionSchemaIncompatible("execution_arp_upgrade_partial_library")
        apply_ddl(writer, descriptor.sql)
        writer.execute(
            "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES (?,?,?)",
            (descriptor.version, descriptor.name, descriptor.checksum),
        )
        if [tuple(r) for r in writer.execute("PRAGMA integrity_check")] != [("ok",)] or list(
            writer.execute("PRAGMA foreign_key_check")
        ):
            raise ExecutionSchemaIncompatible("execution_arp_upgrade_integrity_failed")
        writer.execute("COMMIT")
        return ExecutionArpUpgradeReceipt(
            10, ARP_SCHEMA_VERSION, rows[-1][2], descriptor.checksum, True
        )
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()


__all__ = (
    "ExecutionArpUpgradeReceipt",
    "apply_ddl",
    "assert_runtime_sqlite_pragmas",
    "migrate_execution_to_v11",
    "partition_ddl",
)
