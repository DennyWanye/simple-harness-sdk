# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frontier and the bounded step-3 Allocator (§8, theory 02-8 / 05, plan D3-4).

* **Frontier** — Tasks that are READY (every dependency COMPLETED), not finished
  and currently assignable.
* **Allocator** — decides *which* frontier Tasks get an Attempt now: fixed
  priority order (``priority`` desc, ordinal asc), bounded by the Mission's
  concurrency limit (open Attempts across the Mission) and by the per-Task
  candidate count.  No learned policy, no §29.3 formula yet (step 5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts import Attempt, AttemptStatus, Task, TaskStatus

OPEN_ATTEMPT_STATES = frozenset(
    {
        AttemptStatus.PENDING,
        AttemptStatus.CLAIMED,
        AttemptStatus.RUNNING,
        AttemptStatus.SUBMITTED,
        AttemptStatus.VERIFYING,
    }
)


def frontier(tasks: Sequence[Task]) -> list[Task]:
    """READY Tasks whose dependencies are all COMPLETED (§8.1)."""

    by_id = {task.id: task for task in tasks}
    ready = [
        task
        for task in tasks
        if task.status is TaskStatus.READY
        and all(
            dep in by_id and by_id[dep].status is TaskStatus.COMPLETED
            for dep in task.dependency_ids
        )
    ]
    return sorted(ready, key=lambda task: (-task.priority, _ordinal(task.id)))


def _ordinal(task_id: str) -> int:
    try:
        return int(task_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Which (task, candidate ordinal) pairs may get a new Attempt this cycle."""

    grants: tuple[tuple[Task, int], ...]
    open_attempts: int
    concurrency_limit: int | None

    def to_json(self) -> dict[str, object]:
        return {
            "grants": [{"task_id": task.id, "candidate": ordinal} for task, ordinal in self.grants],
            "open_attempts": self.open_attempts,
            "concurrency_limit": self.concurrency_limit,
        }


def allocate(
    tasks: Sequence[Task],
    attempts: Sequence[Attempt],
    *,
    concurrency_limit: int | None,
    candidates_per_task: int = 1,
) -> AllocationPlan:
    """Bounded allocation over the Frontier plus ACTIVE Tasks that still lack a candidate.

    A Task with an open Attempt already counts against the concurrency limit; a
    Task in RETRY_WAIT (all attempts settled, no open one) is eligible again for a
    repair Attempt.  Explorative multi-candidates (``candidates_per_task > 1``)
    are only granted while the Task is READY/ACTIVE with fewer open candidates.
    """

    open_by_task: dict[str, int] = {}
    for attempt in attempts:
        if attempt.status in OPEN_ATTEMPT_STATES:
            open_by_task[attempt.task_id] = open_by_task.get(attempt.task_id, 0) + 1
    open_total = sum(open_by_task.values())
    grants: list[tuple[Task, int]] = []
    candidates = frontier(tasks) + sorted(
        (task for task in tasks if task.status is TaskStatus.ACTIVE),
        key=lambda task: (-task.priority, _ordinal(task.id)),
    )
    seen: set[str] = set()
    for task in candidates:
        if task.id in seen:
            continue
        seen.add(task.id)
        open_here = open_by_task.get(task.id, 0)
        while open_here < max(1, candidates_per_task):
            if concurrency_limit is not None and open_total >= concurrency_limit:
                break
            grants.append((task, open_here + 1))
            open_here += 1
            open_total += 1
    return AllocationPlan(
        grants=tuple(grants), open_attempts=open_total, concurrency_limit=concurrency_limit
    )


__all__ = ("OPEN_ATTEMPT_STATES", "AllocationPlan", "allocate", "frontier")
