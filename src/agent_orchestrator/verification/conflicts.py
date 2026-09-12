# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic conflict detection (§14.4, 理论 10-7 / 10-15, plan D4-6').

A claim ``C`` of a result being accepted conflicts with an existing claim ``X`` of
the same Mission when ``C.contradicts`` names ``X`` (or its knowledge id) or both
carry the same subject ``key`` with different stances.  Only claims that reached
acceptance count as ``X`` (VERIFIED knowledge or SUPPORTED / UNDER_REVIEW
candidates of accepted results); REJECTED and SUPERSEDED ones are history.  A
retry of the same Task never conflicts with itself, and no count of claims is ever
taken: **detection is not a vote**.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..contracts import (
    Claim,
    ClaimStatus,
    ContractError,
    CriterionAssessmentV1,
    ResultEnvelope,
    ids,
)
from ..memory.claims import grade_claim, system_attribution

if TYPE_CHECKING:
    from ..governance.domains import DomainProfileV1
    from ..storage.store import Store

CONFLICTABLE = frozenset(
    {ClaimStatus.VERIFIED, ClaimStatus.SUPPORTED, ClaimStatus.UNDER_REVIEW, ClaimStatus.DISPUTED}
)


@dataclass(frozen=True, slots=True)
class Contradiction:
    claim: Claim
    other: Claim
    key: str
    reason: str  # explicit | stance

    @property
    def other_is_knowledge(self) -> bool:
        return self.other.status is ClaimStatus.VERIFIED


def supported_contradiction(claim: Claim, other: Claim) -> bool:
    """Document explicit claims cannot suppress stronger or unrelated evidence."""
    ranks = {ClaimStatus.UNDER_REVIEW: 0, ClaimStatus.SUPPORTED: 1, ClaimStatus.VERIFIED: 2}
    if claim.status not in {ClaimStatus.SUPPORTED, ClaimStatus.VERIFIED}:
        return False
    if ranks[claim.status] < ranks.get(other.status, 2):
        return False
    basis = claim.confidence_metadata.get("basis", {})
    if not isinstance(basis, Mapping) or basis.get("system_domain") != "doc-research-v1":
        return False
    if basis.get("adapter") not in {
        "citation_integrity@v1",
        "citation_integrity@v2",
    } or not basis.get("assessment_receipts"):
        return False
    attribution = system_attribution(basis)
    if attribution is not None:
        other_basis = other.confidence_metadata.get("basis", {})
        return (
            isinstance(other_basis, Mapping)
            and system_attribution(other_basis) is not None
            and claim.key == other.key
        )
    return basis.get("grade") == "supported"


def find_contradiction(
    claim: Claim, existing: Sequence[Claim], *, domain: DomainProfileV1 | None = None
) -> Contradiction | None:
    """The first existing claim ``claim`` contradicts (deterministic order: by id)."""

    for other in sorted(existing, key=lambda item: item.id):
        if other.id == claim.id or other.mission_id != claim.mission_id:
            continue
        if other.status not in CONFLICTABLE or other.source_task == claim.source_task:
            continue
        if other.id in claim.contradicts and (
            domain is None
            or domain.id != "doc-research-v1"
            or supported_contradiction(claim, other)
        ):
            return Contradiction(
                claim, other, other.key or claim.key or f"claim:{other.id}", "explicit"
            )
        if claim.key is not None and other.key == claim.key and other.stance != claim.stance:
            return Contradiction(claim, other, claim.key, "stance")
    return None


__all__ = (
    "CONFLICTABLE",
    "Contradiction",
    "find_contradiction",
    "supported_contradiction",
    "uncertainty_conflicts",
    "document_uncertainty_conflicts",
)


def uncertainty_conflicts(
    *,
    proposed: Sequence[Claim],
    inconclusive_claim_ids: frozenset[str],
    existing: Sequence[Claim],
) -> list[dict[str, Any]]:
    """Only uncertainty sides use this rejection; ordinary PASS sides keep arbitration."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    eligible = [c for c in existing if c.status in {ClaimStatus.SUPPORTED, ClaimStatus.VERIFIED}]
    for current in sorted(proposed, key=lambda c: c.id):
        if current.id not in inconclusive_claim_ids or current.key is None:
            continue
        for other in sorted([*proposed, *eligible], key=lambda c: c.id):
            if (
                other.id == current.id
                or other.mission_id != current.mission_id
                or other.key != current.key
                or other.stance == current.stance
            ):
                continue
            pair = (min(current.id, other.id), max(current.id, other.id))
            if pair not in found:
                found[pair] = {
                    "claim_id": current.id,
                    "other_claim_id": other.id,
                    "other_claim_revision": other.version,
                    "key": current.key,
                    "reason": "opposite_stance",
                }
    return [found[key] for key in sorted(found)]


def document_uncertainty_conflicts(
    store: Store,
    *,
    mission_id: str,
    envelope: ResultEnvelope,
    assessments: Sequence[CriterionAssessmentV1],
) -> list[dict[str, Any]]:
    """Read current accepted peers; caller runs this inside its acceptance transaction.

    Assessments must already have passed the shared integrity validator. We derive
    effective keys through the same grading function used by the actual projection.
    """
    from ..governance.domains import DOC_DOMAIN, DomainProfileV1

    domain_row = store.get_mission_domain(mission_id)
    if domain_row is None or domain_row["domain_id"] != DOC_DOMAIN:
        return []
    domain = DomainProfileV1.from_json(domain_row["json"])
    if domain.version != "3":
        return []
    proposed = []
    uncertain = set()
    for ordinal, proposal in enumerate(envelope.claims, 1):
        cid = ids.claim_id(envelope.id, ordinal)
        claim = store.get_claim(cid)
        if claim is None or claim.mission_id != mission_id or claim.result_id != envelope.id:
            raise ContractError("uncertainty conflict claim identity is unavailable")
        grade = grade_claim(
            cid,
            proposal.evidence,
            verifier_results=(),
            artifact_paths=(),
            untrusted_prefixes=(),
            domain=domain,
            proposal=proposal,
            assessments=assessments,
        )
        if grade.basis.get("grade") == "insufficient_evidence":
            uncertain.add(cid)
        attribution = system_attribution(grade.basis)
        key = None if proposal.key and proposal.key.startswith("attribution:") else proposal.key
        proposed.append(
            replace(
                claim,
                status=grade.status,
                content=attribution["content"] if attribution else proposal.content,
                type="attribution" if attribution else "statement",
                key=attribution["key"] if attribution else key,
                stance=attribution["stance"] if attribution else proposal.stance,
                confidence_metadata={"basis": dict(grade.basis), "grade": grade.basis.get("grade")},
            )
        )
    if not uncertain:
        return []
    accepted = []
    for claim in store.list_mission_claims(mission_id):
        if claim.result_id == envelope.id or claim.status not in {
            ClaimStatus.SUPPORTED,
            ClaimStatus.VERIFIED,
        }:
            continue
        stored = store.get_result(claim.result_id)
        task = store.get_task(claim.source_task)
        if (
            stored is not None
            and stored.verification_state == "DONE"
            and stored.verdict == "PASS"
            and task is not None
            and task.accepted_result_id == claim.result_id
        ):
            accepted.append(claim)
    return uncertainty_conflicts(
        proposed=proposed, inconclusive_claim_ids=frozenset(uncertain), existing=accepted
    )
