# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Summaries layer of the Blackboard (§11 layer 4, plan D4-13): per-branch and global
compressions rebuilt by the Commit Service after every accepted result and read
by the Context Builder (§10 item 4 "当前分支摘要")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..context.compression import GLOBAL_BRANCH, branch_of, compress
from ..contracts.models import sha256_hex

if TYPE_CHECKING:
    from ..storage.store import Store


def build_summaries(
    store: Store,
    mission_id: str,
    *,
    stale: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """``subject_id → summary`` for every branch plus the global one (pure read)."""

    tasks = store.list_tasks(mission_id)
    tasks_by_id = {task.id: task for task in tasks}
    knowledge = store.list_knowledge(mission_id)
    # Currentness is a read projection; retain the accepted records and claims
    # unchanged so historical evidence remains inspectable.
    invalid = {kid: list(issues) for kid, issues in (stale or {}).items() if issues}
    noncurrent = [record for record in knowledge if record.status != "VERIFIED"]
    for record in noncurrent:
        invalid.setdefault(record.id, []).append(
            {
                "code": "noncurrent_knowledge",
                "status": record.status,
                "superseded_by": record.superseded_by,
            }
        )
    stale_records = [record for record in knowledge if record.id in invalid]
    stale_tasks = {record.source_task for record in stale_records}
    affected_by_task: dict[str, set[str]] = {}
    for record in stale_records:
        affected_by_task.setdefault(record.source_task, set()).add(record.id)
    knowledge = [record for record in knowledge if record.id not in invalid]
    claims = store.list_mission_claims(mission_id)
    open_conflicts = store.list_conflicts(mission_id, state="OPEN")
    result_summaries: dict[str, str] = {}
    for task in tasks:
        if task.accepted_result_id:
            stored = store.get_result(task.accepted_result_id)
            if stored is not None:
                inherited = set(stored.envelope.used_knowledge) & set(invalid)
                if inherited:
                    stale_tasks.add(task.id)
                    affected_by_task.setdefault(task.id, set()).update(inherited)
                result_summaries[task.id] = (
                    "来源依赖已失效，历史分析不作为当前结论；需重新核查。"
                    if task.id in stale_tasks
                    else stored.envelope.summary
                )
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
    if affected_by_task:
        for summary in summaries.values():
            task_ids = {item["task_id"] for item in summary["tasks"]}
            affected_ids = set().union(*(affected_by_task.get(tid, set()) for tid in task_ids))
            affected = {
                kid: [dict(issue) for issue in invalid[kid]] for kid in sorted(affected_ids)
            }
            if affected:
                has_noncurrent = any(
                    issue.get("code") == "noncurrent_knowledge"
                    for issues in affected.values() for issue in issues
                )
                summary["summary_version"] = (
                    "summary-current-v3" if has_noncurrent else "summary-doc-source-v2"
                )
                summary["uncertainty"]["stale_knowledge"] = affected
                summary.pop("version")
                summary["version"] = "sum-" + sha256_hex(summary)[:16]
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
