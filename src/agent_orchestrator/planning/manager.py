# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""System-defined task templates of step 4 (ORCH §6.2: "本版本只开放系统定义的 Conflict
Task 和固定综合任务模板"; plan D4-7' / D4-8').

* **Conflict Task** — opened by the Commit Service when two claims contradict each
  other (§14.4).  It is a *leaf at the end of the topological order*: it depends on
  the disputed claims' source Tasks and nothing ever depends on it, so the
  ``ordinal ≡ topological order`` invariant of the artifact merge holds.  Its
  artifacts live under ``arbitration/<key>/`` and its verification demands an
  external check (``arbitration:<key>`` criterion + the probe test) reviewed by an
  independent Critic.
* **Synthesis Task** — appended at graph commit from ``MissionSpec.synthesis``; it
  depends on every leaf of the Planner's graph and its dependencies never change.
  An open conflict *gates* it (the Allocator skips it) instead of rewiring it.
* ``terminal_task`` — the Mission judgment target: the synthesis Task when there is
  one, otherwise the last non-conflict leaf.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..contracts import Budget, Mission, Task, TaskStatus

CONFLICT_POLICY = ("format_check", "rule_check", "critic_review", "code_test")
CONFLICT_MAX_ATTEMPTS = 2
ARBITRATION_PREFIX = "arbitration"


def arbitration_dir(key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_") or "key"
    return f"{ARBITRATION_PREFIX}/{slug}"


def conflict_task(
    mission: Mission,
    *,
    task_id: str,
    key: str,
    sides: Sequence[Mapping[str, Any]],
    conflict_id: str,
    tokens: int,
    now: float,
) -> Task:
    """The Conflict Task record (committed BLOCKED, unblocked in the same transaction)."""

    directory = arbitration_dir(key)
    sources = sorted({str(side["source_task"]) for side in sides})
    max_attempts = CONFLICT_MAX_ATTEMPTS
    if mission.budget.max_attempts is not None:
        max_attempts = max(1, min(max_attempts, mission.budget.max_attempts))
    return Task(
        id=task_id,
        mission_id=mission.id,
        parent_task_ids=(),
        dependency_ids=tuple(sources),
        kind="conflict",
        goal=f"{ARBITRATION_PREFIX}: 仲裁主题 {key} — 双方结论相反，运行外部检查后提交结论",
        rationale=(
            f"§14.4 冲突处理：{len(sides)} 条 Claim 对 {key} 得出相反结论；"
            "不投票，由 Arbiter 做外部验证并经独立 Critic 复核后 Commit"
        ),
        success_criteria=(f"arbitration:{key}", f"pytest:{directory}/test_probe.py"),
        verification_policy=CONFLICT_POLICY,
        allowed_tools=mission.allowed_tools,
        budget=inherit_limits(Budget(max_tokens=tokens, max_attempts=max_attempts), mission.budget),
        priority=10.0,  # a dispute about a delivered fact is resolved before anything else
        status=TaskStatus.BLOCKED,
        version=1,
        root_goal=mission.goal,
        created_at=now,
        outputs=(f"{directory}/",),
        context={
            "conflict_id": conflict_id,
            "key": key,
            "claim_ids": [str(side["claim_id"]) for side in sides],
            "sides": [dict(side) for side in sides],
            "artifact_dir": directory,
        },
    )


def synthesis_task(
    mission: Mission,
    *,
    task_id: str,
    template: Mapping[str, Any],
    leaves: Sequence[str],
    now: float,
) -> Task:
    """The fixed synthesis Task from ``MissionSpec.synthesis`` (D4-8)."""

    budget = inherit_limits(Budget.from_json(template.get("budget", {})), mission.budget)
    return Task(
        id=task_id,
        mission_id=mission.id,
        parent_task_ids=(),
        dependency_ids=tuple(leaves),
        goal=str(template["goal"]),
        rationale=str(
            template.get("rationale")
            or "§11.3 Synthesizer：组合各分支已验证成果生成新的候选产物，再次验收后交付"
        ),
        success_criteria=tuple(str(c) for c in template["success_criteria"]),
        verification_policy=tuple(
            str(layer)
            for layer in template.get(
                "verification_policy", ("format_check", "rule_check", "code_test")
            )
        ),
        allowed_tools=tuple(str(t) for t in template.get("allowed_tools", mission.allowed_tools)),
        budget=budget,
        priority=float(template.get("priority", 0.5)),
        status=TaskStatus.BLOCKED,
        version=1,
        root_goal=mission.goal,
        created_at=now,
        outputs=tuple(str(o) for o in template.get("outputs", ())),
        kind="synthesis",
        context={"template": dict(template)},
    )


def inherit_limits(budget: Budget, parent: Budget) -> Budget:
    """P0-1 / P1-2: a system template only names tokens / attempts; every other dimension
    the Mission bounds is inherited (child = parent cap) so ``fits_within`` holds and
    ``open_account`` can never reject a system Task inside an accept transaction."""

    changes: dict[str, Any] = {}
    for name in (
        "max_tokens",
        "max_cost_micros",
        "max_attempts",
        "max_concurrency",
        "max_runtime_seconds",
        "max_tool_calls",  # step 6 (D6-8): every dimension the parent bounds is inherited
    ):
        if getattr(budget, name) is None and getattr(parent, name) is not None:
            changes[name] = getattr(parent, name)
    return replace(budget, **changes) if changes else budget


def system_reserve_tokens(mission: Mission) -> int:
    """Tokens the Planner's graph may not use (D4-20): the synthesis budget plus the
    conflict reserve, both fixed in the Mission spec."""

    report = mission.final_report or {}
    synthesis = report.get("synthesis") or {}
    synth_tokens = int((synthesis.get("budget") or {}).get("max_tokens") or 0)
    return synth_tokens + int(report.get("conflict_reserve_tokens") or 0)


def terminal_task(tasks: Sequence[Task]) -> Task:
    """D4-7': the synthesis Task, else the last non-conflict leaf, else the last Task."""

    live = [task for task in tasks if task.status is not TaskStatus.CANCELLED] or list(tasks)
    for task in live:
        if task.kind == "synthesis":
            return task
    from ..artifacts.versioning import topological  # review P2-10: order by edges, not ordinal

    ordered = topological(live, {task.id: task for task in live})
    depended = {dep for task in ordered for dep in task.dependency_ids}
    leaves = [task for task in ordered if task.id not in depended and task.kind != "conflict"]
    return leaves[-1] if leaves else ordered[-1]


__all__ = (
    "ARBITRATION_PREFIX",
    "inherit_limits",
    "CONFLICT_MAX_ATTEMPTS",
    "CONFLICT_POLICY",
    "arbitration_dir",
    "conflict_task",
    "synthesis_task",
    "system_reserve_tokens",
    "terminal_task",
)
