# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · D4-15: an orchestrator library at schema v1 (steps 2–3) is backed up and
upgraded in place to v2; a fresh library is created at v2; a newer library is refused."""

from __future__ import annotations

import sqlite3

import pytest

from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import SchemaIncompatible, Store


def _v1_library(path):
    connection = sqlite3.connect(path)
    for statement in schema.MIGRATIONS[0].ddl.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
        (1, schema.MIGRATIONS[0].name, schema.MIGRATIONS[0].checksum, 1.0),
    )
    connection.execute(
        "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
        " VALUES ('m1','t','k','CREATED',1,'h','{}',1.0,1.0)"
    )
    connection.commit()
    connection.close()


def test_v1_library_is_backed_up_and_upgraded_to_v2(tmp_path):
    path = tmp_path / "orchestrator.db"
    _v1_library(path)
    store = Store.open(path)
    rows = store.connection.execute(
        "SELECT version,name FROM orch_schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, schema.MIGRATIONS[0].name),
        (2, schema.MIGRATIONS[1].name),
    ]
    tables = {
        r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"knowledge", "summaries", "conflicts"} <= tables
    assert store.connection.execute("SELECT count(*) FROM missions").fetchone()[0] == 1
    assert (tmp_path / "orchestrator.db.pre-schema-2.backup").is_file()
    store.close()
    Store.open(path).close()  # idempotent: already at v2


def test_fresh_library_is_v2_and_newer_is_refused(tmp_path):
    store = Store.open(tmp_path / "fresh.db")
    assert schema.SCHEMA_VERSION == 2
    rows = store.connection.execute("SELECT version FROM orch_schema_migrations").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    store.connection.execute("INSERT INTO orch_schema_migrations VALUES (3,'future','x',1.0)")
    store.connection.commit()
    store.close()
    with pytest.raises(SchemaIncompatible):
        Store.open(tmp_path / "fresh.db")
