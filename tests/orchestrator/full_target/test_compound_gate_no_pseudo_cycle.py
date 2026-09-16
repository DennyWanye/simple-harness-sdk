# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1: a compound boundary compiles to gates, and gates are not a cycle.

§24.1 decision 2 / TG §6: "parent opens child" and "child closes parent" are two
arrows in opposite directions once a compound occurrence is compiled into an
``entry``/``exit`` pair.  Unioned naively they are a loop; compiled they are a
DAG.  The contrast here is between two readings of the *same* compiled network,
not between a compiler and a hand-written counterexample.

The rest of the file is the compiler's refusals.  Each has its own test because
each is a different repair: an uncovered root criterion, a required input port
with no producer, a second adopted method for one occurrence, a parent that is
primitive, a parent this network does not have, and a size bound.  A compiler that
answered all six with one boolean would be telling the caller nothing.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))

from htn_world import (  # noqa: E402
    BUDGET,
    Env,
    atom,
    ledger_for,
    method,
    out,
    param,
    ref,
    root_network,
    step,
    task_binding,
)

from agent_orchestrator.contracts.evidence_state import TruthValue  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    BudgetInheritance,
    ObligationOpening,
    ObligationRelation,
    OccurrenceSpec,
    Requiredness,
    TaskForm,
    require_commit_ready,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.semantic_base import (  # noqa: E402
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.graph.dependency_checker import (  # noqa: E402
    DependencyError,
    check_dependencies,
)
from agent_orchestrator.graph.projection_validation import (  # noqa: E402
    ProblemKind,
    validate_execution_projection,
    validate_refinement_acyclic,
)
from agent_orchestrator.graph.task_network import (  # noqa: E402
    ProjectionEdgeKind,
    ProjectionNodeKind,
    TaskNetworkSnapshot,
)
from agent_orchestrator.planning.htn.applicability import assess_method  # noqa: E402
from agent_orchestrator.planning.htn.compiler import (  # noqa: E402
    DEFAULT_CHILD_FUEL_SHARE,
    CompilationRefused,
    apply_obligation_openings,
    compile_refinement_bundle,
    unbound_required_ports,
)
from agent_orchestrator.planning.htn.grounding import ground_method  # noqa: E402
from agent_orchestrator.planning.htn.refinement import (  # noqa: E402
    FrontierItem,
    RefinementOutcome,
    refine,
)
from agent_orchestrator.planning.htn.validation import (  # noqa: E402
    DeltaProblemKind,
    validate_delta,
)


def gate_env() -> Env:
    env = Env()
    env.register_predicate("gate.ready", (("subject", "string"),))
    env.register_type(
        "gate.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-root",),
        domain="gate",
    )
    env.register_type(
        "gate.sub",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-sub",),
        outputs=(("result", "gate.result"),),
        domain="gate",
    )
    env.register_type(
        "gate.leaf",
        parameters=(("subject", "string"),),
        outputs=(("result", "gate.result"),),
        capabilities=("gate.read",),
        domain="gate",
    )
    env.register_type(
        "gate.review",
        parameters=(("subject", "string"),),
        inputs=(("result", "gate.result", True),),
        outputs=(("verdict", "gate.verdict"),),
        capabilities=("gate.read",),
        domain="gate",
    )
    env.register_type(
        "gate.needs-input",
        parameters=(("subject", "string"),),
        inputs=(("missing", "gate.missing", True),),
        outputs=(("verdict", "gate.verdict"),),
        capabilities=("gate.read",),
        domain="gate",
    )
    env.say("gate.ready", {"subject": "alpha"}, TruthValue.TRUE)
    return env


def outer_method(method_id: str = "gate.outer"):
    return method(
        method_id,
        "gate.goal",
        parameter_schema="gate.goal.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step("sub", "gate.sub", TaskForm.COMPOUND, {"subject": param("subject")}),
            step(
                "review",
                "gate.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("sub", "result")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


def inner_method(method_id: str = "gate.inner"):
    return method(
        method_id,
        "gate.sub",
        parameter_schema="gate.sub.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "gate.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-sub", "leaf", "c-produced"),),
        finalizer="leaf",
    )


def compile_method(env: Env, contract, binding, network, **kwargs):
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(
        binding,
        contract,
        {},
        report,
        catalog=env.catalog,
        schemas=env.schemas,
        goal_occurrence_id=kwargs.pop("goal_occurrence_id", None),
    )
    return draft, compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        **kwargs,
    )


def outer_only(env: Env | None = None):
    env = env or gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    draft, bundle = compile_method(env, outer, binding, network)
    return env, binding, draft, bundle


def both_levels(env: Env | None = None):
    env, binding, outer_draft, bundle = outer_only(env)
    inner = inner_method()
    env.admit(inner)
    slot = next(item for item in outer_draft.child_bindings if item.slot_key == "sub")
    sub_binding = next(
        item
        for item in bundle.task_bindings
        if item.occurrence_binding is not None and item.occurrence_binding.slot_key == "sub"
    )
    inner_draft, inner_bundle = compile_method(
        env, inner, sub_binding, bundle.network, goal_occurrence_id=slot.occurrence_id
    )
    return env, binding, slot.occurrence_id, inner_draft, inner_bundle


# ========================================================================== gates


def test_a_compound_child_compiles_to_an_entry_and_an_exit() -> None:
    _, _, draft, bundle = outer_only()
    slot = next(item for item in draft.child_bindings if item.slot_key == "sub")
    projection = bundle.network.execution_projection()
    kinds = {node.kind for node in projection.nodes if node.occurrence_id == slot.occurrence_id}
    assert kinds == {ProjectionNodeKind.COMPOUND_ENTRY, ProjectionNodeKind.COMPOUND_EXIT}


def test_gates_are_not_billable_work() -> None:
    _, _, _, bundle = outer_only()
    projection = bundle.network.execution_projection()
    assert all(not node.billable for node in projection.nodes if node.is_gate)


def test_a_primitive_occurrence_is_one_node() -> None:
    _, _, draft, bundle = outer_only()
    slot = next(item for item in draft.child_bindings if item.slot_key == "review")
    projection = bundle.network.execution_projection()
    nodes = [node for node in projection.nodes if node.occurrence_id == slot.occurrence_id]
    assert len(nodes) == 1
    assert nodes[0].kind is ProjectionNodeKind.OCCURRENCE


def test_an_unexpanded_compound_still_spans_entry_to_exit() -> None:
    """Without the span the exit could be scheduled before the entry."""

    _, _, draft, bundle = outer_only()
    slot = next(item for item in draft.child_bindings if item.slot_key == "sub")
    projection = bundle.network.execution_projection()
    spans = [
        edge
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.COMPOUND_SPAN
        and edge.source == projection.entry_node_id(slot.occurrence_id)
        and edge.target == projection.exit_node_id(slot.occurrence_id)
    ]
    assert len(spans) == 1
    assert "unexpanded" in spans[0].origin


def test_the_parent_entry_opens_every_child() -> None:
    _, binding, draft, bundle = outer_only()
    projection = bundle.network.execution_projection()
    parent_entry = projection.entry_node_id(str(binding.task_id))
    opened = {
        edge.target
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.REFINEMENT_OPEN and edge.source == parent_entry
    }
    assert opened == {projection.entry_node_id(item.occurrence_id) for item in draft.child_bindings}


def test_each_gating_child_closes_the_parent_exit() -> None:
    _, binding, draft, bundle = outer_only()
    projection = bundle.network.execution_projection()
    parent_exit = projection.exit_node_id(str(binding.task_id))
    closing = {
        edge.source
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.REFINEMENT_CLOSE and edge.target == parent_exit
    }
    assert closing == {projection.exit_node_id(item.occurrence_id) for item in draft.child_bindings}


def test_the_compiled_network_is_a_dag() -> None:
    _, _, _, bundle = outer_only()
    report = validate_execution_projection(bundle.network.execution_projection(), BUDGET)
    assert ProblemKind.CYCLE not in report.kinds


def test_the_naive_union_of_the_same_network_is_a_cycle() -> None:
    """The contrast §24.1 decision 2 exists to make: same facts, two readings."""

    _, binding, draft, bundle = outer_only()
    dependencies: dict[str, list[str]] = {}
    parent = str(binding.task_id)
    for item in draft.child_bindings:
        child = str(item.occurrence_id)
        dependencies.setdefault(child, []).append(parent)  # child waits for parent
        dependencies.setdefault(parent, []).append(child)  # parent waits for child
    with pytest.raises(DependencyError):
        check_dependencies(dependencies)
    assert (
        validate_execution_projection(
            bundle.network.execution_projection(), BUDGET
        ).topological_order
        is not None
    )


def test_refinement_relations_are_acyclic_too() -> None:
    _, _, _, bundle = outer_only()
    assert validate_refinement_acyclic(bundle.network).ok


def test_expanding_the_compound_child_closes_its_gate_with_a_real_child() -> None:
    _, _, sub_occurrence, inner_draft, bundle = both_levels()
    projection = bundle.network.execution_projection()
    leaf = inner_draft.child_bindings[0].occurrence_id
    closing = {
        edge.source
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.REFINEMENT_CLOSE
        and edge.target == projection.exit_node_id(sub_occurrence)
    }
    assert closing == {projection.exit_node_id(leaf)}


def test_the_span_disappears_once_the_compound_is_expanded() -> None:
    _, _, sub_occurrence, _, bundle = both_levels()
    projection = bundle.network.execution_projection()
    spans = [
        edge
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.COMPOUND_SPAN
        and edge.source == projection.entry_node_id(sub_occurrence)
    ]
    assert spans == []


def test_two_levels_of_refinement_stay_acyclic() -> None:
    _, _, _, _, bundle = both_levels()
    report = validate_execution_projection(bundle.network.execution_projection(), BUDGET)
    assert report.ok, [problem.detail for problem in report.problems]


def test_the_data_edge_out_of_a_compound_leaves_its_exit() -> None:
    _, _, draft, bundle = outer_only()
    slot = next(item for item in draft.child_bindings if item.slot_key == "sub")
    review = next(item for item in draft.child_bindings if item.slot_key == "review")
    projection = bundle.network.execution_projection()
    data_edges = [edge for edge in projection.edges if edge.kind is ProjectionEdgeKind.DATA]
    assert len(data_edges) == 1
    assert data_edges[0].source == projection.exit_node_id(slot.occurrence_id)
    assert data_edges[0].target == projection.entry_node_id(review.occurrence_id)


def test_a_compound_child_carries_no_operator() -> None:
    _, _, _, bundle = outer_only()
    sub = next(
        item
        for item in bundle.task_bindings
        if item.occurrence_binding is not None and item.occurrence_binding.slot_key == "sub"
    )
    assert sub.form is TaskForm.COMPOUND
    assert sub.operator_ref is None


def test_a_compound_child_declares_no_resources() -> None:
    _, _, _, bundle = outer_only()
    sub = next(
        item
        for item in bundle.task_bindings
        if item.occurrence_binding is not None and item.occurrence_binding.slot_key == "sub"
    )
    assert sub.resource_reads == () and sub.resource_writes == ()
    assert sub.side_effect_kind is None


def test_the_budget_requirement_counts_the_compound_child() -> None:
    _, _, _, bundle = outer_only()
    assert bundle.budget_requirement.new_compound_occurrences == 1
    assert bundle.budget_requirement.new_primitive_occurrences == 1


# ======================================================================= refusals


def test_a_root_coverage_gap_is_refused() -> None:
    env = gate_env()
    contract = method(
        "gate.uncovered",
        "gate.goal",
        parameter_schema="gate.goal.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "gate.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-other", "leaf", "c-produced"),),
        finalizer="leaf",
    )
    # Admission already refuses it; the compiler refuses it again, because a delta
    # may arrive from a session that used a different policy.
    receipt = env.admit(contract)
    assert not receipt.admitted
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused, match="uncovered"):
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
        )


def test_the_coverage_refusal_names_the_missing_criterion() -> None:
    env = gate_env()
    contract = method(
        "gate.uncovered-2",
        "gate.goal",
        parameter_schema="gate.goal.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "gate.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-other", "leaf", "c-produced"),),
        finalizer="leaf",
    )
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused) as error:
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
        )
    assert "c-root" in str(error.value)


def test_a_required_input_port_with_no_producer_is_refused() -> None:
    env = gate_env()
    contract = method(
        "gate.unbound-port",
        "gate.goal",
        parameter_schema="gate.goal.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "hungry",
                "gate.needs-input",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-root", "hungry", "c-fed"),),
        finalizer="hungry",
    )
    env.admit(contract)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused, match="unbound_port"):
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=contract,
            catalog=env.catalog,
            schemas=env.schemas,
        )


def test_a_parent_this_network_does_not_have_is_refused() -> None:
    env = gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, outer, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, outer, {}, report, catalog=env.catalog, schemas=env.schemas)
    other = task_binding(
        env,
        "gate.goal",
        task_id="task-other",
        obligation="obl-other",
        parameters={"subject": "alpha"},
    )
    with pytest.raises(CompilationRefused, match="does not contain"):
        compile_refinement_bundle(
            draft,
            root_network(env, other),
            method=outer,
            catalog=env.catalog,
            schemas=env.schemas,
        )


def test_a_primitive_parent_is_refused_by_the_compiler() -> None:
    env = gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, outer, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, outer, {}, report, catalog=env.catalog, schemas=env.schemas)
    leaf = task_binding(
        env,
        "gate.leaf",
        task_id=str(binding.task_id),
        obligation=str(binding.obligation_id),
        parameters={"subject": "alpha"},
    )
    network = TaskNetworkSnapshot(
        mission_id=env.mission,
        plan_revision=0,
        occurrences=(
            OccurrenceSpec(
                occurrence_id=str(leaf.task_id),
                task_id=leaf.task_id,
                obligation_id=leaf.obligation_id,
                form=TaskForm.PRIMITIVE,
            ),
        ),
        task_bindings=(leaf,),
        root_occurrence_ids=(str(leaf.task_id),),
    )
    with pytest.raises(CompilationRefused, match="primitive"):
        compile_refinement_bundle(
            draft,
            network,
            method=outer,
            catalog=env.catalog,
            schemas=env.schemas,
        )


def test_a_second_adopted_method_for_one_occurrence_is_refused() -> None:
    env, binding, _, bundle = outer_only()
    second = outer_method("gate.outer-2")
    env.admit(second)
    report = assess_method(
        binding, second, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, second, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused, match="two adopted method instances"):
        compile_refinement_bundle(
            draft,
            bundle.network,
            method=second,
            catalog=env.catalog,
            schemas=env.schemas,
            registry=env.registry,
        )


def test_retiring_the_first_method_makes_room_for_the_second() -> None:
    env, binding, first_draft, bundle = outer_only()
    second = outer_method("gate.outer-2")
    env.admit(second)
    report = assess_method(
        binding, second, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, second, {}, report, catalog=env.catalog, schemas=env.schemas)
    replacement = compile_refinement_bundle(
        draft,
        bundle.network,
        method=second,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        retire_instance_ids=(first_draft.instance_id,),
    )
    assert first_draft.instance_id not in replacement.network.adopted_instance_ids
    assert draft.instance_id in replacement.network.adopted_instance_ids


def test_a_retired_instance_is_named_in_the_delta() -> None:
    env, binding, first_draft, bundle = outer_only()
    second = outer_method("gate.outer-2")
    env.admit(second)
    report = assess_method(
        binding, second, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, second, {}, report, catalog=env.catalog, schemas=env.schemas)
    replacement = compile_refinement_bundle(
        draft,
        bundle.network,
        method=second,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        retire_instance_ids=(first_draft.instance_id,),
    )
    assert replacement.delta.retired_instance_ids == (first_draft.instance_id,)


def test_a_size_bound_is_reported_as_a_bound() -> None:
    env = gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, outer, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, outer, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused) as error:
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=outer,
            catalog=env.catalog,
            schemas=env.schemas,
            budget=replace(BUDGET, max_nodes=2),
        )
    assert ProblemKind.BOUND_REACHED in error.value.kinds()


def test_the_refusal_carries_the_structured_report() -> None:
    env = gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    report = assess_method(
        binding, outer, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, outer, {}, report, catalog=env.catalog, schemas=env.schemas)
    with pytest.raises(CompilationRefused) as error:
        compile_refinement_bundle(
            draft,
            root_network(env, binding),
            method=outer,
            catalog=env.catalog,
            schemas=env.schemas,
            budget=replace(BUDGET, max_nodes=2),
        )
    assert error.value.projection_report is not None


# ===================================================================== validation


def test_validate_delta_passes_the_compiled_increment() -> None:
    env, _, _, bundle = outer_only()
    env.admit(inner_method())
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert report.ok, [problem.detail for problem in report.problems]


def test_validate_delta_says_when_it_could_not_check_preconditions() -> None:
    env, _, _, bundle = outer_only()
    report = validate_delta(
        bundle.delta, bundle.network, BUDGET, network=bundle.network, registry=env.registry
    )
    assert DeltaProblemKind.NOT_CHECKED in report.kinds


def test_validate_delta_says_when_it_could_not_check_reducibility() -> None:
    env, _, _, bundle = outer_only()
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert any(
        "reducibility" in problem.detail for problem in report.of_kind(DeltaProblemKind.NOT_CHECKED)
    )


def test_a_compound_no_method_can_refine_is_not_reducible() -> None:
    env, _, _, bundle = outer_only()
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert DeltaProblemKind.NOT_REDUCIBLE in report.kinds


def test_registering_a_method_for_the_child_makes_it_reducible() -> None:
    env, _, _, bundle = outer_only()
    env.admit(inner_method())
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert DeltaProblemKind.NOT_REDUCIBLE not in report.kinds


def test_a_refuted_precondition_is_classified_not_just_reported() -> None:
    from agent_orchestrator.planning.htn.validation import PreconditionClass

    env, _, _, bundle = outer_only()
    env.say("gate.ready", {"subject": "alpha"}, TruthValue.FALSE)
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert [item.classification for item in report.preconditions] == [PreconditionClass.REFUTED]
    assert DeltaProblemKind.PRECONDITION_FALSE in report.kinds


def test_an_unknown_precondition_is_classified_as_needing_evidence() -> None:
    from agent_orchestrator.planning.htn.validation import PreconditionClass

    env, _, _, bundle = outer_only()
    env.say("gate.ready", {"subject": "alpha"}, TruthValue.UNKNOWN)
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert [item.classification for item in report.preconditions] == [
        PreconditionClass.NEEDS_EVIDENCE
    ]


def test_a_conflicting_precondition_is_classified_as_conflicted() -> None:
    from agent_orchestrator.planning.htn.validation import PreconditionClass

    env, _, _, bundle = outer_only()
    env.say("gate.ready", {"subject": "alpha"}, TruthValue.CONFLICT)
    report = validate_delta(
        bundle.delta,
        bundle.network,
        BUDGET,
        network=bundle.network,
        registry=env.registry,
        methods={"gate.outer": outer_method()},
        snapshot=env.snapshot(),
        predicates=env.predicates,
    )
    assert [item.classification for item in report.preconditions] == [PreconditionClass.CONFLICTED]


def test_unbound_required_ports_are_listed_for_the_caller() -> None:
    env, _, _, bundle = outer_only()
    assert unbound_required_ports(bundle.network) == ()
    del env


def test_the_compiler_does_not_touch_the_network_it_was_given() -> None:
    env = gate_env()
    outer = outer_method()
    env.admit(outer)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    before = network.execution_projection().node_ids()
    compile_method(env, outer, binding, network)
    assert network.execution_projection().node_ids() == before


@pytest.mark.parametrize(
    "module",
    ["registry", "grounding", "refinement", "compiler", "validation"],
)
@pytest.mark.parametrize("banned", ["storage", "sqlite3", "commit_service", "scheduling"])
def test_the_planning_modules_import_no_persistence_layer(module: str, banned: str) -> None:
    import agent_orchestrator.planning.htn as package

    source = (Path(package.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    assert f"import {banned}" not in source
    assert f"from {banned}" not in source


@pytest.mark.parametrize(
    "module",
    ["registry", "grounding", "refinement", "compiler", "validation"],
)
def test_no_module_branches_on_a_domain_name(module: str) -> None:
    """The generality gate of §7.1: two domains differ only by their data."""

    import agent_orchestrator.planning.htn as package

    source = (Path(package.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    for needle in ('== "code"', "== 'code'", '== "appworld"', "== 'appworld'"):
        assert needle not in source


# ============================================================ opened obligations


def duty_env() -> Env:
    """``gate_env`` plus a compound side-goal a method may add on its own authority."""

    env = gate_env()
    env.register_type(
        "gate.aux",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-aux",),
        domain="gate",
    )
    return env


def aux_method(method_id: str = "gate.aux-inner"):
    return method(
        method_id,
        "gate.aux",
        parameter_schema="gate.aux.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "gate.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
        ),
        links=(("c-aux", "leaf", "c-produced"),),
        finalizer="leaf",
    )


def with_side_goal(method_id: str = "gate.with-side-goal"):
    """One required refinement plus one independently authorised addition."""

    return method(
        method_id,
        "gate.goal",
        parameter_schema="gate.goal.params",
        applicable=(atom("gate.ready", {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "gate.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("gate.read",),
            ),
            step(
                "aux",
                "gate.aux",
                TaskForm.COMPOUND,
                {"subject": param("subject")},
                relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
            ),
        ),
        links=(("c-root", "leaf", "c-produced"),),
        finalizer="leaf",
    )


def authority(identifier: str = "authority-1") -> TypedRef:
    """The decision that authorised a new duty.

    ``TypedRefKind`` has no ``authority`` member, so the closest honest kind is
    ``review`` — an authorisation is a recorded human or system decision.  Noted in
    the journal as a contract observation rather than worked around silently.
    """

    return TypedRef(
        kind=TypedRefKind.REVIEW,
        id=identifier,
        revision=1,
        content_hash=content_hash_of(identifier),
    )


def side_goal_bundle(env: Env | None = None, **kwargs):
    env = env or duty_env()
    contract = with_side_goal()
    env.admit(contract)
    binding = task_binding(env, "gate.goal", parameters={"subject": "alpha"})
    network = root_network(env, binding)
    report = assess_method(
        binding, contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    draft = ground_method(binding, contract, {}, report, catalog=env.catalog, schemas=env.schemas)
    kwargs.setdefault("slot_authorizations", {"aux": authority()})
    bundle = compile_refinement_bundle(
        draft,
        network,
        method=contract,
        catalog=env.catalog,
        schemas=env.schemas,
        registry=env.registry,
        **kwargs,
    )
    return env, binding, draft, bundle


def test_a_refining_slot_opens_no_duty() -> None:
    """§6.1: splitting a task spends the parent's allowance, it does not mint one."""

    _, _, _, bundle = outer_only()
    assert bundle.new_obligations == ()
    assert bundle.delta.obligation_openings == ()


def test_an_independently_authorized_slot_opens_one_duty() -> None:
    _, binding, draft, bundle = side_goal_bundle()
    assert len(bundle.new_obligations) == 1
    opening = bundle.new_obligations[0]
    assert opening.parent_obligation_id == binding.obligation_id
    aux = next(item for item in draft.child_bindings if item.slot_key == "aux")
    assert opening.obligation_id == aux.obligation_id


def test_the_opening_travels_on_the_delta() -> None:
    _, _, _, bundle = side_goal_bundle()
    assert bundle.delta.obligation_openings == bundle.new_obligations


def test_the_opening_records_the_authority_that_authorised_it() -> None:
    _, _, _, bundle = side_goal_bundle()
    assert bundle.delta.obligation_openings[0].authorization_ref == authority()


def test_an_opening_inherits_a_share_rather_than_inventing_budget() -> None:
    _, _, _, bundle = side_goal_bundle()
    opening = bundle.delta.obligation_openings[0]
    assert opening.budget_inheritance is BudgetInheritance.INHERIT_PARENT_FUEL_SHARE
    assert opening.fuel_share == DEFAULT_CHILD_FUEL_SHARE
    assert opening.grant_ref is None


def test_independently_authorized_work_without_an_authority_is_refused() -> None:
    """§6.1: planning alone does not create responsibility.

    The contract refuses such an opening too, so this asserts the *compiler's* own
    wording: the refusal has to name the step, before an opening is built, or the
    caller is left with a malformed-opening message it cannot trace back to a slot.
    """

    with pytest.raises(CompilationRefused) as error:
        side_goal_bundle(slot_authorizations={})
    assert "slot 'aux' adds independently authorised work" in str(error.value)
    assert "does not create responsibility" in str(error.value)


def test_a_named_grant_makes_the_opening_a_separate_grant() -> None:
    _, _, _, bundle = side_goal_bundle(slot_grants={"aux": "grant-7"})
    opening = bundle.delta.obligation_openings[0]
    assert opening.budget_inheritance is BudgetInheritance.SEPARATE_GRANT
    assert opening.grant_ref == "grant-7"
    assert opening.fuel_share is None


def test_a_refining_slot_may_not_ask_for_its_own_grant() -> None:
    env = duty_env()
    with pytest.raises(CompilationRefused, match="fresh retry"):
        side_goal_bundle(env, slot_grants={"leaf": "grant-9"})


def test_the_contract_refuses_a_separately_granted_refinement() -> None:
    """The same rule, one layer down: a refinement has no grant of its own."""

    env = duty_env()
    signature = env.catalog.require(ref("gate.aux")).goal_signature
    with pytest.raises(ContractError, match="fresh retry"):
        ObligationOpening(
            obligation_id="obl-child",
            parent_obligation_id="obl-root",
            relation=ObligationRelation.REFINES_PARENT,
            requirement_refs=("req-1",),
            goal_signature=signature,
            budget_inheritance=BudgetInheritance.SEPARATE_GRANT,
            grant_ref="grant-1",
        )


def test_the_contract_refuses_an_unauthorised_independent_duty() -> None:
    env = duty_env()
    signature = env.catalog.require(ref("gate.aux")).goal_signature
    with pytest.raises(ContractError, match="authorised it"):
        ObligationOpening(
            obligation_id="obl-child",
            parent_obligation_id="obl-root",
            relation=ObligationRelation.INDEPENDENT_AUTHORIZED,
            requirement_refs=("req-1",),
            goal_signature=signature,
            fuel_share=1,
        )


def test_opening_the_duty_moves_fuel_out_of_the_parent() -> None:
    env, binding, _, bundle = side_goal_bundle()
    ledger = ledger_for(binding, fuel=3, mission=env.mission)
    before = ledger.remaining_fuel(binding.obligation_id)
    apply_obligation_openings(ledger, bundle.delta)
    opening = bundle.delta.obligation_openings[0]
    assert ledger.remaining_fuel(binding.obligation_id) == before - opening.fuel_share
    assert ledger.remaining_fuel(opening.obligation_id) == opening.fuel_share


def test_a_newly_opened_sub_goal_can_be_refined() -> None:
    """The whole point of CR#6: the child is workable the moment it is opened."""

    env, binding, draft, bundle = side_goal_bundle()
    env.admit(aux_method())
    ledger = ledger_for(binding, fuel=3, mission=env.mission)
    apply_obligation_openings(ledger, bundle.delta)
    aux = next(item for item in draft.child_bindings if item.slot_key == "aux")
    frontier = (FrontierItem.of(bundle.network.occurrence(aux.occurrence_id)),)
    report = refine(
        frontier,
        network=bundle.network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.REFINED,)


def test_refining_a_duty_nobody_opened_says_so_instead_of_crashing() -> None:
    env, binding, draft, bundle = side_goal_bundle()
    env.admit(aux_method())
    ledger = ledger_for(binding, fuel=3, mission=env.mission)
    aux = next(item for item in draft.child_bindings if item.slot_key == "aux")
    frontier = (FrontierItem.of(bundle.network.occurrence(aux.occurrence_id)),)
    report = refine(
        frontier,
        network=bundle.network,
        registry=env.registry,
        catalog=env.catalog,
        schemas=env.schemas,
        predicates=env.predicates,
        snapshot=env.snapshot(),
        capabilities=env.capabilities(),
        ledger=ledger,
        budget=BUDGET,
    )
    assert report.outcomes == (RefinementOutcome.OBLIGATION_NOT_OPENED,)
    assert "ObligationOpening" in report.decisions[0].reason


def test_an_independently_authorized_slot_does_not_gate_the_parent() -> None:
    """§6.1: authorised-optional work is added, not required of the parent."""

    _, _, draft, bundle = side_goal_bundle()
    aux = next(item for item in draft.child_bindings if item.slot_key == "aux")
    assert aux.requiredness is Requiredness.OPTIONAL_AUTHORIZED
    alternative = bundle.network.refinement_view().alternatives_for(bundle.parent_binding.task_id)[
        0
    ]
    assert aux.occurrence_id in alternative.optional_children


def test_reopening_an_existing_duty_is_refused() -> None:
    env, binding, _, bundle = side_goal_bundle()
    ledger = ledger_for(binding, fuel=3, mission=env.mission)
    apply_obligation_openings(ledger, bundle.delta)
    with pytest.raises(ContractError, match="already registered"):
        apply_obligation_openings(ledger, bundle.delta)


def test_commit_readiness_refuses_an_occurrence_whose_duty_nobody_opened() -> None:
    from dataclasses import replace

    _, binding, _, bundle = side_goal_bundle()
    stripped = replace(bundle.delta, obligation_openings=())
    with pytest.raises(ContractError, match="unregistered duty"):
        require_commit_ready(stripped, registered_obligations=frozenset({binding.obligation_id}))


def test_commit_readiness_accepts_the_increment_with_its_openings() -> None:
    _, binding, _, bundle = side_goal_bundle()
    assert (
        require_commit_ready(
            bundle.delta, registered_obligations=frozenset({binding.obligation_id})
        )
        is bundle.delta
    )


# ============================================================== port overbinding


def test_the_contract_refuses_two_bindings_into_one_input_port() -> None:
    """TG decision 3, inside one delta: a single-valued port takes one binding."""

    env, binding, _, bundle = outer_only()
    existing = bundle.delta.data_requirements[0]
    twin = replace(
        existing,
        requirement_id="data-twin",
        producer_occurrence=str(binding.task_id),
    )
    with pytest.raises(ContractError, match="single-valued input port"):
        replace(bundle.delta, data_requirements=(existing, twin))
    del env


def test_a_second_delta_binding_a_filled_port_is_reported_by_the_projection() -> None:
    """The cross-delta case the contract cannot see, and the layer that can."""

    env, binding, _, bundle = outer_only()
    existing = bundle.delta.data_requirements[0]
    other_producer = str(binding.task_id)
    overbound = TaskNetworkSnapshot(
        mission_id=bundle.network.mission_id,
        plan_revision=bundle.network.plan_revision,
        occurrences=bundle.network.occurrences,
        task_bindings=bundle.network.task_bindings,
        method_instances=bundle.network.method_instances,
        adopted_instance_ids=bundle.network.adopted_instance_ids,
        root_occurrence_ids=bundle.network.root_occurrence_ids,
        order_constraints=bundle.network.order_constraints,
        data_requirements=(
            *bundle.network.data_requirements,
            replace(existing, requirement_id="data-later", producer_occurrence=other_producer),
        ),
        obligation_coverage=bundle.network.obligation_coverage,
        required_obligations=bundle.network.required_obligations,
    )
    report = validate_execution_projection(overbound.execution_projection(), BUDGET)
    assert ProblemKind.SINGLE_PORT_OVERBOUND in report.kinds
    del env
