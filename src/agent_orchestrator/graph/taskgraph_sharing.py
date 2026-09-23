# SPDX-License-Identifier: Apache-2.0
"""Validate retained demand and prospective active sharing without granting work.

The source values come from the original Store read view. This module does not
issue START witnesses, reinterpret a PLAN witness, cancel work or change inputs.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..contracts.evidence_state import EvidenceSnapshot, ValidityWitness
from ..contracts.htn import MethodContract, OccurrenceId, ReleaseCondition, SourceRevisionPolicy
from ..contracts.models import ContractError
from ..knowledge.predicates import PredicateRegistry, PredicateSignature
from ..planning.htn.applicability import authorization_gate, evaluate_condition
from ..planning.htn.grounding import SharedGoalEntry, data_flows
from ..planning.htn.compiler import DEFAULT_ASSURANCE_POLICY, DEFAULT_FRESHNESS_POLICY
from .convergence import compute_convergence_impact
from .eligibility import OccurrenceOutcome, _witness_verdict, order_released, start_preconditions
from .network_codec import NetworkDocumentV1, decode
from .revision_pins import build_revision_pins
from .settlement import SettledTerminalOccurrence


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedInputProof:
    occurrence_id: str
    contract_revision: int
    input_binding_revision: int
    dispatch_generation: int
    semantic_inputs_hash: str
    origins: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SharingSources:
    """Facts for a planning restriction, not a dispatch eligibility certificate."""
    outcomes: Mapping[OccurrenceId, OccurrenceOutcome]
    settlements: Mapping[OccurrenceId, SettledTerminalOccurrence]
    starts: Mapping[str, Mapping[str, ValidityWitness]]
    epochs: Mapping[str, int]
    evidence: EvidenceSnapshot
    predicates: tuple[PredicateSignature, ...]
    methods: tuple[MethodContract, ...]
    now_ms: int
    independent_required: frozenset[str]
    entries: tuple[SharedGoalEntry, ...]
    input_proofs: Mapping[str, SharedInputProof]


def validate_sharing(before: NetworkDocumentV1, candidate: NetworkDocumentV1,
                     sources: SharingSources) -> None:
    """Re-run on freeze and in original Commit; missing facts refuse the change."""
    old, new = decode(before.to_json()).snapshot, decode(candidate.to_json()).snapshot
    old_pins, new_pins = build_revision_pins(before), build_revision_pins(candidate)
    old_slots = {(item.consumer_instance_id, item.slot_key): item for item in old_pins.demand_refs}
    old_members = {str(item.occurrence_id): item for item in old.occurrences}
    new_members = {str(item.occurrence_id): item for item in new.occurrences}
    remaining = {item.producer_occurrence_id for item in new_pins.demand_refs}
    old_demanded = {item.producer_occurrence_id for item in old_pins.demand_refs}
    if sources.independent_required - set(old_members):
        raise ContractError("TASKGRAPH_INDEPENDENT_DEMAND_SOURCE_INVALID")
    impact = compute_convergence_impact(before, candidate)
    if sources.independent_required & {target.occurrence_id for target in impact.targets}:
        raise ContractError("TASKGRAPH_INDEPENDENT_WORK_STILL_REQUIRED")
    for identity in old_demanded & set(new_members):
        if identity not in remaining and identity not in candidate.root_occurrence_ids and identity not in sources.independent_required:
            raise ContractError("TASKGRAPH_RETAINED_PRODUCER_DEMAND_MISSING")

    new_shared = [item for item in new_pins.demand_refs if item.mode in {"share_active", "reuse_accepted"}
             and ((item.consumer_instance_id, item.slot_key) not in old_slots
                  or old_slots[item.consumer_instance_id, item.slot_key].source_slot_hash != item.source_slot_hash)]
    indexed = {str(entry.occurrence_id): entry for entry in sources.entries}
    methods = {contract.method_ref(): contract for contract in sources.methods}
    adopted = {str(draft.instance_id): draft for draft in new.method_instances
               if draft.instance_id in new.adopted_instance_ids}
    children = {(str(draft.instance_id), child.slot_key): child
                for draft in new.method_instances for child in draft.child_bindings}
    changed = {target.occurrence_id for target in impact.targets}
    for demand in new_shared:
        entry = indexed.get(demand.producer_occurrence_id)
        child = children.get((demand.consumer_instance_id, demand.slot_key))
        if entry is None or child is None or demand.producer_occurrence_id in changed:
            raise ContractError("TASKGRAPH_SHARED_PRODUCER_SOURCE_CHANGED")
        proof = sources.input_proofs.get(demand.producer_occurrence_id)
        binding = old.binding_for_occurrence(entry.occurrence_id)
        if (proof is None or not proof.origins or proof.occurrence_id != demand.producer_occurrence_id
                or (proof.contract_revision, proof.input_binding_revision, proof.dispatch_generation) !=
                   (int(binding.contract_revision), int(binding.input_binding_revision), int(binding.dispatch_generation))):
            raise ContractError("TASKGRAPH_SHARED_INPUT_VERSIONS_UNPROVEN")
        draft = adopted.get(demand.consumer_instance_id)
        contract = None if draft is None else methods.get(draft.method_ref)
        if draft is None or contract is None:
            raise ContractError("TASKGRAPH_SHARED_METHOD_SOURCE_MISSING")
        by_slot = {item.slot_key: item for item in draft.child_bindings}
        expected_inputs = set()
        for source_slot, output_port, consumer_slot, input_port in data_flows(contract):
            if consumer_slot != demand.slot_key:
                continue
            source_child = by_slot.get(source_slot)
            if source_child is None:
                raise ContractError("TASKGRAPH_SHARED_DATA_SLOT_MISSING")
            expected_inputs.add((str(source_child.goal_occurrence_id or source_child.occurrence_id),
                                 output_port, input_port))
        actual_inputs = [item for item in old.data_requirements
                         if str(item.consumer_occurrence) == demand.producer_occurrence_id]
        if expected_inputs != {(str(item.producer_occurrence), item.output_port, item.input_port)
                               for item in actual_inputs}:
            raise ContractError("TASKGRAPH_SHARED_DATA_DECLARATION_DIFFERS")
        # The original compiler emits these exact policies for a method DATA
        # flow. An old edge with stronger/different terms cannot silently stand
        # in for the prospective slot's declaration, even when merge retains it.
        if any(item.assurance_policy_ref != DEFAULT_ASSURANCE_POLICY
               or item.freshness_policy_ref != DEFAULT_FRESHNESS_POLICY
               or item.source_revision_policy is not SourceRevisionPolicy.PINNED for item in actual_inputs):
            raise ContractError("TASKGRAPH_SHARED_DATA_POLICY_DIFFERS")
        if demand.mode == "reuse_accepted":
            if (entry.acceptance_ref is None or child.acceptance_ref != entry.acceptance_ref
                    or sources.outcomes.get(entry.occurrence_id) is not OccurrenceOutcome.ACCEPTED):
                raise ContractError("TASKGRAPH_REUSE_ACCEPTANCE_NOT_CURRENT")
        elif entry.acceptance_ref is not None:
            raise ContractError("TASKGRAPH_ACCEPTED_PRODUCER_REQUIRES_EXACT_REUSE")
    added = [item for item in new_shared if item.mode == "share_active"]
    if not added:
        return
    registry = PredicateRegistry()
    for signature in sources.predicates:
        registry.register(signature)
    parent_methods: dict[str, set[str]] = {}
    for demand in new_pins.demand_refs:
        parent_methods.setdefault(demand.producer_occurrence_id, set()).add(demand.consumer_instance_id)

    for demand in added:
        producer = demand.producer_occurrence_id
        previous_member = old_members.get(producer)
        if previous_member is None:
            raise ContractError("TASKGRAPH_SHARE_ACTIVE_PRODUCER_NOT_EXISTING")
        before_binding = old.binding_for_occurrence(OccurrenceId(producer))
        after_binding = new.binding_for_occurrence(OccurrenceId(producer))
        if (previous_member.task_id != new_members[producer].task_id
                or before_binding.contract_hash != after_binding.contract_hash
                or before_binding.contract_revision != after_binding.contract_revision
                or before_binding.input_binding_revision != after_binding.input_binding_revision
                or before_binding.dispatch_generation != after_binding.dispatch_generation):
            raise ContractError("TASKGRAPH_SHARE_ACTIVE_PRODUCER_CHANGED")
        if any(target.occurrence_id == producer for target in impact.targets):
            raise ContractError("TASKGRAPH_SHARE_ACTIVE_INPUTS_CHANGED")
        # Existing work's own START licence is consumer- and purpose-bound. A
        # method's stored PLAN witness is not an alternative licence here.
        for digest in start_preconditions(before_binding):
            if _witness_verdict(sources.starts.get(str(before_binding.task_id), {}).get(digest), digest,
                    consumer_task=before_binding.task_id, scope_epochs=sources.epochs, now_ms=sources.now_ms) is not None:
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_START_NOT_CURRENT")

        # Also visit the new consumer's enclosing method path. A shared producer
        # must not bypass an ORDER into the new parent just because another parent
        # already started it. Follow explicit slots, never infer graph roots.
        path_occurrences = {producer}
        pending = [demand.consumer_instance_id]
        visited = set()
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            draft = adopted.get(identity)
            if draft is None:
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_CONSUMER_MISSING")
            parent = str(draft.effective_goal_occurrence_id)
            path_occurrences.add(parent)
            pending.extend(parent_methods.get(parent, ()))
            contract = methods.get(draft.method_ref)
            if contract is None:
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_METHOD_SOURCE_MISSING")
            parameters = {item.name: item.value for item in draft.grounded_parameters}
            # This is a fresh prospective-consumer restriction using the original
            # evidence interpreter. It creates no witness or execution permission.
            # Worker START admission still needs its original persisted licence.
            for condition in contract.applicable_when:
                evaluation = evaluate_condition(condition, registry=registry, snapshot=sources.evidence,
                    parameters=parameters, now_ms=sources.now_ms)
                if not authorization_gate(evaluation).allowed:
                    raise ContractError("TASKGRAPH_SHARE_ACTIVE_CONSUMER_PRECONDITION_UNMET")
        for edge in new.order_constraints:
            if str(edge.after) not in path_occurrences:
                continue
            outcome = sources.outcomes.get(edge.before)
            if outcome is None or not order_released(outcome, edge.release_condition):
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_ORDER_UNMET")
            original = old_members.get(str(edge.before))
            if original is None:
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_ORDER_SOURCE_MISSING")
            current_binding = old.binding_for_occurrence(edge.before)
            candidate_binding = new.binding_for_occurrence(edge.before)
            if (current_binding.task_id != candidate_binding.task_id
                    or current_binding.contract_hash != candidate_binding.contract_hash
                    or current_binding.contract_revision != candidate_binding.contract_revision
                    or current_binding.input_binding_revision != candidate_binding.input_binding_revision
                    or current_binding.dispatch_generation != candidate_binding.dispatch_generation):
                raise ContractError("TASKGRAPH_SHARE_ACTIVE_ORDER_BINDING_CHANGED")
            if edge.release_condition is ReleaseCondition.SETTLED_TERMINAL:
                fact = sources.settlements.get(edge.before)
                if (fact is None or fact.mission_id != before.mission_id or fact.plan_revision != before.revision
                        or fact.occurrence_id != str(edge.before) or fact.outcome != str(outcome)
                        or fact.contract_revision != int(current_binding.contract_revision)
                        or fact.dispatch_generation != int(current_binding.dispatch_generation)):
                    raise ContractError("TASKGRAPH_SHARE_ACTIVE_ORDER_UNSETTLED")
