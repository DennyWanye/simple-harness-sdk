# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState, UnitOfWorkConflict


class InjectedFault(RuntimeError):
    pass


ROOT_WRITE_POINTS = (
    "root_start.session.before_write",
    "root_start.session.after_write",
    "root_start.run.before_write",
    "root_start.run.after_write",
    "root_start.snapshot.before_write",
    "root_start.snapshot.after_write",
    "root_start.event.before_write",
    "root_start.event.after_write",
)


def _raise_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return inject


def _create(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"messages": [{"role": "user", "content": "hello"}]},
        event_id="event-root-1",
        now=1.0,
        fault=fault,
    )


@pytest.mark.parametrize("fault_point", ROOT_WRITE_POINTS)
def test_root_start_fault_before_commit_reopens_as_all_before(
    tmp_path: Path, fault_point: str
) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    with pytest.raises(InjectedFault, match=fault_point):
        _create(SqliteExecutionUnitOfWork(database), fault=_raise_at(fault_point))
    database.close()

    with Database.open(path) as reopened:
        for table in ("execution_sessions", "runs", "run_start_snapshots", "run_events"):
            assert reopened.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert reopened.integrity_check() == ("ok",)
        assert reopened.foreign_key_violations() == ()


def test_root_start_fault_after_commit_reopens_as_all_after_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    with pytest.raises(InjectedFault, match="root_start.after_commit"):
        _create(
            SqliteExecutionUnitOfWork(database),
            fault=_raise_at("root_start.after_commit"),
        )
    database.close()

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert _create(uow).state is RunState.CREATED
        for table in ("execution_sessions", "runs", "run_start_snapshots", "run_events"):
            assert reopened.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_root_start_uses_immediate_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    competing = sqlite3.connect(path, timeout=0.01, isolation_level=None)

    def prove_lock(point: str) -> None:
        if point == "root_start.session.after_write":
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
            raise InjectedFault(point)

    try:
        with pytest.raises(InjectedFault):
            _create(SqliteExecutionUnitOfWork(database), fault=prove_lock)
    finally:
        competing.close()
        database.close()


def test_root_request_identity_rejects_different_replay(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        uow = SqliteExecutionUnitOfWork(database)
        _create(uow)
        with pytest.raises(UnitOfWorkConflict, match="different root intent"):
            uow.create_with_start_snapshot(
                execution_session_id="session-1",
                run_id="run-other",
                request_id="request-1",
                profile_key="agent.general",
                driver_kind="react",
                snapshot={"messages": []},
                event_id="event-other",
                now=2.0,
            )
