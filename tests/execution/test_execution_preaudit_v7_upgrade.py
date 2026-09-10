# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host integration (2026-09-10): a v7 library written before the explicit audit schema
(the live Host execution file) must upgrade 7 -> 9 -> 10 through the explicit
upgraders alone, and a Host may keep calling both upgraders unconditionally."""

from __future__ import annotations

import sqlite3

from test_base_agent_schema_v10 import build_legacy_library

import simple_harness as h
from simple_harness.execution.sqlite import Database, schema
from simple_harness.execution.sqlite.audit_schema import AUDIT_SCHEMA_VERSION
from simple_harness.execution.sqlite.context_use_migration import _DESCRIPTOR_SQL


def _preaudit_v7(path):
    """Exactly what an SDK from before the audit schema left behind: descriptor + DDL."""

    descriptor = schema.legacy_v7_descriptor()
    connection = sqlite3.connect(path)
    connection.executescript(_DESCRIPTOR_SQL + ";" + descriptor.sql)
    connection.execute(
        "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES(?,?,?)",
        (descriptor.version, descriptor.name, descriptor.checksum),
    )
    connection.commit()
    connection.close()
    return path


def _v9(path, tmp_path):
    return h.migrate_execution_to_v9(
        path, backup_path=tmp_path / (path.name + ".pre-schema-9.backup")
    )


def _v10(path, tmp_path):
    return h.migrate_execution_to_v10(
        path, backup_path=tmp_path / (path.name + ".pre-schema-10.backup")
    )


def _versions(path):
    return [
        row[0]
        for row in sqlite3.connect(path).execute(
            "SELECT version FROM sdk_schema_migrations ORDER BY version"
        )
    ]


def test_preaudit_v7_upgrades_to_v10_and_both_upgraders_replay(tmp_path):
    path = _preaudit_v7(tmp_path / "host.db")
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert "sdk_audit_schema" not in names
    nine = _v9(path, tmp_path)
    assert nine is not None and nine.from_version == 7
    assert _versions(path) == [7, 9]
    assert tuple(
        sqlite3.connect(path).execute("SELECT version FROM sdk_audit_schema").fetchone()
    ) == (AUDIT_SCHEMA_VERSION,)
    # The retained backup is the pre-audit image, still accepted on replay.
    assert _v9(path, tmp_path) == nine
    ten = _v10(path, tmp_path)
    assert ten is not None and _versions(path) == [7, 9, 10]
    # Unconditional startup order on an already-upgraded library: both replay.
    assert _v9(path, tmp_path) == nine
    assert _v10(path, tmp_path) == ten
    with Database.open(path) as database:
        assert database.schema_version == 10


def test_fresh_v10_library_is_a_noop_for_the_v9_upgrader(tmp_path):
    path = tmp_path / "fresh.db"
    with Database.open(path):
        pass
    assert _versions(path) == [10]
    assert _v9(path, tmp_path) is None
    assert _v10(path, tmp_path) is None
    assert not (tmp_path / "fresh.db.pre-schema-9.backup").exists()


def test_partial_audit_schema_is_still_refused(tmp_path):
    path = _preaudit_v7(tmp_path / "partial.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sdk_audit_schema(version INTEGER, checksum TEXT)")
    connection.commit()
    connection.close()
    import pytest

    from simple_harness.execution.sqlite.database import ExecutionSchemaIncompatible

    with pytest.raises(ExecutionSchemaIncompatible):
        _v9(path, tmp_path)
    assert _versions(path) == [7]


def test_v9_library_with_audit_still_upgrades(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    assert _v9(path, tmp_path) is None
    assert _v10(path, tmp_path) is not None
    assert _v9(path, tmp_path) is None
