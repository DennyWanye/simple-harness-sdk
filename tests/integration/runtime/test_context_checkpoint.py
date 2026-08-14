# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import pytest

from simple_harness.contracts import CallId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime import SqliteContextPort
from simple_harness.runtime.context import _append_context_in_transaction


def test_context_append_is_idempotent_reopen_safe_and_epoch_fenced(tmp_path) -> None:
    path = tmp_path / "context.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="event-created",
        now=1.0,
    )
    _, first = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=5.0,
    )
    context = SqliteContextPort(database, clock=lambda: 3.0)
    entry = Message(MessageRole.USER, "hello")
    _, bypass = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="evil-owner",
        namespace="evil",
        now=2.0,
        lease_ttl_seconds=10.0,
    )
    try:
        context.append(RunId("run-1"), bypass, 0, "evil-append", (entry,))
    except UnitOfWorkConflict:
        pass
    else:
        raise AssertionError("non-runtime namespace lease must not authorize context")
    assert context.load(RunId("run-1")).revision == 0
    stored = context.append(RunId("run-1"), first, 0, "append-1", (entry,))
    assert (stored.revision, stored.messages) == (1, (entry,))
    assert context.append(RunId("run-1"), first, 0, "append-1", (entry,)) == stored
    database.close()

    reopened = Database.open(path)
    assert SqliteContextPort(reopened).load(RunId("run-1")) == stored
    uow = SqliteExecutionUnitOfWork(reopened)
    _, second = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-2",
        namespace="runtime.kernel",
        now=8.0,
        lease_ttl_seconds=5.0,
    )
    context = SqliteContextPort(reopened, clock=lambda: 9.0)
    try:
        context.append(
            RunId("run-1"),
            first,
            1,
            "stale-owner",
            (Message(MessageRole.ASSISTANT, "stale"),),
        )
    except UnitOfWorkConflict:
        pass
    else:
        raise AssertionError("stale owner epoch must be rejected")
    advanced = context.append(
        RunId("run-1"),
        second,
        1,
        "append-2",
        (Message(MessageRole.ASSISTANT, "ok"),),
    )
    assert advanced.revision == 2
    reopened.close()


def test_context_append_joins_caller_transaction_rollback_and_replay(tmp_path) -> None:
    database = Database.open(tmp_path / "context-transaction.db")
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="event-created",
        now=1.0,
    )
    _, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=10.0,
    )
    entry = Message(
        MessageRole.TOOL,
        "spawned",
        name="workflow_spawn",
        call_id=CallId("call-1"),
    )

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        database.transaction() as connection,
    ):
        appended = _append_context_in_transaction(
            connection,
            RunId("run-1"),
            lease,
            0,
            "spawn-result-1",
            (entry,),
            now=3.0,
        )
        assert appended.revision == 1
        raise RuntimeError("force rollback")

    context = SqliteContextPort(database)
    assert context.load(RunId("run-1")).revision == 0

    with database.transaction() as connection:
        committed = _append_context_in_transaction(
            connection,
            RunId("run-1"),
            lease,
            0,
            "spawn-result-1",
            (entry,),
            now=4.0,
        )
    with database.transaction() as connection:
        replayed = _append_context_in_transaction(
            connection,
            RunId("run-1"),
            lease,
            0,
            "spawn-result-1",
            (entry,),
            now=5.0,
        )
    assert replayed == committed
    assert context.load(RunId("run-1")) == committed
    database.close()
