# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from simple_harness.contracts import RequestId, RunId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.budget import (
    BudgetExceededError,
    BudgetPolicy,
    BudgetUnknownError,
    FrozenPriceEstimator,
)
from simple_harness.execution.dispatch import (
    ProviderInvocationConflictError,
    ProviderInvocationCoordinator,
    ProviderInvocationUnknownError,
)
from simple_harness.execution.provider_invocations import ProviderInvocationState
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import CancelToken, ProviderRequest

from .provider_ledger_fakes import RecordingProvider


def _request(request_id: str = "provider-request-1") -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId(request_id),
        messages=(Message(role=MessageRole.USER, content="hello"),),
        max_output_tokens=100,
    )


def _estimator(*, output_rate: int = 2_000_000) -> FrozenPriceEstimator:
    return FrozenPriceEstimator(
        snapshot_id="prices-1",
        pricing_key="model-1",
        input_micros_per_million_tokens=1_000_000,
        output_micros_per_million_tokens=output_rate,
    )


def _create_run(uow: SqliteExecutionUnitOfWork):
    uow.create_with_start_snapshot(
        execution_session_id="session-1",
        run_id="run-1",
        request_id="root-request-1",
        profile_key="agent.general",
        driver_kind="react",
        snapshot={"prompt": "hello"},
        event_id="event-1",
        now=1.0,
    )
    return uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="provider-test",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )[1]


def _lease(uow: SqliteExecutionUnitOfWork):
    return uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="provider-test",
        namespace="runtime.kernel",
        now=2.0,
        lease_ttl_seconds=100.0,
    )[1]


def _coordinator(uow, provider, *, output_rate=2_000_000, hard_cap=50_000):
    return ProviderInvocationCoordinator(
        uow=uow,
        provider=provider,
        budget_policy=BudgetPolicy(hard_cap_micros=hard_cap),
        estimator=_estimator(output_rate=output_rate),
        clock=lambda: 10.0,
    )


def test_sqlite_reopen_returns_durable_response_without_replay(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    first_provider = RecordingProvider()
    with Database.open(path, wal=True) as database:
        uow = SqliteExecutionUnitOfWork(database)
        lease = _create_run(uow)
        first = asyncio.run(
            _coordinator(uow, first_provider).invoke(
                RunId("run-1"), _request(), cancel=CancelToken(), execution_lease=lease
            )
        )
        row = database.connection.execute(
            "SELECT state, target_digest, estimator_digest FROM provider_invocations"
        ).fetchone()
        invocation_id = _ids(database)[0]
        stored = uow.read_provider_invocation(invocation_id)
        assert stored is not None
        assert tuple(row) == (
            "succeeded",
            stored.target_digest,
            _estimator().snapshot_digest,
        )

    second_provider = RecordingProvider()
    with Database.open(path, wal=True) as reopened:
        second = asyncio.run(
            _coordinator(SqliteExecutionUnitOfWork(reopened), second_provider).invoke(
                RunId("run-1"),
                _request(),
                cancel=CancelToken(),
                execution_lease=_lease(SqliteExecutionUnitOfWork(reopened)),
            )
        )
    assert first == second
    assert first_provider.calls == 1
    assert second_provider.calls == 0


def _ids(database: Database) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in database.connection.execute(
            "SELECT invocation_id FROM provider_invocations"
        )
    )


@pytest.mark.parametrize("changed", ["target", "estimator", "content"])
def test_restart_config_or_content_drift_keeps_one_row_and_zero_transport(
    tmp_path: Path, changed: str
) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        lease = _create_run(uow)
        provider = RecordingProvider()
        coordinator = _coordinator(uow, provider)
        record = asyncio.run(
            coordinator.prepare_claim(RunId("run-1"), _request(), execution_lease=lease)
        )
        uow.hand_off_provider_invocation(
            record.invocation_id,
            expected_version=record.version,
            handed_off_at=2.0,
            execution_lease=lease,
        )

    provider = RecordingProvider(
        endpoint_identity=(
            "https://different.invalid/v1/chat/completions"
            if changed == "target"
            else "https://provider.invalid/v1/chat/completions"
        )
    )
    request = _request()
    if changed == "content":
        request = ProviderRequest(
            request_id=request.request_id,
            messages=(Message(role=MessageRole.USER, content="different"),),
            max_output_tokens=100,
        )
    with Database.open(path) as reopened:
        coordinator = _coordinator(
            SqliteExecutionUnitOfWork(reopened),
            provider,
            output_rate=9_000_000 if changed == "estimator" else 2_000_000,
        )
        expected = (
            ProviderInvocationConflictError
            if changed in {"target", "estimator", "content"}
            else ProviderInvocationUnknownError
        )
        with pytest.raises(expected):
            asyncio.run(
                coordinator.invoke(
                    RunId("run-1"),
                    request,
                    cancel=CancelToken(),
                    execution_lease=_lease(SqliteExecutionUnitOfWork(reopened)),
                )
            )
        assert (
            reopened.connection.execute(
                "SELECT count(*) FROM provider_invocations"
            ).fetchone()[0]
            == 1
        )
    assert provider.calls == 0


def test_sqlite_recovery_marks_handoff_unknown_and_hard_cap_is_atomic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path) as database:
        uow = SqliteExecutionUnitOfWork(database)
        lease = _create_run(uow)
        provider = RecordingProvider()
        coordinator = _coordinator(uow, provider)
        record = asyncio.run(
            coordinator.prepare_claim(RunId("run-1"), _request(), execution_lease=lease)
        )
        uow.hand_off_provider_invocation(
            record.invocation_id,
            expected_version=record.version,
            handed_off_at=2.0,
            execution_lease=lease,
        )

    with Database.open(path) as reopened:
        uow = SqliteExecutionUnitOfWork(reopened)
        provider = RecordingProvider()
        settled = asyncio.run(_coordinator(uow, provider).reconcile_incomplete())
        assert settled == 1
        assert uow.read_provider_invocation(record.invocation_id).state is (
            ProviderInvocationState.UNKNOWN
        )
        with pytest.raises(
            (ProviderInvocationUnknownError, BudgetExceededError, BudgetUnknownError)
        ):
            asyncio.run(
                _coordinator(uow, provider, hard_cap=1).invoke(
                    RunId("run-1"),
                    _request("provider-request-2"),
                    cancel=CancelToken(),
                    execution_lease=_lease(uow),
                )
            )
        assert provider.calls == 0


def test_provider_schema_freezes_target_and_estimator_identity(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        assert {
            "target_json",
            "target_digest",
            "estimator_json",
            "estimator_digest",
        } <= database.column_names("provider_invocations")
        indexes = database.connection.execute(
            "PRAGMA index_list(provider_invocations)"
        ).fetchall()
        unique_columns = {
            tuple(
                str(column[2])
                for column in database.connection.execute(
                    f"PRAGMA index_info('{index[1]}')"
                )
            )
            for index in indexes
            if index[2]
        }
        assert ("run_id", "request_id") in unique_columns
