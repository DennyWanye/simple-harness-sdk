# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from simple_harness.contracts import CallId, EffectId, RunId
from simple_harness.execution import EffectState, effect_request_hash
from simple_harness.execution.effects import TaskExecutionEnvelope
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.tools import ToolResult


class InjectedCrash(RuntimeError):
    pass


def _uow(path: Path) -> tuple[Database, SqliteExecutionUnitOfWork]:
    database = Database.open(path, wal=True)
    uow = SqliteExecutionUnitOfWork(database)
    if uow.read_run("run-1") is None:
        uow.create_with_start_snapshot(
            execution_session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"catalog_generation": 1},
            event_id="event-1",
            now=1.0,
        )
    return database, uow


def _runtime_lease(uow: SqliteExecutionUnitOfWork):
    return uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="worker-1",
        namespace="runtime.kernel",
        now=1.5,
        lease_ttl_seconds=100.0,
    )[1]


def _prepare(uow: SqliteExecutionUnitOfWork, run_fence, execution_lease, **extra: object):
    return uow.prepare_effect(
        effect_id=EffectId("effect-1"),
        run_id=RunId("run-1"),
        call_id=CallId("call-1"),
        tool_name="read",
        arguments={"path": "."},
        request_hash=effect_request_hash(tool_name="read", arguments={"path": "."}),
        authorization_receipt_ref="authorization:1",
        run_fence=run_fence,
        execution_lease=execution_lease,
        now=2.0,
        **extra,
    )


def _crash_at(point: str):
    def crash(actual: str) -> None:
        if actual == point:
            raise InjectedCrash(point)

    return crash


@pytest.mark.parametrize(
    "point,persisted",
    [
        ("effect_prepare.before_write", False),
        ("effect_prepare.after_write", False),
        ("effect_prepare.after_commit", True),
    ],
)
def test_prepare_fault_is_before_or_after_atomic(
    tmp_path: Path, point: str, persisted: bool
) -> None:
    path = tmp_path / "effects.db"
    database, uow = _uow(path)
    runtime_lease = _runtime_lease(uow)
    lease = asyncio.run(uow.acquire(RunId("run-1"), runtime_lease, now=1.5))
    with pytest.raises(InjectedCrash):
        _prepare(uow, lease, runtime_lease, fault=_crash_at(point))
    database.close()

    reopened, recovered = _uow(path)
    assert (recovered.read_effect(EffectId("effect-1")) is not None) is persisted
    assert reopened.integrity_check() == ("ok",)
    assert reopened.foreign_key_violations() == ()
    reopened.close()


def test_task_execution_envelope_reopens_with_exact_effect_authority(tmp_path: Path) -> None:
    path = tmp_path / "effect-envelope.db"
    database, uow = _uow(path)
    runtime_lease = _runtime_lease(uow)
    fence = asyncio.run(uow.acquire(RunId("run-1"), runtime_lease, now=1.5))
    envelope = TaskExecutionEnvelope(
        RunId("run-1"),
        CallId("call-1"),
        EffectId("effect-1"),
        "raw-1",
        1,
        0,
        "read",
        "host:read",
        "a" * 64,
        "route-1",
        "b" * 64,
        "task-1",
        "root-1",
        "c" * 64,
        2,
        "effect-1",
    )
    _prepare(
        uow,
        fence,
        runtime_lease,
        raw_call_id="raw-1",
        turn_ordinal=1,
        call_ordinal=0,
        task_execution_envelope=envelope,
    )
    database.close()

    reopened, recovered = _uow(path)
    record = recovered.read_effect(EffectId("effect-1"))
    assert record is not None
    assert record.task_execution_envelope == envelope
    assert record.task_execution_envelope.envelope_hash == envelope.envelope_hash
    reopened.close()


def test_task_execution_envelope_reopen_rejects_payload_hash_drift(tmp_path: Path) -> None:
    path = tmp_path / "effect-envelope-drift.db"
    database, uow = _uow(path)
    runtime_lease = _runtime_lease(uow)
    fence = asyncio.run(uow.acquire(RunId("run-1"), runtime_lease, now=1.5))
    envelope = TaskExecutionEnvelope(
        RunId("run-1"),
        CallId("call-1"),
        EffectId("effect-1"),
        "raw-1",
        1,
        0,
        "read",
        "host:read",
        "a" * 64,
        "route-1",
        "b" * 64,
        "task-1",
        "root-1",
        "c" * 64,
        2,
        "effect-1",
    )
    _prepare(
        uow,
        fence,
        runtime_lease,
        raw_call_id="raw-1",
        turn_ordinal=1,
        call_ordinal=0,
        task_execution_envelope=envelope,
    )
    payload = envelope.to_json()
    payload["binding_set_revision"] = 3
    database.connection.execute(
        "UPDATE execution_effects SET task_execution_envelope_json=? WHERE effect_id=?",
        (json.dumps(payload), "effect-1"),
    )
    database.connection.commit()
    database.close()

    reopened, recovered = _uow(path)
    with pytest.raises(ValueError, match="hash differs"):
        recovered.read_effect(EffectId("effect-1"))
    reopened.close()


def test_effect_handoff_unknown_reopen_and_reconcile_settlement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "effects.db"
    database, uow = _uow(path)
    runtime_lease = _runtime_lease(uow)
    lease = asyncio.run(uow.acquire(RunId("run-1"), runtime_lease, now=1.5))
    prepared = _prepare(uow, lease, runtime_lease)
    handed_off = uow.mark_effect_handed_off(
        prepared.effect_id,
        expected_version=prepared.version,
        run_fence=lease,
        handoff_receipt_ref="handoff:1",
        execution_lease=runtime_lease,
        now=3.0,
    )
    unknown = uow.mark_effect_unknown(
        handed_off.effect_id,
        expected_version=handed_off.version,
        expected_fence_epoch=lease.epoch,
        evidence_ref="crash:1",
        now=4.0,
    )
    assert unknown.state is EffectState.UNKNOWN
    database.close()

    reopened, recovered = _uow(path)
    record = recovered.read_effect(EffectId("effect-1"))
    assert record is not None and record.state is EffectState.UNKNOWN
    settled = recovered.settle_effect(
        record.effect_id,
        expected_version=record.version,
        expected_fence_epoch=record.fence_epoch,
        result=ToolResult.succeeded(CallId("call-1"), {"ok": True}),
        evidence_ref="external-receipt:1",
        now=5.0,
    )
    assert settled.state is EffectState.SUCCEEDED
    assert settled.result == ToolResult.succeeded(CallId("call-1"), {"ok": True})
    reopened.close()


def test_new_fence_epoch_invalidates_old_owner(tmp_path: Path) -> None:
    database, uow = _uow(tmp_path / "effects.db")
    _, first_runtime = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="worker-1",
        namespace="runtime.kernel",
        now=1.5,
        lease_ttl_seconds=1.0,
    )
    first = asyncio.run(uow.acquire(RunId("run-1"), first_runtime, now=1.5))
    _, second_runtime = uow.claim_runtime_activation(
        run_id="run-1",
        owner_id="worker-2",
        namespace="runtime.kernel",
        now=3.0,
        lease_ttl_seconds=100.0,
    )
    second = asyncio.run(uow.acquire(RunId("run-1"), second_runtime, now=3.0))

    assert second.epoch == first.epoch + 1
    asyncio.run(uow.release(first))
    assert asyncio.run(uow.current_epoch(RunId("run-1"))) == second.epoch
    asyncio.run(uow.release(second))
    database.close()
