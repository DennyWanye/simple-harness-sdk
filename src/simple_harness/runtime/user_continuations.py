# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FIFO continuation runtime with explicit durable acknowledge."""

from __future__ import annotations

from typing import Protocol

from simple_harness.execution.uow import ContinuationRecord


class ContinuationUnitOfWork(Protocol):
    def claim_continuation(
        self, *, run_id: str, owner_id: str, now: float
    ) -> ContinuationRecord | None: ...

    def ack_continuation(
        self,
        *,
        continuation_id: str,
        owner_id: str,
        expected_version: int,
        now: float,
    ) -> ContinuationRecord: ...


class UserContinuationRuntime:
    def __init__(self, uow: ContinuationUnitOfWork, *, owner_id: str) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._uow = uow
        self._owner_id = owner_id

    def claim(self, *, run_id: str, now: float) -> ContinuationRecord | None:
        return self._uow.claim_continuation(
            run_id=run_id, owner_id=self._owner_id, now=now
        )

    def acknowledge(
        self, continuation: ContinuationRecord, *, now: float
    ) -> ContinuationRecord:
        if continuation.claimed_by != self._owner_id:
            raise RuntimeError("continuation is not owned by this runtime")
        return self._uow.ack_continuation(
            continuation_id=continuation.continuation_id,
            owner_id=self._owner_id,
            expected_version=continuation.version,
            now=now,
        )


__all__ = ("ContinuationUnitOfWork", "UserContinuationRuntime")
