# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 5 · BA37: explicit backup-first v9 -> v10 upgrade keeps legacy Runs readable
and makes BaseAgents usable on the same library."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from test_base_agent_schema_v10 import build_legacy_library

import simple_harness as h
from simple_harness import AllowAllAdmission, Message, MessageRole, RunId
from simple_harness.agents import AgentConfig, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import ExecutionSessionId, RequestId
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork, schema
from simple_harness.execution.sqlite.database import ExecutionSchemaIncompatible
from simple_harness.execution.uow import RunState
from simple_harness.runtime import (
    DriverResult,
    RunStart,
    RuntimePorts,
    RuntimeProfile,
    SqliteContextPort,
    build_runtime,
)


class _Noop:
    async def reconcile(self) -> None:
        return None

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        raise AssertionError("no provider call expected")


class _Catalog:
    def current_generation(self) -> int:
        return 1


class _LegacyDriver:
    async def start(self, invocation, *, context, cancel):  # type: ignore[no-untyped-def]
        del invocation, cancel
        return DriverResult(RunState.COMPLETED, {"answer": "legacy ok"})


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


async def _run_legacy(path) -> str:
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    noop = _Noop()
    runtime = build_runtime(
        uow,
        {"agent.general": RuntimeProfile("agent.general", "legacy")},
        {"legacy": _LegacyDriver()},
        RuntimePorts(
            provider=noop,
            tools=noop,
            authorization=noop,
            context=SqliteContextPort(database),
            delivery=noop,
            tool_reconciliation=noop,
            reconciliation=noop,
            provider_reconciliation=noop,
            react_checkpoint=uow,
            tool_catalog=_Catalog(),
            owner_id="legacy-owner",
            admission=AllowAllAdmission(),
        ),
        close_hook=database.close,
    )
    async with runtime:
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("legacy-session"),
                RunId("legacy-run"),
                RequestId("legacy-request"),
                "legacy-turn",
                {"messages": [Message(MessageRole.USER, "hi").to_dict()]},
                1,
            )
        )
        await runtime.wait_idle(RunId("legacy-run"))
        run = uow.read_run("legacy-run")
        assert run is not None and run.state is RunState.COMPLETED
    return "legacy-run"


def test_v9_library_with_legacy_run_migrates_and_keeps_history(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    asyncio.run(_run_legacy(path))
    # A v9 library opens, but BaseAgents refuse it until the explicit upgrade.
    with pytest.raises(ExecutionSchemaIncompatible):
        build_agent_runtime(
            AgentRuntimePorts(
                provider=_Noop(), authorization=AllowAllAuthorization(), database_path=str(path)
            )
        )
    backup = tmp_path / "nine.db.pre-schema-10.backup"
    receipt = h.migrate_execution_to_v10(path, backup_path=backup)
    assert receipt is not None
    assert receipt.from_version == 9 and receipt.to_version == 10
    assert receipt.prior_descriptor_hash == schema.legacy_v9_descriptor().checksum
    assert receipt.new_descriptor_hash == schema.fresh_descriptor().checksum
    assert backup.is_file()
    rows = _descriptor_rows(path)
    assert rows[-1] == (10, "0010_fresh", schema.fresh_descriptor().checksum)
    assert rows[0][0] == 9
    # Replay returns the identical receipt without touching the library again.
    again = h.migrate_execution_to_v10(path, backup_path=backup)
    assert again == receipt
    # The backup still validates as v9 and holds the legacy run.
    saved = sqlite3.connect(backup)
    try:
        assert [r[0] for r in saved.execute("SELECT version FROM sdk_schema_migrations")] == [9]
        assert saved.execute("SELECT state FROM runs WHERE run_id='legacy-run'").fetchone()[0] == (
            "completed"
        )
    finally:
        saved.close()

    async def after():
        with Database.open(path) as database:
            assert database.schema_version == 10
            uow = SqliteExecutionUnitOfWork(database)
            run = uow.read_run("legacy-run")
            assert run is not None and run.state is RunState.COMPLETED
            events = uow.database.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id='legacy-run'"
            ).fetchone()[0]
            assert events >= 1
            snapshot = uow.read_start_snapshot("legacy-run")
            assert snapshot is not None

        class Provider:
            async def invoke(self, request, *, cancel):
                from simple_harness.providers import ProviderResponse

                return ProviderResponse(
                    request.request_id,
                    Message(MessageRole.ASSISTANT, "迁移后可用"),
                    model="m",
                    finish_reason="stop",
                )

        ports = AgentRuntimePorts(
            provider=Provider(),
            authorization=AllowAllAuthorization(),
            database_path=str(path),
            model="m",
            owner_id="after-upgrade",
        )
        async with build_agent_runtime(ports) as runtime:
            agent = await runtime.create(
                AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p"),
                creation_key="post-upgrade",
            )
            result = await agent.ask("你好", input_id="i1", timeout=5)
            assert result.public_output.content == "迁移后可用"
            legacy = runtime.uow.read_run("legacy-run")
            assert legacy is not None and legacy.state is RunState.COMPLETED

    asyncio.run(after())


def test_fresh_v10_library_returns_no_receipt_and_v7_is_refused(tmp_path):
    path = tmp_path / "fresh.db"
    with Database.open(path):
        pass
    assert h.migrate_execution_to_v10(path, backup_path=tmp_path / "fresh.backup") is None
    assert not (tmp_path / "fresh.backup").exists()
    seven = build_legacy_library(tmp_path / "seven.db", schema.legacy_v7_descriptor())
    with pytest.raises(ExecutionSchemaIncompatible):
        h.migrate_execution_to_v10(seven, backup_path=tmp_path / "seven.backup")
    assert not (tmp_path / "seven.backup").exists()


def test_upgrade_refuses_symlinks_and_foreign_backup_directories(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(ValueError):
        h.migrate_execution_to_v10(path, backup_path=other / "nine.backup")
    link = tmp_path / "link.db"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        h.migrate_execution_to_v10(link, backup_path=tmp_path / "link.backup")


def test_tampered_backup_is_detected_on_replay(tmp_path):
    path = build_legacy_library(tmp_path / "nine.db", schema.legacy_v9_descriptor())
    backup = tmp_path / "nine.backup"
    receipt = h.migrate_execution_to_v10(path, backup_path=backup)
    assert receipt is not None
    with backup.open("ab") as stream:
        stream.write(b"\\x00")
    with pytest.raises(ExecutionSchemaIncompatible):
        h.migrate_execution_to_v10(path, backup_path=backup)
