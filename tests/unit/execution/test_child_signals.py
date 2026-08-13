# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from simple_harness.execution.contracts.children import (
    ChildSignalAckReceipt,
    ChildSignalAckResult,
    ChildSignalRecord,
    ChildSignalState,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _claimed(*, owner: str = "runtime-a", epoch: int = 1) -> ChildSignalRecord:
    return ChildSignalRecord(
        signal_id="signal-1",
        parent_run_id="root-1",
        child_run_id="child-1",
        payload={"outcome": "completed"},
        state=ChildSignalState.CLAIMED,
        version=epoch,
        claimed_by=owner,
        claimed_at=10.0,
        claim_expires_at=20.0,
        claim_epoch=epoch,
    )


def _acked() -> ChildSignalRecord:
    claimed = _claimed()
    return ChildSignalRecord(
        signal_id=claimed.signal_id,
        parent_run_id=claimed.parent_run_id,
        child_run_id=claimed.child_run_id,
        payload=claimed.payload,
        state=ChildSignalState.ACKED,
        version=2,
        claimed_by=claimed.claimed_by,
        claimed_at=claimed.claimed_at,
        claim_expires_at=claimed.claim_expires_at,
        claim_epoch=claimed.claim_epoch,
        acked_at=12.0,
        ack_receipt_id="receipt-1",
    )


def _receipt() -> ChildSignalAckReceipt:
    return ChildSignalAckReceipt(
        receipt_id="receipt-1",
        signal_id="signal-1",
        parent_run_id="root-1",
        owner_id="runtime-a",
        claim_epoch=1,
        continuation_id="continuation-1",
        event_id="event-1",
        continuation_payload_hash=_hash("continuation"),
        event_payload_hash=_hash("event"),
        created_at=12.0,
    )


def test_signal_claim_requires_complete_independent_epoch_lease() -> None:
    record = _claimed(epoch=3)
    assert record.claim_epoch == 3
    assert record.claimed_by == "runtime-a"

    with pytest.raises(ValueError, match="owner and lease timestamps"):
        ChildSignalRecord(
            signal_id="signal-1",
            parent_run_id="root-1",
            child_run_id="child-1",
            payload={},
            state=ChildSignalState.CLAIMED,
            version=1,
            claim_epoch=1,
        )


def test_pending_signal_cannot_smuggle_claim_or_receipt_metadata() -> None:
    with pytest.raises(ValueError, match="cannot have a claim lease"):
        ChildSignalRecord(
            signal_id="signal-1",
            parent_run_id="root-1",
            child_run_id="child-1",
            payload={},
            state=ChildSignalState.PENDING,
            version=0,
            claimed_by="runtime-a",
        )


def test_ack_result_binds_receipt_to_exact_owner_epoch_and_signal() -> None:
    result = ChildSignalAckResult(_acked(), _receipt())
    assert result.receipt.receipt_id == result.signal.ack_receipt_id

    wrong_epoch = ChildSignalAckReceipt(
        receipt_id="receipt-1",
        signal_id="signal-1",
        parent_run_id="root-1",
        owner_id="runtime-a",
        claim_epoch=2,
        continuation_id="continuation-1",
        event_id="event-1",
        continuation_payload_hash=_hash("continuation"),
        event_payload_hash=_hash("event"),
        created_at=12.0,
    )
    with pytest.raises(ValueError, match="epoch"):
        ChildSignalAckResult(_acked(), wrong_epoch)
