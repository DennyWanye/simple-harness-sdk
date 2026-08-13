# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FIFO continuation claims bound to the canonical Runtime lease."""

from __future__ import annotations

from typing import Protocol

from simple_harness.execution.uow import ContinuationRecord, ExecutionLease


class ContinuationUnitOfWork(Protocol):
    def claim_continuation(
        self, *, run_id: str, execution_lease: ExecutionLease, now: float
    ) -> ContinuationRecord | None: ...


class UserContinuationRuntime:
    def __init__(self, uow: ContinuationUnitOfWork) -> None:
        self._uow = uow

    def claim(
        self, *, run_id: str, execution_lease: ExecutionLease, now: float
    ) -> ContinuationRecord | None:
        return self._uow.claim_continuation(
            run_id=run_id, execution_lease=execution_lease, now=now
        )


__all__ = ("ContinuationUnitOfWork", "UserContinuationRuntime")
