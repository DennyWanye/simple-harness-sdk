# SPDX-License-Identifier: Apache-2.0
"""Read-only evidence decisions through the registered observation pipeline."""
from __future__ import annotations

from typing import Any

from ..contracts.models import ContractError
from ..contracts.planning_decisions import RequestEvidenceDecision
from ..knowledge.predicates import proposition_key
from ..planning.htn.evidence_round import EvidenceAsk
from ..planning.htn.observation_pipeline import observe_predicate, record_observation


def evidence_authority(world: Any, authority: Any) -> tuple[frozenset[str], frozenset[str]]:
    index = getattr(world, "observers", None)
    if index is None:
        return frozenset(), frozenset()
    installed = frozenset(index.predicate_ids())
    allowed = frozenset(
        f"{signature.predicate_ref.id}@{signature.predicate_ref.version}"
        for signature in world.predicates.signatures()
        if signature.predicate_ref.id in installed
        and signature.authority_scope in (None, authority.scope_id)
        and authority.active and not authority.expired
        and "REQUEST_EVIDENCE" in authority.allowed_decisions
    )
    return installed, allowed


def prepare_questions(world: Any, payload: RequestEvidenceDecision, authority: Any) -> tuple[EvidenceAsk, ...]:
    _, permitted = evidence_authority(world, authority)
    asks = []
    seen = set()
    for question in payload.questions:
        candidates = tuple(s for s in world.predicates.signatures()
            if question.predicate_key in (s.predicate_ref.id, f"{s.predicate_ref.id}@{s.predicate_ref.version}"))
        if len(candidates) != 1:
            raise ContractError("evidence predicate must resolve to one registered version")
        signature = candidates[0]
        if f"{signature.predicate_ref.id}@{signature.predicate_ref.version}" not in permitted:
            raise ContractError("evidence predicate is outside current observer authority")
        if not world.predicates.check_arguments(signature, question.arguments).ok:
            raise ContractError("evidence question arguments do not match the registered schema")
        key = proposition_key(signature, question.arguments)
        if key in seen:
            raise ContractError("evidence questions repeat a grounded proposition")
        seen.add(key)
        asks.append(EvidenceAsk(signature.predicate_ref, dict(question.arguments), key))
    return tuple(asks)


def observe_questions(world: Any, asks: tuple[EvidenceAsk, ...], *, now_ms: int) -> tuple[Any, ...]:
    # External reads happen outside the writer. A crash may repeat a read, never
    # an effect; records and terminal decision receipt are committed together.
    return tuple(observe_predicate(world.observers, ask.predicate_ref, ask.arguments,
                                   now_ms=now_ms) for ask in asks)


def persist_questions(htn: Any, mission_id: str, asks: tuple[EvidenceAsk, ...], outcomes: tuple[Any, ...], *, scope_id: str) -> dict[str, Any]:
    records = []
    for ask, outcome in zip(asks, outcomes, strict=True):
        recorded = record_observation(htn, mission_id, outcome, scope_id=scope_id)
        records.append({"question": ask.to_json(), "recorded": recorded.recorded,
                        "observation_id": None if recorded.record is None else recorded.record.observation_id,
                        "reason": None if recorded.reason is None else str(recorded.reason)})
    return {"questions": records, "recorded_count": sum(r["recorded"] for r in records)}
