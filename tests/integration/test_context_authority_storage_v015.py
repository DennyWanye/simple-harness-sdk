# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio
import sqlite3
from pathlib import Path

import pytest

from simple_harness import RunId, thaw_json
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.sqlite.schema import migrations
from simple_harness.providers import CancelToken, ProviderToolSpec
from simple_harness.tools import normalize_public_progress_arguments

from .provider_ledger_fakes import RecordingProvider
from .test_provider_sqlite_ledger import _coordinator, _create_run, _request


def test_catalog_generation_is_content_addressed_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.db"
    specs = (
        ProviderToolSpec(
            "read_file",
            "Read one file",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "deskpet_public_progress": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
    )
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        first = uow.put_tool_catalog_snapshot(specs, created_at=1)
        duplicate = uow.put_tool_catalog_snapshot(specs, created_at=2)
        changed = uow.put_tool_catalog_snapshot(
            (ProviderToolSpec("read_file", "Changed", thaw_json(specs[0].parameters)),),  # type: ignore[arg-type]
            created_at=3,
        )
        assert duplicate.generation == first.generation
        assert changed.generation > first.generation

    with Database.open(path) as reopened:
        restored = SqliteExecutionUnitOfWork(reopened).read_tool_catalog_snapshot(
            first.generation
        )
        assert restored == first


def test_settlement_and_projection_receipt_commit_atomically_and_cursor_is_stable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outbox.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        lease = _create_run(uow)
        asyncio.run(
            _coordinator(uow, RecordingProvider()).invoke(
                RunId("run-1"),
                _request(),
                cancel=CancelToken(),
                execution_lease=lease,
            )
        )
        receipts = uow.list_provider_projection_receipts()
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.execution_session_id == "session-1"
        assert receipt.payload["state"] == "succeeded"  # type: ignore[index]

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.list_provider_projection_receipts() == receipts
        assert uow.list_provider_projection_receipts(
            after_sequence=receipt.sequence
        ) == ()


def test_settlement_rolls_back_ledger_when_projection_outbox_write_cannot_finish(
    tmp_path: Path,
) -> None:
    with Database.open(tmp_path / "atomic-outbox.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        lease = _create_run(uow)
        coordinator = _coordinator(uow, RecordingProvider())
        claimed = asyncio.run(
            coordinator.prepare_claim(RunId("run-1"), _request(), execution_lease=lease)
        )
        handed_off = uow.hand_off_provider_invocation(
            claimed.invocation_id,
            expected_version=claimed.version,
            handed_off_at=3,
            execution_lease=lease,
        )
        terminal = handed_off.settle_failed(
            error_code="provider_test_failure",
            at=4,
            expected_version=handed_off.version,
        )

        def fail_after_ledger(point: str) -> None:
            if point == "provider_settlement.ledger.after_write":
                raise RuntimeError("injected projection failure")

        with pytest.raises(RuntimeError, match="injected projection failure"):
            uow.settle_provider_invocation(
                terminal,
                expected_version=handed_off.version,
                fault=fail_after_ledger,
            )
        restored = uow.read_provider_invocation(claimed.invocation_id)
        assert restored is not None
        assert restored.state is ProviderInvocationState.HANDED_OFF
        assert uow.list_provider_projection_receipts() == ()

        settled = uow.settle_provider_invocation(
            terminal, expected_version=handed_off.version
        )
        assert settled.state is ProviderInvocationState.FAILED
        assert len(uow.list_provider_projection_receipts()) == 1


def test_public_progress_normalization_never_blocks_business_arguments() -> None:
    for value in (None, "", 12, {"bad": True}):
        arguments, narration = normalize_public_progress_arguments(
            {"path": "a.txt", "deskpet_public_progress": value}
        )
        assert arguments == {"path": "a.txt"}
        assert narration is None
    arguments, narration = normalize_public_progress_arguments(
        {"path": "a.txt", "deskpet_public_progress": "  Reading file  "}
    )
    assert arguments == {"path": "a.txt"}
    assert narration == "Reading file"


def test_v014_database_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    first = migrations()[0]
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE sdk_schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT);"
        + first.sql
    )
    connection.execute(
        "INSERT INTO sdk_schema_migrations VALUES (?,?,?,CURRENT_TIMESTAMP)",
        (first.version, first.name, first.checksum),
    )
    connection.commit()
    connection.close()

    with Database.open(path) as migrated:
        assert migrated.schema_version == 2
        assert {"tool_catalog_snapshots", "provider_projection_outbox"} <= (
            migrated.table_names()
        )
