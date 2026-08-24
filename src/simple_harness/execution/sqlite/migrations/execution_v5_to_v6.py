# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit backup-first execution schema v5 to v6 catalog migration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from simple_harness.contracts import JsonValue, canonical_json

from ..schema import fresh_descriptor
from .execution_v3_to_v4 import ExecutionMigrationError


@dataclass(frozen=True, slots=True)
class ExecutionV5ToV6MigrationReceipt:
    source_sha256: str
    backup_sha256: str
    migrated_catalog_count: int


def migrate_execution_v5_to_v6(
    database_path: str | Path, *, backup_path: str | Path
) -> ExecutionV5ToV6MigrationReceipt:
    """Replace one closed exact-v5 database after preserving an exact backup."""

    source_path = Path(database_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()
    if source_path == backup or backup.parent != source_path.parent:
        raise ValueError("backup_path must differ and share the database directory")
    if not source_path.is_file():
        raise ExecutionMigrationError("execution_migration_source_missing")
    if backup.exists():
        raise ExecutionMigrationError("execution_migration_backup_exists")
    temp = source_path.with_name(f".{source_path.name}.v6-{uuid4().hex}.tmp")
    restore = source_path.with_name(f".{source_path.name}.restore-{uuid4().hex}.tmp")
    source: sqlite3.Connection | None = None
    replaced = False
    try:
        source = sqlite3.connect(source_path, timeout=0.0, isolation_level=None)
        source.execute("PRAGMA foreign_keys=ON")
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            source.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            raise ExecutionMigrationError("execution_migration_runtime_not_closed") from error
        _validate_v5(source)
        if tuple(source.execute("PRAGMA integrity_check").fetchone()) != ("ok",):
            raise ExecutionMigrationError("execution_migration_source_integrity")
        source_hash = _file_hash(source_path)
        shutil.copy2(source_path, backup)
        os.chmod(backup, 0o600)
        backup_hash = _file_hash(backup)
        if backup_hash != source_hash:
            raise ExecutionMigrationError("execution_migration_backup_hash")
        shutil.copy2(source_path, temp)
        source.commit()
        source.close()
        source = None

        migrated_count = _upgrade_copy(temp)
        os.replace(temp, source_path)
        replaced = True
        _validate_v6(source_path)
        return ExecutionV5ToV6MigrationReceipt(source_hash, backup_hash, migrated_count)
    except BaseException:
        if source is not None:
            if source.in_transaction:
                source.rollback()
            source.close()
        if replaced:
            shutil.copy2(backup, restore)
            os.replace(restore, source_path)
        raise
    finally:
        for disposable in (temp, restore):
            try:
                disposable.unlink()
            except FileNotFoundError:
                pass


def _validate_v5(connection: sqlite3.Connection) -> None:
    sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0005_fresh.sql")
        .read_text(encoding="utf-8")
    )
    expected = (5, "0005_fresh", hashlib.sha256(sql.encode()).hexdigest())
    rows = connection.execute(
        "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
    ).fetchall()
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ExecutionMigrationError("execution_migration_requires_exact_v5")


def _upgrade_copy(path: Path) -> int:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE tool_catalog_snapshots ADD COLUMN provider_specs_fingerprint TEXT "
            "CHECK(provider_specs_fingerprint IS NULL OR length(provider_specs_fingerprint)=64)"
        )
        connection.execute(
            "ALTER TABLE tool_catalog_snapshots ADD COLUMN catalog_envelope_json TEXT"
        )
        connection.execute(
            "ALTER TABLE tool_catalog_snapshots ADD COLUMN catalog_envelope_digest_v6 TEXT "
            "CHECK(catalog_envelope_digest_v6 IS NULL OR length(catalog_envelope_digest_v6)=64)"
        )
        rows = connection.execute(
            "SELECT generation,content_fingerprint,specs_json FROM tool_catalog_snapshots"
        ).fetchall()
        for row in rows:
            specs = json.loads(str(row["specs_json"]))
            records: list[JsonValue] = []
            for spec in specs:
                provider_name = str(spec["name"])
                identity = hashlib.sha256(canonical_json(spec).encode()).hexdigest()
                records.append(
                    {
                        "kind": "legacy_static_tool",
                        "provider_name": provider_name,
                        "handler_locator": f"legacy-static:{provider_name}",
                        "handler_identity_digest": identity,
                    }
                )
            envelope: JsonValue = {
                "schema_version": 6,
                "compatibility": "legacy_static",
                "records": records,
            }
            encoded = canonical_json(envelope)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            connection.execute(
                "UPDATE tool_catalog_snapshots SET provider_specs_fingerprint=?,"
                "catalog_envelope_json=?,catalog_envelope_digest_v6=? WHERE generation=?",
                (str(row["content_fingerprint"]), encoded, digest, int(row["generation"])),
            )
        connection.execute(
            "CREATE UNIQUE INDEX tool_catalog_envelope_digest_v6_idx "
            "ON tool_catalog_snapshots(catalog_envelope_digest_v6) "
            "WHERE catalog_envelope_digest_v6 IS NOT NULL"
        )
        descriptor = fresh_descriptor()
        connection.execute(
            "UPDATE sdk_schema_migrations SET version=?,name=?,checksum=?,"
            "applied_at=CURRENT_TIMESTAMP",
            (descriptor.version, descriptor.name, descriptor.checksum),
        )
        connection.commit()
        return len(rows)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _validate_v6(path: Path) -> None:
    from ..database import Database

    with Database.open(path) as database:
        if database.integrity_check() != ("ok",) or database.foreign_key_violations():
            raise ExecutionMigrationError("execution_migration_target_integrity")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = ("ExecutionV5ToV6MigrationReceipt", "migrate_execution_v5_to_v6")
