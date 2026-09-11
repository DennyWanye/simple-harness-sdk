# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · D7-12: a v4 library (step 6) is backed up and upgraded to v5 with the action
ledger, approvals, decisions and overrides tables; the old rows survive."""

from __future__ import annotations

import sqlite3

from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store


def _v4_library(path):
    connection = sqlite3.connect(path)
    for migration in schema.MIGRATIONS[:4]:
        for statement in migration.ddl.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
            (migration.version, migration.name, migration.checksum, 1.0),
        )
    connection.execute(
        "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
        " VALUES ('m1','t','k','CREATED',1,'h','{}',1.0,1.0)"
    )
    connection.commit()
    connection.close()


def test_a_v4_library_is_backed_up_and_gains_the_step7_tables(tmp_path):
    path = tmp_path / "orchestrator.db"
    _v4_library(path)
    store = Store.open(path)
    assert schema.MIGRATIONS[4].version == 5 and schema.MIGRATIONS[4].name == "orchestrator-step07"
    rows = store.connection.execute(
        "SELECT version FROM orch_schema_migrations ORDER BY version"
    ).fetchall()
    assert [r[0] for r in rows][:5] == [1, 2, 3, 4, 5]
    tables = {
        r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"actions", "approvals", "approval_decisions", "human_overrides"} <= tables
    assert store.connection.execute("SELECT count(*) FROM missions").fetchone()[0] == 1
    assert (tmp_path / f"orchestrator.db.pre-schema-{schema.SCHEMA_VERSION}.backup").is_file()
    assert store.list_actions("m1") == [] and store.list_approvals("m1") == []
    store.close()
    Store.open(path).close()  # idempotent


def test_a_nonce_is_unique_per_request_and_a_receipt_is_stored_once(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    store.connection.execute(
        "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,spec_hash,json,created_at,updated_at)"
        " VALUES ('m1','t','k','CREATED',1,'h','{}',1.0,1.0)"
    )
    store.connection.commit()
    store.put_approval(
        {
            "request_id": "q1",
            "kind": "action",
            "mission_id": "m1",
            "subject_key": "a:v1",
            "state": "PENDING",
            "version": 1,
        }
    )
    decision = {
        "receipt_hash": "r1",
        "request_id": "q1",
        "principal_id": "alice",
        "decision": "grant",
        "nonce": "n1",
    }
    with store.transaction():
        assert store.insert_decision(decision) is True
        assert store.insert_decision(decision) is False  # the same receipt again
        assert (
            store.insert_decision({**decision, "receipt_hash": "r2"}) is False
        )  # the same nonce again
        assert store.insert_decision({**decision, "receipt_hash": "r3", "nonce": "n2"}) is True
    assert [d["receipt_hash"] for d in store.list_decisions("q1")] == ["r1", "r3"]
    store.close()
