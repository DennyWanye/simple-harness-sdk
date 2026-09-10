# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T2: after schema v10 the explicit v7/v8 -> v9 upgrader still targets exactly v9."""

from __future__ import annotations

import sqlite3

from test_base_agent_schema_v10 import build_legacy_library

import simple_harness as h
from simple_harness.execution.sqlite import Database, schema
from simple_harness.execution.sqlite.short_context_migration import _validate


def _descriptor_rows(path):
    connection = sqlite3.connect(path)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def test_existing_v8_library_migrates_to_nine_after_v10(tmp_path):
    path = build_legacy_library(
        tmp_path / "eight.db", schema.legacy_v7_descriptor(), schema.legacy_v8_descriptor()
    )
    receipt = h.migrate_execution_to_v9(path, backup_path=tmp_path / "pre9.backup")
    assert receipt is not None
    assert receipt.from_version == 8 and receipt.to_version == 9
    nine = schema.legacy_v9_descriptor()
    assert receipt.new_descriptor_hash == nine.checksum
    assert _descriptor_rows(path)[-1] == (9, nine.name, nine.checksum)
    with Database.open(path) as database:
        assert database.schema_version == 9


def test_existing_v9_library_validates_against_legacy_v9_catalog(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        assert _validate(connection) == 9
    finally:
        connection.close()
    assert h.migrate_execution_to_v9(path, backup_path=tmp_path / "pre9.backup") is None
