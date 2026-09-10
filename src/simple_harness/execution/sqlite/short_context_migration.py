"""Explicit backup-first execution7/8 -> 9; no receipt or source restamping."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from simple_harness.contracts import canonical_json
from simple_harness.execution.context_use import ProviderContextUseAttemptV1, use_hash

from . import audit_schema
from .context_use import DDL as V8_DDL
from .context_use_migration import (
    _DESCRIPTOR_SQL,
    _bytes_hash,
    _catalog,
    _readonly,
    _root,
    _statements,
    audit_objects_present,
)
from .context_use_migration import (
    _validate as _validate_legacy,
)
from .database import ExecutionSchemaIncompatible
from .schema import (
    accepted_descriptor_rows,
    legacy_v7_descriptor,
    legacy_v8_descriptor,
    legacy_v9_descriptor,
)
from .short_context_schema import DDL


@dataclass(frozen=True, slots=True)
class ExecutionShortContextUpgradeReceiptV1:
    backup_path: str
    backup_sha256: str
    source_root_hash: str
    prior_descriptor_hash: str
    new_descriptor_hash: str
    from_version: int
    to_version: int = 9
    schema_version: int = 1

    def to_json(self):
        return asdict(self)

    @property
    def receipt_hash(self):
        return use_hash("simple-harness/execution-short-context-upgrade/v1", self.to_json())


def _carriers(connection, version):
    """Validate every retained known carrier, including terminal checkpoint history.

    This is a finite catalog scan, not a hard RSS/time bound. Never interpret user
    payloads as fragments or search arbitrary text for discriminants.
    """
    try:
        if version >= 8:
            for row in connection.execute(
                "SELECT intent_json,intent_hash FROM provider_context_use_attempts"
            ):
                attempt = ProviderContextUseAttemptV1.from_json(json.loads(row[0]))
                if attempt.intent_hash != row[1]:
                    raise ValueError("intent hash")
        for row in connection.execute(
            "SELECT checkpoint_json,checkpoint_hash FROM workflow_checkpoints "
            "WHERE namespace='react.termination.v1'"
        ):
            payload = json.loads(row[0])
            import hashlib

            if (
                not isinstance(payload, dict)
                or hashlib.sha256(canonical_json(payload).encode()).hexdigest() != row[1]
            ):
                raise ValueError("checkpoint hash")
            # Absence/None was a legal pre-reservation or generic checkpoint.
            if payload.get("context_use_attempt") is not None:
                ProviderContextUseAttemptV1.from_json(payload["context_use_attempt"])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ExecutionSchemaIncompatible("execution_short_upgrade_carrier_incompatible") from error


def _validate(connection, *, allow_missing_audit=False):
    try:
        rows = tuple(
            tuple(r)
            for r in connection.execute(
                "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
            )
        )
        if rows not in accepted_descriptor_rows():
            version = _validate_legacy(connection, allow_missing_audit=allow_missing_audit)
            _carriers(connection, version)
            return version
        if rows[-1][0] > 9:
            # Already beyond 9 (the later explicit upgrader validated its own catalog):
            # only the accepted descriptor sequence and file integrity are re-checked,
            # so a Host may keep calling the v9 upgrader unconditionally at startup.
            if [tuple(r) for r in connection.execute("PRAGMA integrity_check")] != [
                ("ok",)
            ] or list(connection.execute("PRAGMA foreign_key_check")):
                raise ExecutionSchemaIncompatible("execution_short_upgrade_integrity_failed")
            return int(rows[-1][0])
        audit_version = audit_schema.validate_audit_schema(connection, allow_v1=True)
        expected = sqlite3.connect(":memory:")
        try:
            expected.execute(_DESCRIPTOR_SQL)
            expected.executescript(legacy_v9_descriptor().sql)
            for statement in audit_schema.V1_DDL if audit_version == 1 else audit_schema.DDL:
                expected.execute(statement)
            if _catalog(connection) != _catalog(expected):
                raise ExecutionSchemaIncompatible("execution_short_upgrade_unknown_catalog")
        finally:
            expected.close()
        if [tuple(r) for r in connection.execute("PRAGMA integrity_check")] != [("ok",)] or list(
            connection.execute("PRAGMA foreign_key_check")
        ):
            raise ExecutionSchemaIncompatible("execution_short_upgrade_integrity_failed")
        _carriers(connection, 9)
        return 9
    except (sqlite3.DatabaseError, audit_schema.AuditSchemaIncompatible) as error:
        raise ExecutionSchemaIncompatible("execution_short_upgrade_source_unavailable") from error


def _receipt(connection, backup):
    rows = list(
        connection.execute("SELECT receipt_json,receipt_hash FROM short_context_upgrade_receipt")
    )
    descriptors = tuple(
        r[0]
        for r in connection.execute("SELECT version FROM sdk_schema_migrations ORDER BY version")
    )
    if 9 not in descriptors:
        if descriptors and descriptors[0] > 9 and not rows:
            return None  # fresh beyond 9: nothing was ever upgraded to 9
        raise ExecutionSchemaIncompatible("execution_short_upgrade_receipt_missing_or_invalid")
    index = descriptors.index(9)
    if index == 0:
        # Fresh 9 (possibly upgraded further since): nothing was ever upgraded to 9.
        if rows:
            raise ExecutionSchemaIncompatible("execution_short_upgrade_receipt_missing_or_invalid")
        return None
    if len(rows) != 1:
        raise ExecutionSchemaIncompatible("execution_short_upgrade_receipt_missing_or_invalid")
    try:
        raw = json.loads(rows[0][0])
        receipt = ExecutionShortContextUpgradeReceiptV1(**raw)
        prior = legacy_v7_descriptor() if descriptors[index - 1] == 7 else legacy_v8_descriptor()
        if (
            type(receipt.from_version) is not int
            or receipt.from_version != prior.version
            or type(receipt.to_version) is not int
            or receipt.to_version != 9
            or type(receipt.schema_version) is not int
            or receipt.schema_version != 1
            or receipt.prior_descriptor_hash != prior.checksum
            or receipt.new_descriptor_hash != legacy_v9_descriptor().checksum
            or receipt.to_json() != raw
            or receipt.receipt_hash != rows[0][1]
            or receipt.backup_path != str(backup)
            or not backup.is_file()
            or _bytes_hash(backup) != receipt.backup_sha256
        ):
            raise ValueError("receipt binding")
        saved = _readonly(backup, 5.0)
        try:
            saved.execute("BEGIN")
            if (
                _validate(saved, allow_missing_audit=True) != receipt.from_version
                or _root(saved) != receipt.source_root_hash
            ):
                raise ValueError("backup root")
        finally:
            saved.close()
        return receipt
    except (TypeError, ValueError, KeyError) as error:
        raise ExecutionSchemaIncompatible(
            "execution_short_upgrade_backup_or_receipt_differs"
        ) from error


def migrate_execution_to_v9(
    path: str | Path, *, backup_path: str | Path, timeout: float = 5.0
) -> ExecutionShortContextUpgradeReceiptV1 | None:
    """Synchronous explicit exact7/8 -> 9 upgrade, with no live runtime handles.

    Fresh9 returns None; upgraded9 replays its exact retained receipt and backup.
    Missing paths belong to builders. Unknown catalogs or incompatible old short
    carriers refuse read-only before backup/DDL, and are rechecked under the write
    lock. timeout is SQLite lock waiting, NOT a total migration deadline.
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
        if _validate(reader, allow_missing_audit=True) >= 9:
            return _receipt(reader, backup)
    finally:
        reader.close()
    writer = sqlite3.connect(source, isolation_level=None, timeout=timeout)
    writer.row_factory = sqlite3.Row
    try:
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("PRAGMA synchronous=FULL")
        writer.execute("BEGIN IMMEDIATE")
        version = _validate(writer, allow_missing_audit=True)
        if version >= 9:
            return _receipt(writer, backup)
        root = _root(writer)
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            reader, target = _readonly(source, timeout), sqlite3.connect(backup)
            try:
                reader.backup(target)
            finally:
                target.close()
                reader.close()
            with backup.open("rb") as stream:
                os.fsync(stream.fileno())
            directory = os.open(backup.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        saved = _readonly(backup, timeout)
        try:
            saved.execute("BEGIN")
            if _validate(saved, allow_missing_audit=True) != version or _root(saved) != root:
                raise ExecutionSchemaIncompatible("execution_short_upgrade_retained_backup_differs")
        finally:
            saved.close()
        if not audit_objects_present(writer):
            # A library written before the explicit audit schema existed (it can no
            # longer be opened directly): bootstrap the audit objects inside this same
            # transaction, after the backup is retained, exactly as Database.open does.
            audit_schema.ensure_audit_schema_on(writer)
        prior = legacy_v7_descriptor() if version == 7 else legacy_v8_descriptor()
        receipt = ExecutionShortContextUpgradeReceiptV1(
            str(backup),
            _bytes_hash(backup),
            root,
            prior.checksum,
            legacy_v9_descriptor().checksum,
            version,
        )
        _statements(writer, (V8_DDL if version == 7 else "") + DDL)
        descriptor = legacy_v9_descriptor()
        writer.execute(
            "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES (?,?,?)",
            (9, descriptor.name, descriptor.checksum),
        )
        writer.execute(
            "INSERT INTO short_context_upgrade_receipt VALUES (1,?,?)",
            (canonical_json(receipt.to_json()), receipt.receipt_hash),
        )
        if _validate(writer) != 9:
            raise ExecutionSchemaIncompatible("execution_short_upgrade_postcondition_failed")
        writer.commit()
        return receipt
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
