# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from simple_harness import RunId, thaw_json
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.sqlite import (
    Database,
    ExecutionSchemaIncompatible,
    SqliteExecutionUnitOfWork,
)
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
        restored = SqliteExecutionUnitOfWork(reopened).read_tool_catalog_snapshot(first.generation)
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
        assert receipt.payload["handoff_attempt"] == 1  # type: ignore[index]
        assert receipt.payload["target"]["model"] == "model-1"  # type: ignore[index]

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.list_provider_projection_receipts() == receipts
        assert uow.list_provider_projection_receipts(after_sequence=receipt.sequence) == ()


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

        settled = uow.settle_provider_invocation(terminal, expected_version=handed_off.version)
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


def test_v015_database_requires_fresh_v3_storage_set(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    resources = files("simple_harness.execution.sqlite.migrations")
    first = resources.joinpath("0001_initial.sql").read_text(encoding="utf-8")
    second = resources.joinpath("0002_context_authority.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE sdk_schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT);" + first + second
    )
    connection.executemany(
        "INSERT INTO sdk_schema_migrations VALUES (?,?,?,CURRENT_TIMESTAMP)",
        (
            (1, "0001_initial", hashlib.sha256(first.encode()).hexdigest()),
            (
                2,
                "0002_context_authority",
                hashlib.sha256(second.encode()).hexdigest(),
            ),
        ),
    )
    snapshot = json.dumps(
        {
            "schema_version": 4,
            "profile_key": "agent.general",
            "driver_kind": "react",
            "turn_id": "turn-v4",
            "tool_catalog_generation": 1,
            "input": {},
            "policy_fingerprint": None,
            "tool_catalog_fingerprint": "a" * 64,
            "provider_budget_fingerprint": "b" * 64,
            "workflow_admission": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute("INSERT INTO execution_sessions VALUES ('session-v4', 1)")
    connection.execute(
        "INSERT INTO runs(run_id,execution_session_id,request_id,root_run_id,"
        "profile_key,driver_kind,state,created_at,updated_at) "
        "VALUES ('run-v4','session-v4','request-v4','run-v4','agent.general',"
        "'react','created',1,1)"
    )
    connection.execute(
        "INSERT INTO run_start_snapshots VALUES ('run-v4',?,?,1)",
        (snapshot, hashlib.sha256(snapshot.encode()).hexdigest()),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ExecutionSchemaIncompatible, match="fresh schema v3"):
        Database.open(path)

    check = sqlite3.connect(path)
    assert check.execute(
        "SELECT version FROM sdk_schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,)]
    assert check.execute(
        "SELECT snapshot_json FROM run_start_snapshots WHERE run_id='run-v4'"
    ).fetchone() == (snapshot,)
    assert (
        check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_outbox'"
        ).fetchone()
        is None
    )
    check.close()
