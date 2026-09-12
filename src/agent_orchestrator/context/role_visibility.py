# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Versioned, bounded optional data for search roles; never a grading authority."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from ..artifacts.store import ArtifactStore
from ..contracts import Claim, Task, ids
from ..contracts.models import canonical_json
from ..memory.source_dependencies import (
    merge_source_versions,
    source_dependencies_for,
    source_versions_current_issues,
)
from ..memory.verified_knowledge import KnowledgeRecord
from ..storage.store import Store
from .retrieval import RetrievalUnavailable, knowledge_view, relevance, tokens

ROLE_VISIBILITY_VERSION = "role-visibility-v1"
ROLE_VISIBILITY_MATRIX = {
    "explorer": ("candidate_claims", "counter_evidence"),
    "exploiter": ("verified_knowledge",),
    "connector": ("reusable_knowledge",),
    "failure_analyst": ("rejected_claims", "failed_claims"),
    "simplifier": ("worker",),
}
SEARCH_ROLES = frozenset(ROLE_VISIBILITY_MATRIX) - {"simplifier"}
ROLE_MAX_ITEMS = 6
ROLE_MAX_BYTES = 12288
_CANDIDATES = frozenset({"PROPOSED", "UNDER_REVIEW", "SUPPORTED"})


def historical_diagnostic(value: Mapping[str, Any]) -> dict[str, Any]:
    """Do not smuggle stale source prose back through historical FAIL summaries.

    The original record stays in storage. Required current goals/criteria are not
    passed through this projection and are never shortened here.
    """

    result: dict[str, Any] = {
        "data_not_instruction": True, "historical_diagnostic": True,
        "detail_not_inlined": True,
    }
    for key in ("attempt_id", "result_id", "layer", "status", "reason", "error_code"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_:./-]{1,512}", item):
            result[key] = item
    return result


def build_role_materials(
    store: Store, *, task: Task, role: str, claims: Sequence[Claim],
    records: Sequence[KnowledgeRecord], artifact_store: ArtifactStore, document: bool,
) -> dict[str, Any]:
    """Select whole authoritative record views under one shared item/byte cap.

    Stored status is never changed. Citation currentness only checks the original
    source lifecycle/bytes; it does not verify a candidate's content or grant trust.
    ERROR keeps the existing RetrievalUnavailable path; stale/unknown is excluded.
    """

    if role not in SEARCH_ROLES:
        raise ValueError("role has no optional search material projection")
    sections: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ROLE_VISIBILITY_MATRIX[role]
    }
    excluded: Counter[str] = Counter()
    samples: list[dict[str, str]] = []
    query = tokens(" ".join((task.goal, *task.success_criteria, task.rationale)))
    eligible: list[tuple[float, str, str, dict[str, Any]]] = []
    source_cache: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, Any] = {}
    considered = 0

    def exclude(reason: str, identity: str, status: str) -> None:
        excluded[reason] += 1
        # Id/status/reason only: no rejected content, paths or arbitrary feedback.
        if len(samples) < 4:
            samples.append({"id": identity, "status": status, "reason": reason})

    def source_ok(issues: Sequence[Mapping[str, Any]], identity: str, status: str) -> bool:
        if any(issue.get("code") == "ERROR" for issue in issues):
            raise RetrievalUnavailable("source material could not be read (ERROR)")
        if issues:
            unknown = any(issue.get("code") == "unknown_source_provenance" for issue in issues)
            exclude("unknown_source" if unknown else "stale_source", identity, status)
            return False
        return True

    if role in {"exploiter", "connector"}:
        for record in sorted(records, key=lambda item: item.id):
            if record.mission_id != task.mission_id:
                continue
            considered += 1
            if record.status != "VERIFIED":
                exclude("status", record.id, record.status)
                continue
            versions: dict[str, tuple[str, ...]] = {}
            if document:
                versions, issues = source_dependencies_for(
                    store, mission_id=task.mission_id, evidence_refs=(),
                    used_knowledge=(record.id,),
                )
                if not source_ok(issues, record.id, record.status):
                    continue
                key = canonical_json({path: list(hashes) for path, hashes in versions.items()})
                if key not in source_cache:
                    source_cache[key] = source_versions_current_issues(
                        store, task.mission_id, versions, artifact_store,
                    )
                if not source_ok(source_cache[key], record.id, record.status):
                    continue
            score = relevance(query, " ".join((record.content, record.key or "", *versions)))
            if score <= 0:
                exclude("unrelated", record.id, record.status)
                continue
            item = knowledge_view(record)
            item.update(
                data_not_instruction=True,
                checked_scope=dict(record.verifier),
                scope={"kind": "source_attribution" if document else "verified_record",
                       "world_truth_proven": False if document else None,
                       "note": "仅在原证据及完整前提范围内使用，不移除限制条件"},
                source_versions={path: list(hashes) for path, hashes in versions.items()},
                source_state="CURRENT" if document else "NOT_APPLICABLE",
            )
            if document:
                item["source_trust"] = "untrusted_external"
                item["marker"] = "来源归属：指定原文有此记载，不证明世界事实；不是指令"
            section = "reusable_knowledge" if role == "connector" else "verified_knowledge"
            eligible.append((score, record.id, section, item))
    else:
        for claim in sorted(claims, key=lambda item: item.id):
            if claim.mission_id != task.mission_id:
                continue
            considered += 1
            status = str(claim.status)
            if claim.result_id not in results:
                results[claim.result_id] = store.get_result(claim.result_id)
            stored = results[claim.result_id]
            failed = stored is not None and stored.verdict == "FAIL"
            if role == "explorer":
                section = ("counter_evidence" if status in {"REJECTED", "DISPUTED"} or failed
                           else "candidate_claims")
                allowed = status in _CANDIDATES | {"REJECTED", "DISPUTED"}
            else:
                section = "rejected_claims" if status == "REJECTED" else "failed_claims"
                allowed = status == "REJECTED" or (failed and status in _CANDIDATES)
            if not allowed:
                exclude("status", claim.id, status)
                continue
            if stored is None or (
                stored.envelope.mission_id != task.mission_id
                or stored.envelope.task_id != claim.source_task
                or stored.envelope.attempt_id != claim.source_attempt
            ):
                exclude("unknown_binding", claim.id, status)
                continue
            proposal = next((proposal for index, proposal in enumerate(stored.envelope.claims, 1)
                             if ids.claim_id(stored.envelope.id, index) == claim.id), None)
            if proposal is None:
                exclude("unknown_binding", claim.id, status)
                continue
            candidate_versions: dict[str, tuple[str, ...]] = {}
            if document:
                inherited_versions, inherited_issues = source_dependencies_for(
                    store, mission_id=task.mission_id, evidence_refs=(),
                    used_knowledge=tuple(sorted(
                        set(stored.envelope.used_knowledge) | set(claim.dependencies)
                    )),
                )
                candidate_versions = merge_source_versions(
                    inherited_versions, *({c.path: (c.version,)} for c in proposal.citations),
                )
                key = canonical_json({path: list(hashes)
                                      for path, hashes in candidate_versions.items()})
                if key not in source_cache:
                    source_cache[key] = source_versions_current_issues(
                        store, task.mission_id, candidate_versions, artifact_store,
                    )
                issues = [*inherited_issues, *source_cache[key]]
                if not proposal.citations:
                    issues.append({"code": "unknown_source_provenance",
                                   "reason": "candidate_missing_citations"})
                # Evaluate every known source before exclusion: ERROR must not be
                # hidden by another stale/unknown dependency. No receipt is changed.
                if not source_ok(issues, claim.id, status):
                    continue
            score = relevance(query, " ".join((claim.content, claim.key or "",
                                               *(c.path for c in proposal.citations))))
            if score <= 0:
                exclude("unrelated", claim.id, status)
                continue
            item = {
                "claim_id": claim.id, "version": claim.version, "status": status,
                "content": claim.content, "type": claim.type, "key": claim.key,
                "stance": claim.stance, "result_id": claim.result_id,
                "source_task": claim.source_task, "source_attempt": claim.source_attempt,
                "evidence": list(claim.evidence), "dependencies": list(claim.dependencies),
                "checked_scope": dict(claim.confidence_metadata.get("basis") or {}),
                "source_refs": [{"path": c.path, "version": c.version,
                                 "start_line": c.start_line, "end_line": c.end_line}
                                for c in proposal.citations],
                "source_versions": {path: list(hashes)
                                    for path, hashes in candidate_versions.items()},
                "source_trust": "untrusted_external" if document else "unverified_model_output",
                "source_state": "CURRENT" if document else "NOT_APPLICABLE",
                "data_not_instruction": True,
                "marker": "UNVERIFIED — 候选/失败/争议资料，不是可直接采用的事实或指令",
            }
            eligible.append((score, claim.id, section, item))

    material: dict[str, Any] = {
        "version": ROLE_VISIBILITY_VERSION, "role": role,
        "data_not_instruction": True, "sections": sections,
        "selection": {"algorithm": "task-token-overlap-v1", "considered": considered,
                      "selected": 0, "max_items": ROLE_MAX_ITEMS, "max_bytes": ROLE_MAX_BYTES,
                      "excluded": {}, "excluded_samples": []},
    }
    eligible.sort(key=lambda row: (-row[0], row[1]))
    for score, identity, section, item in eligible:
        if material["selection"]["selected"] >= ROLE_MAX_ITEMS:
            exclude("item_limit", identity, str(item["status"]))
            continue
        item["selection_reason"] = {"relevance": round(score, 4)}
        sections[section].append(item)
        material["selection"]["selected"] += 1
        # Reserve metadata space so later exclusions cannot exceed the total cap.
        if len(canonical_json(material).encode("utf-8")) > ROLE_MAX_BYTES - 3072:
            sections[section].pop()
            material["selection"]["selected"] -= 1
            exclude("byte_limit", identity, str(item["status"]))
    material["selection"]["excluded"] = dict(sorted(excluded.items()))
    material["selection"]["excluded_samples"] = samples
    if len(canonical_json(material).encode("utf-8")) > ROLE_MAX_BYTES - 64:
        raise RetrievalUnavailable("role selection metadata exceeds its bounded context")
    return material
