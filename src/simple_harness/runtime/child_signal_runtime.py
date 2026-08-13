# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable child-terminal signal consumption."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from simple_harness.contracts import JsonValue, thaw_json
from simple_harness.execution.contracts.children import (
    ChildSignalAckResult,
    ChildSignalRecord,
)


class ChildSignalUnitOfWork(Protocol):
    def claim_next_child_signal(
        self,
        *,
        parent_run_id: str,
        owner_id: str,
        now: float,
        lease_seconds: float,
    ) -> ChildSignalRecord | None: ...

    def ack_child_signal_and_commit_parent_progress(
        self,
        *,
        signal_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt_id: str,
        continuation_id: str,
        continuation_payload: dict[str, JsonValue],
        event_id: str,
        event_payload: dict[str, JsonValue],
        now: float,
    ) -> ChildSignalAckResult: ...


class ChildSignalRuntime:
    def __init__(self, uow: ChildSignalUnitOfWork, *, owner_id: str) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._uow = uow
        self._owner_id = owner_id

    def receive_one(
        self,
        *,
        parent_run_id: str,
        now: float,
        lease_seconds: float = 30.0,
    ) -> ChildSignalAckResult | None:
        signal = self._uow.claim_next_child_signal(
            parent_run_id=parent_run_id,
            owner_id=self._owner_id,
            now=now,
            lease_seconds=lease_seconds,
        )
        if signal is None:
            return None
        payload = signal.payload
        if not isinstance(payload, Mapping):
            raise TypeError("child signal payload must be an object")
        mutable_payload = thaw_json(payload)
        if not isinstance(mutable_payload, dict):
            raise TypeError("child signal payload must be an object")
        continuation_id = f"child-signal:{signal.signal_id}:continuation"
        event_id = f"child-signal:{signal.signal_id}:acked"
        receipt_id = f"child-signal:{signal.signal_id}:receipt"
        continuation_payload: dict[str, JsonValue] = {
            "kind": "child_terminal",
            "signal_id": signal.signal_id,
            "child_run_id": signal.child_run_id,
            "payload": mutable_payload,
        }
        event_payload: dict[str, JsonValue] = {
            "signal_id": signal.signal_id,
            "continuation_id": continuation_id,
            "receipt_id": receipt_id,
        }
        return self._uow.ack_child_signal_and_commit_parent_progress(
            signal_id=signal.signal_id,
            owner_id=self._owner_id,
            claim_epoch=signal.claim_epoch,
            receipt_id=receipt_id,
            continuation_id=continuation_id,
            continuation_payload=continuation_payload,
            event_id=event_id,
            event_payload=event_payload,
            now=now,
        )


__all__ = ("ChildSignalRuntime", "ChildSignalUnitOfWork")
