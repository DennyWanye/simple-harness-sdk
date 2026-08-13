# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.contracts import RunId
from simple_harness.execution.budget import BudgetSnapshot
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict
from simple_harness.runtime.react_checkpoint import DurableReactCheckpoint
from simple_harness.runtime.termination import TerminationLimits


def _seed(path):
    database = Database.open(path)
    uow = SqliteExecutionUnitOfWork(database)
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={},
        event_id="run-1:created",
        now=1.0,
    )
    _, lease = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="owner-1",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )
    return database, uow, lease


def test_provider_reservation_survives_reopen_with_same_stable_identity(tmp_path) -> None:
    path = tmp_path / "checkpoint.db"
    database, uow, lease = _seed(path)
    checkpoint = DurableReactCheckpoint(uow, clock=lambda: 3.0)
    state, version = checkpoint.load_or_create(RunId("run-1"), lease)
    reserved = state.before_provider(
        TerminationLimits(), now=3.0, budget=BudgetSnapshot()
    )
    reserved, version = checkpoint.cas(RunId("run-1"), lease, version, reserved)
    database.close()

    with Database.open(path) as reopened:
        loaded, loaded_version = DurableReactCheckpoint(
            SqliteExecutionUnitOfWork(reopened), clock=lambda: 4.0
        ).load_or_create(RunId("run-1"), lease)
        assert loaded == reserved
        assert loaded_version == version
        assert loaded.provider_turns_reserved_total == 1
        assert loaded.provider_request_id == "provider-turn:1"


def test_checkpoint_cas_rejects_second_writer_without_partial_total(tmp_path) -> None:
    database, uow, lease = _seed(tmp_path / "checkpoint-cas.db")
    checkpoint = DurableReactCheckpoint(uow, clock=lambda: 3.0)
    state, version = checkpoint.load_or_create(RunId("run-1"), lease)
    first = state.before_tool_batch(
        ("calculator:a", "calculator:b"),
        TerminationLimits(),
        now=3.0,
        budget=BudgetSnapshot(),
    )
    checkpoint.cas(RunId("run-1"), lease, version, first)

    with pytest.raises(UnitOfWorkConflict, match="version CAS"):
        checkpoint.cas(RunId("run-1"), lease, version, first)

    stored = uow.read_react_checkpoint("run-1")
    assert stored is not None
    assert stored.checkpoint["tool_calls_reserved_total"] == 2
    database.close()
