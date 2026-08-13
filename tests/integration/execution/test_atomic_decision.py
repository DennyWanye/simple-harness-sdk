# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import AdmissionState, DecisionState


class InjectedFault(RuntimeError):
    pass


def _raise_at(target: str):
    def inject(point: str) -> None:
        if point == target:
            raise InjectedFault(point)

    return inject


def _create(uow: SqliteExecutionUnitOfWork):
    return uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"messages": []},
        event_id="event-root-1",
        now=1.0,
    )


START_POINTS = tuple(
    f"admission_start.{write}.{side}_write"
    for write in ("run", "admission", "event")
    for side in ("before", "after")
)
RESOLVE_POINTS = tuple(
    f"admission_resolve.{write}.{side}_write"
    for write in ("admission", "run", "event")
    for side in ("before", "after")
)
DECISION_POINTS = tuple(
    f"decision.{write}.{side}_write"
    for write in ("decision", "run", "event")
    for side in ("before", "after")
)


def _start(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.start_admission(
        admission_id="admission-1",
        run_id="run-1",
        prompt={"kind": "approval"},
        expires_at=100.0,
        event_id="event-admission-start",
        now=2.0,
        fault=fault,
    )


def _resolve(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.resolve_admission(
        admission_id="admission-1",
        state=AdmissionState.ALLOWED,
        response={"approved": True},
        event_id="event-admission-resolve",
        now=3.0,
        fault=fault,
    )


def _decision(uow: SqliteExecutionUnitOfWork, *, fault=None):
    return uow.commit_decision(
        decision_id="decision-1",
        run_id="run-1",
        kind="human_input",
        state=DecisionState.ALLOWED,
        request={"question": "continue?"},
        response={"answer": "yes"},
        event_id="event-decision",
        now=4.0,
        fault=fault,
    )


@pytest.mark.parametrize("fault_point", START_POINTS)
def test_start_admission_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        _start(uow, fault=_raise_at(fault_point))
    database.close()

    with Database.open(path) as reopened:
        assert reopened.connection.execute("SELECT state FROM runs").fetchone()[0] == "created"
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_admissions").fetchone()[0] == 0
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1


def test_start_admission_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match="admission_start.after_commit"):
        _start(uow, fault=_raise_at("admission_start.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert _start(uow).state is AdmissionState.PENDING
        assert uow.read_run("run-1").state.value == "admission_pending"  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 2


@pytest.mark.parametrize("fault_point", RESOLVE_POINTS)
def test_resolve_admission_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    _start(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        _resolve(uow, fault=_raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_admission("admission-1").state is AdmissionState.PENDING  # type: ignore[union-attr]
        assert uow.read_run("run-1").state.value == "admission_pending"  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 2


def test_resolve_admission_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    _start(uow)
    with pytest.raises(InjectedFault, match="admission_resolve.after_commit"):
        _resolve(uow, fault=_raise_at("admission_resolve.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert _resolve(uow).state is AdmissionState.ALLOWED
        assert uow.read_run("run-1").state.value == "queued"  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 3


@pytest.mark.parametrize("fault_point", DECISION_POINTS)
def test_decision_fault_reopens_all_before(tmp_path: Path, fault_point: str) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match=fault_point):
        _decision(uow, fault=_raise_at(fault_point))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert uow.read_decision("decision-1") is None
        assert uow.read_run("run-1").state.value == "created"  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1


def test_decision_after_commit_reopens_all_after(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    _create(uow)
    with pytest.raises(InjectedFault, match="decision.after_commit"):
        _decision(uow, fault=_raise_at("decision.after_commit"))
    database.close()
    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        assert _decision(uow).state is DecisionState.ALLOWED
        assert uow.read_run("run-1").state.value == "running"  # type: ignore[union-attr]
        assert reopened.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 2
