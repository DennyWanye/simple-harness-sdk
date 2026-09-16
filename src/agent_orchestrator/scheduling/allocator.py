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

P2.3c adds a *second* entry point beside those two, and adds nothing to them.
Plan §18.5 hard constraint 2 is explicit that ``allocate()`` keeps its old READY
entry and that :class:`~..graph.eligibility.EligiblePrimitiveTask` is an
*additional* gate for the hierarchical mode rather than the only input type — a
legacy Mission with no semantic binding is dispatched exactly as before.  So
:func:`frontier_v2` / :func:`allocate_v2` are new functions, the two legacy ones
are not touched (the suite hashes their source), and the new pair:

* takes only records that :func:`~..graph.eligibility.admit_for_dispatch` built,
  so a "ready" judgement can never reach the Worker path around the gate;
* intercepts a ``form=compound`` occurrence by its **form** — from the semantic
  binding, never from ``Task.status`` and never from the semantics version — and
  records ``NEEDS_REFINEMENT`` for it (§18.5 hard constraint 4).  The answer comes
  from :func:`~..graph.eligibility.legacy_ready_is_not_eligibility` so this module
  and ``orchestrator/hierarchical_dispatch.py`` cannot disagree about it;
* reuses the §29.3 scoring, the physical concurrency arithmetic and the
  verification backpressure of the legacy path unchanged — the new thing here is
  *who may compete for a slot*, not how the slots are counted.

Membership in the v2 frontier is still not a permission (plan §24.1 decision 6):
``EligiblePrimitiveTask`` is not a security token, and the dispatch transaction
re-checks identity, budget and capacity before anything leaves the process.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Attempt, AttemptStatus, Task, TaskStatus
from ..contracts.htn import TaskSemanticBindingV1
from ..contracts.state_machines import TERMINAL_TASK
from ..governance.promotion import weights_hash
from ..graph.deduplicator import normalise_goal
from ..graph.eligibility import (
    EligiblePrimitiveTask,
    ReadinessReason,
    legacy_ready_is_not_eligibility,
)
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
    weights_hash: str | None = None  # step 9 (plan D9-4'): which weights scored it

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "score": round(self.score, 4),
            "parts": {k: round(v, 4) for k, v in self.parts.items()},
            "tier": self.tier,
            "allocator_version": self.version,
            "weights_hash": self.weights_hash,
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
    weights: Mapping[str, float] | None = None,
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
    table = WEIGHTS if weights is None else weights  # step 9: a policy version's weights
    hashed = weights_hash(table)
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
        score = sum(float(table[name]) * value for name, value in parts.items())
        tier = 0 if task.kind == "conflict" else (1 if waiting >= 1.0 else 2)
        scores[task.id] = TaskScore(task.id, score, parts, tier, weights_hash=hashed)
    return scores


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Which (task, candidate ordinal) pairs may get a new Attempt this cycle."""

    grants: tuple[tuple[Task, int], ...]
    open_attempts: int
    concurrency_limit: int | None
    scores: Mapping[str, TaskScore] = field(default_factory=dict)
    pressure: str | None = None  # the BackpressureState.level the plan was made under
    eligible: int = 0  # step 9 (plan D9-5'): Tasks competing for the free slots
    slots: int | None = None  # free slots before granting (None = no concurrency limit)

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
            "eligible": self.eligible,
            "slots": self.slots,
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
    weights: Mapping[str, float] | None = None,
    waiting_attempt_ids: frozenset[str] = frozenset(),
    selection_task_ids: frozenset[str] = frozenset(),
) -> AllocationPlan:
    """Bounded allocation over the Frontier plus ACTIVE Tasks that still lack a candidate.

    Eligibility first (paused, dependencies, the Mission-wide concurrency limit, the
    per-Task candidate count), then §29.3 ordering: conflict Tasks, then starving
    Tasks (a whole aging window waited), then by score, ordinal as the tie-break.
    """

    open_by_task: dict[str, int] = {}
    for attempt in attempts:
        if attempt.status in OPEN_ATTEMPT_STATES and attempt.id not in waiting_attempt_ids:
            open_by_task[attempt.task_id] = open_by_task.get(attempt.task_id, 0) + 1
    open_total = sum(open_by_task.values())
    raised = pressure is not None and pressure.is_raised
    if raised and concurrency_limit is not None:
        # §18.5 "降低 Worker 并发" (D6-3 ①): the gate outside the §29.3 formula
        concurrency_limit = max(1, int(concurrency_limit * reduced_concurrency_ratio))
    grants: list[tuple[Task, int]] = []
    eligible = frontier(tasks) + [
        task
        for task in tasks
        if (
            task.status is TaskStatus.ACTIVE
            or (task.id in selection_task_ids and task.status is TaskStatus.VERIFYING)
        )
        and not task.paused
    ]
    scores = score_tasks(
        tasks,
        attempts,
        now=now if now is not None else 0.0,
        aging_window_seconds=aging_window_seconds if now is not None else 0.0,
        mission_max_tokens=mission_max_tokens,
        weights=weights,
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
    slots = None if concurrency_limit is None else max(0, concurrency_limit - open_total)
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
        eligible=len(ordered),
        slots=slots,
    )


#: The new-mode entry point's own version.  It is a *second* version rather than a
#: bump of :data:`ALLOCATOR_VERSION` because the legacy path is unchanged and its
#: receipts must keep naming the version that scored them.
ALLOCATOR_V2_VERSION = "allocator-v2"


@dataclass(frozen=True, slots=True)
class FrontierRefusal:
    """Why one Task did not enter the new-mode frontier.

    The reason is a :class:`~..graph.eligibility.ReadinessReason`, not a boolean and
    not a private code: "this is a compound that needs refining", "no admission was
    presented" and "this Task has no semantic binding, which is corruption" call for
    three different repairs, and the scheduler is the wrong place to merge them.
    """

    task_id: str
    reason: ReadinessReason
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "reason": str(self.reason), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class FrontierV2:
    """The new-mode frontier: what may compete, and what was refused with why."""

    admitted: tuple[EligiblePrimitiveTask, ...] = ()
    refusals: tuple[FrontierRefusal, ...] = ()

    @property
    def needs_refinement(self) -> tuple[str, ...]:
        """The Tasks the form gate sent back to the planner (§18.5 constraint 4)."""

        return tuple(
            item.task_id
            for item in self.refusals
            if item.reason is ReadinessReason.NEEDS_REFINEMENT
        )

    def refusal_for(self, task_id: str) -> FrontierRefusal | None:
        for item in self.refusals:
            if item.task_id == task_id:
                return item
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "allocator_version": ALLOCATOR_V2_VERSION,
            "admitted": [str(item.task_id) for item in self.admitted],
            "refusals": [item.to_json() for item in self.refusals],
        }


def evaluate_frontier_v2(
    tasks: Sequence[Task],
    bindings: Mapping[str, TaskSemanticBindingV1],
    readiness: Mapping[str, EligiblePrimitiveTask],
) -> FrontierV2:
    """The new-mode frontier as a pure query, with a named refusal for every miss.

    The order of the checks is the load-bearing part:

    1. **no semantic binding** → ``GRAPH_INTEGRITY``.  §18.5: a new-mode Task
       without a :class:`~..contracts.htn.TaskSemanticBindingV1` is corruption, and
       corruption does not degrade into "dispatch it anyway".
    2. **the form gate** → ``NEEDS_REFINEMENT`` for a compound, *before* anything
       looks at ``Task.status``, at an admission record or at ``paused``.  A
       compound must be refused whatever else is true of it, so nothing that could
       answer "yes" may run first.
    3. only then the admission itself: a record that
       :func:`~..graph.eligibility.admit_for_dispatch` built, for *this* Task, and a
       Task that is neither paused nor already finished.

    Nothing here scores, reserves or writes; the caller passes the admissions in.
    """

    by_id = {task.id: task for task in tasks}
    admitted: list[EligiblePrimitiveTask] = []
    refusals: list[FrontierRefusal] = []
    for task in tasks:
        binding = bindings.get(task.id)
        if binding is None:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.GRAPH_INTEGRITY,
                    detail=(
                        "the new-mode allocator was given a Task with no "
                        "TaskSemanticBindingV1; that is corruption, not a legacy "
                        "fallback — the legacy allocate() is the entry for a Mission "
                        "that has no bindings (§18.5 hard constraints 2 and 4)"
                    ),
                )
            )
            continue
        # §18.5 constraint 4, asked of the one function that spells the rule out, so
        # the allocator and the hierarchical dispatch cannot drift apart.
        verdict = legacy_ready_is_not_eligibility(str(task.status), form=binding.form)
        if verdict.gate_reason is not None:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id, reason=verdict.gate_reason, detail=verdict.explanation
                )
            )
            continue
        record = readiness.get(task.id)
        if record is None:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.NOT_SELECTED,
                    detail=(
                        "no EligiblePrimitiveTask was admitted for this Task; run "
                        "evaluate_readiness() and admit_for_dispatch() first — a legacy "
                        f"status {str(task.status)!r} is a rebuildable display index"
                    ),
                )
            )
            continue
        if not isinstance(record, EligiblePrimitiveTask) or not record.gate_passed:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.GRAPH_INTEGRITY,
                    detail=(
                        "the presented admission does not carry the mark "
                        "admit_for_dispatch() hands out; a record nobody admitted is "
                        "not an admission (§18.5)"
                    ),
                )
            )
            continue
        if str(record.task_id) != task.id:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.GRAPH_INTEGRITY,
                    detail=(
                        f"the admission was built for task {record.task_id!s}; an "
                        "admission is not transferable between Tasks"
                    ),
                )
            )
            continue
        if task.paused:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.NOT_SELECTED,
                    detail="a paused route is not allocated (step 5, plan D5-1)",
                )
            )
            continue
        if task.status in TERMINAL_TASK:
            refusals.append(
                FrontierRefusal(
                    task_id=task.id,
                    reason=ReadinessReason.NOT_SELECTED,
                    detail=f"the Task is {task.status!s} and wants no further dispatch",
                )
            )
            continue
        admitted.append(record)
    admitted.sort(
        key=lambda item: (
            -by_id[str(item.task_id)].priority,
            _ordinal(str(item.task_id)),
        )
    )
    return FrontierV2(admitted=tuple(admitted), refusals=tuple(refusals))


def frontier_v2(
    tasks: Sequence[Task],
    bindings: Mapping[str, TaskSemanticBindingV1],
    readiness: Mapping[str, EligiblePrimitiveTask],
) -> list[EligiblePrimitiveTask]:
    """The admitted half of :func:`evaluate_frontier_v2`, ordered as :func:`frontier`."""

    return list(evaluate_frontier_v2(tasks, bindings, readiness).admitted)


def _pressure_keep(
    ordered: Sequence[Task],
    scores: Mapping[str, TaskScore],
    attempts: Sequence[Attempt],
    exploration_slots: int,
) -> list[Task]:
    """§18.5 "暂停低优先级任务" (D6-3 ②) with theory 05-10's exploration quota.

    The same rule the legacy :func:`allocate` applies inline.  It is a function here
    because :func:`allocate_v2` needs it too and the legacy body may not be edited —
    the suite asserts the two agree rather than trusting that they do.
    """

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
    return kept


@dataclass(frozen=True, slots=True)
class AllocationPlanV2:
    """Which admitted occurrences may get a new Attempt now, and what was refused."""

    grants: tuple[tuple[EligiblePrimitiveTask, int], ...] = ()
    open_attempts: int = 0
    concurrency_limit: int | None = None
    scores: Mapping[str, TaskScore] = field(default_factory=dict)
    refusals: tuple[FrontierRefusal, ...] = ()
    pressure: str | None = None
    eligible: int = 0
    slots: int | None = None

    @property
    def granted_task_ids(self) -> tuple[str, ...]:
        return tuple(str(item.task_id) for item, _ordinal_ in self.grants)

    def to_json(self) -> dict[str, object]:
        return {
            "allocator_version": ALLOCATOR_V2_VERSION,
            "grants": [
                {
                    "task_id": str(admitted.task_id),
                    "occurrence_id": str(admitted.occurrence_id),
                    "candidate": ordinal,
                    "input_manifest_hash": admitted.input_manifest_hash,
                    "dispatch_generation": int(admitted.dispatch_generation),
                    **(
                        self.scores[str(admitted.task_id)].to_json()
                        if str(admitted.task_id) in self.scores
                        else {}
                    ),
                }
                for admitted, ordinal in self.grants
            ],
            "open_attempts": self.open_attempts,
            "concurrency_limit": self.concurrency_limit,
            "pressure": self.pressure,
            "eligible": self.eligible,
            "slots": self.slots,
            "refusals": [item.to_json() for item in self.refusals],
        }


def allocate_v2(
    tasks: Sequence[Task],
    attempts: Sequence[Attempt],
    bindings: Mapping[str, TaskSemanticBindingV1],
    readiness: Mapping[str, EligiblePrimitiveTask],
    *,
    concurrency_limit: int | None,
    candidates_per_task: int = 1,
    now: float | None = None,
    aging_window_seconds: float = 300.0,
    mission_max_tokens: int | None = None,
    pressure: BackpressureState | None = None,
    reduced_concurrency_ratio: float = 0.5,
    exploration_slots: int = 1,
    weights: Mapping[str, float] | None = None,
    waiting_attempt_ids: frozenset[str] = frozenset(),
) -> AllocationPlanV2:
    """Bounded allocation over the new-mode frontier only (§18.5, §24.1 decision 6).

    Everything outside "who may compete" is the legacy behaviour, reached through the
    same helpers: :func:`score_tasks` for §29.3, :data:`OPEN_ATTEMPT_STATES` for the
    physical count, ``pressure`` for the verification backpressure gates, and
    :func:`_pressure_keep` for the exploration quota.  What changed is that the
    candidate set is :func:`evaluate_frontier_v2` — admissions only — so a compound
    cannot be walked into the Worker path and a "ready" string cannot stand in for
    an eligibility check.
    """

    computed = evaluate_frontier_v2(tasks, bindings, readiness)
    by_id = {task.id: task for task in tasks}
    open_by_task: dict[str, int] = {}
    for attempt in attempts:
        if attempt.status in OPEN_ATTEMPT_STATES and attempt.id not in waiting_attempt_ids:
            open_by_task[attempt.task_id] = open_by_task.get(attempt.task_id, 0) + 1
    open_total = sum(open_by_task.values())
    raised = pressure is not None and pressure.is_raised
    if raised and concurrency_limit is not None:
        concurrency_limit = max(1, int(concurrency_limit * reduced_concurrency_ratio))
    scores = score_tasks(
        tasks,
        attempts,
        now=now if now is not None else 0.0,
        aging_window_seconds=aging_window_seconds if now is not None else 0.0,
        mission_max_tokens=mission_max_tokens,
        weights=weights,
    )
    admitted_by_task = {str(item.task_id): item for item in computed.admitted}
    ordered = [by_id[task_id] for task_id in admitted_by_task]
    ordered.sort(
        key=lambda task: (
            scores[task.id].tier if task.id in scores else 2,
            -(scores[task.id].score if task.id in scores else task.priority),
            _ordinal(task.id),
        )
    )
    if raised:
        ordered = _pressure_keep(ordered, scores, attempts, exploration_slots)
    slots = None if concurrency_limit is None else max(0, concurrency_limit - open_total)
    grants: list[tuple[EligiblePrimitiveTask, int]] = []
    for task in ordered:
        open_here = open_by_task.get(task.id, 0)
        while open_here < max(1, candidates_per_task):
            if concurrency_limit is not None and open_total >= concurrency_limit:
                break
            grants.append((admitted_by_task[task.id], open_here + 1))
            open_here += 1
            open_total += 1
    return AllocationPlanV2(
        grants=tuple(grants),
        open_attempts=open_total,
        concurrency_limit=concurrency_limit,
        scores={task.id: scores[task.id] for task in ordered if task.id in scores},
        refusals=computed.refusals,
        pressure=None if pressure is None else pressure.level,
        eligible=len(ordered),
        slots=slots,
    )


__all__ = (
    "ALLOCATOR_V2_VERSION",
    "ALLOCATOR_VERSION",
    "OPEN_ATTEMPT_STATES",
    "WEIGHTS",
    "AllocationPlan",
    "AllocationPlanV2",
    "FrontierRefusal",
    "FrontierV2",
    "TaskScore",
    "allocate",
    "allocate_v2",
    "evaluate_frontier_v2",
    "frontier",
    "frontier_v2",
    "score_tasks",
)
