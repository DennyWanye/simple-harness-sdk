# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frontier and the bounded Allocator (§8, §29.3, theory 02-8 / 05, plan D3-4 / D5-8').

* **Frontier** — Tasks that are READY (every dependency COMPLETED), not paused, not
  finished and currently assignable.
* **Allocator** — decides *which* frontier Tasks get an Attempt now.  Eligibility is
  checked first (paused, dependencies, concurrency, per-Task candidates); the
  ordering is §29.3's starting formula with its weights verbatim:

      priority = 0.30·mission_importance + 0.20·unlock_value + 0.15·progress_signal
               + 0.15·uncertainty + 0.10·waiting_age − 0.05·estimated_cost
               − 0.05·duplication_score

  The *scales* of the inputs are this build's convention (plan §6.1, versioned as
  ``ALLOCATOR_VERSION``); no learned policy.  Two guards sit outside the formula:
  a Conflict Task (a dispute about a delivered fact) is ranked before any work Task,
  and a Task that has waited a whole ``aging_window`` is promoted so it cannot
  starve (§19.4, theory 05-10).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Attempt, AttemptStatus, Task, TaskStatus
from ..graph.deduplicator import normalise_goal
from .backpressure import BackpressureState

ALLOCATOR_VERSION = "allocator-v1"
WEIGHTS: Mapping[str, float] = {
    "mission_importance": 0.30,
    "unlock_value": 0.20,
    "progress_signal": 0.15,
    "uncertainty": 0.15,
    "waiting_age": 0.10,
    "estimated_cost": -0.05,
    "duplication_score": -0.05,
}
OPEN_ATTEMPT_STATES = frozenset(
    {
        AttemptStatus.PENDING,
        AttemptStatus.CLAIMED,
        AttemptStatus.RUNNING,
        AttemptStatus.SUBMITTED,
        AttemptStatus.VERIFYING,
    }
)
FAILED_REASONS = frozenset({"verification_failed", "outcome_no_progress", "outcome_failure"})


def frontier(tasks: Sequence[Task]) -> list[Task]:
    """READY Tasks whose dependencies are all COMPLETED (§8.1), not paused (D5-1)."""

    by_id = {task.id: task for task in tasks}
    ready = [
        task
        for task in tasks
        if task.status is TaskStatus.READY
        and not task.paused  # step 5 (D5-1): a paused route is not allocated
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
class TaskScore:
    task_id: str
    score: float
    parts: Mapping[str, float]
    tier: int  # 0 = conflict task, 1 = starving (waited a whole window), 2 = formula
    version: str = ALLOCATOR_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "score": round(self.score, 4),
            "parts": {k: round(v, 4) for k, v in self.parts.items()},
            "tier": self.tier,
            "allocator_version": self.version,
        }


def _dependents(tasks: Sequence[Task]) -> dict[str, set[str]]:
    """Transitive dependents per task (unlock value)."""

    direct: dict[str, set[str]] = {t.id: set() for t in tasks}
    for task in tasks:
        for dep in task.dependency_ids:
            direct.setdefault(dep, set()).add(task.id)
    closure: dict[str, set[str]] = {}

    def visit(tid: str) -> set[str]:
        if tid in closure:
            return closure[tid]
        found: set[str] = set()
        for child in direct.get(tid, ()):
            found.add(child)
            found |= visit(child)
        closure[tid] = found
        return found

    for task in tasks:
        visit(task.id)
    return closure


def score_tasks(
    tasks: Sequence[Task],
    attempts: Sequence[Attempt],
    *,
    now: float,
    aging_window_seconds: float = 300.0,
    mission_max_tokens: int | None = None,
) -> dict[str, TaskScore]:
    """§29.3 for every live Task (the caller picks the eligible ones)."""

    live = [t for t in tasks if t.status is not TaskStatus.CANCELLED]
    if not live:
        return {}
    max_priority = max((t.priority for t in live), default=1.0) or 1.0
    dependents = _dependents(live)
    total = max(1, len(live))
    attempts_by_task: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        attempts_by_task.setdefault(attempt.task_id, []).append(attempt)
    goals: dict[str, list[str]] = {}
    for task in live:
        goals.setdefault(normalise_goal(task.goal), []).append(task.id)
    pool = mission_max_tokens or sum(int(t.budget.max_tokens or 0) for t in live) or 1
    scores: dict[str, TaskScore] = {}
    for task in live:
        history = attempts_by_task.get(task.id, [])
        tries = len(history)
        failures = sum(
            1 for a in history if str((a.failure or {}).get("reason", "")) in FAILED_REASONS
        )
        waiting = 0.0
        if task.ready_at is not None and aging_window_seconds > 0:
            waiting = min(1.0, max(0.0, now - task.ready_at) / aging_window_seconds)
        parts = {
            "mission_importance": max(0.0, min(1.0, task.priority / max_priority)),
            "unlock_value": len(dependents.get(task.id, ())) / total,
            "progress_signal": 0.5 if tries == 0 else max(0.0, 1.0 - failures / tries),
            "uncertainty": 0.5**tries,
            "waiting_age": waiting,
            "estimated_cost": min(1.0, int(task.budget.max_tokens or 0) / pool),
            "duplication_score": (len(goals[normalise_goal(task.goal)]) - 1) / total,
        }
        score = sum(WEIGHTS[name] * value for name, value in parts.items())
        tier = 0 if task.kind == "conflict" else (1 if waiting >= 1.0 else 2)
        scores[task.id] = TaskScore(task.id, score, parts, tier)
    return scores


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Which (task, candidate ordinal) pairs may get a new Attempt this cycle."""

    grants: tuple[tuple[Task, int], ...]
    open_attempts: int
    concurrency_limit: int | None
    scores: Mapping[str, TaskScore] = field(default_factory=dict)
    pressure: str | None = None  # the BackpressureState.level the plan was made under

    def to_json(self) -> dict[str, object]:
        return {
            "allocator_version": ALLOCATOR_VERSION,
            "grants": [
                {
                    "task_id": task.id,
                    "candidate": ordinal,
                    **(self.scores[task.id].to_json() if task.id in self.scores else {}),
                }
                for task, ordinal in self.grants
            ],
            "open_attempts": self.open_attempts,
            "concurrency_limit": self.concurrency_limit,
            "pressure": self.pressure,
        }


def allocate(
    tasks: Sequence[Task],
    attempts: Sequence[Attempt],
    *,
    concurrency_limit: int | None,
    candidates_per_task: int = 1,
    now: float | None = None,
    aging_window_seconds: float = 300.0,
    mission_max_tokens: int | None = None,
    pressure: BackpressureState | None = None,
    reduced_concurrency_ratio: float = 0.5,
    exploration_slots: int = 1,
) -> AllocationPlan:
    """Bounded allocation over the Frontier plus ACTIVE Tasks that still lack a candidate.

    Eligibility first (paused, dependencies, the Mission-wide concurrency limit, the
    per-Task candidate count), then §29.3 ordering: conflict Tasks, then starving
    Tasks (a whole aging window waited), then by score, ordinal as the tie-break.
    """

    open_by_task: dict[str, int] = {}
    for attempt in attempts:
        if attempt.status in OPEN_ATTEMPT_STATES:
            open_by_task[attempt.task_id] = open_by_task.get(attempt.task_id, 0) + 1
    open_total = sum(open_by_task.values())
    raised = pressure is not None and pressure.is_raised
    if raised and concurrency_limit is not None:
        # §18.5 "降低 Worker 并发" (D6-3 ①): the gate outside the §29.3 formula
        concurrency_limit = max(1, int(concurrency_limit * reduced_concurrency_ratio))
    grants: list[tuple[Task, int]] = []
    eligible = frontier(tasks) + [
        task for task in tasks if task.status is TaskStatus.ACTIVE and not task.paused
    ]
    scores = score_tasks(
        tasks,
        attempts,
        now=now if now is not None else 0.0,
        aging_window_seconds=aging_window_seconds if now is not None else 0.0,
        mission_max_tokens=mission_max_tokens,
    )
    seen: set[str] = set()
    ordered: list[Task] = []
    for task in eligible:
        if task.id in seen:
            continue
        seen.add(task.id)
        ordered.append(task)
    ordered.sort(
        key=lambda task: (
            scores[task.id].tier if task.id in scores else 2,
            -(scores[task.id].score if task.id in scores else task.priority),
            _ordinal(task.id),
        )
    )
    if raised:
        # §18.5 "暂停低优先级任务" (D6-3 ②): only conflict (tier 0) and starving (tier 1)
        # Tasks are expanded; formula-tier Tasks wait — except an exploration quota of
        # never-attempted Tasks (theory 05-10 "保留固定探索预算")
        tried = {attempt.task_id for attempt in attempts}
        kept: list[Task] = []
        explored = 0
        for task in ordered:
            tier = scores[task.id].tier if task.id in scores else 2
            if tier <= 1:
                kept.append(task)
            elif task.id not in tried and explored < max(0, exploration_slots):
                kept.append(task)
                explored += 1
        ordered = kept
    for task in ordered:
        open_here = open_by_task.get(task.id, 0)
        while open_here < max(1, candidates_per_task):
            if concurrency_limit is not None and open_total >= concurrency_limit:
                break
            grants.append((task, open_here + 1))
            open_here += 1
            open_total += 1
    return AllocationPlan(
        grants=tuple(grants),
        open_attempts=open_total,
        concurrency_limit=concurrency_limit,
        scores={task.id: scores[task.id] for task in ordered if task.id in scores},
        pressure=None if pressure is None else pressure.level,
    )


__all__ = (
    "ALLOCATOR_VERSION",
    "OPEN_ATTEMPT_STATES",
    "WEIGHTS",
    "AllocationPlan",
    "TaskScore",
    "allocate",
    "frontier",
    "score_tasks",
)
