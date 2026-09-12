# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Mission uncertainty derives from accepted Task evidence and the original catalog.

This is not a semantic judge or a source trust upgrade. Structural/execution checks
remain with their actual verifiers; a model candidate alone proves nothing.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from ..contracts import ClaimStatus, ContractError, Mission, TaskStatus, ids
from ..contracts.models import sha256_hex
from ..governance.domains import DOC_DOMAIN, DomainProfileV1
from .assessments import (
    accepted_assessments_for,
    mission_contract_revision,
    mission_criterion_catalog,
    normalise_literal,
)

if TYPE_CHECKING:
    from ..storage.store import Store


def mission_coverage(store: Store, mission: Mission, domain: DomainProfileV1) -> dict[str, Any]:
    if domain.id != DOC_DOMAIN or domain.version != "3":
        raise ContractError("Mission coverage requires the frozen document v3 profile")
    catalog = mission_criterion_catalog(mission)
    evaluations = []
    for task in store.list_tasks(mission.id):
        if task.status is TaskStatus.CANCELLED or (
            task.paused and task.status in {TaskStatus.READY, TaskStatus.BLOCKED}
        ):
            continue
        if task.status is not TaskStatus.COMPLETED:
            continue
        binding, assessments = accepted_assessments_for(store, task=task)
        grouped: dict[str, list[Any]] = defaultdict(list)
        for assessment in assessments:
            grouped[assessment.claim_id].append(assessment)
        for ordinal, proposal in enumerate(binding.envelope.claims, 1):
            cid = ids.claim_id(binding.result_id, ordinal)
            claim = store.get_claim(cid)
            rows = grouped.get(cid, [])
            evaluations.append((binding, ordinal, proposal, claim, rows))
    verdicts = []
    for criterion in catalog:
        kind, text = criterion["kind"], criterion["text"]
        item: dict[str, Any] = {
            **dict(criterion),
            "verdict": "FAIL",
            "reasons": [],
            "claim_ids": [],
            "task_assessment_receipt_ids": [],
            "limitations": [],
        }
        if kind in {"file", "action", "arbitration"}:
            item["verdict"] = "STRUCTURAL"
            item["reasons"] = ["requires_actual_structural_or_execution_check"]
            verdicts.append(item)
            continue
        passed, uncertain, failed = False, False, False
        for binding, ordinal, proposal, claim, rows in evaluations:
            candidate = criterion["criterion_id"] in proposal.mission_criterion_ids
            matches = (
                any(c.path == text.removeprefix("cite:") for c in proposal.citations)
                if kind == "cite"
                else normalise_literal(proposal.content) == normalise_literal(text)
            )
            if not candidate and not matches:
                continue
            if claim is None or claim.status not in {
                ClaimStatus.VERIFIED,
                ClaimStatus.SUPPORTED,
                ClaimStatus.UNDER_REVIEW,
            }:
                failed = True
                item["reasons"].append("claim_not_usable")
                continue
            if not rows:
                failed = True
                item["reasons"].append("missing_valid_task_assessment")
                continue
            if any(r.verdict not in {"PASS", "INCONCLUSIVE"} for r in rows):
                failed = True
                item["reasons"].append("task_assessment_not_usable")
                continue
            refs = [ref for row in rows for ref in row.evidence_refs]
            if not refs or any(ref.get("status") != "resolved" for ref in refs):
                failed = True
                item["reasons"].append("citation_not_resolved")
                continue
            inconclusive = [r for r in rows if r.verdict == "INCONCLUSIVE"]
            if inconclusive:
                if not candidate:
                    failed = True
                    item["reasons"].append("missing_mission_candidate_binding")
                    continue
                limitations = [
                    limitation
                    for limitation in binding.envelope.limitations
                    if limitation.claim_id == f"claim:{ordinal}"
                    and limitation.criterion_id in {r.criterion_id for r in inconclusive}
                ]
                if {limitation.criterion_id for limitation in limitations} != {
                    r.criterion_id for r in inconclusive
                }:
                    failed = True
                    item["reasons"].append("missing_task_limitations")
                    continue
                uncertain = True
                item["reasons"].append("task_evidence_inconclusive")
                item["limitations"].extend(
                    {
                        "task_id": binding.task_id,
                        "result_id": binding.result_id,
                        "claim_id": claim.id,
                        "task_criterion_id": limitation.criterion_id,
                        "missing": limitation.missing,
                    }
                    for limitation in limitations
                )
            elif matches:
                passed = True
            else:
                failed = True
                item["reasons"].append("candidate_without_task_uncertainty_or_content_binding")
                continue
            item["claim_ids"].append(claim.id)
            item["task_assessment_receipt_ids"].extend(r.receipt_id for r in rows)
        item["verdict"] = (
            "FAIL" if failed else "INCONCLUSIVE" if uncertain else "PASS" if passed else "FAIL"
        )
        if not passed and not uncertain and not item["reasons"]:
            item["reasons"].append("no_accepted_content_binding")
        item["reasons"] = sorted(set(item["reasons"]))
        item["claim_ids"] = sorted(set(item["claim_ids"]))
        item["task_assessment_receipt_ids"] = sorted(set(item["task_assessment_receipt_ids"]))
        item["limitations"] = sorted(item["limitations"], key=sha256_hex)
        verdicts.append(item)
    numerator = sum(item["verdict"] == "INCONCLUSIVE" for item in verdicts)
    denominator = len(catalog)
    limit = domain.completion_rules.get("inconclusive_share_limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 <= limit <= 1:
        raise ContractError("invalid frozen Mission inconclusive share limit")
    share = numerator / denominator if denominator else 0.0
    body = {
        "schema": 1,
        "mission_id": mission.id,
        "mission_contract_revision": mission_contract_revision(mission),
        "criteria": verdicts,
        "numerator": numerator,
        "denominator": denominator,
        "share": share,
        "limit": limit,
        "insufficient": share > limit,
    }
    return {**body, "hash": sha256_hex(body)}


__all__ = ("mission_coverage",)
