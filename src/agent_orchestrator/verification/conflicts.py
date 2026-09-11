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

from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts import Claim, ClaimStatus

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


def find_contradiction(claim: Claim, existing: Sequence[Claim]) -> Contradiction | None:
    """The first existing claim ``claim`` contradicts (deterministic order: by id)."""

    for other in sorted(existing, key=lambda item: item.id):
        if other.id == claim.id or other.mission_id != claim.mission_id:
            continue
        if other.status not in CONFLICTABLE or other.source_task == claim.source_task:
            continue
        if other.id in claim.contradicts:
            return Contradiction(
                claim, other, other.key or claim.key or f"claim:{other.id}", "explicit"
            )
        if claim.key is not None and other.key == claim.key and other.stance != claim.stance:
            return Contradiction(claim, other, claim.key, "stance")
    return None


__all__ = ("CONFLICTABLE", "Contradiction", "find_contradiction")
