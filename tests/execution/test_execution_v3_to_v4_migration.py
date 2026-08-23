# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from simple_harness import AgentIdentity, CommittedTurn, MemoryScopeRef, RunId, canonical_json
from simple_harness.execution.memory_outbox import CommittedTurnSpec
from simple_harness.execution.sqlite import (
    Database,
    ExecutionMigrationError,
    LegacyDisposition,
    LegacyIdentityBinding,
    LegacyIdentityMap,
    SqliteExecutionUnitOfWork,
    migrate_execution_v3_to_v4,
)
from simple_harness.execution.uow import RunState
from simple_harness.runtime.conversation_memory import (
    ConversationMemoryIntent,
    ConversationMemoryRole,
)
from simple_harness.runtime.start_snapshot import StartSnapshot

IDENTITY = AgentIdentity("deployment-1", "household-1", "actor-1", "session-1")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _identity_map() -> LegacyIdentityMap:
    return LegacyIdentityMap.from_bindings(
        (LegacyIdentityBinding("legacy-user", "session-1", IDENTITY),)
    )


def _create_v3(path: Path, *, run_id: str, state: str) -> sqlite3.Connection:
    sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0003_fresh.sql")
        .read_text(encoding="utf-8")
    )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        "CREATE TABLE sdk_schema_migrations("
        "version INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,"
        "checksum TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);" + sql
    )
    connection.execute(
        "INSERT INTO sdk_schema_migrations(version,name,checksum) VALUES(3,'0003_fresh',?)",
        (_digest(sql),),
    )
    connection.execute("INSERT INTO execution_users VALUES('legacy-user',1.0)")
    connection.execute("INSERT INTO execution_sessions VALUES('session-1','legacy-user',1.0)")
    connection.execute(
        "INSERT INTO runs VALUES(?, 'session-1', ?, ?, NULL, 'agent.general','react',?,3,1.0,4.0)",
        (run_id, f"request-{run_id}", run_id, state),
    )
    snapshot = {
        "schema_version": 5,
        "profile_key": "agent.general",
        "driver_kind": "react",
        "turn_id": f"turn-{run_id}",
        "tool_catalog_generation": 1,
        "input": {"messages": []},
        "policy_fingerprint": None,
        "tool_catalog_fingerprint": None,
        "provider_budget_fingerprint": None,
        "conversation": {
            "user_id": "legacy-user",
            "session_id": "session-1",
            "message": {"role": "user", "content": "root question", "metadata": {}},
            "memory_text": "root question",
        },
        "context_preparation_mode": None,
        "context_stage_id": None,
        "context_stage_hash": None,
        "prepared_context": None,
        "workflow_admission": None,
    }
    snapshot_json = canonical_json(snapshot)
    connection.execute(
        "INSERT INTO run_start_snapshots VALUES(?,?,?,1.0)",
        (run_id, snapshot_json, _digest(snapshot_json)),
    )
    connection.commit()
    return connection


def _continuation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    continuation_id: str,
    fifo: int,
    terminal: bool = False,
) -> None:
    payload = canonical_json(
        {
            "kind": "conversation_user",
            "conversation": {
                "message": {
                    "role": "user",
                    "content": f"question {continuation_id}",
                    "metadata": {},
                },
                "memory_text": f"question {continuation_id}",
            },
            "prepared_context": {"provider_messages": []},
        }
    )
    connection.execute(
        "INSERT INTO continuations(continuation_id,run_id,fifo_seq,payload_json,state,version,"
        "claimed_by,runtime_lease_epoch,claim_epoch,ack_receipt_id,created_at,claimed_at,acked_at,"
        "context_stage_id,context_stage_hash) VALUES(?,?,?,?,'claimed',1,'runtime-1',1,1,NULL,"
        "?, ?,NULL,NULL,NULL)",
        (continuation_id, run_id, fifo, payload, 1.0 + fifo, 1.0 + fifo),
    )
    terminal_event_id = f"{run_id}:terminal:continuation:{continuation_id}:1:completed:event"
    receipt_id = (
        terminal_event_id[: -len("event")] + "receipt" if terminal else f"receipt-{continuation_id}"
    )
    connection.execute(
        "INSERT INTO continuation_progress_receipts VALUES(?,?,?,'runtime-1',1,1,?,?)",
        (receipt_id, continuation_id, run_id, "a" * 64, 2.0 + fifo),
    )
    connection.execute(
        "UPDATE continuations SET state='acked',ack_receipt_id=?,acked_at=? "
        "WHERE continuation_id=?",
        (receipt_id, 2.0 + fifo, continuation_id),
    )
    if terminal:
        connection.execute(
            "INSERT INTO run_events VALUES(?,?,1,'run.completed',?,?)",
            (
                terminal_event_id,
                run_id,
                canonical_json({"answer": "done"}),
                4.0,
            ),
        )


def _memory(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    source_event_id: str,
    role: ConversationMemoryRole,
    text: str,
    continuation_id: str | None,
    created_at: float,
) -> None:
    intent = ConversationMemoryIntent(
        source_event_id,
        "legacy-user",
        "session-1",
        role,
        text,
    )
    connection.execute(
        "INSERT INTO memory_outbox VALUES(?,?,?,?,?,?,?,?,?,'applied',1,1,NULL,NULL,NULL,"
        "0,NULL,?,?,?)",
        (
            source_event_id,
            source_event_id,
            run_id,
            continuation_id,
            "legacy-user",
            "session-1",
            role.value,
            text,
            intent.payload_hash,
            created_at,
            created_at,
            created_at,
        ),
    )


def _seed_completed(path: Path, *, ambiguous: bool = False) -> None:
    connection = _create_v3(path, run_id="run-completed", state="completed")
    _continuation(
        connection,
        run_id="run-completed",
        continuation_id="continuation-1",
        fifo=1,
    )
    _continuation(
        connection,
        run_id="run-completed",
        continuation_id="continuation-2",
        fifo=2,
        terminal=True,
    )
    connection.execute(
        "INSERT INTO delivery_outbox VALUES('delivery-1','run-completed','presenter',"
        "'delivery-key-1',?,'delivered',1,3.0,NULL,4.0)",
        (canonical_json({"answer": "done"}),),
    )
    connection.execute(
        "INSERT INTO context_preparation_staging(stage_id,kind,identity_key,user_id,session_id,"
        "input_hash,mode,state,lease_owner,lease_token,lease_expires_at,memory_result_id,"
        "memory_result_hash,private_snapshot,private_snapshot_hash,consumed_run_id,"
        "consumed_continuation_id,created_at,updated_at) VALUES('legacy-stage','continuation',"
        "'legacy-stage-identity','legacy-user','session-1',?,'sdk_prepared','consumed',"
        "NULL,NULL,NULL,NULL,NULL,NULL,?,NULL,'continuation-1',2.0,3.0)",
        ("b" * 64, "c" * 64),
    )
    if ambiguous:
        connection.execute(
            "INSERT INTO run_events VALUES('run-completed:terminal:completed','run-completed',2,"
            "'run.completed',?,4.1)",
            (canonical_json({"answer": "other"}),),
        )
    _memory(
        connection,
        run_id="run-completed",
        source_event_id="harness-memory/v1/user/run-completed",
        role=ConversationMemoryRole.USER,
        text="root question",
        continuation_id=None,
        created_at=1.0,
    )
    for index in (1, 2):
        _memory(
            connection,
            run_id="run-completed",
            source_event_id=f"harness-memory/v1/user-continuation/continuation-{index}",
            role=ConversationMemoryRole.USER,
            text=f"question continuation-{index}",
            continuation_id=f"continuation-{index}",
            created_at=1.0 + index,
        )
    _memory(
        connection,
        run_id="run-completed",
        source_event_id="harness-memory/v1/assistant/run-completed",
        role=ConversationMemoryRole.ASSISTANT,
        text="final answer",
        continuation_id=None,
        created_at=4.0,
    )
    event_id = "run-completed:terminal:continuation:continuation-2:1:completed:event"
    event_payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM run_events WHERE event_id=?", (event_id,)
        ).fetchone()[0]
    )
    assistant_hash = connection.execute(
        "SELECT payload_hash FROM memory_outbox WHERE role='assistant'"
    ).fetchone()[0]
    outcome_hash = _digest(
        canonical_json(
            {
                "run_id": "run-completed",
                "expected_version": 2,
                "terminal_state": "completed",
                "event_id": event_id,
                "payload": event_payload,
                "deliveries": [
                    {
                        "delivery_id": "delivery-1",
                        "sink_kind": "presenter",
                        "idempotency_key": "delivery-key-1",
                        "payload": {"answer": "done"},
                    }
                ],
                "memory_intent_hash": assistant_hash,
            }
        )
    )
    connection.execute(
        "UPDATE continuation_progress_receipts SET outcome_hash=? WHERE continuation_id=?",
        (outcome_hash, "continuation-2"),
    )
    connection.commit()
    connection.close()


def _seed_nonterminal(path: Path, *, run_id: str = "run-active") -> None:
    connection = _create_v3(path, run_id=run_id, state="waiting")
    _continuation(
        connection,
        run_id=run_id,
        continuation_id="legacy-continuation",
        fifo=1,
    )
    _memory(
        connection,
        run_id=run_id,
        source_event_id=f"harness-memory/v1/user/{run_id}",
        role=ConversationMemoryRole.USER,
        text="root question",
        continuation_id=None,
        created_at=1.0,
    )
    _memory(
        connection,
        run_id=run_id,
        source_event_id="harness-memory/v1/user-continuation/legacy-continuation",
        role=ConversationMemoryRole.USER,
        text="legacy latest",
        continuation_id="legacy-continuation",
        created_at=2.0,
    )
    connection.commit()
    connection.close()


def _seed_root_completed(path: Path) -> None:
    connection = _create_v3(path, run_id="run-root", state="completed")
    connection.execute(
        "INSERT INTO run_events VALUES('run-root:terminal:completed','run-root',1,"
        "'run.completed',?,3.0)",
        (canonical_json({"answer": "root answer"}),),
    )
    _memory(
        connection,
        run_id="run-root",
        source_event_id="harness-memory/v1/user/run-root",
        role=ConversationMemoryRole.USER,
        text="root question",
        continuation_id=None,
        created_at=1.0,
    )
    _memory(
        connection,
        run_id="run-root",
        source_event_id="harness-memory/v1/assistant/run-root",
        role=ConversationMemoryRole.ASSISTANT,
        text="root answer",
        continuation_id=None,
        created_at=3.0,
    )
    connection.commit()
    connection.close()


def _stage(
    database: Database,
    *,
    stage_id: str,
    continuation_id: str,
    now: float,
) -> tuple[str, dict[str, list[object]]]:
    prepared = {"provider_messages": []}
    raw = canonical_json(prepared).encode()
    stage_hash = hashlib.sha256(raw).hexdigest()
    database.connection.execute(
        "INSERT INTO context_preparation_staging(stage_id,kind,identity_key,user_id,session_id,"
        "input_hash,mode,state,lease_owner,lease_token,lease_expires_at,memory_result_id,"
        "memory_result_hash,memory_query_hash,memory_write_fence,outcome,error_code,"
        "product_result_hash,source_snapshot_ref,turn_started_at,private_snapshot,"
        "private_snapshot_hash,consumed_run_id,consumed_continuation_id,created_at,updated_at) "
        "VALUES(?,'continuation',?,'actor-1','session-1',?,'sdk_prepared','staged',"
        "NULL,NULL,NULL,NULL,NULL,NULL,?,'degraded_empty',NULL,NULL,NULL,?,?,?,NULL,NULL,?,?)",
        (
            stage_id,
            f"identity-{stage_id}",
            "a" * 64,
            f"fence-{stage_id}",
            now,
            raw,
            stage_hash,
            now,
            now,
        ),
    )
    return stage_hash, prepared


def _enqueue(
    uow: SqliteExecutionUnitOfWork,
    database: Database,
    *,
    run_id: str,
    continuation_id: str,
    now: float,
    fault=None,  # type: ignore[no-untyped-def]
) -> None:
    stage_id = f"stage-{continuation_id}"
    stage_hash, prepared = _stage(
        database,
        stage_id=stage_id,
        continuation_id=continuation_id,
        now=now,
    )
    uow.enqueue_continuation(
        continuation_id=continuation_id,
        run_id=run_id,
        payload={
            "kind": "conversation_user",
            "conversation": {
                "message": {
                    "role": "user",
                    "content": f"question {continuation_id}",
                    "metadata": {},
                },
                "memory_text": f"question {continuation_id}",
            },
            "prepared_context": prepared,
        },
        context_stage_id=stage_id,
        context_stage_hash=stage_hash,
        now=now,
        fault=fault,
    )


def test_completed_null_continuation_resolves_unique_pair_and_preserves_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    backup = tmp_path / "execution.v3.backup"
    _seed_completed(path)
    manifest = migrate_execution_v3_to_v4(
        path,
        backup_path=backup,
        identity_map=_identity_map(),
    )
    assert backup.is_file()
    dispositions = [entry.disposition for entry in manifest.entries]
    assert dispositions.count(LegacyDisposition.KEEP_COMPLETED_PAIR) == 2
    assert dispositions.count(LegacyDisposition.SUPPRESS_TENTATIVE) == 2
    kept = [
        entry
        for entry in manifest.entries
        if entry.disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
    ]
    assert {entry.causal_continuation_id for entry in kept} == {"continuation-2"}
    assert {entry.causal_claim_epoch for entry in kept} == {1}
    assert type(manifest).from_json(manifest.to_json()) == manifest
    tampered = manifest.to_json()
    tampered["source_database_hash"] = "0" * 64
    with pytest.raises(ValueError, match="digest differs"):
        type(manifest).from_json(tampered)
    with Database.open(path) as database:
        assert database.schema_version == 5
        assert database.connection.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 2
        assert (
            database.connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 1
        )
        assert (
            database.connection.execute(
                "SELECT consumed_continuation_id FROM context_preparation_staging"
            ).fetchone()[0]
            == "continuation-1"
        )
        assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 1
        snapshot = json.loads(
            database.connection.execute(
                "SELECT snapshot_json FROM run_start_snapshots WHERE run_id='run-completed'"
            ).fetchone()[0]
        )
        assert StartSnapshot.from_json(snapshot).conversation.identity == IDENTITY  # type: ignore[union-attr]


def test_identity_map_can_rename_target_actor_and_session_without_dangling_legacy_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "renamed.db"
    _seed_completed(path)
    renamed = AgentIdentity("deployment-new", "household-new", "actor-new", "session-new")
    identity_map = LegacyIdentityMap.from_bindings(
        (LegacyIdentityBinding("legacy-user", "session-1", renamed),)
    )

    manifest = migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / "renamed.backup",
        identity_map=identity_map,
    )

    kept = next(
        entry
        for entry in manifest.entries
        if entry.disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
    )
    assert CommittedTurn.from_json(kept.canonical_turn).identity == renamed  # type: ignore[arg-type]
    with Database.open(path) as database:
        assert tuple(
            database.connection.execute(
                "SELECT session_id,user_id FROM execution_sessions"
            ).fetchone()
        ) == ("session-new", "actor-new")
        assert (
            database.connection.execute(
                "SELECT execution_session_id FROM runs WHERE run_id='run-completed'"
            ).fetchone()[0]
            == "session-new"
        )
        assert tuple(
            database.connection.execute(
                "SELECT session_id,actor_id FROM agent_identity_bindings"
            ).fetchone()
        ) == ("session-new", "actor-new")
        assert tuple(
            database.connection.execute(
                "SELECT user_id,session_id FROM context_preparation_staging"
            ).fetchone()
        ) == ("actor-new", "session-new")
        assert tuple(
            database.connection.execute("SELECT actor_id,session_id FROM memory_outbox").fetchone()
        ) == ("actor-new", "session-new")
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM execution_users WHERE user_id='legacy-user'"
            ).fetchone()[0]
            == 0
        )
        snapshot = json.loads(
            database.connection.execute(
                "SELECT snapshot_json FROM run_start_snapshots WHERE run_id='run-completed'"
            ).fetchone()[0]
        )
        assert StartSnapshot.from_json(snapshot).conversation.identity == renamed  # type: ignore[union-attr]


def test_completed_root_migrates_exact_pair(tmp_path: Path) -> None:
    path = tmp_path / "root.db"
    _seed_root_completed(path)
    manifest = migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / "root.backup",
        identity_map=_identity_map(),
    )
    assert {entry.disposition for entry in manifest.entries} == {
        LegacyDisposition.KEEP_COMPLETED_PAIR
    }
    assert {entry.turn_id for entry in manifest.entries} == {"turn-run-root"}
    with Database.open(path) as database:
        row = database.connection.execute("SELECT turn_id,state FROM memory_outbox").fetchone()
        assert tuple(row) == ("turn-run-root", "pending")


@pytest.mark.parametrize("state", ("failed", "cancelled"))
def test_legacy_failed_cancelled_suppress_all_and_create_zero_pair(
    tmp_path: Path, state: str
) -> None:
    path = tmp_path / f"legacy-{state}.db"
    connection = _create_v3(path, run_id=f"run-{state}", state=state)
    _memory(
        connection,
        run_id=f"run-{state}",
        source_event_id=f"harness-memory/v1/user/run-{state}",
        role=ConversationMemoryRole.USER,
        text="tentative question",
        continuation_id=None,
        created_at=1.0,
    )
    connection.commit()
    connection.close()
    manifest = migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / f"legacy-{state}.backup",
        identity_map=_identity_map(),
    )
    assert [entry.disposition for entry in manifest.entries] == [
        LegacyDisposition.SUPPRESS_TERMINAL
    ]
    with Database.open(path) as database:
        assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
        assert (
            database.connection.execute("SELECT COUNT(*) FROM legacy_turn_cursors").fetchone()[0]
            == 0
        )


def test_null_continuation_ambiguity_fails_closed_without_replacing_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous.db"
    backup = tmp_path / "ambiguous.v3.backup"
    _seed_completed(path, ambiguous=True)
    before = path.read_bytes()
    with pytest.raises(ExecutionMigrationError, match="terminal_event_ambiguous"):
        migrate_execution_v3_to_v4(
            path,
            backup_path=backup,
            identity_map=_identity_map(),
        )
    assert path.read_bytes() == before
    assert (
        sqlite3.connect(path).execute("SELECT version FROM sdk_schema_migrations").fetchone()[0]
        == 3
    )


def test_missing_or_extra_identity_mapping_fails_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "identity.db"
    backup = tmp_path / "identity.backup"
    _seed_nonterminal(path)
    wrong = LegacyIdentityMap.from_bindings(
        (
            LegacyIdentityBinding(
                "another-user",
                "another-session",
                AgentIdentity(
                    "deployment-1",
                    "household-1",
                    "another-user",
                    "another-session",
                ),
            ),
        )
    )
    with pytest.raises(ExecutionMigrationError, match="identity_map_incomplete"):
        migrate_execution_v3_to_v4(path, backup_path=backup, identity_map=wrong)
    assert not backup.exists()
    assert (
        sqlite3.connect(path).execute("SELECT version FROM sdk_schema_migrations").fetchone()[0]
        == 3
    )


@pytest.mark.parametrize(
    "fault_point",
    (
        "execution_migration.table.runs.after_copy",
        "execution_migration.table.context_preparation_staging.after_copy",
        "execution_migration.before_replace",
    ),
)
def test_copy_and_pre_replace_faults_leave_original_v3(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / f"migration-{fault_point}.db"
    backup = tmp_path / f"migration-{fault_point}.backup"
    _seed_nonterminal(path)
    before = path.read_bytes()

    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=fault_point):
        migrate_execution_v3_to_v4(
            path,
            backup_path=backup,
            identity_map=_identity_map(),
            fault=inject,
        )
    assert path.read_bytes() == before
    assert (
        sqlite3.connect(path).execute("SELECT version FROM sdk_schema_migrations").fetchone()[0]
        == 3
    )


@pytest.mark.parametrize(
    "fault_point",
    (
        "continuation_enqueue.legacy_disposition.before_write",
        "continuation_enqueue.legacy_disposition.after_write",
        "continuation_enqueue.legacy_cursor.before_write",
        "continuation_enqueue.legacy_cursor.after_write",
    ),
)
def test_cursor_supersession_all_write_faults_roll_back(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / f"{fault_point}.db"
    _seed_nonterminal(path)
    migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / f"{fault_point}.backup",
        identity_map=_identity_map(),
    )
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)

        def inject(point: str) -> None:
            if point == fault_point:
                raise RuntimeError(point)

        with pytest.raises(RuntimeError, match=fault_point):
            _enqueue(
                uow,
                database,
                run_id="run-active",
                continuation_id="continuation-new",
                now=10.0,
                fault=inject,
            )
        cursor = uow.read_legacy_turn_cursor("run-active")
        assert cursor is not None and cursor.cursor_version == 1
        assert cursor.source_namespace == "legacy-source"
        assert uow.read_continuation("continuation-new") is None


def test_two_continuations_supersede_and_terminal_consumes_latest_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.db"
    _seed_nonterminal(path)
    migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / "active.backup",
        identity_map=_identity_map(),
    )
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        _enqueue(
            uow,
            database,
            run_id="run-active",
            continuation_id="continuation-2",
            now=10.0,
        )
        _enqueue(
            uow,
            database,
            run_id="run-active",
            continuation_id="continuation-3",
            now=11.0,
        )
        cursor = uow.read_legacy_turn_cursor("run-active")
        assert cursor is not None and cursor.cursor_version == 3
        assert cursor.turn_id == "continuation-3"
        assert cursor.user_text == "question continuation-3"
        dispositions = dict(
            database.connection.execute(
                "SELECT source_key,disposition FROM legacy_memory_dispositions"
            ).fetchall()
        )
        assert (
            dispositions["legacy-source:harness-memory/v1/user-continuation/legacy-continuation"]
            == LegacyDisposition.SUPPRESS_TENTATIVE.value
        )
        assert (
            dispositions["turn-input:continuation-2"] == LegacyDisposition.SUPPRESS_TENTATIVE.value
        )
        _, lease = uow.claim_runtime_activation(
            run_id="run-active",
            owner_id="runtime-new",
            namespace="runtime.kernel",
            now=12.0,
            lease_ttl_seconds=100.0,
        )
        fence = asyncio.run(uow.acquire(RunId("run-active"), lease, now=12.0))
        run = uow.read_run("run-active")
        assert run is not None
        spec = CommittedTurnSpec.from_domain(
            CommittedTurn(
                cursor.turn_id,
                IDENTITY,
                cursor.user_text,
                "final answer",
                MemoryScopeRef.personal("actor-1"),
                cursor.write_fence,
                cursor.turn_started_at,
            )
        )
        uow.commit_root_terminal_with_deliveries(
            run_id="run-active",
            expected_version=run.version,
            terminal_state=RunState.COMPLETED,
            event_id="run-active:terminal:completed",
            terminal_payload={"answer": "final answer"},
            deliveries=(),
            fence=fence,
            execution_lease=lease,
            terminal_fence_receipt_ref="receipt://terminal",
            now=13.0,
            committed_turn=spec,
            legacy_cursor_version=cursor.cursor_version,
        )
        consumed = uow.read_legacy_turn_cursor("run-active")
        assert consumed is not None and consumed.state == "consumed"
        assert consumed.committed_turn_hash == spec.payload_hash
        rows = database.connection.execute(
            "SELECT turn_id,payload_hash FROM memory_outbox WHERE run_id='run-active'"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("continuation-3", spec.payload_hash)]


@pytest.mark.parametrize(
    "fault_point,persisted",
    (
        ("root_terminal.legacy_cursor.before_write", False),
        ("root_terminal.legacy_cursor.after_write", False),
        ("root_terminal.after_commit", True),
    ),
)
def test_terminal_cursor_all_crash_windows_are_atomic(
    tmp_path: Path, fault_point: str, persisted: bool
) -> None:
    path = tmp_path / f"terminal-{fault_point}.db"
    _seed_nonterminal(path)
    migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / f"terminal-{fault_point}.backup",
        identity_map=_identity_map(),
    )
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    cursor = uow.read_legacy_turn_cursor("run-active")
    assert cursor is not None
    _, lease = uow.claim_runtime_activation(
        run_id="run-active",
        owner_id="runtime-new",
        namespace="runtime.kernel",
        now=10.0,
        lease_ttl_seconds=100.0,
    )
    fence = asyncio.run(uow.acquire(RunId("run-active"), lease, now=10.0))
    run = uow.read_run("run-active")
    assert run is not None
    spec = CommittedTurnSpec.from_domain(
        CommittedTurn(
            cursor.turn_id,
            IDENTITY,
            cursor.user_text,
            "final answer",
            MemoryScopeRef.personal("actor-1"),
            cursor.write_fence,
            cursor.turn_started_at,
        )
    )

    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=fault_point):
        uow.commit_root_terminal_with_deliveries(
            run_id="run-active",
            expected_version=run.version,
            terminal_state=RunState.COMPLETED,
            event_id="run-active:terminal:completed",
            terminal_payload={"answer": "final answer"},
            deliveries=(),
            fence=fence,
            execution_lease=lease,
            terminal_fence_receipt_ref="receipt://terminal",
            now=11.0,
            committed_turn=spec,
            legacy_cursor_version=cursor.cursor_version,
            fault=inject,
        )
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        stored_cursor = uow.read_legacy_turn_cursor("run-active")
        assert stored_cursor is not None
        assert stored_cursor.state == ("consumed" if persisted else "active")
        assert reopened.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == (
            1 if persisted else 0
        )
        assert uow.read_run("run-active").state is (  # type: ignore[union-attr]
            RunState.COMPLETED if persisted else RunState.WAITING
        )
        if persisted:
            replay = uow.commit_root_terminal_with_deliveries(
                run_id="run-active",
                expected_version=run.version,
                terminal_state=RunState.COMPLETED,
                event_id="run-active:terminal:completed",
                terminal_payload={"answer": "final answer"},
                deliveries=(),
                fence=fence,
                execution_lease=lease,
                terminal_fence_receipt_ref="receipt://terminal",
                now=12.0,
                committed_turn=spec,
                legacy_cursor_version=cursor.cursor_version,
            )
            assert replay.run.state is RunState.COMPLETED


@pytest.mark.parametrize("terminal_state", (RunState.FAILED, RunState.CANCELLED))
def test_migrated_cursor_failure_consumes_without_pair(
    tmp_path: Path, terminal_state: RunState
) -> None:
    path = tmp_path / f"{terminal_state.value}.db"
    _seed_nonterminal(path, run_id=f"run-{terminal_state.value}")
    migrate_execution_v3_to_v4(
        path,
        backup_path=tmp_path / f"{terminal_state.value}.backup",
        identity_map=_identity_map(),
    )
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        run_id = f"run-{terminal_state.value}"
        cursor = uow.read_legacy_turn_cursor(run_id)
        assert cursor is not None
        _, lease = uow.claim_runtime_activation(
            run_id=run_id,
            owner_id="runtime-new",
            namespace="runtime.kernel",
            now=10.0,
            lease_ttl_seconds=100.0,
        )
        fence = asyncio.run(uow.acquire(RunId(run_id), lease, now=10.0))
        run = uow.read_run(run_id)
        assert run is not None
        uow.commit_root_terminal_with_deliveries(
            run_id=run_id,
            expected_version=run.version,
            terminal_state=terminal_state,
            event_id=f"{run_id}:terminal:{terminal_state.value}",
            terminal_payload={},
            deliveries=(),
            fence=fence,
            execution_lease=lease,
            terminal_fence_receipt_ref="receipt://terminal",
            now=11.0,
            committed_turn=None,
            legacy_cursor_version=cursor.cursor_version,
        )
        assert database.connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
        assert uow.read_legacy_turn_cursor(run_id).state == "consumed"  # type: ignore[union-attr]


def test_after_replace_fault_restores_exact_v3_database(tmp_path: Path) -> None:
    path = tmp_path / "restore.db"
    backup = tmp_path / "restore.backup"
    _seed_nonterminal(path)
    before = path.read_bytes()

    def inject(point: str) -> None:
        if point == "execution_migration.after_replace":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="after_replace"):
        migrate_execution_v3_to_v4(
            path,
            backup_path=backup,
            identity_map=_identity_map(),
            fault=inject,
        )
    assert path.read_bytes() == before
    assert (
        sqlite3.connect(path).execute("SELECT version FROM sdk_schema_migrations").fetchone()[0]
        == 3
    )
