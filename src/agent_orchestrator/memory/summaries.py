# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Summaries layer of the Blackboard (§11 layer 4, plan D4-13): per-branch and global
compressions rebuilt by the Commit Service after every accepted result and read
by the Context Builder (§10 item 4 "当前分支摘要")."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..context.compression import GLOBAL_BRANCH, branch_of, compress

if TYPE_CHECKING:
    from ..storage.store import Store


def build_summaries(store: Store, mission_id: str) -> dict[str, dict[str, Any]]:
    """``subject_id → summary`` for every branch plus the global one (pure read)."""

    tasks = store.list_tasks(mission_id)
    tasks_by_id = {task.id: task for task in tasks}
    knowledge = store.list_knowledge(mission_id)
    claims = store.list_mission_claims(mission_id)
    open_conflicts = store.list_conflicts(mission_id, state="OPEN")
    result_summaries: dict[str, str] = {}
    for task in tasks:
        if task.accepted_result_id:
            stored = store.get_result(task.accepted_result_id)
            if stored is not None:
                result_summaries[task.id] = stored.envelope.summary
    branches: dict[str, list] = {}
    for task in tasks:
        branches.setdefault(branch_of(task, tasks_by_id), []).append(task)
    summaries: dict[str, dict[str, Any]] = {}
    for subject_id, members in branches.items():
        summaries[subject_id] = compress(
            scope="branch" if subject_id != GLOBAL_BRANCH else "global",
            subject_id=subject_id,
            tasks=members,
            result_summaries=result_summaries,
            knowledge=knowledge,
            claims=claims,
            open_conflicts=open_conflicts,
        )
    summaries[f"mission:{mission_id}"] = compress(
        scope="mission",
        subject_id=mission_id,
        tasks=tasks,
        result_summaries=result_summaries,
        knowledge=knowledge,
        claims=claims,
        open_conflicts=open_conflicts,
    )
    return summaries


def refresh_summaries(store: Store, mission_id: str) -> Mapping[str, dict[str, Any]]:
    """Recompute and persist (inside the caller's transaction)."""

    summaries = build_summaries(store, mission_id)
    for subject_id, summary in summaries.items():
        store.upsert_summary(
            mission_id,
            scope=str(summary["scope"]),
            subject_id=str(summary["subject_id"]),
            version=str(summary["version"]),
            summary=summary,
        )
    return summaries


__all__ = ("build_summaries", "refresh_summaries")
