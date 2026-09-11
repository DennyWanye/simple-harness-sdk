# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Knowledge retrieval for the Context Builder (§10.1, 理论 04-4, plan D4-9).

Retrieval is *not* vector similarity: a deterministic score combines semantic
relevance (token overlap between the Task and the knowledge), the knowledge's
trust level, its DAG distance to the Task, its age and its reuse value, then
removes duplicates and superseded entries.  The permission pre-filter is the
Mission boundary (S4-06): only this Mission's records are ever considered.  The
weights below are this build's implementation convention (plan §6.1), versioned
as ``RETRIEVAL_VERSION`` so every context package records which ranking it saw
(30-29).

The result is small: ids, scores and the reasons; the builder reads the full
records back from the store (回读原文) — never a paraphrase.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Claim, ClaimStatus, Task
from ..memory.verified_knowledge import KnowledgeRecord

RETRIEVAL_VERSION = "retrieval-v1"
WEIGHTS = {"relevance": 3.0, "trust": 2.0, "proximity": 1.0, "recency": 0.5, "reuse": 0.5}
# Only Verified Knowledge is ranked in this build (plan §6.1 / review P2-6): the trust
# factor is constant for it and is kept as a weighted term so a later build that ranks
# candidate claims for explorer/critic templates changes RETRIEVAL_VERSION, not the shape.
TRUST = {"VERIFIED": 1.0}
DEFAULT_LIMIT = 12
_TOKEN = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


class RetrievalUnavailable(RuntimeError):
    """The knowledge index could not be read (S4-07): the caller must degrade or block
    explicitly — never report "no knowledge"."""


def tokens(text: str) -> frozenset[str]:
    return frozenset(t.lower() for t in _TOKEN.findall(text or ""))


def relevance(query: frozenset[str], text: str) -> float:
    """Overlap coefficient of the token sets (|q ∩ c| / min(|q|, |c|)): a short, exact
    statement is not penalised for the query's length."""

    candidate = tokens(text)
    if not query or not candidate:
        return 0.0
    return len(query & candidate) / min(len(query), len(candidate))


def normalised_content(content: str) -> str:
    return " ".join(sorted(tokens(content)))


@dataclass(frozen=True, slots=True)
class Scored:
    id: str
    score: float
    parts: Mapping[str, float]
    status: str
    key: str | None
    stance: str
    source_task: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": round(self.score, 4),
            "parts": {k: round(v, 4) for k, v in self.parts.items()},
            "status": self.status,
            "key": self.key,
            "stance": self.stance,
            "source_task": self.source_task,
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    version: str
    status: str  # ok | unavailable | disabled
    items: tuple[Scored, ...]
    superseded: tuple[Mapping[str, Any], ...]
    dropped: Mapping[str, Any]
    considered: int
    reason: str | None = None
    limit: int = DEFAULT_LIMIT

    def to_json(self) -> dict[str, Any]:
        return {
            "retrieval_version": self.version,
            "status": self.status,
            "reason": self.reason,
            "considered": self.considered,
            "returned": len(self.items),
            "limit": self.limit,
            "dropped": {
                k: (dict(v) if isinstance(v, Mapping) else list(v)) for k, v in self.dropped.items()
            },
            "items": [item.to_json() for item in self.items],
            "superseded": [dict(item) for item in self.superseded],
        }

    @classmethod
    def unavailable(cls, reason: str, *, status: str = "unavailable") -> RetrievalResult:
        return cls(RETRIEVAL_VERSION, status, (), (), {}, 0, reason=reason)


def dag_proximity(task: Task, source_task: str, tasks_by_id: Mapping[str, Task]) -> float:
    """1.0 for an ancestor (or the Task itself), 0.6 for a task under the same root,
    0.3 otherwise (§10.1 "Task DAG 距离" / "分支相关性")."""

    if source_task == task.id:
        return 1.0
    ancestors = _ancestor_ids(task.id, tasks_by_id)
    if source_task in ancestors:
        return 1.0
    if _roots(task.id, tasks_by_id) & _roots(source_task, tasks_by_id):
        return 0.6
    return 0.3


def _ancestor_ids(task_id: str, tasks_by_id: Mapping[str, Task]) -> set[str]:
    seen: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        task = tasks_by_id.get(current)
        if task is None:
            continue
        for dep in task.dependency_ids:
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _roots(task_id: str, tasks_by_id: Mapping[str, Task]) -> set[str]:
    task = tasks_by_id.get(task_id)
    if task is None:
        return set()
    if not task.dependency_ids:
        return {task_id}
    return {a for a in _ancestor_ids(task_id, tasks_by_id) if not tasks_by_id[a].dependency_ids}


def rank_knowledge(
    task: Task,
    records: Sequence[KnowledgeRecord],
    *,
    tasks_by_id: Mapping[str, Task],
    limit: int = DEFAULT_LIMIT,
    query_text: str | None = None,
) -> RetrievalResult:
    """Deterministic ranking of one Mission's knowledge for ``task`` (D4-9)."""

    query = tokens(query_text or " ".join((task.goal, *task.success_criteria, task.rationale)))
    own = [r for r in records if r.mission_id == task.mission_id]  # permission pre-filter
    considered = len(own)
    superseded: list[dict[str, Any]] = [
        {"id": r.id, "superseded_by": r.superseded_by, "key": r.key, "status": r.status}
        for r in own
        if r.status == "SUPERSEDED"
    ]
    superseded_ids = [str(item["id"]) for item in superseded]
    live = [r for r in own if r.status == "VERIFIED"]
    if not live:
        return RetrievalResult(
            RETRIEVAL_VERSION,
            "ok",
            (),
            tuple(superseded),
            {"superseded": superseded_ids},
            considered,
            limit=limit,
        )
    newest = max(r.created_at for r in live)
    oldest = min(r.created_at for r in live)
    span = max(newest - oldest, 1e-9)
    scored: list[Scored] = []
    for record in live:
        parts = {
            "relevance": relevance(query, " ".join((record.content, record.key or ""))),
            "trust": TRUST.get(record.status, 0.0),
            "proximity": dag_proximity(task, record.source_task, tasks_by_id),
            "recency": (record.created_at - oldest) / span if newest > oldest else 1.0,
            "reuse": min(len(record.used_by), 3) / 3.0,
        }
        score = sum(WEIGHTS[name] * value for name, value in parts.items())
        scored.append(
            Scored(
                record.id,
                score,
                parts,
                record.status,
                record.key,
                record.stance,
                record.source_task,
            )
        )
    scored.sort(key=lambda item: (-item.score, item.id))
    kept: list[Scored] = []
    duplicates: list[str] = []
    duplicate_of: dict[str, str] = {}  # P2-7: dropped id → the record that represents it
    representative: dict[str, str] = {}
    by_id = {r.id: r for r in live}
    for item in scored:
        record = by_id[item.id]
        subject = (
            f"key:{record.key}:{record.stance}"
            if record.key
            else f"content:{normalised_content(record.content)}"
        )
        if subject in representative:
            duplicates.append(item.id)
            duplicate_of[item.id] = representative[subject]
            continue
        representative[subject] = item.id
        kept.append(item)
    truncated = [item.id for item in kept[limit:]]
    return RetrievalResult(
        RETRIEVAL_VERSION,
        "ok",
        tuple(kept[:limit]),
        tuple(superseded),
        {
            "duplicate": duplicates,
            "duplicate_of": duplicate_of,
            "superseded": superseded_ids,
            "over_limit": truncated,
        },
        considered,
        limit=limit,
    )


def disputed_claims(claims: Sequence[Claim], *, mission_id: str) -> list[dict[str, Any]]:
    """§10 item 7: contested claims, always marked, never as facts."""

    return [
        {
            "claim_id": claim.id,
            "status": str(claim.status),
            "key": claim.key,
            "stance": claim.stance,
            "content": claim.content,
            "evidence": list(claim.evidence),  # P2-8: the verifier sees the references
            "source_task": claim.source_task,
            "conflict_id": claim.conflict_id,
            "resolved_by": claim.resolved_by,
            "marker": "DISPUTED — 争议中，不是事实",
        }
        for claim in claims
        if claim.mission_id == mission_id and claim.status is ClaimStatus.DISPUTED
    ]


def candidate_claims(
    claims: Sequence[Claim], *, mission_id: str, statuses: Sequence[ClaimStatus]
) -> list[dict[str, Any]]:
    wanted = set(statuses)
    return [
        {
            "claim_id": claim.id,
            "status": str(claim.status),
            "key": claim.key,
            "stance": claim.stance,
            "content": claim.content,
            "source_task": claim.source_task,
            "evidence": list(claim.evidence),
            "marker": "UNVERIFIED — 候选结论，不能当作事实",
        }
        for claim in claims
        if claim.mission_id == mission_id and claim.status in wanted
    ]


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    """What the Context Builder puts into a package (visibility applied there)."""

    retrieval: RetrievalResult
    verified: tuple[Mapping[str, Any], ...] = ()
    disputed: tuple[Mapping[str, Any], ...] = ()
    candidates: tuple[Mapping[str, Any], ...] = ()
    rejected: tuple[Mapping[str, Any], ...] = ()
    branch_summary: Mapping[str, Any] | None = None
    global_summary: Mapping[str, Any] | None = None
    summary_status: Mapping[str, Any] = field(default_factory=lambda: {"status": "ok"})
    raw_refs: Mapping[str, Any] | None = None  # §11 layer 1: references only (P2-11)

    @classmethod
    def unavailable(cls, reason: str, *, status: str = "unavailable") -> KnowledgeContext:
        return cls(
            retrieval=RetrievalResult.unavailable(reason, status=status),
            summary_status={"status": status, "reason": reason},
        )

    @property
    def frozen_ids(self) -> list[dict[str, Any]]:
        return [{"id": item["id"], "version": item["version"]} for item in self.verified]


def knowledge_view(record: KnowledgeRecord, scored: Scored | None = None) -> dict[str, Any]:
    """The full record as offered to an Agent (回读原文): id/version first, then the
    statement, its subject, its verifier and its provenance."""

    return {
        "id": record.id,
        "version": record.version,
        "status": record.status,
        "key": record.key,
        "stance": record.stance,
        "content": record.content,
        "type": record.type,
        "verifier": dict(record.verifier),
        "evidence": list(record.evidence),
        "source_task": record.source_task,
        "source_attempt": record.source_attempt,
        "dependencies": list(record.dependencies),
        "used_by": list(record.used_by),
        "disputed_by": list(record.disputed_by),
        "score": None if scored is None else round(scored.score, 4),
    }


__all__ = (
    "DEFAULT_LIMIT",
    "RETRIEVAL_VERSION",
    "TRUST",
    "WEIGHTS",
    "KnowledgeContext",
    "RetrievalResult",
    "RetrievalUnavailable",
    "Scored",
    "candidate_claims",
    "dag_proximity",
    "disputed_claims",
    "knowledge_view",
    "rank_knowledge",
    "relevance",
    "tokens",
)
