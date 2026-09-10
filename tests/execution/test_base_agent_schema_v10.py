# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T2: execution schema v10 adds the BaseAgent tables and keeps v9 openable."""

from __future__ import annotations

import sqlite3

import pytest

from simple_harness.execution.sqlite import Database, schema
from simple_harness.execution.sqlite.audit_schema import AUDIT_SCHEMA_VERSION, CHECKSUM
from simple_harness.execution.sqlite.audit_schema import DDL as AUDIT_DDL
from simple_harness.execution.sqlite.context_use_migration import _DESCRIPTOR_SQL

BASE_AGENT_TABLES = {
    "base_agent_bindings_v1",
    "base_agent_turns_v1",
    "base_agent_turn_results_v1",
    "base_agent_delegations_v1",
}

# Values computed on main fd12e7dd before the v10 change; they must never move.
FROZEN_CHECKSUMS = {
    7: "1d567e5720eb767c92d8a9ea2af7091ea3d305e04a5c6d717f50305f418fd463",
    8: "e35e714093c8946e39c5ff90c97ee40abbcffe2091e4b9f2db118b3b5cbe5655",
    9: "d9cb3ed5942c25465f90db86c9b8faf79f49ed5e7ed57f1b0b47843af4802103",
}


def build_legacy_library(path, *descriptors):
    """Create a library exactly as an older SDK would have left it (descriptor + audit)."""

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(_DESCRIPTOR_SQL)
        connection.executescript(descriptors[-1].sql)  # autocommit; executescript commits anyway
        for statement in AUDIT_DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO sdk_audit_schema VALUES (?,?)", (AUDIT_SCHEMA_VERSION, CHECKSUM)
        )
        for descriptor in descriptors:
            connection.execute(
                "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES (?,?,?)",
                (descriptor.version, descriptor.name, descriptor.checksum),
            )
    finally:
        connection.close()
    return path


def table_names(path):
    connection = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


def test_fresh_open_is_v10(tmp_path):
    with Database.open(tmp_path / "a.db") as database:
        assert database.schema_version == 10
    assert schema.SCHEMA_VERSION == 10
    assert BASE_AGENT_TABLES <= table_names(tmp_path / "a.db")


def test_v9_database_still_opens(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    with Database.open(path) as database:
        assert database.schema_version == 9
    assert not (BASE_AGENT_TABLES & table_names(path))


def test_descriptor_checksums_are_stable():
    assert schema.legacy_v7_descriptor().checksum == FROZEN_CHECKSUMS[7]
    assert schema.legacy_v8_descriptor().checksum == FROZEN_CHECKSUMS[8]
    assert schema.legacy_v9_descriptor().checksum == FROZEN_CHECKSUMS[9]
    assert schema.legacy_v9_descriptor().name == "0009_fresh"
    fresh = schema.fresh_descriptor()
    assert (fresh.version, fresh.name) == (10, "0010_fresh")
    assert fresh.checksum not in FROZEN_CHECKSUMS.values()
    assert fresh.sql.startswith(schema.legacy_v9_descriptor().sql)


def test_accepted_rows_keep_all_v9_combinations():
    def row(descriptor):
        return (descriptor.version, descriptor.name, descriptor.checksum)

    seven, eight, nine, ten = map(
        row,
        (
            schema.legacy_v7_descriptor(),
            schema.legacy_v8_descriptor(),
            schema.legacy_v9_descriptor(),
            schema.fresh_descriptor(),
        ),
    )
    accepted = schema.accepted_descriptor_rows()
    for legacy in ((nine,), (seven, nine), (eight, nine), (seven, eight, nine)):
        assert legacy in accepted
    assert (ten,) in accepted
    assert (nine, ten) in accepted
    assert (eight,) not in accepted


def test_schema_all_exports_descriptor_helpers():
    assert set(schema.__all__) >= {
        "accepted_descriptor_rows",
        "legacy_v7_descriptor",
        "legacy_v8_descriptor",
        "legacy_v9_descriptor",
        "fresh_descriptor",
        "SCHEMA_VERSION",
        "Migration",
        "migrations",
        "initial_migration",
    }


def test_delegation_uniques(tmp_path):
    with Database.open(tmp_path / "a.db") as database:
        connection = database.connection
        # Only the delegation table's own UNIQUE/CHECK shape is under test here; the
        # runs-row foreign key chain is exercised by the runtime-level tests.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN")
        _seed_binding_and_turn(connection)
        row = (
            "d-1", "a1", "t1", 1, "child-a", "child-run", "ticket-1", "reserved", 0.0, 0.0,
        )
        connection.execute(
            "INSERT INTO base_agent_delegations_v1 VALUES (?,?,?,?,?,?,?,?,?,?)", row
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO base_agent_delegations_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("d-2", "a1", "t1", 1, "child-b", "child-run-b", "ticket-2", "reserved", 0.0, 0.0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO base_agent_delegations_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("d-3", "a1", "t1", 2, "child-a", "child-run-c", "ticket-3", "reserved", 0.0, 0.0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO base_agent_delegations_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("d-4", "a1", "t1", 3, "child-d", "child-run-d", "ticket-4", "bogus", 0.0, 0.0),
            )
        connection.execute("ROLLBACK")
        connection.execute("PRAGMA foreign_keys = ON")


def _seed_binding_and_turn(connection):
    connection.execute(
        "INSERT INTO base_agent_bindings_v1(agent_id,run_id,owner_scope,api_mode,role,"
        "creation_key,config_json,config_hash,control_generation,created_at) VALUES "
        "('a1','r1','owner','base_agent_v1','root','ck-1','{}',?,0,0.0)",
        ("0" * 64,),
    )
    connection.execute(
        "INSERT INTO base_agent_turns_v1(turn_id,agent_id,input_id,input_hash,input_json,"
        "seq,phase,created_at,updated_at) VALUES ('t1','a1','i1',?,'{}',1,'queued',0.0,0.0)",
        ("0" * 64,),
    )


# ---- Slice 2 · T1: lifecycle column, creation batches, control commands ----


def test_binding_lifecycle_defaults_open_and_is_checked(tmp_path):
    with Database.open(tmp_path / "a.db") as database:
        connection = database.connection
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN")
        _seed_binding_and_turn(connection)
        row = connection.execute(
            "SELECT lifecycle, lifecycle_updated_at FROM base_agent_bindings_v1 WHERE agent_id='a1'"
        ).fetchone()
        assert tuple(row) == ("open", None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE base_agent_bindings_v1 SET lifecycle='dead' WHERE agent_id='a1'"
            )
        connection.execute(
            "UPDATE base_agent_bindings_v1 SET lifecycle='closing', lifecycle_updated_at=1.0 "
            "WHERE agent_id='a1'"
        )
        connection.execute("ROLLBACK")
        connection.execute("PRAGMA foreign_keys = ON")


def test_creation_batches_unique_per_owner_and_key(tmp_path):
    with Database.open(tmp_path / "a.db") as database:
        connection = database.connection
        connection.execute("BEGIN")
        cols = (
            "batch_id,owner_scope,batch_key,batch_fingerprint,agent_ids_json,"
            "config_hashes_json,state,receipt_json,created_at,updated_at"
        )
        connection.execute(
            f"INSERT INTO base_agent_creation_batches_v1({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("b-1", "owner", "k1", "f" * 64, "[]", "[]", "reserved", None, 0.0, 0.0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO base_agent_creation_batches_v1({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("b-2", "owner", "k1", "e" * 64, "[]", "[]", "reserved", None, 0.0, 0.0),
            )
        # A different owner may reuse the key.
        connection.execute(
            f"INSERT INTO base_agent_creation_batches_v1({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("b-3", "other", "k1", "e" * 64, "[]", "[]", "committed", "{}", 0.0, 0.0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO base_agent_creation_batches_v1({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("b-4", "owner", "k2", "short", "[]", "[]", "reserved", None, 0.0, 0.0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO base_agent_creation_batches_v1({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("b-5", "owner", "k3", "a" * 64, "[]", "[]", "pending", None, 0.0, 0.0),
            )
        connection.execute("ROLLBACK")


def test_control_commands_unique_id_and_kind_check(tmp_path):
    with Database.open(tmp_path / "a.db") as database:
        connection = database.connection
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN")
        _seed_binding_and_turn(connection)
        cols = (
            "command_id,agent_id,kind,target_turn_id,control_generation,request_hash,"
            "receipt_json,created_at"
        )
        connection.execute(
            f"INSERT INTO base_agent_control_commands_v1({cols}) VALUES (?,?,?,?,?,?,?,?)",
            ("c-1", "a1", "close", None, 1, "a" * 64, "{}", 0.0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO base_agent_control_commands_v1({cols}) VALUES (?,?,?,?,?,?,?,?)",
                ("c-1", "a1", "close", None, 2, "a" * 64, "{}", 0.0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO base_agent_control_commands_v1({cols}) VALUES (?,?,?,?,?,?,?,?)",
                ("c-2", "a1", "pause", None, 2, "a" * 64, "{}", 0.0),
            )
        connection.execute(
            f"INSERT INTO base_agent_control_commands_v1({cols}) VALUES (?,?,?,?,?,?,?,?)",
            ("c-3", "a1", "cancel_turn", "t1", 2, "b" * 64, "{}", 0.0),
        )
        connection.execute("ROLLBACK")
        connection.execute("PRAGMA foreign_keys = ON")


def test_only_one_v10_descriptor_and_v9_unchanged():
    from simple_harness.execution.sqlite import schema

    assert schema.legacy_v9_descriptor().checksum.startswith("d9cb3ed5")
    assert [m.version for m in schema.migrations()] == [10]
    assert "base_agent_creation_batches_v1" in schema.fresh_descriptor().sql
    assert "base_agent_control_commands_v1" in schema.fresh_descriptor().sql
    assert "base_agent_creation_batches_v1" not in schema.legacy_v9_descriptor().sql
