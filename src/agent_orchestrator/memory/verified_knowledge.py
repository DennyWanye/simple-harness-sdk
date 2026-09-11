# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Verified Knowledge (§11 layer 3, plan D4-3): the projection of VERIFIED claims that
other Agents may depend on, stored separately from the claims (30-07) and carrying
the full provenance of §11.2 (30-08): who proposed it, from which Task / Attempt /
Result, on which evidence, verified by what, depending on which knowledge, used by
which Tasks, superseding / superseded by what.

Records are written only by the Commit Service inside the accept transaction; a
claim below VERIFIED never appears here — SUPPORTED is evidence, not knowledge
(理论 04-9).  ``KnowledgeIndex`` is the read model the Verifier uses to check
``used_knowledge`` references (D4-4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from ..contracts import ClaimStatus, ContractError
from ..contracts.models import _object, _text, _texts

if TYPE_CHECKING:
    from ..storage.store import Store

KNOWLEDGE_STATES = ("VERIFIED", "SUPERSEDED")


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    id: str  # = the VERIFIED claim's id
    mission_id: str
    claim_id: str
    content: str
    type: str
    status: str
    version: int
    key: str | None
    stance: str
    proposed_by: str
    source_task: str
    source_attempt: str
    source_result: str
    evidence: tuple[str, ...]
    verifier: Mapping[str, Any]
    dependencies: tuple[str, ...]
    created_at: float
    used_by: tuple[str, ...] = ()
    supersedes: str | None = None
    superseded_by: str | None = None
    disputed_by: tuple[str, ...] = ()
    confirmed_by: tuple[str, ...] = ()
    resolves: tuple[str, ...] = ()
    evidence_trust: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "id",
            "mission_id",
            "claim_id",
            "source_task",
            "source_attempt",
            "source_result",
        ):
            object.__setattr__(
                self, name, _text(getattr(self, name), f"knowledge.{name}", limit=512)
            )
        object.__setattr__(self, "content", _text(self.content, "knowledge.content"))
        if self.status not in KNOWLEDGE_STATES:
            raise ContractError(f"knowledge.status must be one of {list(KNOWLEDGE_STATES)}")
        object.__setattr__(self, "verifier", _object(self.verifier, "knowledge.verifier"))
        for name in (
            "evidence",
            "dependencies",
            "used_by",
            "disputed_by",
            "confirmed_by",
            "resolves",
            "evidence_trust",
        ):
            object.__setattr__(self, name, _texts(getattr(self, name), f"knowledge.{name}"))

    def to_json(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        for name, value in list(data.items()):
            if isinstance(value, tuple):
                data[name] = list(value)
            elif isinstance(value, Mapping):
                data[name] = dict(value)
        return data

    @classmethod
    def from_json(cls, value: object) -> KnowledgeRecord:
        data = _object(value, "knowledge")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in data:
                raw = data[f.name]
                kwargs[f.name] = tuple(raw) if isinstance(raw, list) else raw
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class KnowledgeIndex:
    """Read model of one Mission's knowledge and claim statuses (D4-4)."""

    mission_id: str
    records: Mapping[str, KnowledgeRecord]
    claim_status: Mapping[str, ClaimStatus]

    @classmethod
    def load(cls, store: Store, mission_id: str) -> KnowledgeIndex:
        records = {record.id: record for record in store.list_knowledge(mission_id)}
        claims = {claim.id: claim.status for claim in store.list_mission_claims(mission_id)}
        return cls(mission_id=mission_id, records=records, claim_status=claims)

    @classmethod
    def empty(cls, mission_id: str) -> KnowledgeIndex:
        return cls(mission_id=mission_id, records={}, claim_status={})

    def verified(self) -> list[KnowledgeRecord]:
        return [r for r in self.records.values() if r.status == "VERIFIED"]

    def check(self, used_knowledge: Sequence[str]) -> list[str]:
        """Problems with ``used_knowledge`` references (empty list = all usable)."""

        problems: list[str] = []
        for reference in used_knowledge:
            record = self.records.get(reference)
            if record is None:
                status = self.claim_status.get(reference)
                if status is None:
                    if ":claim-" in reference:  # shaped like a claim id → another Mission's
                        problems.append(
                            f"used_knowledge {reference!r} is not in this Mission's "
                            "Verified Knowledge"
                        )
                    else:
                        problems.append(f"used_knowledge {reference!r} is unknown")
                else:
                    problems.append(
                        f"used_knowledge {reference!r} is a claim in status {status}, "
                        "not VERIFIED knowledge"
                    )
                continue
            if record.status == "SUPERSEDED":
                problems.append(
                    f"used_knowledge {reference!r} is SUPERSEDED; "
                    f"the current version is {record.superseded_by!r}"
                )
        return problems


__all__ = ("KNOWLEDGE_STATES", "KnowledgeIndex", "KnowledgeRecord")
