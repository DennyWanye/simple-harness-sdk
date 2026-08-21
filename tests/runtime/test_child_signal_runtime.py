# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

from simple_harness.contracts import canonical_json
from simple_harness.execution.contracts.children import (
    ChildSignalAckReceipt,
    ChildSignalAckResult,
    ChildSignalRecord,
    ChildSignalState,
)
from simple_harness.runtime.child_signal_runtime import ChildSignalRuntime


class FakeSignalUnitOfWork:
    def __init__(self) -> None:
        self.claim_calls: list[dict[str, object]] = []
        self.ack_calls: list[dict[str, object]] = []

    def list_child_signal_parent_run_ids(self) -> tuple[str, ...]:
        return ("root-1",)

    def claim_next_child_signal(self, **kwargs: object) -> ChildSignalRecord:
        self.claim_calls.append(kwargs)
        return ChildSignalRecord(
            signal_id="signal-1",
            parent_run_id="root-1",
            child_run_id="child-1",
            payload={"outcome": "completed", "value": {"answer": 42}},
            state=ChildSignalState.CLAIMED,
            version=1,
            claimed_by=str(kwargs["owner_id"]),
            claimed_at=float(kwargs["now"]),
            claim_expires_at=float(kwargs["now"]) + float(kwargs["lease_seconds"]),
            claim_epoch=1,
        )

    def ack_child_signal_and_commit_parent_progress(
        self, **kwargs: object
    ) -> ChildSignalAckResult:
        self.ack_calls.append(kwargs)
        continuation = kwargs["continuation_payload"]
        event = kwargs["event_payload"]
        assert isinstance(continuation, dict) and isinstance(event, dict)
        receipt = ChildSignalAckReceipt(
            receipt_id=str(kwargs["receipt_id"]),
            signal_id=str(kwargs["signal_id"]),
            parent_run_id="root-1",
            owner_id=str(kwargs["owner_id"]),
            claim_epoch=int(kwargs["claim_epoch"]),
            continuation_id=str(kwargs["continuation_id"]),
            event_id=str(kwargs["event_id"]),
            continuation_payload_hash=hashlib.sha256(
                canonical_json(continuation).encode("utf-8")
            ).hexdigest(),
            event_payload_hash=hashlib.sha256(
                canonical_json(event).encode("utf-8")
            ).hexdigest(),
            created_at=float(kwargs["now"]),
        )
        signal = ChildSignalRecord(
            signal_id=receipt.signal_id,
            parent_run_id=receipt.parent_run_id,
            child_run_id="child-1",
            payload={"outcome": "completed", "value": {"answer": 42}},
            state=ChildSignalState.ACKED,
            version=2,
            claimed_by=receipt.owner_id,
            claimed_at=10.0,
            claim_expires_at=40.0,
            claim_epoch=receipt.claim_epoch,
            acked_at=12.0,
            ack_receipt_id=receipt.receipt_id,
        )
        return ChildSignalAckResult(signal, receipt)


def test_runtime_uses_hol_claim_and_atomic_ack_with_stable_identities() -> None:
    uow = FakeSignalUnitOfWork()
    runtime = ChildSignalRuntime(uow, owner_id="runtime-a")

    result = runtime.receive_one(
        parent_run_id="root-1", now=10.0, lease_seconds=30.0
    )

    assert result is not None
    assert uow.claim_calls == [
        {
            "parent_run_id": "root-1",
            "owner_id": "runtime-a",
            "now": 10.0,
            "lease_seconds": 30.0,
        }
    ]
    assert uow.ack_calls[0]["claim_epoch"] == 1
    assert uow.ack_calls[0]["receipt_id"] == "child-signal:signal-1:receipt"
    assert uow.ack_calls[0]["continuation_id"] == (
        "child-signal:signal-1:continuation"
    )
    assert uow.ack_calls[0]["event_id"] == "child-signal:signal-1:acked"


def test_reconcile_does_not_ack_signal_while_parent_has_active_child() -> None:
    uow = FakeSignalUnitOfWork()
    runtime = ChildSignalRuntime(uow, owner_id="runtime-a")

    results = runtime.reconcile_all(
        now=10.0, blocked_parent_run_ids={"root-1"}
    )

    assert results == ()
    assert uow.claim_calls == []
    assert uow.ack_calls == []
