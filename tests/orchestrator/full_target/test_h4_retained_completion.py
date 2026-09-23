"""H4 repair preserves actual accepted content and rejects a dirtied reuse pin."""
from dataclasses import replace
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "operation_completion"))
from test_completion_compound_commit import completed_nested_world
from test_h4_graph_repair_commits import _inputs
from agent_orchestrator.contracts.htn import ProposeSuccessorOperation
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.orchestrator.completion_status import read_occurrence_completion
from agent_orchestrator.orchestrator.completion_support import current_child_supports
from agent_orchestrator.orchestrator.planning_graph_repairs import graph_repair_sources
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.planning.htn.graph_repair import compile_successor
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore


def test_unrelated_successor_retains_real_completion_and_dirty_resolution_cannot_commit(tmp_path):
    world, inner_occ, leaf_occ, resolution = completed_nested_world(tmp_path, package_version=7)
    dispatch = world.dispatch
    network = world.network()
    sources = graph_repair_sources(world.store, network)
    row = next(row for row in sources if row["occurrence_id"] == str(inner_occ))
    assert row["resolution_ref"]["id"] == str(resolution.resolution_id)
    leaf_before = read_occurrence_completion(world.store, world.mission.id, str(leaf_occ))
    completion = OperationCompletionStore(world.store)
    original = completion.list_scoped_contributions(world.mission.id, leaf_before.scope.scope_id)
    assert len(original) == 1
    def proposal_for_act(suffix):
        current = world.network()
        act = next(b for b in current.task_bindings if b.goal_signature.signature_id == "plan.act")
        spec = next(s for s in world.env.catalog.task_types() if s.goal_signature == act.goal_signature)
        operation = ProposeSuccessorOperation(str(act.task_id), str(act.obligation_id), spec.task_type_ref, act.typed_parameters)
        inputs = _inputs(world, dispatch, operation, suffix)
        compiled = compile_successor(inputs, operation)
        return inputs, compiled
    inputs, compiled = proposal_for_act("retained-content")
    world.service.commit_plan_revision(dispatch.build_command(world.mission.id, inputs.proposal, compiled,
        principal=world.principal, command_id="commit-retained-content"), world.principal)
    leaf_after = read_occurrence_completion(world.store, world.mission.id, str(leaf_occ))
    assert leaf_after.scope.scope_id != leaf_before.scope.scope_id
    assert leaf_after.complete
    assert read_occurrence_completion(world.store, world.mission.id, str(inner_occ)).complete
    assert completion.get_acceptance_scope_exact(world.mission.id, original[0]["document"].acceptance_id) == original[0]
    nested = world.network().adopted_instance_for(inner_occ)
    assert str(leaf_occ) in current_child_supports(world.store, world.mission.id, nested.child_bindings)
    inputs, compiled = proposal_for_act("stale-resolution")
    pin = TypedRef(TypedRefKind.RESOLUTION, str(resolution.resolution_id), 1, content_hash_of(resolution.to_json()))
    compiled = replace(compiled, delta=replace(compiled.delta, resolution_reuses=(pin,)))
    command = dispatch.build_command(world.mission.id, inputs.proposal, compiled,
        principal=world.principal, command_id="commit-stale-resolution")
    before = world.network().plan_revision
    world.semantics.mark_dirty(world.mission.id, subject_kind="resolution", subject_id=str(resolution.resolution_id),
        scope_id="mission", epoch=world.semantics.epoch(world.mission.id, "mission"), reason="source_invalidated")
    row = next(row for row in graph_repair_sources(world.store, world.network()) if row["occurrence_id"] == str(inner_occ))
    assert row["resolution_ref"] is None
    with pytest.raises(PlanCommitRejected, match="no longer CURRENT"):
        world.service.commit_plan_revision(command, world.principal)
    assert world.network().plan_revision == before


def test_current_resolution_reuse_commits_and_survives_cold_network_read(tmp_path):
    from htn_world import method, step, param
    from agent_orchestrator.contracts.htn import BindSharedGoalOperation, TaskForm
    from agent_orchestrator.planning.htn.graph_repair import compile_bind_existing
    from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
    world, inner_occ, _leaf_occ, resolution = completed_nested_world(
        tmp_path, package_version=7, with_reuse_consumer=True)
    network = world.network()
    consumer = next(binding for binding in network.task_bindings if binding.goal_signature.signature_id == "plan.consumer")
    contract = method("plan.consume-existing", "plan.consumer", parameter_schema="plan.consumer.params",
        steps=(step("existing", "plan.subgoal", TaskForm.COMPOUND, {"subject": param("subject")}),),
        links=(("c-sub", "existing", "c-sub"),), finalizer="existing")
    assert world.env.admit(contract).admitted
    world.semantics.register_method(contract, world.env.registry.registration(contract.method_ref()))
    from agent_orchestrator.planning.htn.applicability import assess_method
    from agent_orchestrator.planning.htn.grounding import ground_method
    from agent_orchestrator.planning.htn.compiler import compile_refinement_bundle
    from agent_orchestrator.contracts.htn import RefineOperation
    from test_h4_graph_repair_commits import _proposal as proposal_for
    report = assess_method(consumer, contract, world.env.snapshot(), world.env.capabilities(), registry=world.env.predicates)
    consumer_occ = next(spec.occurrence_id for spec in network.occurrences if spec.task_id == consumer.task_id)
    draft = ground_method(consumer, contract, {}, report, catalog=world.env.catalog, schemas=world.env.schemas,
        goal_occurrence_id=consumer_occ, plan_revision=network.plan_revision)
    bundle = compile_refinement_bundle(draft, network, method=contract, catalog=world.env.catalog,
        schemas=world.env.schemas, registry=world.env.registry, requirements_revision=1)
    proposal = proposal_for(world, network, RefineOperation(str(consumer.task_id), str(consumer.obligation_id),
        contract.method_ref(), {}), "prepare-consumer")
    world.service.commit_plan_revision(world.dispatch.build_command(world.mission.id, proposal, bundle,
        principal=world.principal, command_id="commit-consumer"), world.principal)
    network = world.network()
    instance = network.adopted_instance_for(next(spec.occurrence_id for spec in network.occurrences if spec.task_id == consumer.task_id))
    target = network.binding_for_occurrence(inner_occ)
    operation = BindSharedGoalOperation(str(instance.instance_id), "existing", str(target.task_id), str(resolution.resolution_id))
    inputs = replace(_inputs(world, world.dispatch, operation, "current-reuse"),
        goal_reuse_sources=graph_repair_sources(world.store, network))
    compiled = compile_bind_existing(inputs, operation)
    command = world.dispatch.build_command(world.mission.id, inputs.proposal, compiled,
        principal=world.principal, command_id="commit-current-reuse")
    receipt = world.service.commit_plan_revision(command, world.principal)
    cold = HierarchicalDispatch(world.store, world.service, planning=world.env).network(world.mission.id)
    assert receipt.new_plan_revision == int(network.plan_revision) + 1
    assert cold.binding_for_task(target.task_id) == target
    adopted = cold.adopted_instance_for(next(spec.occurrence_id for spec in cold.occurrences if spec.task_id == consumer.task_id))
    child = adopted.child_bindings[0]
    assert child.occurrence_id == inner_occ
    assert child.resolution_ref.id == str(resolution.resolution_id)
    assert child.acceptance_ref is None
    assert current_child_supports(world.store, world.mission.id, adopted.child_bindings)[str(inner_occ)] == (str(resolution.resolution_id),)
    from agent_orchestrator.orchestrator.composition_review import CompositionAcceptanceAssembly
    formed = CompositionAcceptanceAssembly(world.store, world.service, dispatch=world.dispatch).resolve_ready(world.mission.id)
    assert any(str(item.resolution.goal_task_id) == str(consumer.task_id) for item in formed)
    row = next(row for row in graph_repair_sources(world.store, cold) if row["occurrence_id"] == str(inner_occ))
    assert row["resolution_ref"]["id"] == str(resolution.resolution_id)
    assert not row["share_active"]
    assert current_child_supports(world.store, world.mission.id, adopted.child_bindings)[str(inner_occ)] == (str(resolution.resolution_id),)
