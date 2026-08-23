# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest

from simple_harness import (
    AgentIdentity,
    CancelCommandIntent,
    CommandError,
    CommandErrorCode,
    CommandOutputState,
    CommandState,
    ContinueCommandIntent,
    Message,
    MessageRole,
    RequestId,
    RunId,
    StartCommandIntent,
)
from simple_harness.execution.command_ingress import CommandIngress
from simple_harness.execution.sqlite import Database, ExecutionSchemaIncompatible
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.runtime import ConversationContinuationInput, ConversationTurnInput


def _start(command_id: str = "start-1") -> StartCommandIntent:
    return StartCommandIntent(
        "deployment/phone",
        "key-1",
        command_id,
        RunId("run-1"),
        RequestId("request-1"),
        "turn-1",
        ConversationTurnInput(
            AgentIdentity("deployment", "household", "actor", "session"),
            Message(MessageRole.USER, "hello"),
            "hello",
        ),
    )


def _continuation(command_id: str, continuation_id: str) -> ContinueCommandIntent:
    return ContinueCommandIntent(
        "deployment/phone",
        "key-1",
        command_id,
        RunId("run-1"),
        continuation_id,
        continuation_id,
        ConversationContinuationInput(Message(MessageRole.USER, "next"), "next"),
    )


def test_fresh_v5_schema_and_command_admission_replay_conflicts(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        assert database.schema_version == 5
        assert {
            "conversation_command_namespaces",
            "conversation_run_modes",
            "conversation_command_streams",
            "conversation_commands",
            "conversation_outputs",
        } <= database.table_names()
        ingress = CommandIngress(database)
        accepted = ingress.submit_start(_start(), now=1)
        replay = ingress.submit_start(_start(), now=2)
        assert accepted == replay
        assert accepted.accept_seq == 0
        assert accepted.state is CommandState.ACCEPTED
        with pytest.raises(CommandError) as conflict:
            ingress.submit_start(_start("different-command"), now=3)
        assert conflict.value.code is CommandErrorCode.RUN_MODE_CONFLICT
        with pytest.raises(CommandError) as identity_conflict:
            ingress.submit_start(
                StartCommandIntent(
                    "deployment/phone",
                    "key-1",
                    "start-1",
                    RunId("different-run"),
                    RequestId("request-1"),
                    "turn-1",
                    _start().conversation,
                ),
                now=3,
            )
        assert identity_conflict.value.code is CommandErrorCode.INTENT_CONFLICT


def test_fifo_cancel_fence_and_terminal_raw_clear(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        ingress = CommandIngress(database)
        ingress.submit_start(_start(), now=1)
        first = ingress.submit_continue(_continuation("continue-1", "c-1"), now=2)
        second = ingress.submit_continue(_continuation("continue-2", "c-2"), now=3)
        cancel = ingress.submit_cancel(
            CancelCommandIntent("deployment/phone", "key-1", "cancel-1", RunId("run-1")),
            now=4,
        )
        assert (first.accept_seq, second.accept_seq, cancel.accept_seq) == (1, 2, 3)
        assert cancel.state is CommandState.APPLIED
        assert ingress.get("start-1").state is CommandState.CANCELLED
        assert ingress.get("continue-1").state is CommandState.CANCELLED
        assert ingress.raw_payload("continue-2") is None
        assert ingress.raw_payload("cancel-1") is None
        assert ingress.snapshot("start-1").output_state is CommandOutputState.ABSENT
        assert ingress.snapshot("cancel-1").output_state is CommandOutputState.ABSENT
        with pytest.raises(CommandError) as fenced:
            ingress.submit_continue(_continuation("continue-3", "c-3"), now=5)
        assert fenced.value.code is CommandErrorCode.CANCEL_FENCE


def test_cancel_before_start_is_not_found_and_reserves_nothing(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        ingress = CommandIngress(database)
        with pytest.raises(CommandError) as missing:
            ingress.submit_cancel(
                CancelCommandIntent(
                    "deployment/phone", "key-1", "cancel-too-early", RunId("run-1")
                ),
                now=1,
            )
        assert missing.value.code is CommandErrorCode.NOT_FOUND
        assert (
            database.connection.execute("SELECT COUNT(*) FROM conversation_commands").fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM conversation_command_namespaces"
            ).fetchone()[0]
            == 0
        )


def test_claim_epoch_expiry_fifo_and_stale_result_fence(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        ingress = CommandIngress(database)
        ingress.submit_start(_start(), now=1)
        claim1 = ingress.claim_next(owner_id="runtime-1", now=2, lease_seconds=2)
        assert claim1 is not None and claim1.claim_epoch == 1
        assert ingress.claim_next(owner_id="runtime-2", now=3, lease_seconds=2) is None
        claim2 = ingress.claim_next(owner_id="runtime-2", now=4, lease_seconds=2)
        assert claim2 is not None and claim2.claim_epoch == 2
        with pytest.raises(CommandError):
            ingress.transition(
                claim1,
                expected=CommandState.ACCEPTED,
                target=CommandState.CONTEXT_CALL_INTENT,
                now=4,
            )
        entered = ingress.transition(
            claim2,
            expected=CommandState.ACCEPTED,
            target=CommandState.CONTEXT_CALL_INTENT,
            now=4,
        )
        assert entered.state is CommandState.CONTEXT_CALL_INTENT
        applied = ingress.transition(
            claim2,
            expected=CommandState.CONTEXT_CALL_INTENT,
            target=CommandState.APPLIED,
            now=5,
        )
        assert applied.state is CommandState.APPLIED
        assert ingress.raw_payload("start-1") is None


def test_legacy_reservation_is_permanent_and_mixed_mode_closed(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        ingress = CommandIngress(database)
        ingress.reserve_legacy_run(
            namespace="deployment/phone",
            projection_key_id="key-1",
            run_id="run-1",
            intent_hash="a" * 64,
            now=1,
        )
        ingress.reserve_legacy_run(
            namespace="deployment/phone",
            projection_key_id="key-1",
            run_id="run-1",
            intent_hash="a" * 64,
            now=1000,
        )
        with pytest.raises(CommandError) as mixed:
            ingress.submit_start(_start(), now=1001)
            assert mixed.value.code is CommandErrorCode.RUN_MODE_CONFLICT


def test_legacy_and_command_start_race_has_exactly_one_mode_winner(tmp_path: Path) -> None:
    for index in range(8):
        path = tmp_path / f"race-{index}.db"
        with Database.open(path):
            pass
        barrier = threading.Barrier(2)

        def legacy() -> str:
            with Database.open(path) as database:
                barrier.wait()
                try:
                    CommandIngress(database).reserve_legacy_run(
                        namespace="deployment/phone",
                        projection_key_id="key-1",
                        run_id="run-1",
                        intent_hash="a" * 64,
                        now=1,
                    )
                    return "legacy"
                except CommandError as error:
                    assert error.code is CommandErrorCode.RUN_MODE_CONFLICT
                    return "conflict"

        def command() -> str:
            with Database.open(path) as database:
                barrier.wait()
                try:
                    CommandIngress(database).submit_start(_start(), now=1)
                    return "command"
                except CommandError as error:
                    assert error.code is CommandErrorCode.RUN_MODE_CONFLICT
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            legacy_result = pool.submit(legacy)
            command_result = pool.submit(command)
            results = (legacy_result.result(), command_result.result())
        assert sorted(results) in (["command", "conflict"], ["conflict", "legacy"])
        with Database.open(path) as database:
            mode = database.connection.execute(
                "SELECT api_mode FROM conversation_run_modes WHERE run_id='run-1'"
            ).fetchone()
            assert mode is not None and str(mode[0]) in {"legacy", "command"}


def test_normal_open_rejects_v4_without_writing_any_bytes(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0004_fresh.sql")
        .read_text(encoding="utf-8")
    )
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    connection = sqlite3.connect(path, isolation_level=None)
    connection.executescript(
        "BEGIN IMMEDIATE;"
        "CREATE TABLE sdk_schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL "
        "UNIQUE, checksum TEXT NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP) STRICT;"
        + sql
        + "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES "
        + f"(4,'0004_fresh','{checksum}');COMMIT;"
    )
    connection.close()
    before = path.read_bytes()
    before_stat = path.stat()
    with pytest.raises(ExecutionSchemaIncompatible, match="fresh schema v5"):
        Database.open(path, wal=True)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()
    assert not path.with_name(path.name + "-journal").exists()


def test_start_apply_and_command_settlement_share_one_transaction(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        ingress = CommandIngress(database)
        ingress.submit_start(_start(), now=1)
        claim = ingress.claim_next(owner_id="runtime", now=2, lease_seconds=10)
        assert claim is not None
        ingress.transition(
            claim,
            expected=CommandState.ACCEPTED,
            target=CommandState.CONTEXT_CALL_INTENT,
            now=2,
        )
        ingress.transition(
            claim,
            expected=CommandState.CONTEXT_CALL_INTENT,
            target=CommandState.CONTEXT_READY,
            now=3,
        )
        uow = SqliteExecutionUnitOfWork(database)

        def cut(point: str) -> None:
            if point == "root_start.session.after_write":
                raise RuntimeError("cut")

        with pytest.raises(RuntimeError, match="cut"):
            uow.apply_start_command(
                claim,
                execution_session_id="session",
                request_id="request-1",
                profile_key="root",
                driver_kind="react",
                snapshot={"schema_version": 5},
                event_id="event-1",
                now=4,
                user_id="actor",
                fault=cut,
            )
        assert uow.read_run("run-1") is None
        assert ingress.get("start-1").state is CommandState.CONTEXT_READY
        applied = uow.apply_start_command(
            claim,
            execution_session_id="session",
            request_id="request-1",
            profile_key="root",
            driver_kind="react",
            snapshot={"schema_version": 5},
            event_id="event-1",
            now=5,
            user_id="actor",
        )
        assert applied.run_id == "run-1"
        assert ingress.get("start-1").state is CommandState.APPLIED
        assert ingress.raw_payload("start-1") is None
