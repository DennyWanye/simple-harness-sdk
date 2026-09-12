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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..contracts import Claim, ClaimStatus
from ..memory.claims import system_attribution

if TYPE_CHECKING:
    from ..governance.domains import DomainProfileV1

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
    if basis.get("adapter") != "citation_integrity@v1" or not basis.get("assessment_receipts"):
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


__all__ = ("CONFLICTABLE", "Contradiction", "find_contradiction", "supported_contradiction")
