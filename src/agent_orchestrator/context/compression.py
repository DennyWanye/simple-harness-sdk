# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic knowledge compression (§11 "知识压缩", 理论 04-6, plan D4-13).

No model is involved in this build: a summary is a stable, hash-versioned
projection of what the library already holds — Tasks, accepted result summaries
(truncated), knowledge ids with their statuses, disputes and open conflicts —
together with its ``sources`` and an explicit ``uncertainty`` block.  A summary
can never change a claim's trust level: there is no code path from here to the
claims table (ORCH §6.2 "摘要不能自动升级知识可信状态").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import Claim, ClaimStatus, Task
from ..contracts.models import sha256_hex
from ..memory.verified_knowledge import KnowledgeRecord

SUMMARY_VERSION = "summary-v1"
SUMMARY_TEXT_LIMIT = 200
GLOBAL_BRANCH = "global"


def truncate(text: str, limit: int = SUMMARY_TEXT_LIMIT) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def branch_of(task: Task, tasks_by_id: Mapping[str, Task]) -> str:
    """D4-13': the branch of a Task is its topologically smallest root; a Task whose
    ancestors span several roots (a join, the synthesis Task) belongs to ``global``."""

    if not task.dependency_ids:
        return task.id
    roots: set[str] = set()
    stack = list(task.dependency_ids)
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        ancestor = tasks_by_id.get(current)
        if ancestor is None:
            continue
        if not ancestor.dependency_ids:
            roots.add(ancestor.id)
        else:
            stack.extend(ancestor.dependency_ids)
    if len(roots) == 1:
        return next(iter(roots))
    return GLOBAL_BRANCH


def compress(
    *,
    scope: str,
    subject_id: str,
    tasks: Sequence[Task],
    result_summaries: Mapping[str, str],
    knowledge: Sequence[KnowledgeRecord],
    claims: Sequence[Claim],
    open_conflicts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    task_ids = {task.id for task in tasks}
    scoped_knowledge = [k for k in knowledge if k.source_task in task_ids]
    scoped_claims = [c for c in claims if c.source_task in task_ids]
    body: dict[str, Any] = {
        "scope": scope,
        "subject_id": subject_id,
        "summary_version": SUMMARY_VERSION,
        "tasks": [
            {
                "task_id": task.id,
                "kind": task.kind,
                "goal": truncate(task.goal, 120),
                "status": str(task.status),
                "accepted_summary": truncate(result_summaries.get(task.id, "")),
            }
            for task in tasks
        ],
        "knowledge": [
            {"id": k.id, "status": k.status, "key": k.key, "stance": k.stance}
            for k in scoped_knowledge
        ],
        "disputed": [c.id for c in scoped_claims if c.status is ClaimStatus.DISPUTED],
        "open_conflicts": [str(c.get("conflict_id")) for c in open_conflicts],
        "sources": {
            "results": sorted({c.result_id for c in scoped_claims}),
            "claims": [c.id for c in scoped_claims],
            "knowledge": [k.id for k in scoped_knowledge],
        },
        "uncertainty": {
            "unverified_claims": sum(
                1
                for c in scoped_claims
                if c.status
                not in {ClaimStatus.VERIFIED, ClaimStatus.SUPERSEDED, ClaimStatus.REJECTED}
            ),
            "disputed_claims": sum(1 for c in scoped_claims if c.status is ClaimStatus.DISPUTED),
            "note": "摘要是派生物，不是验证；知识的可信状态以 knowledge/claims 记录为准",
        },
    }
    body["version"] = "sum-" + sha256_hex(body)[:16]
    return body


__all__ = (
    "GLOBAL_BRANCH",
    "SUMMARY_TEXT_LIMIT",
    "SUMMARY_VERSION",
    "branch_of",
    "compress",
    "truncate",
)
