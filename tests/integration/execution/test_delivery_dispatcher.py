# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from simple_harness.contracts import JsonValue, freeze_json
from simple_harness.execution.delivery import (
    DeliveryDispatcher,
    DeliveryRecord,
    DeliveryState,
)


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
