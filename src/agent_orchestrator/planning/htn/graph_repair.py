# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure graph repairs. Runtime convergence and CAS remain CommitService gates."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ...contracts.htn import (
    ContractRevision, DispatchGeneration, InputBindingRevision, OccurrenceId, PlanRevision,
    ProposedPlanDelta, ReadItem, ReadItemKind, RebindInputOperation,
    SemanticReadSet, TaskBindingRewrite, TaskRef,
)
from ...contracts.models import ContractError
from ...contracts.semantic_base import content_hash_of
from ...graph.projection_validation import validate_execution_projection, validate_refinement_acyclic
from ...graph.task_network import TaskNetworkSnapshot
from .compiler import BudgetRequirement, RefinementCompilation

if TYPE_CHECKING:
    from ..plan_preview import PreviewInputs


def affected_occurrences(
    network: TaskNetworkSnapshot, roots: Sequence[str], indexes: Mapping[str, Any]
) -> tuple[OccurrenceId, ...]:
    """Close actual reverse DATA/support and enclosing composition dependencies.

    A child changing invalidates its parent's composition, not unrelated siblings.
    Method membership is therefore read child-to-parent here; cancellation's
    descendant traversal is a separate concern.
    """
    edges: dict[str, set[str]] = {}
    for name in ("reverse_data", "reverse_support", "acceptance_refs"):
        for source, targets in indexes.get(name, {}).items():
            edges.setdefault(str(source), set()).update(str(target) for target in targets)
    for spec in network.occurrences:
        task, occ = str(spec.task_id), str(spec.occurrence_id)
        edges.setdefault(task, set()).add(occ)
        edges.setdefault(occ, set()).add(task)
    for requirement in network.data_requirements:
        edges.setdefault(str(requirement.producer_occurrence), set()).add(str(requirement.consumer_occurrence))
    for instance in network.method_instances:
        if instance.instance_id not in network.adopted_instance_ids:
            continue
        for child in instance.child_bindings:
            edges.setdefault(str(child.goal_occurrence_id or child.occurrence_id), set()).add(str(instance.effective_goal_occurrence_id))
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(edges.get(current, ()))
    return tuple(sorted((spec.occurrence_id for spec in network.occurrences
                         if str(spec.occurrence_id) in seen), key=str))



def _current_owner(network: TaskNetworkSnapshot, binding: Any, instances: Sequence[Any] | None = None) -> Any:
    """Resolve a retained Task's live owner without rewriting its creation history."""
    from ...contracts.htn import MethodOccurrenceBinding
    occurrences = {spec.occurrence_id for spec in network.occurrences if spec.task_id == binding.task_id}
    candidates = [(instance, child) for instance in (
        tuple(item for item in network.method_instances if network.is_adopted(item.instance_id))
        if instances is None else instances) for child in instance.child_bindings
        if (child.goal_occurrence_id or child.occurrence_id) in occurrences]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (str(pair[0].instance_id), pair[1].slot_key))
    old = binding.occurrence_binding
    instance, child = next((pair for pair in candidates if old is not None
        and pair[0].instance_id == old.method_instance_id and pair[1].slot_key == old.slot_key), candidates[0])
    return MethodOccurrenceBinding(instance.instance_id, child.goal_occurrence_id or child.occurrence_id, child.slot_key)


def compile_rebind(inputs: PreviewInputs, operation: RebindInputOperation) -> RefinementCompilation:
    current = inputs.network
    if inputs.repair_impact is None:
        raise ContractError("rebind requires the authoritative reverse-support snapshot")
    consumer = [spec for spec in current.occurrences if str(spec.task_id) == operation.consumer_task_id]
    producer = [spec for spec in current.occurrences if str(spec.task_id) == operation.producer_task_id]
    if len(consumer) != 1 or len(producer) != 1:
        raise ContractError("rebind requires one exact current producer and consumer occurrence")
    old = next((edge for edge in current.data_requirements if edge.requirement_id == operation.requirement_id), None)
    if (old is None or old.consumer_occurrence != consumer[0].occurrence_id
            or content_hash_of(old.to_json()) != operation.expected_requirement_hash):
        raise ContractError("rebind DATA requirement is stale or belongs to another consumer")
    output = next((port for port in current.binding_for_task(TaskRef(operation.producer_task_id)).output_ports
                   if port.port_key == operation.output_port), None)
    if output is None or output.schema_ref != old.schema_ref:
        raise ContractError("replacement producer does not declare the exact required output schema")
    if old.producer_occurrence == producer[0].occurrence_id and old.output_port == operation.output_port:
        raise ContractError("rebind must change the DATA source; use NO_CHANGE for an unchanged binding")
    replacement = replace(old, requirement_id="data-rebind-" + content_hash_of({
        "decision": inputs.decision_id, "old": old.to_json(), "new": operation.to_json()}),
        producer_occurrence=producer[0].occurrence_id, output_port=operation.output_port)
    affected = affected_occurrences(current, (operation.consumer_task_id,), inputs.repair_impact)
    affected_tasks = {current.occurrence(occ).task_id for occ in affected}
    rewrites = tuple(TaskBindingRewrite(content_hash_of(binding.to_json()), replace(binding,
        contract_revision=ContractRevision(int(binding.contract_revision) + 1),
        occurrence_binding=_current_owner(current, binding),
        input_binding_revision=InputBindingRevision(int(binding.input_binding_revision) + 1),
        dispatch_generation=DispatchGeneration(int(binding.dispatch_generation) + 1)))
        for binding in current.task_bindings if binding.task_id in affected_tasks)
    changed = {item.binding.task_id: item.binding for item in rewrites}
    network = replace(current, plan_revision=PlanRevision(int(current.plan_revision) + 1),
        data_requirements=tuple(replacement if edge == old else edge for edge in current.data_requirements),
        task_bindings=tuple(changed.get(binding.task_id, binding) for binding in current.task_bindings))
    read_set = SemanticReadSet(requirements_revision=inputs.requirements_revision,
        goal_revisions=tuple(ReadItem(ReadItemKind.TASK, str(binding.task_id),
            int(binding.contract_revision), binding.contract_hash) for binding in current.task_bindings))
    delta = ProposedPlanDelta(delta_id="delta-rebind-" + content_hash_of(operation.to_json()),
        mission_id=current.mission_id, base_plan_revision=current.plan_revision, read_set=read_set,
        data_requirements=(replacement,), referenced_occurrences=tuple(spec.occurrence_id for spec in current.occurrences),
        compiled_from_proposal_id=inputs.proposal.proposal_id, binding_rewrites=rewrites)
    parent = current.binding_for_task(TaskRef(operation.consumer_task_id))
    instance = next((item for item in current.method_instances
        if current.is_adopted(item.instance_id) and any(
            (child.goal_occurrence_id or child.occurrence_id) == consumer[0].occurrence_id
            for child in item.child_bindings)), None)
    if instance is None:
        raise ContractError("rebind consumer must belong to an adopted method")
    return RefinementCompilation(delta=delta, task_bindings=(), adopted_instance_id=instance.instance_id,
        parent_binding=changed[parent.task_id],
        budget_requirement=BudgetRequirement(parent.obligation_id, 0, 0, 0, len(current.occurrences), 0, 1, 1),
        projection_report=validate_execution_projection(network.execution_projection(), inputs.budget),
        refinement_report=validate_refinement_acyclic(network), network=network,
        superseded_occurrences=affected, steps=("replace exact DATA source; revoke transitive dependent execution rights",))


def compile_cancel(inputs: PreviewInputs, operation: Any) -> RefinementCompilation:
    """Cancel an optional slot, retaining any goal still held by another consumer."""
    from ...contracts.htn import MethodInstanceId, Requiredness

    current = inputs.network
    original = current.instance(MethodInstanceId(operation.method_instance_id))
    if not current.is_adopted(original.instance_id):
        raise ContractError("cancel target method is no longer adopted")
    child = next((item for item in original.child_bindings if item.slot_key == operation.step), None)
    if child is None or child.requiredness is not Requiredness.OPTIONAL_AUTHORIZED:
        raise ContractError("cancel_branch cannot withdraw a required slot or required root coverage")
    if inputs.repair_impact is None:
        raise ContractError("cancel requires an authoritative reverse-support snapshot")
    # Determine orphaned subtrees by actual incoming memberships. A goal used by
    # another adopted method remains live, including its own nested refinement.
    removed: set[OccurrenceId] = set()
    retired = {original.instance_id}
    queue = [child.occurrence_id]
    while queue:
        occurrence = queue.pop()
        if occurrence in current.root_occurrence_ids:
            raise ContractError("branch cancellation cannot remove a root")
        holders = [instance for instance in current.method_instances
                   if instance.instance_id not in retired and current.is_adopted(instance.instance_id)
                   and any((slot.goal_occurrence_id or slot.occurrence_id) == occurrence
                           for slot in instance.child_bindings)]
        retained_slots = [slot for slot in original.child_bindings if slot.slot_key != child.slot_key]
        if holders or any((slot.goal_occurrence_id or slot.occurrence_id) == occurrence for slot in retained_slots):
            continue
        removed.add(occurrence)
        nested = current.adopted_instance_for(occurrence)
        if nested is not None:
            retired.add(nested.instance_id)
            queue.extend(slot.occurrence_id for slot in nested.child_bindings)
    if any(edge.producer_occurrence in removed and edge.consumer_occurrence not in removed
           for edge in current.data_requirements):
        raise ContractError("cancelled branch still supplies a retained DATA consumer; rebind it first")
    if any(set(claim.covered_by) & removed for claim in current.obligation_coverage):
        raise ContractError("cancelled branch is still named by an Obligation coverage claim")
    # ORDER reachability through a removed optional branch still constrains the
    # remaining endpoints. No new accepted outcome is invented for a cancelled leaf.
    from ...contracts.htn import OrderConstraint
    order = [edge for edge in current.order_constraints if edge.before not in removed and edge.after not in removed]
    for entering in current.order_constraints:
        if entering.before in removed or entering.after not in removed:
            continue
        pending = [entering.after]
        seen: set[OccurrenceId] = set()
        while pending:
            vertex = pending.pop()
            if vertex in seen:
                continue
            seen.add(vertex)
            for edge in current.order_constraints:
                if edge.before != vertex:
                    continue
                if edge.after in removed:
                    pending.append(edge.after)
                else:
                    order.append(OrderConstraint(entering.before, edge.after, entering.release_condition))
    return replace_membership(inputs, original=original,
        children=tuple(slot for slot in original.child_bindings if slot.slot_key != child.slot_key),
        removed=removed, retired=retired, order=tuple(dict.fromkeys(order)),
        data=tuple(edge for edge in current.data_requirements
                   if edge.producer_occurrence not in removed and edge.consumer_occurrence not in removed))


def replace_membership(
    inputs: PreviewInputs, *, original: Any, children: tuple[Any, ...],
    removed: set[OccurrenceId], retired: set[Any], order: tuple[Any, ...], data: tuple[Any, ...],
    new_occurrences: tuple[Any, ...] = (), new_bindings: tuple[Any, ...] = (),
    coverage: tuple[Any, ...] | None = None, extra_affected: tuple[str, ...] = (),
) -> RefinementCompilation:
    """Version a method membership; retain semantic contracts and duty accounts."""
    from ...contracts.htn import MethodInstanceId
    from .compiler import _edge_names_any

    current = inputs.network
    if inputs.repair_impact is None:
        raise ContractError("membership rewrite lacks authoritative impact")
    instance_id = MethodInstanceId("mi-repair-" + content_hash_of({
        "old": original.to_json(), "proposal": inputs.proposal.to_json()}))
    draft = replace(original, instance_id=instance_id,
        plan_revision=PlanRevision(int(current.plan_revision) + 1),
        child_bindings=tuple(replace(child, instance_id=instance_id) for child in children))
    specs = tuple(spec for spec in current.occurrences if spec.occurrence_id not in removed) + new_occurrences
    remaining_tasks = {spec.task_id for spec in specs}
    removed_tasks = {binding.task_id for binding in current.task_bindings} - remaining_tasks
    affected = set(affected_occurrences(current, (str(original.goal_id), *extra_affected), inputs.repair_impact)) - removed
    # A new method owner changes the enclosing composition and its dependents.
    # Unchanged goals retain their semantic record and completion evidence.
    # Their creation owner is provenance; current dispatch resolves live memberships.
    affected_tasks = {current.occurrence(occ).task_id for occ in affected}
    rewrites = []
    bindings = []
    superseded = removed | affected
    for binding in current.task_bindings:
        if binding.task_id in removed_tasks:
            continue
        updates: dict[str, Any] = {}
        if binding.task_id in affected_tasks:
            owner = _current_owner(current, binding, tuple(item for item in current.method_instances
                if current.is_adopted(item.instance_id) and item.instance_id not in retired) + (draft,))
            if owner != binding.occurrence_binding:
                updates["occurrence_binding"] = owner
        if binding.adopted_method_instance_id == original.instance_id:
            updates["adopted_method_instance_id"] = instance_id
        if binding.task_id in affected_tasks:
            updates["input_binding_revision"] = InputBindingRevision(int(binding.input_binding_revision) + 1)
        if updates:
            updates["contract_revision"] = ContractRevision(int(binding.contract_revision) + 1)
            updates["dispatch_generation"] = DispatchGeneration(int(binding.dispatch_generation) + 1)
            updated = replace(binding, **updates)
            rewrites.append(TaskBindingRewrite(content_hash_of(binding.to_json()), updated))
            superseded.update(spec.occurrence_id for spec in specs if spec.task_id == binding.task_id)
            bindings.append(updated)
        else:
            bindings.append(binding)
    new_bindings = tuple(replace(binding, occurrence_binding=replace(binding.occurrence_binding, method_instance_id=instance_id))
                         if binding.occurrence_binding is not None else binding for binding in new_bindings)
    bindings.extend(new_bindings)
    network = replace(current, plan_revision=PlanRevision(int(current.plan_revision) + 1),
        occurrences=specs, task_bindings=tuple(bindings),
        obligation_coverage=current.obligation_coverage if coverage is None else coverage,
        method_instances=tuple(instance for instance in current.method_instances if instance.instance_id not in retired) + (draft,),
        adopted_instance_ids=tuple(item for item in current.adopted_instance_ids if item not in retired) + (instance_id,),
        order_constraints=order, data_requirements=data,
        typed_edges=tuple(edge for edge in current.typed_edges if not _edge_names_any(edge, frozenset(removed), removed_tasks, retired)))
    parent = network.binding_for_task(original.goal_id)
    delta = ProposedPlanDelta(delta_id="delta-repair-" + content_hash_of(inputs.proposal.to_json()),
        mission_id=current.mission_id, base_plan_revision=current.plan_revision,
        read_set=SemanticReadSet(requirements_revision=inputs.requirements_revision,
            goal_revisions=tuple(ReadItem(ReadItemKind.TASK, str(binding.task_id), int(binding.contract_revision), binding.contract_hash)
                for binding in current.task_bindings),
            method_revisions=(ReadItem(ReadItemKind.METHOD, str(original.method_ref.method_id),
                original.method_ref.version, original.method_ref.content_hash),)),
        method_instances=(draft,), retired_instance_ids=tuple(sorted(retired, key=str)),
        occurrences=new_occurrences,
        referenced_occurrences=tuple(spec.occurrence_id for spec in specs),
        order_constraints=order, data_requirements=data,
        compiled_from_proposal_id=inputs.proposal.proposal_id, binding_rewrites=tuple(rewrites))
    return RefinementCompilation(delta=delta, task_bindings=new_bindings, adopted_instance_id=instance_id,
        parent_binding=parent,
        budget_requirement=BudgetRequirement(parent.obligation_id, len(new_occurrences),
            sum(str(spec.form) == "primitive" for spec in new_occurrences),
            sum(str(spec.form) == "compound" for spec in new_occurrences),
            len(specs) - len(new_occurrences), len(order), len(data),
            max((sum(edge.before == spec.occurrence_id for edge in order) + sum(edge.producer_occurrence == spec.occurrence_id for edge in data)
                 for spec in specs), default=0)),
        projection_report=validate_execution_projection(network.execution_projection(), inputs.budget),
        refinement_report=validate_refinement_acyclic(network), network=network,
        superseded_occurrences=tuple(sorted(superseded, key=str)),
        steps=("version membership; preserve shared goals; revoke dependent execution rights",))


def compile_successor(inputs: PreviewInputs, operation: Any) -> RefinementCompilation:
    """A fresh Task replaces one owned leaf while retaining its original duty."""
    from ...contracts.htn import MethodOccurrenceBinding, OccurrenceSpec, ReusePolicy, TaskForm, TaskSemanticBindingV1
    from .grounding import ParameterBindingsError

    current = inputs.network
    old = current.binding_for_task(TaskRef(operation.old_task_id))
    if str(old.obligation_id) != operation.obligation_id:
        raise ContractError("successor must retain the exact original Obligation")
    specs = [spec for spec in current.occurrences if spec.task_id == old.task_id]
    if len(specs) != 1 or specs[0].occurrence_id in current.root_occurrence_ids:
        raise ContractError("successor requires one non-root occurrence; root meaning changes need Requirements revision")
    old_spec = specs[0]
    consumers = [(instance, child) for instance in current.method_instances
        if current.is_adopted(instance.instance_id) for child in instance.child_bindings
        if (child.goal_occurrence_id or child.occurrence_id) == old_spec.occurrence_id]
    if len(consumers) != 1:
        raise ContractError("shared successor requires consumers to release or rebind their memberships first")
    original, slot = consumers[0]
    # Replacing a compound subtree is REPLACE_METHOD. A successor preserves the
    # leaf contract; the old Task/result/Attempts remain immutable history.
    if old.form is not TaskForm.PRIMITIVE:
        raise ContractError("compound repair must refine or replace its adopted method")
    spec = inputs.catalog.resolve(operation.goal_type_ref)
    if spec is None or spec.goal_signature != old.goal_signature or spec.form != old.form:
        raise ContractError("successor type must retain the original goal contract and form")
    prior_types = [candidate for candidate in inputs.catalog.task_types()
        if candidate.goal_signature == old.goal_signature and candidate.operator_ref == old.operator_ref
        and candidate.input_ports == old.input_ports and candidate.output_ports == old.output_ports]
    if len(prior_types) != 1 or prior_types[0].preconditions != spec.preconditions:
        raise ContractError("successor cannot inherit witnesses for a different type precondition contract")
    if any(not inputs.capabilities.available(capability) for capability in spec.required_capabilities):
        raise ContractError("successor requires a capability unavailable in this deployment")
    if spec.input_ports != old.input_ports or spec.output_ports != old.output_ports:
        raise ContractError("successor ports must preserve the existing DATA contracts")
    if spec.parameter_schema_ref is None:
        if operation.bindings:
            raise ParameterBindingsError("successor type declares no parameter schema")
    else:
        schema = inputs.schemas.resolve(spec.parameter_schema_ref)
        if schema is None or not schema.check(operation.bindings).ok:
            raise ParameterBindingsError("successor bindings fail the exact registered schema")
    identity = content_hash_of({"old": old.to_json(), "proposal": inputs.proposal.to_json()})
    task_id, occurrence = TaskRef("task-successor-" + identity), OccurrenceId("occ-successor-" + identity)
    successor = TaskSemanticBindingV1(task_id=task_id, obligation_id=old.obligation_id,
        contract_revision=old.contract_revision,
        contract_hash=content_hash_of({"type": spec.task_type_ref.to_json(), "parameters": dict(operation.bindings),
                                     "supersedes": old.to_json(), "decision": inputs.decision_id}),
        form=spec.form, goal_signature=spec.goal_signature, typed_parameters=dict(operation.bindings),
        requirement_refs=old.requirement_refs, input_ports=spec.input_ports, output_ports=spec.output_ports,
        operator_ref=spec.operator_ref, semantic_scope=old.semantic_scope,
        capability_requirements=spec.required_capabilities, precondition_refs=old.precondition_refs,
        applicability_check_policy=old.applicability_check_policy,
        occurrence_binding=MethodOccurrenceBinding(original.instance_id, occurrence, slot.slot_key),
        input_binding_revision=InputBindingRevision(int(old.input_binding_revision) + 1),
        dispatch_generation=DispatchGeneration(int(old.dispatch_generation) + 1),
        resource_reads=spec.resource_reads, resource_writes=spec.resource_writes, side_effect_kind=spec.side_effect_kind)
    remap = lambda value: occurrence if value == old_spec.occurrence_id else value
    children = tuple(replace(child, occurrence_id=occurrence, goal_occurrence_id=None,
                            acceptance_ref=None, resolution_ref=None, reuse_policy=ReusePolicy.NEW_WORK) if child == slot else child
                     for child in original.child_bindings)
    order = tuple(replace(edge, before=remap(edge.before), after=remap(edge.after)) for edge in current.order_constraints)
    data = tuple(replace(edge, producer_occurrence=remap(edge.producer_occurrence), consumer_occurrence=remap(edge.consumer_occurrence),
        requirement_id="data-successor-" + content_hash_of({"edge": edge.to_json(), "successor": identity}))
        if old_spec.occurrence_id in (edge.producer_occurrence, edge.consumer_occurrence) else edge
        for edge in current.data_requirements)
    coverage = tuple(replace(claim, covered_by=tuple(remap(occ) for occ in claim.covered_by))
                     for claim in current.obligation_coverage)
    return replace_membership(inputs, original=original, children=children,
        removed={old_spec.occurrence_id}, retired={original.instance_id}, order=order, data=data,
        new_occurrences=(OccurrenceSpec(occurrence, task_id, old.obligation_id, old.form, old_spec.requiredness),),
        new_bindings=(successor,), coverage=coverage, extra_affected=(str(old.task_id),))


def compile_bind_existing(inputs: PreviewInputs, operation: Any) -> RefinementCompilation:
    """Bind an existing compatible goal, pinning CURRENT completion when requested."""
    from ...contracts.htn import MethodInstanceId, ReusePolicy
    from ...contracts.semantic_base import Provenance, TypedRef, TypedRefKind
    from .grounding import SharingSignature, may_share

    current = inputs.network
    original = current.instance(MethodInstanceId(operation.consumer_method_instance_id))
    if not current.is_adopted(original.instance_id):
        raise ContractError("sharing consumer method is not adopted")
    slot = next((child for child in original.child_bindings if child.slot_key == operation.step), None)
    if slot is None:
        raise ContractError("sharing slot is absent from the adopted consumer")
    old = current.binding_for_occurrence(slot.occurrence_id)
    targets = [spec for spec in current.occurrences if str(spec.task_id) == operation.goal_id]
    if len(targets) != 1:
        raise ContractError("sharing goal must resolve to one exact occurrence")
    target = targets[0]
    if target.occurrence_id == slot.occurrence_id:
        raise ContractError("slot already binds this goal; use NO_CHANGE")
    new = current.binding_for_task(target.task_id)
    if old.obligation_id != new.obligation_id:
        raise ContractError("sharing cannot silently transfer the original Obligation to another account")
    if current.adopted_instance_for(slot.occurrence_id) is not None:
        raise ContractError("decomposed old goal needs its nested method retired before sharing")
    if any(instance.instance_id != original.instance_id and current.is_adopted(instance.instance_id)
           and any(child.occurrence_id == slot.occurrence_id for child in instance.child_bindings)
           for instance in current.method_instances):
        raise ContractError("old slot is still shared; release its other consumers before replacing it")
    declared = [spec for spec in inputs.catalog.task_types()
                if spec.goal_signature == old.goal_signature and spec.operator_ref == old.operator_ref
                and spec.input_ports == old.input_ports and spec.output_ports == old.output_ports]
    if len(declared) != 1:
        raise ContractError("sharing needs an unambiguous exact registered Task type")
    task_type = declared[0]
    from .registry import effective_reuse_policy
    contract = inputs.registry.definition(original.method_ref)
    declared_step = None if contract is None else next(
        (step for step in contract.steps if step.local_id == slot.slot_key), None)
    if declared_step is None:
        raise ContractError("sharing slot has no registered Method declaration")
    permitted = effective_reuse_policy(declared_step, task_type)
    if (new.goal_signature != old.goal_signature or new.operator_ref != old.operator_ref
            or new.input_ports != old.input_ports or new.output_ports != old.output_ports
            or new.capability_requirements != old.capability_requirements
            or new.precondition_refs != old.precondition_refs):
        raise ContractError("existing goal has a different semantic or precondition contract")
    policy = ReusePolicy.REUSE_ACCEPTED if operation.resolution_id is not None else ReusePolicy.SHARE_ACTIVE
    def signature(binding: Any, occurrence: OccurrenceId) -> Any:
        versions = tuple(sorted(content_hash_of({key: value for key, value in edge.to_json().items()
                           if key not in {"requirement_id", "consumer_occurrence"}})
                           for edge in current.data_requirements if edge.consumer_occurrence == occurrence))
        return SharingSignature.of(task_type, binding.typed_parameters,
            authority_scope=binding.semantic_scope, semantic_scope=binding.semantic_scope, input_versions=versions)
    if permitted is ReusePolicy.NEW_WORK or not may_share(
            signature(old, slot.occurrence_id), signature(new, target.occurrence_id), reuse_policy=policy).shareable:
        raise ContractError("task type or full semantic/input signature forbids sharing")
    sources = [row for row in inputs.goal_reuse_sources if row["occurrence_id"] == str(target.occurrence_id)]
    if len(sources) != 1:
        raise ContractError("shared goal has no current authoritative demand")
    source = sources[0]
    resolution_ref = None
    if operation.resolution_id is None:
        if not source["share_active"]:
            raise ContractError("SHARE_ACTIVE requires demanded active work")
    else:
        quoted = source["resolution_ref"]
        if quoted is None or quoted["id"] != operation.resolution_id:
            raise ContractError("REUSE_ACCEPTED requires this goal's exact CURRENT adopted resolution")
        resolution_ref = TypedRef(TypedRefKind.RESOLUTION, quoted["id"], quoted["semantic_revision"],
                                  quoted["content_hash"], produced_by=Provenance.TOOL)
    remap = lambda value: target.occurrence_id if value == slot.occurrence_id else value
    children = tuple(replace(child, occurrence_id=target.occurrence_id, goal_occurrence_id=target.occurrence_id,
        reuse_policy=policy, acceptance_ref=None, resolution_ref=resolution_ref) if child == slot else child
        for child in original.child_bindings)
    order = tuple(dict.fromkeys(replace(edge, before=remap(edge.before), after=remap(edge.after))
                                for edge in current.order_constraints))
    data = tuple(replace(edge, producer_occurrence=remap(edge.producer_occurrence),
        requirement_id="data-share-" + content_hash_of({"edge": edge.to_json(), "target": str(target.occurrence_id)}))
        if edge.producer_occurrence == slot.occurrence_id else edge for edge in current.data_requirements
        if edge.consumer_occurrence != slot.occurrence_id)
    coverage = tuple(replace(claim, covered_by=tuple(dict.fromkeys(remap(occ) for occ in claim.covered_by)))
                     for claim in current.obligation_coverage)
    compilation = replace_membership(inputs, original=original, children=children,
        removed={slot.occurrence_id}, retired={original.instance_id}, order=order, data=data,
        coverage=coverage, extra_affected=(str(old.task_id),))
    if resolution_ref is not None:
        compilation = replace(compilation, delta=replace(compilation.delta, resolution_reuses=(resolution_ref,)))
    return compilation
