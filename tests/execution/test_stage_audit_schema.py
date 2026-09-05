"""Explicit audit1→2 migration, passive old-writer-compatible stage observations."""

import sqlite3

import pytest
from test_audit_schema import digest, old_empty_v7

from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.audit_schema import (
    V1_CHECKSUM,
    V1_DDL,
    AuditSchemaIncompatible,
)


def legacy_v1(path):
    old_empty_v7(path)
    with sqlite3.connect(path) as connection:
        for statement in V1_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO sdk_audit_schema VALUES(1,?)", (V1_CHECKSUM,))


def test_exact_audit1_upgrades_atomically_and_readonly_never_upgrades(tmp_path):
    path = tmp_path / "audit1.db"
    legacy_v1(path)
    before = digest(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    readonly_host = Database(path, connection)
    with pytest.raises(AuditSchemaIncompatible):
        with readonly_host.audit_reader():
            pass
    assert digest(path) == before
    readonly_host.close()
    for _ in range(2):
        with Database.open(path) as database:
            assert database.schema_version == 7 and database.audit_schema_version == 2
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM sdk_stage_audit_events"
                ).fetchone()[0]
                == 0
            )


def test_stage_extension_failure_rolls_back_exact_old_descriptor(tmp_path, monkeypatch):
    from simple_harness.execution.sqlite import audit_schema

    path = tmp_path / "atomic.db"
    legacy_v1(path)
    before = digest(path)
    monkeypatch.setattr(audit_schema, "STAGE_DDL", (*audit_schema.STAGE_DDL, "INVALID DDL"))
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)
    assert digest(path) == before


@pytest.mark.parametrize("corruption", ["partial", "noop", "column"])
def test_invalid_stage_schema_rejects_without_write(tmp_path, corruption):
    path = tmp_path / "invalid.db"
    with Database.open(path) as database:
        c = database.connection
        if corruption == "partial":
            c.execute("DROP TRIGGER sdk_stage_observe_insert")
        elif corruption == "noop":
            c.execute("DROP TRIGGER sdk_stage_observe_insert")
            c.execute(
                "CREATE TRIGGER sdk_stage_observe_insert AFTER INSERT "
                "ON context_preparation_staging BEGIN SELECT 1; END"
            )
        else:
            c.execute("ALTER TABLE sdk_stage_audit_events ADD COLUMN wrong TEXT")
    before = digest(path)
    with pytest.raises(AuditSchemaIncompatible):
        Database.open(path)
    assert digest(path) == before
