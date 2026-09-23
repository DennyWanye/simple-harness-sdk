# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Execution v11 (ARP additive) upgrade: fresh, idempotent, rollback, legacy v10 reader."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.migration import (
    apply_ddl,
    assert_runtime_sqlite_pragmas,
    migrate_execution_to_v11,
    partition_ddl,
)
from simple_harness.execution.sqlite import schema
from simple_harness.execution.sqlite.base_agent.migration import migrate_execution_to_v10
from simple_harness.execution.sqlite.database import Database, ExecutionSchemaIncompatible


def _rows(path: Path) -> list[tuple[int, str, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (int(r[0]), str(r[1]), str(r[2]))
            for r in connection.execute(
                "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(r[0]) for r in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    finally:
        connection.close()


def test_fresh_v10_then_arp_upgrade_is_accepted_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    Database.open(path).close()
    assert [r[0] for r in _rows(path)] == [10]
    first = migrate_execution_to_v11(path)
    assert (first.from_version, first.to_version, first.applied_now) == (10, 11, True)
    assert first.prior_descriptor_hash == schema.fresh_descriptor().checksum
    assert first.new_descriptor_hash == schema.arp_descriptor().checksum
    assert [r[0] for r in _rows(path)] == [10, 11]
    assert {"arp_profiles", "arp_agent_sessions", "arp_context_recalls", "arp_jobs"} <= _tables(path)
    second = migrate_execution_to_v11(path)
    assert second.applied_now is False and second.new_descriptor_hash == first.new_descriptor_hash
    # The original runner opens the upgraded library and reads v11.
    database = Database.open(path)
    try:
        assert database.schema_version == 11
        assert database.connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        database.close()
    # The frozen v9 -> v10 upgrader sees an already-upgraded v10 library and does nothing.
    assert migrate_execution_to_v10(path, backup_path=tmp_path / "backup.sqlite3") is None
    assert [r[0] for r in _rows(path)] == [10, 11]


def test_frozen_v10_descriptor_is_untouched() -> None:
    fresh = schema.fresh_descriptor()
    assert (fresh.version, fresh.name) == (10, "0010_fresh")
    assert schema.SCHEMA_VERSION == 10 and schema.ARP_SCHEMA_VERSION == 11
    arp = schema.arp_descriptor()
    assert "CREATE TABLE arp_profiles" in arp.sql and "arp_" not in fresh.sql
    assert "BEGIN" not in arp.sql.upper().split("TRIGGER")[0]


def test_upgrade_rolls_back_on_failure_and_refuses_partial(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    Database.open(path).close()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE arp_profiles(x)")  # a foreign partial application
    connection.commit()
    connection.close()
    with pytest.raises(ExecutionSchemaIncompatible) as info:
        migrate_execution_to_v11(path)
    assert "partial" in str(info.value)
    assert [r[0] for r in _rows(path)] == [10]
    assert "arp_agent_sessions" not in _tables(path)


def test_tampered_v11_checksum_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    Database.open(path).close()
    migrate_execution_to_v11(path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE sdk_schema_migrations SET checksum=? WHERE version=11", ("f" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(ExecutionSchemaIncompatible):
        migrate_execution_to_v11(path)
    with pytest.raises(ExecutionSchemaIncompatible):
        Database.open(path)


def test_triggers_enforce_initial_states_and_no_replace(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    Database.open(path).close()
    migrate_execution_to_v11(path)
    connection = sqlite3.connect(path, isolation_level=None)
    assert_runtime_sqlite_pragmas(connection)
    connection.execute("BEGIN")
    connection.execute("INSERT INTO arp_profiles VALUES('p',1,?,'{}','{}')", ("1" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="identity already exists"):
        connection.execute("INSERT INTO arp_profiles VALUES('p',1,?,'{}','{}')", ("2" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable arp_profiles"):
        connection.execute("UPDATE arp_profiles SET body_json='{\"a\":1}'")
    with pytest.raises(sqlite3.IntegrityError, match="retention permit required"):
        connection.execute("DELETE FROM arp_profiles")
    with pytest.raises(sqlite3.IntegrityError, match="creation intent must start prepared"):
        connection.execute(
            "INSERT INTO arp_creation_intents VALUES('i','o','k',?,'a','r','{}','BOUND',1,'{}')",
            ("3" * 64,),
        )
    connection.execute("ROLLBACK")
    connection.close()


def test_partition_ddl_applies_on_a_fresh_file(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "index.sqlite3", isolation_level=None)
    assert_runtime_sqlite_pragmas(connection)
    connection.execute("BEGIN")
    count = apply_ddl(connection, partition_ddl())
    connection.execute("COMMIT")
    assert count > 20
    names = {str(r[0]) for r in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert {"session_partition", "index_generations", "session_chunks", "session_vectors"} <= names
    assert "session_words" in names and "session_trigrams" in names  # FTS5 virtual tables
    connection.close()


def test_pragmas_must_be_set_outside_a_transaction() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("BEGIN")
    with pytest.raises(ArpError) as info:
        assert_runtime_sqlite_pragmas(connection)
    assert info.value.code == "SQL_PRAGMA_UNSUPPORTED"
    connection.execute("ROLLBACK")
    with pytest.raises(RuntimeError):
        apply_ddl(connection, "CREATE TABLE t(x);")
