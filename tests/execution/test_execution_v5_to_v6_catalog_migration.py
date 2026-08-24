# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from simple_harness.contracts import canonical_json
from simple_harness.execution import (
    CatalogHandlerBinding,
    DurableToolCatalogResolver,
)
from simple_harness.execution.sqlite import (
    Database,
    SqliteExecutionUnitOfWork,
    migrate_execution_v5_to_v6,
)
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.providers import ProviderToolSpec


def _v5_database(path: Path) -> tuple[str, str]:
    sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0005_fresh.sql")
        .read_text(encoding="utf-8")
    )
    specs = [
        {"name": "alpha", "description": "A", "parameters": {"type": "object"}},
        {"name": "beta", "description": "B", "parameters": {"type": "object"}},
    ]
    specs_json = canonical_json(specs)
    fingerprint = hashlib.sha256(specs_json.encode()).hexdigest()
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE sdk_schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,"
        "checksum TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP) STRICT;" + sql
    )
    connection.execute(
        "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES(5,'0005_fresh',?)",
        (hashlib.sha256(sql.encode()).hexdigest(),),
    )
    connection.execute(
        "INSERT INTO tool_catalog_snapshots(content_fingerprint,specs_json,created_at) "
        "VALUES(?,?,1)",
        (fingerprint, specs_json),
    )
    connection.commit()
    connection.close()
    return specs_json, fingerprint


def test_v5_catalog_migrates_backup_first_and_reopens_byte_compatible(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    backup = tmp_path / "execution.v5.backup.db"
    specs_json, fingerprint = _v5_database(path)

    receipt = migrate_execution_v5_to_v6(path, backup_path=backup)

    assert receipt.source_sha256 == receipt.backup_sha256
    assert receipt.migrated_catalog_count == 1
    old = sqlite3.connect(backup)
    assert old.execute("SELECT specs_json FROM tool_catalog_snapshots").fetchone()[0] == specs_json
    old.close()
    with Database.open(path) as database:
        row = database.connection.execute(
            "SELECT specs_json,content_fingerprint,provider_specs_fingerprint,"
            "catalog_envelope_digest_v6 FROM tool_catalog_snapshots"
        ).fetchone()
        assert row[0] == specs_json
        assert row[1] == row[2] == fingerprint
        assert len(row[3]) == 64
        snapshot = SqliteExecutionUnitOfWork(database).read_tool_catalog_snapshot(1)
        assert snapshot is not None
        assert snapshot.provider_specs_fingerprint == fingerprint
        assert [spec.name for spec in snapshot.specs] == ["alpha", "beta"]


def test_v6_handler_resolution_is_order_independent_and_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    _v5_database(path)
    migrate_execution_v5_to_v6(path, backup_path=tmp_path / "execution.v5.backup.db")
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        snapshot = uow.read_tool_catalog_snapshot(1)
        assert snapshot is not None and snapshot.catalog_envelope is not None
        records = snapshot.catalog_envelope["records"]  # type: ignore[index]
        alpha = records[0]  # type: ignore[index]
        beta = records[1]  # type: ignore[index]
        bindings = (
            CatalogHandlerBinding(beta["handler_locator"], beta["handler_identity_digest"], "B"),  # type: ignore[index]
            CatalogHandlerBinding(alpha["handler_locator"], alpha["handler_identity_digest"], "A"),  # type: ignore[index]
        )
        resolver = DurableToolCatalogResolver(uow)
        resolved = resolver.resolve_handlers(1, snapshot.catalog_envelope_digest_v6 or "", bindings)
        assert resolved is not None and resolved.handlers == {"alpha": "A", "beta": "B"}
        assert (
            resolver.resolve_handlers(1, snapshot.catalog_envelope_digest_v6 or "", bindings[:1])
            is None
        )
        changed = (
            CatalogHandlerBinding(alpha["handler_locator"], "0" * 64, "A"),  # type: ignore[index]
            bindings[0],
        )
        assert (
            resolver.resolve_handlers(1, snapshot.catalog_envelope_digest_v6 or "", changed) is None
        )
        extra = (*bindings, CatalogHandlerBinding("unexpected", "f" * 64, "X"))
        assert (
            resolver.resolve_handlers(1, snapshot.catalog_envelope_digest_v6 or "", extra) is None
        )


def test_same_provider_projection_with_changed_v6_envelope_fails_closed(tmp_path: Path) -> None:
    with Database.open(tmp_path / "fresh-v6.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        specs = (ProviderToolSpec("alpha", "A", {"type": "object"}),)
        first = {
            "schema_version": 6,
            "records": [
                {
                    "kind": "executable_tool",
                    "provider_name": "alpha",
                    "handler_locator": "handler:alpha",
                    "handler_identity_digest": "a" * 64,
                },
                {
                    "kind": "skill_resource",
                    "capability_id": "skill:translate-doc",
                },
            ],
        }
        snapshot = uow.put_tool_catalog_snapshot(specs, catalog_envelope=first)
        assert snapshot.catalog_envelope_digest_v6
        resolved = DurableToolCatalogResolver(uow).resolve_handlers(
            snapshot.generation,
            snapshot.catalog_envelope_digest_v6,
            (CatalogHandlerBinding("handler:alpha", "a" * 64, "A"),),
        )
        assert resolved is not None and resolved.handlers == {"alpha": "A"}

        changed = dict(first)
        changed["records"] = [dict(first["records"][0], handler_locator="handler:other")]
        with pytest.raises(UnitOfWorkConflict, match="v6 envelope differs"):
            uow.put_tool_catalog_snapshot(specs, catalog_envelope=changed)
