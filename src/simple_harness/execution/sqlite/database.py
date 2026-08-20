# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit SQLite lifecycle and single-connection transaction owner."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .schema import migrations


class Database:
    """One explicitly opened SQLite execution database.

    The instance owns one connection. Command UoWs use ``transaction()`` so a
    logical command cannot accidentally commit through another repository.
    """

    __slots__ = ("path", "_connection", "_transaction_active")

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = connection
        self._transaction_active = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        wal: bool = False,
        timeout: float = 5.0,
    ) -> Database:
        resolved = Path(path).expanduser().resolve()
        if not resolved.parent.is_dir():
            raise FileNotFoundError(
                f"database parent directory does not exist: {resolved.parent}"
            )
        connection = sqlite3.connect(
            resolved,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        database = cls(resolved, connection)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA journal_mode = {'WAL' if wal else 'DELETE'}")
            connection.execute("PRAGMA synchronous = FULL")
            database._initialize_or_validate()
        except BaseException:
            connection.close()
            database._connection = None
            raise
        return database

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is closed")
        return self._connection

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT version FROM sdk_schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("SDK schema version is missing")
        return int(row[0])

    @property
    def foreign_keys_enabled(self) -> bool:
        return bool(self.connection.execute("PRAGMA foreign_keys").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        return str(self.connection.execute("PRAGMA journal_mode").fetchone()[0])

    def table_names(self) -> set[str]:
        return {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def column_names(self, table: str) -> set[str]:
        if table not in self.table_names():
            raise KeyError(table)
        return {
            str(row[1])
            for row in self.connection.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
            )
        }

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transaction_active:
            raise RuntimeError("nested transaction is forbidden")
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        self._transaction_active = True
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            self._transaction_active = False

    def integrity_check(self) -> tuple[str, ...]:
        return tuple(
            str(row[0]) for row in self.connection.execute("PRAGMA integrity_check")
        )

    def foreign_key_violations(self) -> tuple[tuple[object, ...], ...]:
        return tuple(
            tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check")
        )

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        if self._transaction_active:
            connection.rollback()
            self._transaction_active = False
        if self.journal_mode == "wal":
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        self._connection = None

    def __enter__(self) -> Database:
        if not self.is_open:
            raise RuntimeError("database is closed")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _initialize_or_validate(self) -> None:
        connection = self.connection
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        if "sdk_schema_migrations" not in tables:
            non_internal = {name for name in tables if not name.startswith("sqlite_")}
            if non_internal:
                raise RuntimeError("database is not an empty Simple Harness SDK database")
            migration = migrations()[0]
            script = (
                "BEGIN IMMEDIATE;\n"
                "CREATE TABLE sdk_schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                "checksum TEXT NOT NULL CHECK(length(checksum) = 64), "
                "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ") STRICT;\n"
                + migration.sql
                + "\nINSERT INTO sdk_schema_migrations(version, name, checksum) VALUES ("
                + f"{migration.version}, '{migration.name}', '{migration.checksum}'"
                + ");\nCOMMIT;"
            )
            try:
                connection.executescript(script)
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        applied = {
            int(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT version,name,checksum FROM sdk_schema_migrations"
            )
        }
        known = migrations()
        for migration in known:
            existing = applied.get(migration.version)
            if existing is not None and existing != (migration.name, migration.checksum):
                raise RuntimeError("corrupt SDK schema migration history")
            if existing is None:
                if any(version > migration.version for version in applied):
                    raise RuntimeError("SDK schema migration history has a gap")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + migration.sql
                    + "\nINSERT INTO sdk_schema_migrations(version,name,checksum) VALUES ("
                    + f"{migration.version},'{migration.name}','{migration.checksum}');\nCOMMIT;"
                )
                connection.executescript(script)
                applied[migration.version] = (migration.name, migration.checksum)
        if set(applied) != {migration.version for migration in known}:
            raise RuntimeError("unsupported SDK schema version")
        if self.integrity_check() != ("ok",) or self.foreign_key_violations():
            raise RuntimeError("SDK execution database failed integrity validation")


__all__ = ("Database",)
