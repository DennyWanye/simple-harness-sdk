# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The Blackboard (§11, 理论 04): what the team already knows, in four layers —
Raw Logs (references only), Candidate Claims, Verified Knowledge, Summaries.

This facade is **read-only by construction**: it exposes no write method at all.
Every layer is written by the Commit Service inside its own transactions, so no
Agent (and no Context Builder) can turn a candidate into knowledge (原则三).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..contracts import Claim, ClaimStatus
from ..storage.store import Store
from .verified_knowledge import KnowledgeRecord


class Blackboard:
    def __init__(self, store: Store) -> None:
        self._store = store

    # layer 1 — Raw Logs: references, never the bodies
    def raw_refs(self, mission_id: str) -> dict[str, Any]:
        tasks = self._store.list_tasks(mission_id)
        attempts = [a for t in tasks for a in self._store.list_attempts(t.id)]
        results = [self._store.find_result_for_attempt(a.id) for a in attempts]
        return {
            "events": self._store.count_events(mission_id),
            "results": [r.envelope.id for r in results if r is not None],
            "artifacts": [a.id for a in self._store.list_mission_artifacts(mission_id)],
        }

    # layer 2 — Candidate Claims: everything that is not (yet) knowledge
    def candidate_claims(
        self, mission_id: str, *, statuses: Sequence[ClaimStatus] | None = None
    ) -> list[Claim]:
        wanted = None if statuses is None else set(statuses)
        return [
            claim
            for claim in self._store.list_mission_claims(mission_id)
            if claim.status is not ClaimStatus.VERIFIED
            and (wanted is None or claim.status in wanted)
        ]

    # layer 3 — Verified Knowledge
    def verified_knowledge(self, mission_id: str) -> list[KnowledgeRecord]:
        return self._store.list_knowledge(mission_id, status="VERIFIED")

    def superseded_knowledge(self, mission_id: str) -> list[KnowledgeRecord]:
        return self._store.list_knowledge(mission_id, status="SUPERSEDED")

    # layer 4 — Summaries
    def summaries(self, mission_id: str) -> list[dict[str, Any]]:
        return self._store.list_summaries(mission_id)


__all__ = ("Blackboard",)
