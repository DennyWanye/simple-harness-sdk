# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit backup-first execution schema v9 -> v10 upgrade (Slice 5 · BA37).

Mirrors ``short_context_migration.migrate_execution_to_v9``: refuse anything that
is not an accepted v9 library, take a same-directory backup first (never
overwritten), apply the frozen v10 BaseAgent DDL, stamp the v10 descriptor and a
receipt bound to the backup bytes.  A v10 library replays its retained receipt.
No live runtime handle may be open on the file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from simple_harness.contracts import canonical_json

from ..context_use_migration import _root
from ..database import ExecutionSchemaIncompatible
from ..schema import accepted_descriptor_rows, fresh_descriptor, legacy_v9_descriptor
from .schema import DDL


@dataclass(frozen=True, slots=True)
class ExecutionBaseAgentUpgradeReceiptV1:
    backup_path: str
    backup_sha256: str
    prior_descriptor_hash: str
    new_descriptor_hash: str
    source_root_hash: str
    from_version: int = 9
    to_version: int = 10
    schema_version: int = 1

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @property
    def receipt_hash(self) -> str:
        payload = {"protocol": "simple-harness/execution-base-agent-upgrade/v1", **self.to_json()}
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()  # type: ignore[arg-type]


def _bytes_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly(path: Path, timeout: float) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=timeout
    )
    connection.row_factory = sqlite3.Row
    return connection


def _descriptor_rows(connection: sqlite3.Connection) -> tuple[tuple[int, str, str], ...]:
    return tuple(
        (int(r[0]), str(r[1]), str(r[2]))
        for r in connection.execute(
            "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
        )
    )


def _validate(connection: sqlite3.Connection) -> int:
    """Return the library's current version (9 or 10); refuse anything else.

    Guarantee (review M2): the descriptor rows are an accepted v9/v10 sequence, the
    file passes SQLite's integrity and foreign-key checks, and the table set is not a
    half-applied v10.  Row-level catalog/audit content is *not* re-validated here; the
    backup is bound to the exact source image by ``source_root_hash`` instead.
    """

    try:
        rows = _descriptor_rows(connection)
    except sqlite3.DatabaseError as error:
        raise ExecutionSchemaIncompatible(
            "execution_base_agent_upgrade_source_unavailable"
        ) from error
    if rows not in accepted_descriptor_rows():
        raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_unknown_descriptor")
    if [tuple(r) for r in connection.execute("PRAGMA integrity_check")] != [("ok",)] or list(
        connection.execute("PRAGMA foreign_key_check")
    ):
        raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_integrity_failed")
    version = rows[-1][0]
    tables = {
        str(r[0]) for r in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    if version == 9 and "base_agent_bindings_v1" in tables:
        raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_partial_library")
    if version == 10 and "base_agent_upgrade_receipt_v1" not in tables:
        raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_unknown_descriptor")
    return version


def _receipt(
    connection: sqlite3.Connection, backup: Path
) -> ExecutionBaseAgentUpgradeReceiptV1 | None:
    rows = list(
        connection.execute("SELECT receipt_json,receipt_hash FROM base_agent_upgrade_receipt_v1")
    )
    descriptors = tuple(r[0] for r in _descriptor_rows(connection))
    if not rows and descriptors == (10,):
        return None  # fresh v10: nothing was upgraded
    if len(rows) != 1 or descriptors == (10,):
        raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_receipt_missing_or_invalid")
    try:
        import json

        raw = json.loads(rows[0][0])
        receipt = ExecutionBaseAgentUpgradeReceiptV1(**raw)
        if (
            receipt.from_version != 9
            or receipt.to_version != 10
            or receipt.schema_version != 1
            or receipt.prior_descriptor_hash != legacy_v9_descriptor().checksum
            or receipt.new_descriptor_hash != fresh_descriptor().checksum
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
            # The retained backup must still be the v9 image this library was
            # upgraded from, not merely the bytes recorded at upgrade time (review M1).
            if _validate(saved) != 9 or _root(saved) != receipt.source_root_hash:
                raise ValueError("backup is not the upgraded source image")
        finally:
            saved.close()
        return receipt
    except (TypeError, ValueError, KeyError) as error:
        raise ExecutionSchemaIncompatible(
            "execution_base_agent_upgrade_backup_or_receipt_differs"
        ) from error


def _statements(connection: sqlite3.Connection, ddl: str) -> None:
    for statement in ddl.split(";"):
        text = statement.strip()
        if text:
            connection.execute(text)


def migrate_execution_to_v10(
    path: str | Path, *, backup_path: str | Path, timeout: float = 5.0
) -> ExecutionBaseAgentUpgradeReceiptV1 | None:
    """Synchronous explicit v9 -> v10 upgrade with a retained same-directory backup.

    Fresh v10 returns None; an upgraded v10 replays its exact receipt and backup.
    Libraries below v9 must first go through ``migrate_execution_to_v9``.
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
        if _validate(reader) == 10:
            return _receipt(reader, backup)
    finally:
        reader.close()
    writer = sqlite3.connect(source, isolation_level=None, timeout=timeout)
    writer.row_factory = sqlite3.Row
    try:
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("PRAGMA synchronous=FULL")
        writer.execute("BEGIN IMMEDIATE")
        if _validate(writer) == 10:
            return _receipt(writer, backup)
        source_root_hash = _root(writer)
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            source_reader, target = _readonly(source, timeout), sqlite3.connect(backup)
            try:
                source_reader.backup(target)
            finally:
                target.close()
                source_reader.close()
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
            # A pre-existing file at the backup path is only accepted when it is
            # byte-for-byte the same v9 content as the source (review M1).
            if _validate(saved) != 9 or _root(saved) != source_root_hash:
                raise ExecutionSchemaIncompatible(
                    "execution_base_agent_upgrade_retained_backup_differs"
                )
        finally:
            saved.close()
        receipt = ExecutionBaseAgentUpgradeReceiptV1(
            str(backup),
            _bytes_hash(backup),
            legacy_v9_descriptor().checksum,
            fresh_descriptor().checksum,
            source_root_hash,
        )
        _statements(writer, DDL)
        descriptor = fresh_descriptor()
        writer.execute(
            "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES (?,?,?)",
            (descriptor.version, descriptor.name, descriptor.checksum),
        )
        writer.execute(
            "INSERT INTO base_agent_upgrade_receipt_v1 VALUES (1,?,?)",
            (canonical_json(receipt.to_json()), receipt.receipt_hash),  # type: ignore[arg-type]
        )
        if _validate(writer) != 10:
            raise ExecutionSchemaIncompatible("execution_base_agent_upgrade_postcondition_failed")
        writer.commit()
        return receipt
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()


__all__ = ("ExecutionBaseAgentUpgradeReceiptV1", "migrate_execution_to_v10")
