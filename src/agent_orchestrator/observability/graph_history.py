# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Graph history (ORCH §7.4: "用户应该能在任务图和事件时间线上看到 v1→v2、变更依据、旧产物和
新增工作"): one entry per ``graph_version`` — the Tasks that existed at that version,
the change that produced it (basis, operations, superseded Tasks with their
registered artifacts) — reconstructed from the graph change ledger and the Task
records, never from a model's memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..storage.store import Store


def graph_history(store: Store, mission_id: str) -> dict[str, Any]:
    tasks = store.list_tasks(mission_id)
    changes = store.list_graph_changes(mission_id)
    mission = store.get_mission(mission_id)
    current = int(((mission.final_report if mission else None) or {}).get("graph_version") or 1)
    by_version = {int(c["to_version"]): c for c in changes}

    def task_view(task) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {
            "task_id": task.id,
            "kind": task.kind,
            "goal": task.goal,
            "status": str(task.status),
            "dependencies": list(task.dependency_ids),
            "added_in": int(task.context.get("graph_version") or 1),
            "supersedes_task": task.context.get("supersedes_task"),
            "replaced_by": task.context.get("replaced_by"),
            "accepted_artifacts": list(task.accepted_artifacts),
            "attempts": task.attempt_count,
            "role": task.context.get("role", "worker"),
            "paused": task.paused,
        }

    versions: list[dict[str, Any]] = []
    for version in range(1, current + 1):
        superseded_before = {
            old
            for c in changes
            if int(c["to_version"]) <= version
            for old in c.get("superseded", {})
        }
        cancelled_before = {
            old
            for c in changes
            if int(c["to_version"]) <= version
            for old in c.get("cancelled", [])
        }
        members = [
            t
            for t in tasks
            if int(t.context.get("graph_version") or 1) <= version
            and t.id not in superseded_before
            and t.id not in cancelled_before
        ]
        entry: dict[str, Any] = {
            "version": version,
            "tasks": [task_view(t) for t in members],
            "source": "planner"
            if version == 1
            else str(by_version.get(version, {}).get("source", {}).get("template", "change")),
        }
        change = by_version.get(version)
        if change is not None:
            old_ids = list(change.get("superseded", {}).keys()) + list(change.get("cancelled", []))
            entry["change"] = {
                "change_id": change["change_id"],
                "from_version": change["from_version"],
                "basis": change.get("basis"),
                "rationale": change.get("rationale"),
                "operations": change.get("operations"),
                "new_task_ids": change.get("new_task_ids"),
                "superseded": change.get("superseded"),
                "cancelled": change.get("cancelled"),
                "rebased_from": change.get("rebased_from"),
                "old_work": [  # the superseded / cancelled Tasks' registered artifacts (history)
                    {
                        "task_id": old,
                        "artifacts": [
                            {
                                "artifact_id": a.id,
                                "path": a.path,
                                "version": a.version,
                                "attempt_id": a.attempt_id,
                            }
                            for a in store.list_mission_artifacts(mission_id)
                            if a.task_id == old
                        ],
                    }
                    for old in old_ids
                ],
            }
        versions.append(entry)
    return {"mission_id": mission_id, "current_version": current, "versions": versions}


__all__ = ("graph_history",)
