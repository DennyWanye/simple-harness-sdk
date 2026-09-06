"""Explicit additive execution7 -> execution8 upgrade with a WAL-aware backup.

No auto-migration by builders. No old receipt, checkpoint, event, or authority row
is restamped. Call with no live Runtime; the write lock fences other SQLite writers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from simple_harness.contracts import canonical_json
from simple_harness.execution.context_use import use_hash

from . import audit_schema
from .context_use import DDL
from .database import ExecutionSchemaIncompatible
from .schema import fresh_descriptor, legacy_v7_descriptor

_DESCRIPTOR_SQL = (
    "CREATE TABLE sdk_schema_migrations ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
    "checksum TEXT NOT NULL CHECK(length(checksum) = 64), "
    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP) STRICT"
)


@dataclass(frozen=True, slots=True)
class ExecutionContextUseUpgradeReceiptV1:
    backup_path: str
    backup_sha256: str
    source_root_hash: str
    prior_descriptor_hash: str
    new_descriptor_hash: str
    from_version: int = 7
    to_version: int = 8
    schema_version: int = 1

    def to_json(self):
        from dataclasses import asdict

        return asdict(self)

    @property
    def receipt_hash(self):
        return use_hash("simple-harness/execution-context-use-upgrade/v1", self.to_json())


def _statements(connection, sql):
    # executescript would implicitly commit the caller's migration transaction.
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        raise ValueError("execution_upgrade_incomplete_ddl")


def _catalog(connection):
    return tuple(
        (kind, name, table, " ".join(sql.split()) if sql else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    )


def _expected_catalog(version, audit_version):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_DESCRIPTOR_SQL)
        connection.executescript(
            (legacy_v7_descriptor() if version == 7 else fresh_descriptor()).sql
        )
        for statement in audit_schema.V1_DDL if audit_version == 1 else audit_schema.DDL:
            connection.execute(statement)
        return _catalog(connection)
    finally:
        connection.close()


def _validate(connection):
    try:
        applied = [
            tuple(r)
            for r in connection.execute(
                "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
            )
        ]
        old, new = legacy_v7_descriptor(), fresh_descriptor()
        if applied == [(7, old.name, old.checksum)]:
            version = 7
        elif applied in (
            [(8, new.name, new.checksum)],
            [(7, old.name, old.checksum), (8, new.name, new.checksum)],
        ):
            version = 8
        else:
            raise ExecutionSchemaIncompatible("execution_upgrade_unknown_descriptor")
        audit_version = audit_schema.validate_audit_schema(connection, allow_v1=True)
        if _catalog(connection) != _expected_catalog(version, audit_version):
            raise ExecutionSchemaIncompatible("execution_upgrade_unknown_catalog")
        if [tuple(r) for r in connection.execute("PRAGMA integrity_check")] != [("ok",)] or list(
            connection.execute("PRAGMA foreign_key_check")
        ):
            raise ExecutionSchemaIncompatible("execution_upgrade_integrity_failed")
        return version
    except (sqlite3.DatabaseError, audit_schema.AuditSchemaIncompatible) as error:
        raise ExecutionSchemaIncompatible("execution_upgrade_source_unavailable") from error


def _root(connection):
    """Complete finite-catalog snapshot commitment, including WAL-only rows."""
    tables = [
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    table_hashes = []
    for name in tables:
        rows = []
        for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"'):
            values = [
                {"blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row
            ]
            rows.append(canonical_json(values))
        table_hashes.append(
            [name, use_hash("simple-harness/execution-upgrade-table/v1", sorted(rows))]
        )
    return use_hash("simple-harness/execution-upgrade-root/v1", table_hashes)


def _bytes_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _readonly(path, timeout):
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=timeout
    )
    connection.row_factory = sqlite3.Row
    return connection


def _existing_receipt(connection, backup):
    rows = list(
        connection.execute("SELECT receipt_json,receipt_hash FROM context_use_upgrade_receipt")
    )
    if not rows:
        descriptor_count = connection.execute(
            "SELECT count(*) FROM sdk_schema_migrations"
        ).fetchone()[0]
        if descriptor_count != 1:
            raise ExecutionSchemaIncompatible("execution_upgrade_receipt_missing")
        return None  # Valid fresh8; no backup required or fabricated.
    if len(rows) != 1:
        raise ExecutionSchemaIncompatible("execution_upgrade_receipt_invalid")
    receipt = ExecutionContextUseUpgradeReceiptV1(**json.loads(rows[0][0]))
    if (
        receipt.receipt_hash != rows[0][1]
        or receipt.backup_path != str(backup)
        or not backup.is_file()
        or _bytes_hash(backup) != receipt.backup_sha256
    ):
        raise ExecutionSchemaIncompatible("execution_upgrade_backup_or_receipt_differs")
    return receipt


def migrate_execution_v7_to_v8(
    path: str | Path, *, backup_path: str | Path, timeout: float = 5.0
) -> ExecutionContextUseUpgradeReceiptV1 | None:
    """Synchronous explicit upgrade. Fresh8 returns None; upgraded8 replays its receipt.

    Existing v7 requires a same-directory backup. Retained identical pre-commit
    backups can resume; different/incomplete backups fail closed, never overwrite.
    Missing path belongs to the builder. Unknown/future/corrupt sources are refused
    on the read-only connection before a write connection or backup is created.
    """
    source_input, backup_input = Path(path).expanduser(), Path(backup_path).expanduser()
    if source_input.is_symlink() or backup_input.is_symlink():
        raise ValueError("execution_upgrade_symlink_forbidden")
    source, backup = source_input.resolve(strict=True), backup_input.resolve()
    if source == backup or source.parent != backup.parent:
        raise ValueError("execution_upgrade_same_directory_distinct_backup_required")
    reader = _readonly(source, timeout)
    try:
        reader.execute("BEGIN")
        version = _validate(reader)
        if version == 8:
            return _existing_receipt(reader, backup)
    finally:
        reader.close()
    writer = sqlite3.connect(source, isolation_level=None, timeout=timeout)
    writer.row_factory = sqlite3.Row
    try:
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("PRAGMA synchronous=FULL")
        writer.execute("BEGIN IMMEDIATE")
        if _validate(writer) == 8:
            return _existing_receipt(writer, backup)
        source_root = _root(writer)
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            reader = _readonly(source, timeout)
            target = sqlite3.connect(backup)
            try:
                reader.backup(target)
                target.close()
                with backup.open("rb") as stream:
                    os.fsync(stream.fileno())
                directory = os.open(backup.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                reader.close()
                target.close()
        saved = _readonly(backup, timeout)
        try:
            if _validate(saved) != 7 or _root(saved) != source_root:
                raise ExecutionSchemaIncompatible("execution_upgrade_retained_backup_differs")
        finally:
            saved.close()
        receipt = ExecutionContextUseUpgradeReceiptV1(
            str(backup),
            _bytes_hash(backup),
            source_root,
            legacy_v7_descriptor().checksum,
            fresh_descriptor().checksum,
        )
        _statements(writer, DDL)
        descriptor = fresh_descriptor()
        writer.execute(
            "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES (?,?,?)",
            (8, descriptor.name, descriptor.checksum),
        )
        writer.execute(
            "INSERT INTO context_use_upgrade_receipt VALUES (1,?,?)",
            (canonical_json(receipt.to_json()), receipt.receipt_hash),
        )
        if _validate(writer) != 8:
            raise ExecutionSchemaIncompatible("execution_upgrade_postcondition_failed")
        writer.commit()
        return receipt
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
