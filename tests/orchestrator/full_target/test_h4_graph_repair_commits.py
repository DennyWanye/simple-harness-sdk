# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Focused H4 graph-repair checks through the original Plan Commit service."""

from __future__ import annotations

from dataclasses import replace

import pytest

from test_plan_commits import _world
from test_h1i_production_entry import _approve_root_content_only_spec

from agent_orchestrator.contracts.htn import (
    CancelBranchOperation,
    PlanProposal,
    ProposeSuccessorOperation,
    ReadItem,
    ReadItemKind,
    RebindInputOperation,
    RunningWorkPolicy,
)
from agent_orchestrator.contracts.models import ContractError, sha256_hex
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol
from agent_orchestrator.orchestrator.repair_impact import read_repair_impact_indexes
from agent_orchestrator.planning.htn.graph_repair import (
    compile_cancel,
    compile_rebind,
    compile_successor,
)
from agent_orchestrator.planning.plan_preview import PreviewInputs
from agent_orchestrator.runtime.planning_operations import StoreOperationReader, read_running_work
from agent_orchestrator.storage.htn_store import HtnStore


def _committed(tmp_path):
    return _commit_world(_world(tmp_path, key="h4-graph-repair"))


def _commit_world(world):
    bind_planning_protocol(world.store, world.mission.id, "planning-decision-v1")
    _approve_root_content_only_spec(
        world.service,
        world.mission,
        world.binding,
        command_id="approve-h4-graph-repair-completion",
    )
    world.command = replace(world.command, delta=replace(world.command.delta,
        read_set=replace(world.command.delta.read_set, requirements_revision=1)))
    world.commit()
    return world, HierarchicalDispatch(world.store, world.service)


def _proposal(world, network, operation, suffix: str) -> PlanProposal:
    root = network.binding_for_task(network.occurrences[0].task_id)
    return PlanProposal(
        proposal_id=f"h4-{suffix}",
        mission_id=network.mission_id,
        expected_plan_revision=network.plan_revision,
        trigger_refs=(),
        read_set=(
            ReadItem(
                ReadItemKind.TASK,
                str(root.task_id),
                int(root.contract_revision),
                root.contract_hash,
            ),
        ),
        operations=(operation,),
        rationale="repair the committed graph",
        running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE,
    )


def _inputs(world, dispatch, operation, suffix: str) -> PreviewInputs:
    network = dispatch.network(world.mission.id)
    proposal = _proposal(world, network, operation, suffix)
    requirements = HtnStore(world.store).latest_requirements_revision(world.mission.id)
    assert requirements is not None
    return PreviewInputs(
        decision_id=f"decision-{suffix}",
        decision_hash=sha256_hex(proposal.to_json()),
        request_id=f"request-{suffix}",
        source_plan_revision=int(network.plan_revision),
        source_network_hash=sha256_hex({
            "mission_id": str(network.mission_id),
            "plan_revision": int(network.plan_revision),
            "occurrences": [item.to_json() for item in network.occurrences],
        }),
        proposal=proposal,
        network=network,
        registry=world.env.registry,
        catalog=world.env.catalog,
        schemas=world.env.schemas,
        evidence=world.env.snapshot(),
        predicates=world.env.predicates,
        requirements_revision=int(requirements.revision),
        budget=DEFAULT_PROJECTION_BUDGET,
        system_identity_seed="manager-1",
        now_ms=int(world.store.now * 1000),
        capabilities=world.env.capabilities(),
        runtime_work=read_running_work(
            world.mission.id, (), reader=StoreOperationReader(world.store)
        ),
        repair_impact=read_repair_impact_indexes(world.store, network, world.mission.id),
    )


def _leaf_and_review(network):
    leaves = [item for item in network.task_bindings if str(item.goal_signature.signature_id) == "plan.leaf"]
    reviews = [item for item in network.task_bindings if str(item.goal_signature.signature_id) == "plan.review"]
    assert len(leaves) == len(reviews) == 1
    return leaves[0], reviews[0]


def test_propose_successor_commits_a_fresh_task_with_the_same_obligation_and_budget(tmp_path):
    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    leaf, _ = _leaf_and_review(network)
    leaf_type = next(
        item for item in world.env.catalog.task_types()
        if item.goal_signature == leaf.goal_signature
    )
    operation = ProposeSuccessorOperation(
        str(leaf.task_id), str(leaf.obligation_id), leaf_type.task_type_ref, leaf.typed_parameters
    )
    inputs = _inputs(world, dispatch, operation, "successor")
    compilation = compile_successor(inputs, operation)
    before = world.duties.account(world.mission.id, leaf.obligation_id)

    command = dispatch.build_command(
        world.mission.id,
        inputs.proposal,
        compilation,
        principal=world.principal,
        command_id="commit-h4-successor",
    )
    receipt = world.service.commit_plan_revision(command, world.principal)

    assert receipt.new_plan_revision == int(network.plan_revision) + 1
    reopened = HierarchicalDispatch(world.store, world.service).network(world.mission.id)
    replacements = [item for item in reopened.task_bindings if item.task_id != leaf.task_id
                    and item.obligation_id == leaf.obligation_id
                    and item.goal_signature == leaf.goal_signature]
    assert len(replacements) == 1
    after = world.duties.account(world.mission.id, leaf.obligation_id)
    assert after.fuel_limit == before.fuel_limit
    assert after.consumed_cost_micros == before.consumed_cost_micros
    assert after.consumed_attempts == before.consumed_attempts


def test_propose_successor_cannot_transfer_the_original_obligation(tmp_path):
    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    leaf, _ = _leaf_and_review(network)
    leaf_type = next(item for item in world.env.catalog.task_types()
                     if item.goal_signature == leaf.goal_signature)
    operation = ProposeSuccessorOperation(
        str(leaf.task_id), "obl-other", leaf_type.task_type_ref, leaf.typed_parameters
    )
    with pytest.raises(ContractError, match="exact original Obligation"):
        compile_successor(_inputs(world, dispatch, operation, "wrong-duty"), operation)
    assert int(dispatch.network(world.mission.id).plan_revision) == int(network.plan_revision)


def test_rebind_input_rejects_a_stale_requirement_hash_without_a_plan_write(tmp_path):
    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    leaf, review = _leaf_and_review(network)
    review_occurrence = next(
        item.occurrence_id for item in network.occurrences if item.task_id == review.task_id
    )
    requirement = next(item for item in network.data_requirements
                       if item.consumer_occurrence == review_occurrence)
    operation = RebindInputOperation(
        str(review.task_id), requirement.requirement_id, "0" * 64,
        str(leaf.task_id), requirement.output_port,
    )
    with pytest.raises(ContractError, match="stale or belongs to another consumer"):
        compile_rebind(_inputs(world, dispatch, operation, "stale-data"), operation)
    assert int(dispatch.network(world.mission.id).plan_revision) == int(network.plan_revision)


def test_cancel_branch_rejects_a_required_child_without_retiring_the_method(tmp_path):
    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    instance = next(item for item in network.method_instances if network.is_adopted(item.instance_id))
    required = next(item for item in instance.child_bindings if str(item.requiredness) == "required")
    operation = CancelBranchOperation(str(instance.instance_id), required.slot_key)
    with pytest.raises(ContractError, match="cannot withdraw a required slot"):
        compile_cancel(_inputs(world, dispatch, operation, "required-cancel"), operation)
    cold = dispatch.network(world.mission.id)
    assert cold.is_adopted(instance.instance_id)
    assert int(cold.plan_revision) == int(network.plan_revision)


def test_commit_rejects_rewriting_a_downstream_task_after_it_has_an_accepted_result(tmp_path):
    """CommitService, rather than the pure compiler, owns this immutable-history gate."""

    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    leaf, review = _leaf_and_review(network)
    leaf_type = next(item for item in world.env.catalog.task_types()
                     if item.goal_signature == leaf.goal_signature)
    operation = ProposeSuccessorOperation(
        str(leaf.task_id), str(leaf.obligation_id), leaf_type.task_type_ref, leaf.typed_parameters
    )
    inputs = _inputs(world, dispatch, operation, "accepted-dependent")
    compilation = compile_successor(inputs, operation)
    changed = next(item.binding for item in compilation.delta.binding_rewrites
                   if item.binding.task_id == review.task_id)
    task = world.store.get_task(str(changed.task_id))
    assert task is not None
    world.store.update_task(
        replace(task, accepted_result_id="accepted-review-result", version=task.version + 1),
        expected_version=task.version,
    )
    command = dispatch.build_command(
        world.mission.id, inputs.proposal, compilation,
        principal=world.principal, command_id="commit-h4-accepted-dependent",
    )
    with pytest.raises(PlanCommitRejected, match="accepted or missing dependent"):
        world.service.commit_plan_revision(command, world.principal)
    assert int(dispatch.network(world.mission.id).plan_revision) == int(network.plan_revision)


def test_rebind_input_commits_a_different_declared_producer_and_cold_reads_it(tmp_path, monkeypatch):
    import test_plan_commits as fixtures
    original = fixtures._outer()
    spare = replace(original.steps[0], local_id="spare")
    monkeypatch.setattr(fixtures, "_outer", lambda: replace(original, steps=(*original.steps, spare)))
    world, dispatch = _committed(tmp_path)
    network = dispatch.network(world.mission.id)
    instance = next(item for item in network.method_instances if network.is_adopted(item.instance_id))
    spare_occ = next(child.occurrence_id for child in instance.child_bindings if child.slot_key == "spare")
    spare_binding = network.binding_for_occurrence(spare_occ)
    old = network.data_requirements[0]
    consumer = network.binding_for_occurrence(old.consumer_occurrence)
    operation = RebindInputOperation(str(consumer.task_id), old.requirement_id,
        sha256_hex(old.to_json()), str(spare_binding.task_id), "result")
    inputs = _inputs(world, dispatch, operation, "valid-rebind")
    compilation = compile_rebind(inputs, operation)
    command = dispatch.build_command(world.mission.id, inputs.proposal, compilation,
        principal=world.principal, command_id="commit-valid-rebind")
    receipt = world.service.commit_plan_revision(command, world.principal)
    cold = HierarchicalDispatch(world.store, world.service).network(world.mission.id)
    assert receipt.new_plan_revision == 2
    actual = next(edge for edge in cold.data_requirements if edge.consumer_occurrence == old.consumer_occurrence)
    assert actual.producer_occurrence == spare_occ
    assert actual.schema_ref == old.schema_ref
    rewritten = cold.binding_for_task(consumer.task_id)
    assert rewritten.input_binding_revision == consumer.input_binding_revision + 1
    assert rewritten.dispatch_generation == consumer.dispatch_generation + 1
    assert world.semantics.get_task_semantics(str(consumer.task_id), int(consumer.contract_revision)) == consumer
    assert cold.binding_for_task(spare_binding.task_id) == spare_binding
    assert world.service.commit_plan_revision(command, world.principal) == receipt


def test_share_active_preserves_target_between_two_method_consumers(tmp_path, monkeypatch):
    import test_plan_commits as fixtures
    from htn_world import step, param, ref
    from agent_orchestrator.contracts.htn import BindSharedGoalOperation, ReusePolicy, TaskForm, RefineOperation
    from agent_orchestrator.orchestrator.planning_graph_repairs import graph_repair_sources
    from agent_orchestrator.planning.htn.graph_repair import compile_bind_existing
    from agent_orchestrator.planning.htn.registry import TaskTypeCatalog
    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method
    from agent_orchestrator.planning.htn.compiler import compile_refinement_bundle
    original = fixtures._outer()
    monkeypatch.setattr(fixtures, "_outer", lambda: replace(original, steps=(*original.steps,
        step("branch", "plan.branch", TaskForm.COMPOUND, {"subject": param("subject")}, capabilities=("plan.read",)))))
    make_env = fixtures._env
    def sharing_env(mission):
        env = make_env(mission)
        env.register_type("plan.branch", form=TaskForm.COMPOUND, parameters=(("subject", "string"),),
                          criteria=("c-root",), domain="plan")
        catalog = TaskTypeCatalog()
        for spec in env.catalog.task_types():
            catalog.register(replace(spec, reuse_policy=ReusePolicy.SHARE_ACTIVE)
                if spec.goal_signature.signature_id == "plan.leaf" else spec)
        env.catalog = catalog
        return env
    monkeypatch.setattr(fixtures, "_env", sharing_env)
    world, dispatch = _committed(tmp_path)
    fixtures._admit_root_demand(world)
    network = dispatch.network(world.mission.id)
    outer = next(item for item in network.method_instances if network.is_adopted(item.instance_id))
    target = network.binding_for_occurrence(next(child.occurrence_id for child in outer.child_bindings if child.slot_key == "leaf"))
    branch = network.binding_for_occurrence(next(child.occurrence_id for child in outer.child_bindings if child.slot_key == "branch"))
    contract = replace(original, method_id="plan.branch.method", goal_type_ref=ref("plan.branch"),
                       parameter_schema_ref=ref("plan.branch.params"), output_schema_ref=ref("plan.branch.outputs"))
    assert world.env.admit(contract).admitted
    world.semantics.register_method(contract, world.env.registry.registration(contract.method_ref()))
    report = assess_method(branch, contract, world.env.snapshot(), world.env.capabilities(), registry=world.env.predicates)
    draft = ground_method(branch, contract, {}, report, catalog=world.env.catalog, schemas=world.env.schemas,
        plan_revision=network.plan_revision, goal_occurrence_id=next(spec.occurrence_id for spec in network.occurrences if spec.task_id == branch.task_id))
    bundle = compile_refinement_bundle(draft, network, method=contract, catalog=world.env.catalog,
        schemas=world.env.schemas, registry=world.env.registry, requirements_revision=1)
    proposal = _proposal(world, network, RefineOperation(str(branch.task_id), str(branch.obligation_id), contract.method_ref(), {}), "branch")
    world.service.commit_plan_revision(dispatch.build_command(world.mission.id, proposal, bundle,
        principal=world.principal, command_id="commit-branch"), world.principal)
    network = dispatch.network(world.mission.id)
    consumer = network.adopted_instance_for(next(spec.occurrence_id for spec in network.occurrences if spec.task_id == branch.task_id))
    operation = BindSharedGoalOperation(str(consumer.instance_id), "leaf", str(target.task_id), None)
    inputs = replace(_inputs(world, dispatch, operation, "share-active"),
        goal_reuse_sources=graph_repair_sources(world.store, network))
    compilation = compile_bind_existing(inputs, operation)
    command = dispatch.build_command(world.mission.id, inputs.proposal, compilation,
        principal=world.principal, command_id="commit-share-active")
    world.service.commit_plan_revision(command, world.principal)
    cold = HierarchicalDispatch(world.store, world.service).network(world.mission.id)
    assert cold.binding_for_task(target.task_id) == target
    holders = [item for item in cold.method_instances if cold.is_adopted(item.instance_id)
               and any(cold.binding_for_occurrence(child.occurrence_id).task_id == target.task_id for child in item.child_bindings)]
    assert len(holders) == 2
    assert not cold.is_adopted(consumer.instance_id)


def test_cancel_optional_branch_preserves_required_work_and_releases_only_its_demand(tmp_path):
    import test_plan_commits as fixtures
    from htn_world import root_network
    from agent_orchestrator.contracts.htn import ObligationRelation
    from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind
    from agent_orchestrator.graph.eligibility import ActivePlanView, _method_read
    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method
    from agent_orchestrator.planning.htn.compiler import compile_refinement_bundle
    world = _world(tmp_path, key="h4-optional")
    fixtures._approval(world, 1)
    fixtures._admit_root_demand(world)
    authority = world.service.read_item_for(world.mission.id, ReadItemKind.AUTHORITY, "auth-1")
    contract = replace(world.contract, method_id="plan.optional", steps=(*world.contract.steps,
        replace(world.contract.steps[0], local_id="optional", obligation_relation=ObligationRelation.INDEPENDENT_AUTHORIZED)))
    assert world.env.admit(contract).admitted
    world.semantics.register_method(contract, world.env.registry.registration(contract.method_ref()))
    report = assess_method(world.binding, contract, world.env.snapshot(), world.env.capabilities(), registry=world.env.predicates)
    draft = ground_method(world.binding, contract, {}, report, catalog=world.env.catalog, schemas=world.env.schemas)
    bundle = compile_refinement_bundle(draft, root_network(world.env, world.binding), method=contract,
        catalog=world.env.catalog, schemas=world.env.schemas, registry=world.env.registry,
        slot_authorizations={"optional": TypedRef(TypedRefKind.SOURCE, authority.id,
            authority.semantic_revision, authority.content_hash)})
    world.command = replace(world.command, delta=replace(bundle.delta,
        read_set=replace(bundle.delta.read_set, authority_revisions=(authority,))),
        network=bundle.network, task_bindings=bundle.task_bindings)
    world, dispatch = _commit_world(world)
    network = dispatch.network(world.mission.id)
    instance = next(item for item in network.method_instances if network.is_adopted(item.instance_id))
    optional = next(child for child in instance.child_bindings if child.slot_key == "optional")
    retained = network.binding_for_occurrence(next(child.occurrence_id for child in instance.child_bindings if child.slot_key == "leaf"))
    assert world.duties.account(world.mission.id, optional.obligation_id).has_admitted_demand
    operation = CancelBranchOperation(str(instance.instance_id), "optional")
    inputs = _inputs(world, dispatch, operation, "cancel-optional")
    compiled = compile_cancel(inputs, operation)
    world.service.commit_plan_revision(dispatch.build_command(world.mission.id, inputs.proposal, compiled,
        principal=world.principal, command_id="commit-cancel-optional"), world.principal)
    cold = HierarchicalDispatch(world.store, world.service).network(world.mission.id)
    assert optional.occurrence_id not in {item.occurrence_id for item in cold.occurrences}
    assert cold.binding_for_task(retained.task_id) == retained
    assert not world.duties.account(world.mission.id, optional.obligation_id).has_admitted_demand
    assert world.duties.account(world.mission.id, retained.obligation_id).has_admitted_demand
    reads = _method_read(ActivePlanView(cold), retained)
    assert len(reads) == 1 and cold.is_adopted(reads[0].id)
    assert reads[0].id != str(instance.instance_id)
