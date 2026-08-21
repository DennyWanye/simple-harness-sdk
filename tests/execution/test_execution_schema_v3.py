# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import (
    SCHEMA_VERSION,
    Database,
    ExecutionSchemaIncompatible,
)


def test_fresh_v3_is_one_identity_with_conversation_tables(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        assert database.schema_version == SCHEMA_VERSION == 3
        assert {
            "execution_users",
            "memory_outbox",
            "context_preparation_staging",
            "tool_catalog_snapshots",
            "provider_projection_outbox",
        } <= database.table_names()
        assert database.foreign_keys_enabled
        assert database.integrity_check() == ("ok",)
        assert database.foreign_key_violations() == ()
        history = database.connection.execute(
            "SELECT version,name FROM sdk_schema_migrations"
        ).fetchall()
        assert [tuple(row) for row in history] == [(3, "0003_fresh")]


def test_v2_history_fails_closed_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sdk_schema_migrations(version INTEGER PRIMARY KEY,"
        "name TEXT UNIQUE,checksum TEXT,applied_at TEXT)"
    )
    connection.execute(
        "INSERT INTO sdk_schema_migrations VALUES(2,'0002_context_authority',?,CURRENT_TIMESTAMP)",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ExecutionSchemaIncompatible):
        Database.open(path)
    check = sqlite3.connect(path)
    assert check.execute("SELECT version FROM sdk_schema_migrations").fetchone()[0] == 2
    check.close()
