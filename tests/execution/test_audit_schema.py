"""Explicit observational schema validation without an execution version change."""

import hashlib
import sqlite3

import pytest

from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.audit_schema import CHECKSUM, OBJECTS, AuditSchemaIncompatible
from simple_harness.execution.sqlite.schema import fresh_descriptor


def old_empty_v7(path):
    descriptor = fresh_descriptor()
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE sdk_schema_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT);"
        + descriptor.sql,
    )
    connection.execute(
        "INSERT INTO sdk_schema_migrations VALUES(?,?,?)",
        (descriptor.version, descriptor.name, descriptor.checksum),
    )
    connection.commit()
    connection.close()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_old_empty_execution7_gets_explicit_audit_schema_and_reopens(tmp_path):
    path = tmp_path / "old.db"
    old_empty_v7(path)
    for _ in range(2):
        with Database.open(path) as database:
            assert database.schema_version == 7
            assert tuple(
                database.connection.execute(
                    "SELECT version,checksum FROM sdk_audit_schema"
                ).fetchone()
            ) == (1, CHECKSUM)
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM sdk_command_audit_events"
                ).fetchone()[0]
                == 0
            )
            with database.audit_reader() as reader:
                assert reader.schema_version == 7


@pytest.mark.parametrize("corruption", ["future", "noop_trigger", "extra_column", "partial", "descriptor_column"])
def test_invalid_audit_schema_rejected_without_file_change(tmp_path, corruption):
    path = tmp_path / "invalid.db"
    with Database.open(path) as database:
        connection = database.connection
        if corruption == "future":
            connection.execute("UPDATE sdk_audit_schema SET version=2")
        elif corruption == "noop_trigger":
            connection.execute("DROP TRIGGER sdk_command_audit_no_update")
            connection.execute(
                "CREATE TRIGGER sdk_command_audit_no_update BEFORE UPDATE "
                "ON sdk_command_audit_events BEGIN SELECT 1; END"
            )
        elif corruption == "descriptor_column":
            connection.execute("ALTER TABLE sdk_audit_schema RENAME COLUMN checksum TO wrong")
        elif corruption == "extra_column":
            connection.execute("ALTER TABLE sdk_command_audit_events ADD COLUMN wrong TEXT")
        else:
            connection.execute("DROP TRIGGER sdk_command_audit_no_delete")
        before_read = digest(path)
        with pytest.raises(AuditSchemaIncompatible):
            with database.audit_reader():
                pass
        assert digest(path) == before_read
    before_open = digest(path)
    with pytest.raises(AuditSchemaIncompatible):
        Database.open(path)
    assert digest(path) == before_open


def test_readonly_missing_audit_schema_does_not_initialize(tmp_path):
    path = tmp_path / "readonly.db"
    old_empty_v7(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    database = Database(path, connection)
    before = digest(path)
    with pytest.raises(AuditSchemaIncompatible):
        with database.audit_reader():
            pass
    assert digest(path) == before
    assert not OBJECTS.intersection(
        row[0] for row in connection.execute("SELECT name FROM sqlite_master")
    )
    database.close()


def test_audit_schema_initialization_is_atomic(tmp_path, monkeypatch):
    from simple_harness.execution.sqlite import audit_schema

    path = tmp_path / "atomic.db"
    old_empty_v7(path)
    before = digest(path)
    monkeypatch.setattr(audit_schema, "DDL", (*audit_schema.DDL, "INVALID DDL"))
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)
    assert digest(path) == before
    connection = sqlite3.connect(path)
    try:
        assert not OBJECTS.intersection(
            row[0] for row in connection.execute("SELECT name FROM sqlite_master")
        )
    finally:
        connection.close()
