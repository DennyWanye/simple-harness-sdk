# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Process-local task index; durable ownership remains in the UoW."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable

from simple_harness.execution.uow import ExecutionLease

RuntimeLease = ExecutionLease


class LiveRunIndex:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(
        self, run_id: str, awaitable: Coroutine[object, object, None]
    ) -> asyncio.Task[None]:
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            return current
        task = asyncio.create_task(awaitable, name=f"simple-harness:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda done, key=run_id: self._discard(key, done))
        return task

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(run_id for run_id, task in self._tasks.items() if not task.done())

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def cancel(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def close(self, *, timeout_seconds: float) -> tuple[str, ...]:
        active = tuple(
            (run_id, task) for run_id, task in self._tasks.items() if not task.done()
        )
        for _, task in active:
            task.cancel()
        if active:
            active_tasks = [task for _, task in active]
            _done, pending = await asyncio.wait(
                active_tasks, timeout=timeout_seconds
            )
            for run_id, task in active:
                if task in pending:
                    self._tasks.pop(run_id, None)
        return tuple(run_id for run_id, _ in active)

    def _discard(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)


def lease_run_ids(leases: Iterable[RuntimeLease]) -> tuple[str, ...]:
    return tuple(lease.run_id for lease in leases)


__all__ = ("LiveRunIndex", "RuntimeLease", "lease_run_ids")
