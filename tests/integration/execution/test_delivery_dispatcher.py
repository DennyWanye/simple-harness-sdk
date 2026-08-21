# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

import pytest

from simple_harness.contracts import JsonValue, RunId, freeze_json
from simple_harness.execution.delivery import (
    DeliveryDispatcher,
    DeliveryRecord,
    DeliverySpec,
    DeliveryState,
)
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.execution.uow import RunState


class MemoryUow:
    def __init__(self) -> None:
        self.record = DeliveryRecord(
            "delivery-1",
            "run-1",
            "presenter",
            "terminal:run-1",
            freeze_json({"answer": 42}),
            DeliveryState.PENDING,
            0,
        )
        self.run_state = "completed"
        self.operations: list[str] = []

    def claim_delivery(self, *, sink_kinds, now, claim_ttl_seconds):
        self.operations.append("claim")
        if self.record.state is not DeliveryState.PENDING:
            return None
        self.record = replace(self.record, state=DeliveryState.CLAIMED, version=1)
        return self.record

    def complete_delivery(self, delivery_id, *, expected_version, now):
        self.operations.append("complete")
        assert expected_version == self.record.version
        self.record = replace(self.record, state=DeliveryState.DELIVERED, version=2)
        return self.record

    def release_delivery(self, delivery_id, *, expected_version, now):
        self.operations.append("release")
        assert expected_version == self.record.version
        self.record = replace(self.record, state=DeliveryState.PENDING, version=2)
        return self.record


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Mapping[str, JsonValue], str]] = []

    async def deliver(self, payload, *, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.fail:
            raise RuntimeError("sink unavailable")


class IdempotentRecordingSink:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.visible: dict[str, Mapping[str, JsonValue]] = {}

    async def deliver(self, payload, *, idempotency_key):
        self.attempts.append(idempotency_key)
        self.visible.setdefault(idempotency_key, payload)


def test_dispatch_success_completes_delivery() -> None:
    uow = MemoryUow()
    sink = RecordingSink()
    dispatcher = DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 5.0)
    assert asyncio.run(dispatcher.run_once()) is True
    assert uow.operations == ["claim", "complete"]
    assert uow.record.state is DeliveryState.DELIVERED
    assert sink.calls[0][1] == "terminal:run-1"


def test_sink_failure_releases_only_delivery_and_never_reopens_run() -> None:
    uow = MemoryUow()
    failing = RecordingSink(fail=True)
    dispatcher = DeliveryDispatcher(uow, {"presenter": failing}, clock=lambda: 5.0)
    assert asyncio.run(dispatcher.run_once()) is True
    assert uow.operations == ["claim", "release"]
    assert uow.record.state is DeliveryState.PENDING
    assert uow.run_state == "completed"
    failing.fail = False
    assert asyncio.run(dispatcher.run_once()) is True
    assert uow.record.state is DeliveryState.DELIVERED
    assert [call[1] for call in failing.calls] == ["terminal:run-1", "terminal:run-1"]


def test_empty_sinks_are_allowed_and_never_mark_delivered() -> None:
    uow = MemoryUow()
    # An empty sink set is valid (fail-closed): nothing is silently DELIVERED.
    dispatcher = DeliveryDispatcher(uow, {}, clock=lambda: 5.0)
    assert asyncio.run(dispatcher.run_once()) is True
    assert uow.record.state is DeliveryState.PENDING
    assert "complete" not in uow.operations


def test_sink_success_before_settle_crash_retries_same_key_after_reopen(
    tmp_path,
) -> None:
    class SimulatedCrash(RuntimeError):
        pass

    async def case() -> None:
        path = tmp_path / "delivery-crash-window.db"
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
        run, lease = uow.claim_runtime_activation(
            run_id="run-1",
            owner_id="owner-1",
            namespace="runtime.kernel",
            now=2.0,
            lease_ttl_seconds=30.0,
        )
        fence = await uow.acquire(RunId("run-1"), lease, now=2.0)
        uow.commit_root_terminal_with_deliveries(
            run_id="run-1",
            expected_version=run.version,
            terminal_state=RunState.COMPLETED,
            event_id="run-1:completed",
            terminal_payload={"answer": 42},
            deliveries=(DeliverySpec("delivery-1", "presenter", "terminal:run-1", {"answer": 42}),),
            fence=fence,
            execution_lease=lease,
            terminal_fence_receipt_ref="runtime-fence:owner-1:1",
            now=3.0,
        )
        sink = IdempotentRecordingSink()

        def crash_after_sink(point: str) -> None:
            if point == "delivery.sink_succeeded.before_complete":
                raise SimulatedCrash(point)

        crashing = DeliveryDispatcher(
            uow,
            {"presenter": sink},
            clock=lambda: 5.0,
            claim_ttl_seconds=1.0,
            fault=crash_after_sink,
        )
        with pytest.raises(SimulatedCrash):
            await crashing.run_once()
        claimed = uow.read_delivery("delivery-1")
        assert claimed is not None and claimed.state is DeliveryState.CLAIMED
        database.close()

        reopened_database = Database.open(path)
        reopened_uow = SqliteExecutionUnitOfWork(reopened_database)
        resumed = DeliveryDispatcher(
            reopened_uow,
            {"presenter": sink},
            clock=lambda: 7.0,
            claim_ttl_seconds=1.0,
        )
        assert await resumed.run_once() is True
        delivered = reopened_uow.read_delivery("delivery-1")
        assert delivered is not None and delivered.state is DeliveryState.DELIVERED
        assert sink.attempts == [
            "terminal:run-1",
            "terminal:run-1",
        ]
        assert list(sink.visible) == ["terminal:run-1"]
        assert sink.visible["terminal:run-1"]["answer"] == 42
        reopened_database.close()

    asyncio.run(case())
