# SPDX-License-Identifier: Apache-2.0
"""Pure, complete impact calculation; it does not cancel or authorize work."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, TYPE_CHECKING

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from .network_codec import NetworkDocumentV1, decode
from .revision_pins import build_revision_pins

if TYPE_CHECKING:
    from .execution_contracts import PlanEffectSet


@dataclass(frozen=True, slots=True, kw_only=True)
class ConvergenceTarget:
    occurrence_id: str
    task_id: str
    expected_generation: int
    target_kind: str

    def __post_init__(self) -> None:
        from .notification_contracts import _integer, _text

        _text(self.occurrence_id, "occurrence_id")
        _text(self.task_id, "task_id")
        _integer(self.expected_generation, "expected_generation")
        if self.target_kind not in ("RETIRING", "INPUT_REPLACED"):
            raise ContractError("TASKGRAPH_IMPACT_TARGET_KIND_INVALID")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConvergenceImpact:
    mission_id: str
    source_revision: int
    candidate_hash: str
    targets: tuple[ConvergenceTarget, ...]
    preserved_occurrences: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "source_revision": self.source_revision,
            "candidate_hash": self.candidate_hash,
            "targets": [item.to_json() for item in self.targets],
            "preserved_occurrences": list(self.preserved_occurrences),
        }

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode()).hexdigest()


def compute_convergence_impact(
    before: NetworkDocumentV1, candidate: NetworkDocumentV1
) -> ConvergenceImpact:
    if before.mission_id != candidate.mission_id or candidate.revision != before.revision + 1:
        raise ContractError("TASKGRAPH_IMPACT_REVISION_MISMATCH")
    old = decode(before.to_json()).snapshot
    new = decode(candidate.to_json()).snapshot
    old_pins, new_pins = build_revision_pins(before), build_revision_pins(candidate)
    old_members = {item.occurrence_id: item for item in old_pins.member_pins}
    new_members = {item.occurrence_id: item for item in new_pins.member_pins}
    old_bindings = {str(item.task_id): item for item in old.task_bindings}
    new_bindings = {str(item.task_id): item for item in new.task_bindings}
    new_demands = {item.producer_occurrence_id for item in new_pins.demand_refs}
    retired_instances = set(before.adopted_instance_ids) - set(candidate.adopted_instance_ids)
    affected_producers = {
        item.producer_occurrence_id
        for item in old_pins.demand_refs
        if item.consumer_instance_id in retired_instances
    }
    targets: list[ConvergenceTarget] = []
    preserved: list[str] = []
    for identity, member in sorted(old_members.items()):
        binding = old_bindings[member.task_id]
        replacement = new_members.get(identity)
        if replacement is not None and replacement.task_id != member.task_id:
            raise ContractError("TASKGRAPH_OCCURRENCE_TASK_IDENTITY_CHANGED")
        # The candidate explicitly decides membership. Losing a method slot
        # alone is not retirement: independently requested work may remain. The
        # preview/Commit demand gate verifies that every such retained producer
        # still has a real root, slot or independent obligation demand.
        retiring = replacement is None
        changed = False
        if replacement is not None:
            fresh = new_bindings[replacement.task_id]
            # A global plan revision or parent adoption alone never revokes a producer.
            changed = (
                binding.contract_hash,
                binding.contract_revision,
                binding.input_binding_revision,
                binding.dispatch_generation,
            ) != (
                fresh.contract_hash,
                fresh.contract_revision,
                fresh.input_binding_revision,
                fresh.dispatch_generation,
            )
            old_inputs = sorted(
                canonical_json(item.to_json())
                for item in old.data_requirements
                if str(item.consumer_occurrence) == identity
            )
            new_inputs = sorted(
                canonical_json(item.to_json())
                for item in new.data_requirements
                if str(item.consumer_occurrence) == identity
            )
            changed = changed or old_inputs != new_inputs
        if identity in new_demands and (retiring or changed) and identity in affected_producers:
            # Sharing cannot preserve old work while silently changing its meaning.
            raise ContractError("TASKGRAPH_SHARED_PRODUCER_BINDING_CHANGED")
        if retiring or changed:
            targets.append(
                ConvergenceTarget(
                    occurrence_id=identity,
                    task_id=member.task_id,
                    expected_generation=int(binding.dispatch_generation),
                    target_kind="RETIRING" if retiring else "INPUT_REPLACED",
                )
            )
        elif identity in affected_producers:
            preserved.append(identity)
    return ConvergenceImpact(
        mission_id=before.mission_id,
        source_revision=before.revision,
        candidate_hash=hashlib.sha256(canonical_json(candidate.to_json()).encode()).hexdigest(),
        targets=tuple(targets),
        preserved_occurrences=tuple(preserved),
    )


def compute_plan_effects(before: NetworkDocumentV1, candidate: NetworkDocumentV1) -> "PlanEffectSet":
    """Conservatively revalidate retained work separately from revocation targets.

    A downstream consumer may keep valid frozen inputs; needing a new validity
    judgement is not by itself permission to cancel it or reset its accounting.
    """
    from .execution_contracts import PlanEffectSet

    impact = compute_convergence_impact(before, candidate)
    old, new = decode(before.to_json()).snapshot, decode(candidate.to_json()).snapshot
    previous = {str(item.occurrence_id) for item in old.occurrences}
    current = {str(item.occurrence_id) for item in new.occurrences}
    retiring = {item.occurrence_id for item in impact.targets if item.target_kind == "RETIRING"}
    # No complete support-edge producer is installed in this adapter. DATA-only
    # reachability is therefore not a complete affected set: a retained Task may
    # depend on changed evidence without consuming a DATA port. Re-evaluate all
    # retained occurrences until that producer exists. This is deliberately kept
    # separate from exact cancellation targets and never withdraws shared demand.
    retained = (previous & current) - retiring
    return PlanEffectSet(retained=tuple(sorted(retained)),
                         revalidate=tuple(sorted(retained)),
                         retiring=tuple(sorted(retiring)), newly_materialized=tuple(sorted(current - previous)),
                         shared_retained=impact.preserved_occurrences, coverage="CONSERVATIVE")
