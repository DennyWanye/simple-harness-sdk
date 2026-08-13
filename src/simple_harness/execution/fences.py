# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Monotonic run-execution fences for physical Tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from simple_harness.contracts import RunId

if TYPE_CHECKING:
    from simple_harness.execution.uow import ExecutionLease


class StaleFenceError(RuntimeError):
    code = "stale_fence_epoch"


@dataclass(frozen=True, slots=True)
class RunFenceLease:
    run_id: RunId
    epoch: int
    owner_id: str
    runtime_lease_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must use RunId")
        if self.epoch < 1:
            raise ValueError("epoch must be positive")
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if self.runtime_lease_epoch < 1:
            raise ValueError("runtime_lease_epoch must be positive")


def require_current_epoch(*, expected: int, current: int) -> None:
    if expected < 1 or current < 1 or expected != current:
        raise StaleFenceError(f"expected epoch {expected}, current epoch {current}")


@runtime_checkable
class RunFencePort(Protocol):
    async def acquire(
        self,
        run_id: RunId,
        execution_lease: ExecutionLease,
        *,
        now: float,
    ) -> RunFenceLease: ...

    async def current_epoch(self, run_id: RunId) -> int: ...

    async def release(self, lease: RunFenceLease) -> None: ...


__all__ = (
    "RunFenceLease",
    "RunFencePort",
    "StaleFenceError",
    "require_current_epoch",
)
