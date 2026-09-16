# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1b: the immutable TaskNetwork snapshot and its execution projection.

Every test here is a pure-graph counterexample.  Nothing touches a database, a
scheduler or an agent: a snapshot is built from contract values, a projection is
compiled from it, and the validator reports on the projection.  The two facts the
suite exists to pin down are

* the *compiled* projection of ``parent refines into children`` is **not** a
  cycle, while the naive union of "parent waits for children" and "children wait
  for the parent" **is** one (TG §6, plan §24.1 decision 2), and
* the cycle verdict is a property of the graph, not of the names or of the input
  order — renaming every id or shuffling every input list leaves it unchanged,
  cross-checked against ``graphlib.TopologicalSorter`` (TG §14.2).

Both graphs in the first bullet are derived from the *same* snapshot, by
:func:`naive_union_edges` and by ``execution_projection`` — so the contrast is
between two readings of one network, not between a compiler and a hand-written
counterexample that could drift away from it.
"""

from __future__ import annotations

import graphlib
import random
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from agent_orchestrator.contracts.htn import (
    ChildBinding,
    ContractRevision,
    DataRequirement,
    EndpointKind,
    GoalSignature,
    GraphStructureBudget,
    MethodInstanceDraft,
    MethodInstanceId,
    MethodRef,
    MissionRef,
    NetworkEndpoint,
    ObligationCoverage,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    PortCardinality,
    PortOrdering,
    PortSpec,
    RelationKind,
    ReleaseCondition,
    Requiredness,
    ResourceRef,
    ReusePolicy,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    TypedEdge,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.semantic_base import VersionedRef
from agent_orchestrator.graph.dependency_checker import DependencyError, check_dependencies
from agent_orchestrator.graph.projection_validation import (
    MAX_CONFLICTS_PER_RESOURCE,
    GraphIntegrityError,
    ProblemKind,
    kahn_order,
    require_topological_order,
    validate_execution_projection,
    validate_refinement_acyclic,
)
from agent_orchestrator.graph.task_network import (
    DEFAULT_PROJECTION_BUDGET,
    ExecutionProjection,
    ProjectionEdgeKind,
    ProjectionNodeKind,
    TaskNetworkSnapshot,
    flip_from_dependency_map,
    flip_to_dependency_map,
)

HASH_A = "a" * 64
MISSION = MissionRef("m-1")


# --------------------------------------------------------------------------------------
# Builders.  A test says which shape it wants; the plumbing stays out of the way.
# --------------------------------------------------------------------------------------


def vref(name: str) -> VersionedRef:
    return VersionedRef(id=name, version=1, content_hash=HASH_A)


def budget(**overrides: int) -> GraphStructureBudget:
    fields = {
        "budget_version": DEFAULT_PROJECTION_BUDGET.budget_version,
        "max_live_tasks": DEFAULT_PROJECTION_BUDGET.max_live_tasks,
        "max_depth": DEFAULT_PROJECTION_BUDGET.max_depth,
        "max_expanded_nodes": DEFAULT_PROJECTION_BUDGET.max_expanded_nodes,
        "max_candidates": DEFAULT_PROJECTION_BUDGET.max_candidates,
        "max_recursion_fuel": DEFAULT_PROJECTION_BUDGET.max_recursion_fuel,
        "max_nodes": DEFAULT_PROJECTION_BUDGET.max_nodes,
        "max_edges": DEFAULT_PROJECTION_BUDGET.max_edges,
        "max_fan_out": DEFAULT_PROJECTION_BUDGET.max_fan_out,
    }
    fields.update(overrides)
    return GraphStructureBudget(**fields)


def port(
    key: str,
    *,
    cardinality: PortCardinality = PortCardinality.SINGLE,
    required: bool = True,
    ordering: PortOrdering | None = None,
) -> PortSpec:
    return PortSpec(
        port_key=key,
        schema_ref=vref(f"schema-{key}"),
        cardinality=cardinality,
        required=required,
        ordering=ordering,
    )


def signature(task: str, criteria: Sequence[str] = ()) -> GoalSignature:
    return GoalSignature(
        signature_id=f"gs-{task}",
        version=1,
        parameter_schema_ref=vref("schema-params"),
        output_schema_ref=vref("schema-outputs"),
        statement=f"achieve {task}",
        coverage_criteria=tuple(criteria),
    )


def binding(
    task: str,
    *,
    form: TaskForm = TaskForm.PRIMITIVE,
    inputs: Sequence[PortSpec] = (),
    outputs: Sequence[PortSpec] = (),
    adopted: str | None = None,
    criteria: Sequence[str] = (),
    obligation: str | None = None,
    reads: Sequence[ResourceRef] = (),
    writes: Sequence[ResourceRef] = (),
) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=TaskRef(task),
        obligation_id=ObligationId(obligation or f"ob-{task}"),
        contract_revision=ContractRevision(1),
        contract_hash=HASH_A,
        form=form,
        goal_signature=signature(task, criteria),
        input_ports=tuple(inputs),
        output_ports=tuple(outputs),
        operator_ref=vref(f"op-{task}") if form is TaskForm.PRIMITIVE else None,
        adopted_method_instance_id=None if adopted is None else MethodInstanceId(adopted),
        resource_reads=tuple(reads),
        resource_writes=tuple(writes),
    )


def occurrence(
    occ: str,
    task: str,
    *,
    form: TaskForm = TaskForm.PRIMITIVE,
    requiredness: Requiredness = Requiredness.REQUIRED,
    obligation: str | None = None,
) -> OccurrenceSpec:
    return OccurrenceSpec(
        occurrence_id=OccurrenceId(occ),
        task_id=TaskRef(task),
        obligation_id=ObligationId(obligation or f"ob-{task}"),
        form=form,
        requiredness=requiredness,
    )


Slot = tuple[str, str] | tuple[str, str, Requiredness] | tuple[str, str, Requiredness, ReusePolicy]


def instance(
    instance_id: str,
    goal_task: str,
    goal_occurrence: str,
    children: Sequence[Slot],
    *,
    method: str = "meth-generic",
    obligation: str | None = None,
) -> MethodInstanceDraft:
    bindings = []
    for child in children:
        slot, occ = child[0], child[1]
        needed: Requiredness = child[2] if len(child) > 2 else Requiredness.REQUIRED  # type: ignore[misc]
        reuse: ReusePolicy = child[3] if len(child) > 3 else ReusePolicy.NEW_WORK  # type: ignore[misc]
        bindings.append(
            ChildBinding(
                instance_id=MethodInstanceId(instance_id),
                slot_key=slot,
                occurrence_id=OccurrenceId(occ),
                obligation_id=ObligationId(f"ob-{occ}"),
                requiredness=needed,
                reuse_policy=reuse,
            )
        )
    return MethodInstanceDraft(
        instance_id=MethodInstanceId(instance_id),
        goal_id=TaskRef(goal_task),
        goal_occurrence_id=OccurrenceId(goal_occurrence),
        obligation_id=ObligationId(obligation or f"ob-{goal_task}"),
        method_ref=MethodRef(method_id=method, version=1, content_hash=HASH_A),
        child_bindings=tuple(bindings),
        plan_revision=PlanRevision(1),
    )


def order(before: str, after: str) -> OrderConstraint:
    return OrderConstraint(
        before=OccurrenceId(before),
        after=OccurrenceId(after),
        release_condition=ReleaseCondition.ACCEPTED,
    )


def data(req: str, producer: str, out_port: str, consumer: str, in_port: str) -> DataRequirement:
    return DataRequirement(
        requirement_id=req,
        producer_occurrence=OccurrenceId(producer),
        output_port=out_port,
        consumer_occurrence=OccurrenceId(consumer),
        input_port=in_port,
        schema_ref=vref(f"schema-{out_port}"),
        assurance_policy_ref="pol-assurance",
        freshness_policy_ref="pol-freshness",
    )


def snapshot(**overrides: object) -> TaskNetworkSnapshot:
    """The canonical mission used by most tests.

    ``t-root`` (compound) refines into ``analyse`` / ``build`` / ``deliver``;
    ``build`` is itself compound and refines into ``b1`` / ``b2`` / a composition
    review that waits on its two siblings.  ``analyse`` hands a report to
    ``deliver`` over an explicit DATA port.
    """

    base: dict[str, object] = {
        "mission_id": MISSION,
        "plan_revision": PlanRevision(1),
        "occurrences": (
            occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
            occurrence("o-a", "t-a"),
            occurrence("o-c", "t-c", form=TaskForm.COMPOUND),
            occurrence("o-d", "t-d"),
            occurrence("o-b1", "t-b1"),
            occurrence("o-b2", "t-b2"),
            occurrence("o-join", "t-join"),
        ),
        "task_bindings": (
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding("t-d", inputs=(port("report"),)),
            binding("t-b1"),
            binding("t-b2"),
            binding("t-join"),
        ),
        "method_instances": (
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance(
                "mi-c",
                "t-c",
                "o-c",
                (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join")),
            ),
        ),
        "adopted_instance_ids": (MethodInstanceId("mi-root"), MethodInstanceId("mi-c")),
        "root_occurrence_ids": (OccurrenceId("o-root"),),
        "order_constraints": (
            order("o-a", "o-c"),
            order("o-c", "o-d"),
            order("o-b1", "o-join"),
            order("o-b2", "o-join"),
        ),
        "data_requirements": (data("dr-1", "o-a", "report", "o-d", "report"),),
        "obligation_coverage": (
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1", "c2"),
                covered_by=(OccurrenceId("o-a"), OccurrenceId("o-d")),
            ),
        ),
        "required_obligations": (ObligationId("ob-t-root"),),
    }
    base.update(overrides)
    return TaskNetworkSnapshot(**base)  # type: ignore[arg-type]


def naive_union_edges(net: TaskNetworkSnapshot) -> dict[str, list[str]]:
    """The mistake the gate compilation exists to avoid, derived from the same network.

    Union every arrow that points *somewhere* — "the parent waits for its
    children" and "a child is opened by its parent" — into one ``key →
    dependencies`` map, as if the two relations meant the same thing.  The result
    is what §24.1 decision 2 says not to build.
    """

    edges: dict[str, list[str]] = {str(spec.occurrence_id): [] for spec in net.occurrences}
    for spec in net.occurrences:
        occurrence_id = spec.occurrence_id
        for child in net.adopted_children(occurrence_id):
            parent, kid = str(occurrence_id), str(child.occurrence_id)
            if kid not in edges[parent]:
                edges[parent].append(kid)  # the parent waits for the child
            if parent not in edges[kid]:
                edges[kid].append(parent)  # the child waits to be opened
    for constraint in net.order_constraints:
        after, before = str(constraint.after), str(constraint.before)
        if before not in edges[after]:
            edges[after].append(before)
    return edges


def kinds(report: object) -> set[ProblemKind]:
    return {problem.kind for problem in report.problems}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# 1. Compilation: gates, not a pseudo-cycle
# --------------------------------------------------------------------------------------


def test_snapshot_is_frozen() -> None:
    net = snapshot()
    with pytest.raises(Exception):
        net.plan_revision = PlanRevision(2)  # type: ignore[misc]
    assert net.mission_id == MISSION


def test_compound_occurrence_compiles_to_entry_and_exit_gates() -> None:
    projection = snapshot().execution_projection()
    entry = projection.node(projection.entry_node_id(OccurrenceId("o-c")))
    exit_ = projection.node(projection.exit_node_id(OccurrenceId("o-c")))
    assert entry.kind is ProjectionNodeKind.COMPOUND_ENTRY
    assert exit_.kind is ProjectionNodeKind.COMPOUND_EXIT
    assert entry.node_id != exit_.node_id


def test_primitive_occurrence_is_one_node_whose_entry_equals_its_exit() -> None:
    projection = snapshot().execution_projection()
    entry = projection.entry_node_id(OccurrenceId("o-a"))
    assert entry == projection.exit_node_id(OccurrenceId("o-a"))
    assert projection.node(entry).kind is ProjectionNodeKind.OCCURRENCE


def test_gates_are_not_billable_work_and_primitives_are() -> None:
    projection = snapshot().execution_projection()
    for node in projection.nodes:
        if node.kind is ProjectionNodeKind.OCCURRENCE:
            assert node.billable is True
        else:
            assert node.billable is False, "a gate is a logical boundary, not a charged agent"


def test_compound_has_no_single_occurrence_node() -> None:
    projection = snapshot().execution_projection()
    compound_nodes = [
        node
        for node in projection.nodes
        if node.occurrence_id in {OccurrenceId("o-root"), OccurrenceId("o-c")}
    ]
    assert {node.kind for node in compound_nodes} == {
        ProjectionNodeKind.COMPOUND_ENTRY,
        ProjectionNodeKind.COMPOUND_EXIT,
    }


def test_children_attach_only_to_the_parent_gates() -> None:
    projection = snapshot().execution_projection()
    parent_nodes = {
        projection.entry_node_id(OccurrenceId("o-c")),
        projection.exit_node_id(OccurrenceId("o-c")),
    }
    child_nodes = {
        projection.entry_node_id(OccurrenceId(child)) for child in ("o-b1", "o-b2", "o-join")
    }
    touching = [
        edge
        for edge in projection.edges
        if (edge.source in child_nodes and edge.target in parent_nodes)
        or (edge.source in parent_nodes and edge.target in child_nodes)
    ]
    assert touching, "the children must be connected to the parent"
    for edge in touching:
        endpoints = (projection.node(edge.source), projection.node(edge.target))
        assert any(node.is_gate for node in endpoints)


def test_parent_entry_opens_each_child_and_child_exit_closes_the_parent() -> None:
    projection = snapshot().execution_projection()
    entry = projection.entry_node_id(OccurrenceId("o-c"))
    exit_ = projection.exit_node_id(OccurrenceId("o-c"))
    pairs = {(edge.source, edge.target, edge.kind) for edge in projection.edges}
    for child in ("o-b1", "o-b2", "o-join"):
        child_node = projection.entry_node_id(OccurrenceId(child))
        assert (entry, child_node, ProjectionEdgeKind.REFINEMENT_OPEN) in pairs
        assert (child_node, exit_, ProjectionEdgeKind.REFINEMENT_CLOSE) in pairs


def test_composition_review_waits_on_siblings_not_on_the_parent() -> None:
    projection = snapshot().execution_projection()
    join = projection.entry_node_id(OccurrenceId("o-join"))
    predecessors = projection.predecessors()[join]
    assert projection.entry_node_id(OccurrenceId("o-b1")) in predecessors
    assert projection.entry_node_id(OccurrenceId("o-b2")) in predecessors
    assert projection.exit_node_id(OccurrenceId("o-c")) not in predecessors


def test_parent_child_pseudo_cycle_is_a_cycle_only_in_the_naive_union() -> None:
    net = snapshot()
    with pytest.raises(DependencyError) as excinfo:
        check_dependencies(naive_union_edges(net))
    assert excinfo.value.reason == "cycle"

    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.CYCLE not in kinds(report)


def test_canonical_mission_has_no_cycle_and_a_complete_topological_order() -> None:
    projection = snapshot().execution_projection()
    report = validate_execution_projection(projection, DEFAULT_PROJECTION_BUDGET)
    assert report.problems == ()
    assert report.topological_order is not None
    assert set(report.topological_order) == set(projection.node_ids())


def test_unexpanded_compound_gets_an_explicit_span_edge() -> None:
    net = snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND),  # no adopted method: still a frontier
            binding("t-d", inputs=(port("report"),)),
            binding("t-b1"),
            binding("t-b2"),
            binding("t-join"),
        ),
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
        ),
        adopted_instance_ids=(MethodInstanceId("mi-root"),),
        order_constraints=(order("o-a", "o-c"), order("o-c", "o-d")),
    )
    projection = net.execution_projection()
    spans = [edge for edge in projection.edges if edge.kind is ProjectionEdgeKind.COMPOUND_SPAN]
    assert [edge.source for edge in spans] == [projection.entry_node_id(OccurrenceId("o-c"))]
    assert OccurrenceId("o-b1") not in projection.projected_occurrences


def _all_optional_snapshot() -> TaskNetworkSnapshot:
    return snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance(
                "mi-c",
                "t-c",
                "o-c",
                (
                    ("b1", "o-b1", Requiredness.OPTIONAL_AUTHORIZED),
                    ("b2", "o-b2", Requiredness.OPTIONAL_AUTHORIZED),
                    ("join", "o-join", Requiredness.OPTIONAL_AUTHORIZED),
                ),
            ),
        ),
        order_constraints=(order("o-a", "o-c"), order("o-c", "o-d")),
    )


def test_a_compound_whose_children_are_all_optional_still_spans_entry_to_exit() -> None:
    projection = _all_optional_snapshot().execution_projection()
    entry = projection.entry_node_id(OccurrenceId("o-c"))
    exit_ = projection.exit_node_id(OccurrenceId("o-c"))
    spans = {
        (edge.source, edge.target)
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.COMPOUND_SPAN
    }
    assert (entry, exit_) in spans


def _reaches(projection: ExecutionProjection, source: str, target: str) -> bool:
    successors = projection.successors()
    seen: set[str] = set()
    pending = [source]
    while pending:
        current = pending.pop()
        for nxt in successors.get(current, ()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                pending.append(nxt)
    return False


def test_an_order_through_an_all_optional_compound_is_not_vacuous() -> None:
    """``o-a before o-c before o-d`` must still connect ``o-a`` to ``o-d``.

    Without the span the exit gate has no predecessor at all, the two ORDER
    constraints stop composing, and the only thing keeping ``o-d`` after ``o-a``
    would be the tie-break ordinal — which means nothing.
    """

    projection = _all_optional_snapshot().execution_projection()
    assert _reaches(
        projection,
        projection.entry_node_id(OccurrenceId("o-c")),
        projection.exit_node_id(OccurrenceId("o-c")),
    )
    assert _reaches(
        projection,
        projection.exit_node_id(OccurrenceId("o-a")),
        projection.entry_node_id(OccurrenceId("o-d")),
    )
    order_ = require_topological_order(projection)
    seat = {key: index for index, key in enumerate(order_)}
    assert (
        seat[projection.entry_node_id(OccurrenceId("o-a"))]
        < seat[projection.entry_node_id(OccurrenceId("o-d"))]
    )


def test_a_compound_whose_children_are_all_optional_is_reported() -> None:
    report = validate_execution_projection(
        _all_optional_snapshot().execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    problems = report.of_kind(ProblemKind.NO_GATING_CHILDREN)
    assert len(problems) == 1
    assert "o-c" in problems[0].detail


def test_a_mixed_method_with_one_required_child_gates_normally() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance(
                "mi-c",
                "t-c",
                "o-c",
                (
                    ("b1", "o-b1"),
                    ("b2", "o-b2", Requiredness.OPTIONAL_AUTHORIZED),
                    ("join", "o-join", Requiredness.OPTIONAL_AUTHORIZED),
                ),
            ),
        ),
        order_constraints=(order("o-a", "o-c"), order("o-c", "o-d")),
    )
    projection = net.execution_projection()
    report = validate_execution_projection(projection, DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.NO_GATING_CHILDREN not in kinds(report)
    assert not [edge for edge in projection.edges if edge.kind is ProjectionEdgeKind.COMPOUND_SPAN]


def test_optional_child_is_opened_but_does_not_gate_the_parent_exit() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance(
                "mi-c",
                "t-c",
                "o-c",
                (
                    ("b1", "o-b1"),
                    ("b2", "o-b2", Requiredness.OPTIONAL_AUTHORIZED),
                    ("join", "o-join"),
                ),
            ),
        ),
        order_constraints=(order("o-a", "o-c"), order("o-c", "o-d"), order("o-b1", "o-join")),
    )
    projection = net.execution_projection()
    entry = projection.entry_node_id(OccurrenceId("o-c"))
    exit_ = projection.exit_node_id(OccurrenceId("o-c"))
    b2 = projection.entry_node_id(OccurrenceId("o-b2"))
    pairs = {(edge.source, edge.target) for edge in projection.edges}
    assert (entry, b2) in pairs
    assert (b2, exit_) not in pairs


def test_order_between_compounds_compiles_to_exit_then_entry() -> None:
    projection = snapshot().execution_projection()
    order_edges = {
        (edge.source, edge.target)
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.ORDER
    }
    assert (
        projection.exit_node_id(OccurrenceId("o-a")),
        projection.entry_node_id(OccurrenceId("o-c")),
    ) in order_edges
    assert (
        projection.exit_node_id(OccurrenceId("o-c")),
        projection.entry_node_id(OccurrenceId("o-d")),
    ) in order_edges


def test_order_between_primitives_is_a_direct_edge() -> None:
    projection = snapshot().execution_projection()
    pairs = {
        (edge.source, edge.target)
        for edge in projection.edges
        if edge.kind is ProjectionEdgeKind.ORDER
    }
    assert (
        projection.entry_node_id(OccurrenceId("o-b1")),
        projection.entry_node_id(OccurrenceId("o-join")),
    ) in pairs


def test_data_edge_points_from_producer_to_consumer() -> None:
    projection = snapshot().execution_projection()
    data_edges = [edge for edge in projection.edges if edge.kind is ProjectionEdgeKind.DATA]
    assert len(data_edges) == 1
    assert data_edges[0].source == projection.exit_node_id(OccurrenceId("o-a"))
    assert data_edges[0].target == projection.entry_node_id(OccurrenceId("o-d"))
    assert "dr-1" in data_edges[0].origin


def test_a_real_order_cycle_is_detected_with_a_concrete_path() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    cycles = report.of_kind(ProblemKind.CYCLE)
    assert len(cycles) == 1
    path = cycles[0].path
    assert path[0] == path[-1], "the evidence must be a closed walk"
    assert len(set(path)) >= 2
    assert report.topological_order is None


def test_a_cyclic_projection_says_which_checks_were_only_partial() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    partial = report.of_kind(ProblemKind.PARTIAL_CHECK)
    assert len(partial) == 1
    assert "acyclic part" in partial[0].detail
    assert partial[0].nodes, "the unchecked nodes must be named"


def test_a_three_node_cycle_path_is_a_genuine_walk_of_the_projection() -> None:
    net = snapshot(
        order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-join"), order("o-join", "o-b1"))
    )
    projection = net.execution_projection()
    report = validate_execution_projection(projection, DEFAULT_PROJECTION_BUDGET)
    path = report.of_kind(ProblemKind.CYCLE)[0].path
    successors = projection.successors()
    for source, target in zip(path, path[1:]):
        assert target in successors[source]


def test_gate_node_ids_stay_injective_for_awkward_occurrence_ids() -> None:
    net = snapshot(
        occurrences=(
            occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
            occurrence("x:entry", "t-a"),
            occurrence("x", "t-b1"),
        ),
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1",)),
            binding("t-a"),
            binding("t-b1"),
        ),
        method_instances=(instance("mi-root", "t-root", "o-root", (("a", "x:entry"), ("b", "x"))),),
        adopted_instance_ids=(MethodInstanceId("mi-root"),),
        order_constraints=(),
        data_requirements=(),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("x"),),
            ),
        ),
    )
    projection = net.execution_projection()
    assert len(set(projection.node_ids())) == len(projection.node_ids())


# --------------------------------------------------------------------------------------
# 2. Views: one network, several readings
# --------------------------------------------------------------------------------------


def test_refinement_view_is_and_inside_a_method() -> None:
    view = snapshot().refinement_view()
    alternative = view.adopted_for(TaskRef("t-c"))
    assert alternative is not None
    assert set(alternative.required_children) == {
        OccurrenceId("o-b1"),
        OccurrenceId("o-b2"),
        OccurrenceId("o-join"),
    }
    assert alternative.goal_occurrence_id == OccurrenceId("o-c")


def test_refinement_view_is_or_between_methods() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))),
            instance("mi-c-alt", "t-c", "o-c", (("only", "o-b1"),), method="meth-shortcut"),
        ),
    )
    view = net.refinement_view()
    alternatives = view.alternatives_for(TaskRef("t-c"))
    assert {item.instance_id for item in alternatives} == {
        MethodInstanceId("mi-c"),
        MethodInstanceId("mi-c-alt"),
    }
    assert [item.adopted for item in alternatives].count(True) == 1


def test_two_adopted_methods_for_one_occurrence_are_refused() -> None:
    with pytest.raises(ContractError, match="two adopted method instances"):
        snapshot(
            method_instances=(
                instance(
                    "mi-root",
                    "t-root",
                    "o-root",
                    (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
                ),
                instance(
                    "mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))
                ),
                instance("mi-c-alt", "t-c", "o-c", (("only", "o-b1"),), method="meth-shortcut"),
            ),
            adopted_instance_ids=(
                MethodInstanceId("mi-root"),
                MethodInstanceId("mi-c"),
                MethodInstanceId("mi-c-alt"),
            ),
        )


def test_non_adopted_alternative_is_not_projected() -> None:
    net = snapshot(
        occurrences=snapshot().occurrences + (occurrence("o-alt", "t-alt"),),
        task_bindings=snapshot().task_bindings + (binding("t-alt"),),
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))),
            instance("mi-c-alt", "t-c", "o-c", (("only", "o-alt"),), method="meth-shortcut"),
        ),
    )
    projection = net.execution_projection()
    assert OccurrenceId("o-alt") not in projection.projected_occurrences


def test_refinement_view_maps_child_occurrence_to_its_parents() -> None:
    view = snapshot().refinement_view()
    assert view.parents_of[OccurrenceId("o-b1")] == (OccurrenceId("o-c"),)
    assert view.parents_of[OccurrenceId("o-c")] == (OccurrenceId("o-root"),)
    assert OccurrenceId("o-root") not in view.parents_of


def test_ancestors_of_walks_the_whole_refinement_chain() -> None:
    view = snapshot().refinement_view()
    assert view.ancestors_of(OccurrenceId("o-b1")) == frozenset(
        {OccurrenceId("o-c"), OccurrenceId("o-root")}
    )
    assert view.ancestors_of(OccurrenceId("o-root")) == frozenset()


# -- TG §12: a shared compound sub-goal, held by two adopting slots ----------------------


def shared_goal_snapshot() -> TaskNetworkSnapshot:
    return TaskNetworkSnapshot(
        mission_id=MISSION,
        plan_revision=PlanRevision(1),
        occurrences=(
            occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
            occurrence("o-left", "t-left", form=TaskForm.COMPOUND),
            occurrence("o-right", "t-right", form=TaskForm.COMPOUND),
            occurrence("o-shared", "t-shared", form=TaskForm.COMPOUND),
            occurrence("o-work", "t-work"),
        ),
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1",)),
            binding("t-left", form=TaskForm.COMPOUND, adopted="mi-left"),
            binding("t-right", form=TaskForm.COMPOUND, adopted="mi-right"),
            binding("t-shared", form=TaskForm.COMPOUND, adopted="mi-shared"),
            binding("t-work"),
        ),
        method_instances=(
            instance("mi-root", "t-root", "o-root", (("left", "o-left"), ("right", "o-right"))),
            instance("mi-left", "t-left", "o-left", (("shared", "o-shared"),)),
            instance(
                "mi-right",
                "t-right",
                "o-right",
                (("shared", "o-shared", Requiredness.REQUIRED, ReusePolicy.SHARE_ACTIVE),),
            ),
            instance("mi-shared", "t-shared", "o-shared", (("work", "o-work"),)),
        ),
        adopted_instance_ids=(
            MethodInstanceId("mi-root"),
            MethodInstanceId("mi-left"),
            MethodInstanceId("mi-right"),
            MethodInstanceId("mi-shared"),
        ),
        root_occurrence_ids=(OccurrenceId("o-root"),),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("o-work"),),
            ),
        ),
        required_obligations=(ObligationId("ob-t-root"),),
    )


def test_a_shared_compound_sub_goal_keeps_both_holders() -> None:
    view = shared_goal_snapshot().refinement_view()
    assert set(view.parents_of[OccurrenceId("o-shared")]) == {
        OccurrenceId("o-left"),
        OccurrenceId("o-right"),
    }


def test_a_shared_compound_sub_goal_is_not_a_duplicate_slot() -> None:
    net = shared_goal_snapshot()
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert report.problems == ()


def test_a_shared_compound_sub_goal_gates_both_consumers() -> None:
    projection = shared_goal_snapshot().execution_projection()
    pairs = {(edge.source, edge.target) for edge in projection.edges}
    shared_entry = projection.entry_node_id(OccurrenceId("o-shared"))
    shared_exit = projection.exit_node_id(OccurrenceId("o-shared"))
    for consumer in ("o-left", "o-right"):
        assert (projection.entry_node_id(OccurrenceId(consumer)), shared_entry) in pairs
        assert (shared_exit, projection.exit_node_id(OccurrenceId(consumer))) in pairs


def test_a_shared_compound_sub_goal_is_expanded_once() -> None:
    projection = shared_goal_snapshot().execution_projection()
    shared_nodes = [
        node for node in projection.nodes if node.occurrence_id == OccurrenceId("o-shared")
    ]
    assert len(shared_nodes) == 2, "one entry and one exit, not one pair per consumer"


def test_snapshot_relations_are_typed_per_kind() -> None:
    net = snapshot()
    assert len(net.relations(RelationKind.ORDER)) == 4
    assert len(net.relations(RelationKind.DATA)) == 1
    refinement = net.relations(RelationKind.REFINEMENT)
    assert {edge.source.kind for edge in refinement} <= {
        EndpointKind.TASK,
        EndpointKind.METHOD_INSTANCE,
    }
    assert net.relations(RelationKind.SUPERVISION) == ()


def support_edge(evidence: str, subject: str, label: str) -> TypedEdge:
    """The contract points a SUPPORT edge evidence → subject, not the other way."""

    return TypedEdge(
        relation=RelationKind.SUPPORT,
        source=NetworkEndpoint(EndpointKind.EVIDENCE, evidence),
        target=NetworkEndpoint(EndpointKind.OCCURRENCE, subject),
        label=label,
    )


def test_support_view_keeps_sets_apart() -> None:
    net = snapshot(
        typed_edges=(
            support_edge("ev-1", "o-a", "set-1"),
            TypedEdge(
                relation=RelationKind.ASSUMPTION,
                source=NetworkEndpoint(EndpointKind.EVIDENCE, "ev-2"),
                target=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-a"),
                label="set-2",
            ),
        )
    )
    view = net.support_view()
    assert set(view.by_support_set) == {"set-1", "set-2"}
    assert len(view.by_subject["o-a"]) == 2


def test_support_and_supervision_edges_never_enter_the_execution_projection() -> None:
    net = snapshot(
        typed_edges=(
            TypedEdge(
                relation=RelationKind.SUPERVISION,
                source=NetworkEndpoint(EndpointKind.SUPERVISOR, "mgr-1"),
                target=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-b1"),
            ),
            TypedEdge(
                relation=RelationKind.FUNDING,
                source=NetworkEndpoint(EndpointKind.OBLIGATION, "ob-t-root"),
                target=NetworkEndpoint(EndpointKind.OBLIGATION, "ob-t-b1"),
            ),
            TypedEdge(
                relation=RelationKind.SUPERSEDES,
                source=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-b2"),
                target=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-b1"),
            ),
            support_edge("ev-1", "o-b1", "set-1"),
        )
    )
    before = snapshot().execution_projection()
    after = net.execution_projection()
    assert before.edges == after.edges
    assert validate_execution_projection(after, DEFAULT_PROJECTION_BUDGET).problems == ()


def test_supervision_view_maps_scope_to_occurrences() -> None:
    net = snapshot(
        typed_edges=(
            TypedEdge(
                relation=RelationKind.SUPERVISION,
                source=NetworkEndpoint(EndpointKind.SUPERVISOR, "mgr-1"),
                target=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-b1"),
            ),
        )
    )
    view = net.supervision_view()
    assert view.by_supervisor["mgr-1"] == (OccurrenceId("o-b1"),)
    assert view.supervisor_of[OccurrenceId("o-b1")] == ("mgr-1",)


# --------------------------------------------------------------------------------------
# 3. Direction: producer → consumer, and the explicit flip for the legacy shape
# --------------------------------------------------------------------------------------


def test_dependency_map_is_the_explicit_flip_of_the_projection() -> None:
    projection = snapshot().execution_projection()
    dependencies = projection.to_dependency_map()
    for edge in projection.edges:
        assert edge.source in dependencies[edge.target]
        assert edge.target not in dependencies.get(edge.source, [])


def test_flip_round_trip_preserves_the_edge_set() -> None:
    projection = snapshot().execution_projection()
    pairs = {(edge.source, edge.target) for edge in projection.edges}
    flipped = flip_to_dependency_map(pairs)
    assert set(flip_from_dependency_map(flipped)) == pairs


def test_legacy_dependency_checker_accepts_the_flipped_projection() -> None:
    projection = snapshot().execution_projection()
    legacy_order = check_dependencies(projection.to_dependency_map())
    position = {key: seat for seat, key in enumerate(legacy_order)}
    for edge in projection.edges:
        assert position[edge.source] < position[edge.target]


# --------------------------------------------------------------------------------------
# 4. Invariance under renaming and shuffling, cross-checked with graphlib
# --------------------------------------------------------------------------------------


def _rename(net: TaskNetworkSnapshot, salt: str) -> tuple[TaskNetworkSnapshot, dict[str, str]]:
    """Rename every occurrence / task / instance id with a reversible mangling."""

    def fresh(value: str) -> str:
        return f"{salt}-{value[::-1]}"

    occ_map = {str(spec.occurrence_id): fresh(str(spec.occurrence_id)) for spec in net.occurrences}
    task_map = {str(spec.task_id): fresh(str(spec.task_id)) for spec in net.occurrences}
    inst_map = {
        str(draft.instance_id): fresh(str(draft.instance_id)) for draft in net.method_instances
    }

    occurrences = tuple(
        replace(
            spec,
            occurrence_id=OccurrenceId(occ_map[str(spec.occurrence_id)]),
            task_id=TaskRef(task_map[str(spec.task_id)]),
        )
        for spec in net.occurrences
    )
    bindings = tuple(
        replace(
            item,
            task_id=TaskRef(task_map[str(item.task_id)]),
            goal_signature=replace(
                item.goal_signature, signature_id=fresh(item.goal_signature.signature_id)
            ),
            adopted_method_instance_id=(
                None
                if item.adopted_method_instance_id is None
                else MethodInstanceId(inst_map[str(item.adopted_method_instance_id)])
            ),
        )
        for item in net.task_bindings
    )
    instances = tuple(
        replace(
            draft,
            instance_id=MethodInstanceId(inst_map[str(draft.instance_id)]),
            goal_id=TaskRef(task_map[str(draft.goal_id)]),
            goal_occurrence_id=OccurrenceId(occ_map[str(draft.effective_goal_occurrence_id)]),
            child_bindings=tuple(
                replace(
                    child,
                    instance_id=MethodInstanceId(inst_map[str(child.instance_id)]),
                    occurrence_id=OccurrenceId(occ_map[str(child.occurrence_id)]),
                    goal_occurrence_id=(
                        None
                        if child.goal_occurrence_id is None
                        else OccurrenceId(occ_map[str(child.goal_occurrence_id)])
                    ),
                )
                for child in draft.child_bindings
            ),
        )
        for draft in net.method_instances
    )
    orders = tuple(
        replace(
            item,
            before=OccurrenceId(occ_map[str(item.before)]),
            after=OccurrenceId(occ_map[str(item.after)]),
        )
        for item in net.order_constraints
    )
    requirements = tuple(
        replace(
            item,
            producer_occurrence=OccurrenceId(occ_map[str(item.producer_occurrence)]),
            consumer_occurrence=OccurrenceId(occ_map[str(item.consumer_occurrence)]),
        )
        for item in net.data_requirements
    )
    coverage = tuple(
        replace(item, covered_by=tuple(OccurrenceId(occ_map[str(o)]) for o in item.covered_by))
        for item in net.obligation_coverage
    )
    renamed = replace(
        net,
        occurrences=occurrences,
        task_bindings=bindings,
        method_instances=instances,
        adopted_instance_ids=tuple(
            MethodInstanceId(inst_map[str(item)]) for item in net.adopted_instance_ids
        ),
        root_occurrence_ids=tuple(
            OccurrenceId(occ_map[str(item)]) for item in net.root_occurrence_ids
        ),
        order_constraints=orders,
        data_requirements=requirements,
        obligation_coverage=coverage,
    )
    return renamed, occ_map


def test_renaming_every_id_does_not_change_the_shape_of_the_projection() -> None:
    original = snapshot().execution_projection()
    renamed, _ = _rename(snapshot(), "z")
    projected = renamed.execution_projection()
    assert len(projected.nodes) == len(original.nodes)
    assert len(projected.edges) == len(original.edges)
    assert sorted(edge.kind for edge in projected.edges) == sorted(
        edge.kind for edge in original.edges
    )


def test_renaming_every_id_does_not_change_the_cycle_verdict() -> None:
    clean = snapshot()
    dirty = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    for net, expect_cycle in ((clean, False), (dirty, True)):
        renamed, _ = _rename(net, "q")
        report = validate_execution_projection(
            renamed.execution_projection(), DEFAULT_PROJECTION_BUDGET
        )
        assert (ProblemKind.CYCLE in kinds(report)) is expect_cycle


def _cycle_occurrences(
    projection: ExecutionProjection, path: Sequence[str]
) -> tuple[OccurrenceId, ...]:
    return tuple(projection.node(node_id).occurrence_id for node_id in path)


def test_renamed_cycle_path_maps_back_to_the_original_cycle() -> None:
    dirty = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    base_projection = dirty.execution_projection()
    base = validate_execution_projection(base_projection, DEFAULT_PROJECTION_BUDGET).of_kind(
        ProblemKind.CYCLE
    )[0]

    renamed, occ_map = _rename(dirty, "w")
    other_projection = renamed.execution_projection()
    other = validate_execution_projection(other_projection, DEFAULT_PROJECTION_BUDGET).of_kind(
        ProblemKind.CYCLE
    )[0]

    expected = {
        OccurrenceId(occ_map[str(item)]) for item in _cycle_occurrences(base_projection, base.path)
    }
    assert set(_cycle_occurrences(other_projection, other.path)) == expected
    assert {
        OccurrenceId(occ_map[str(projection_node)])
        for projection_node in (
            base_projection.node(node_id).occurrence_id for node_id in base.nodes
        )
    } == {other_projection.node(node_id).occurrence_id for node_id in other.nodes}


def _shuffled(net: TaskNetworkSnapshot, seed: int) -> TaskNetworkSnapshot:
    rng = random.Random(seed)

    def mix(items: Iterable[object]) -> tuple[object, ...]:
        listed = list(items)
        rng.shuffle(listed)
        return tuple(listed)

    return replace(
        net,
        occurrences=mix(net.occurrences),  # type: ignore[arg-type]
        task_bindings=mix(net.task_bindings),  # type: ignore[arg-type]
        method_instances=mix(net.method_instances),  # type: ignore[arg-type]
        order_constraints=mix(net.order_constraints),  # type: ignore[arg-type]
        data_requirements=mix(net.data_requirements),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 5, 8])
def test_shuffling_the_input_order_yields_an_identical_projection(seed: int) -> None:
    base = snapshot().execution_projection()
    other = _shuffled(snapshot(), seed).execution_projection()
    assert base.nodes == other.nodes
    assert base.edges == other.edges


@pytest.mark.parametrize("seed", [1, 2, 3, 5, 8])
def test_shuffling_the_input_order_yields_an_identical_report(seed: int) -> None:
    base = validate_execution_projection(
        snapshot().execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    other = validate_execution_projection(
        _shuffled(snapshot(), seed).execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    assert base.problems == other.problems
    assert base.topological_order == other.topological_order


def test_graphlib_agrees_that_the_canonical_projection_is_acyclic() -> None:
    projection = snapshot().execution_projection()
    sorter = graphlib.TopologicalSorter(projection.to_dependency_map())
    reference = list(sorter.static_order())
    assert set(reference) == set(projection.node_ids())
    position = {key: seat for seat, key in enumerate(reference)}
    for edge in projection.edges:
        assert position[edge.source] < position[edge.target]


def test_graphlib_agrees_that_the_cyclic_projection_is_cyclic() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    projection = net.execution_projection()
    with pytest.raises(graphlib.CycleError):
        graphlib.TopologicalSorter(projection.to_dependency_map()).prepare()
    report = validate_execution_projection(projection, DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.CYCLE in kinds(report)


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_our_topological_order_is_valid_for_graphlib_too(seed: int) -> None:
    projection = _shuffled(snapshot(), seed).execution_projection()
    order_ = validate_execution_projection(projection, DEFAULT_PROJECTION_BUDGET).topological_order
    assert order_ is not None
    graphlib.TopologicalSorter(projection.to_dependency_map()).prepare()  # no CycleError
    position = {key: seat for seat, key in enumerate(order_)}
    for edge in projection.edges:
        assert position[edge.source] < position[edge.target]


def test_kahn_refuses_to_invent_a_node_it_was_not_given() -> None:
    with pytest.raises(ContractError, match="not a node of this graph"):
        kahn_order({"a": ["ghost"]}, {"a": 0})


# --------------------------------------------------------------------------------------
# 5. Referential integrity at construction time
# --------------------------------------------------------------------------------------


def test_order_endpoint_outside_the_snapshot_is_refused() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(order_constraints=(order("o-a", "o-ghost"),))


def test_data_endpoint_outside_the_snapshot_is_refused() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(data_requirements=(data("dr-x", "o-ghost", "report", "o-d", "report"),))


def test_child_binding_to_an_unknown_occurrence_is_refused() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(
            method_instances=(
                instance(
                    "mi-root",
                    "t-root",
                    "o-root",
                    (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
                ),
                instance("mi-c", "t-c", "o-c", (("b1", "o-ghost"),)),
            )
        )


def test_occurrence_without_a_semantic_binding_is_refused() -> None:
    with pytest.raises(ContractError, match="t-loose"):
        snapshot(occurrences=snapshot().occurrences + (occurrence("o-loose", "t-loose"),))


def test_adopted_instance_outside_active_membership_is_refused() -> None:
    with pytest.raises(ContractError, match="mi-c"):
        snapshot(adopted_instance_ids=(MethodInstanceId("mi-root"),))


def test_adopted_instance_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ContractError, match="mi-nope"):
        snapshot(
            adopted_instance_ids=(
                MethodInstanceId("mi-root"),
                MethodInstanceId("mi-c"),
                MethodInstanceId("mi-nope"),
            )
        )


def test_coverage_pointing_at_an_unknown_occurrence_is_refused() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(
            obligation_coverage=(
                ObligationCoverage(
                    obligation_id=ObligationId("ob-t-root"),
                    criterion_ids=("c1",),
                    covered_by=(OccurrenceId("o-ghost"),),
                ),
            )
        )


def test_method_instance_refining_an_occurrence_of_another_task_is_refused() -> None:
    with pytest.raises(ContractError, match="not to its goal"):
        snapshot(
            method_instances=(
                instance(
                    "mi-root",
                    "t-root",
                    "o-root",
                    (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
                ),
                instance("mi-c", "t-c", "o-root", (("b1", "o-b1"),)),
            )
        )


def test_method_instance_refining_a_primitive_occurrence_is_refused() -> None:
    with pytest.raises(ContractError, match="primitive"):
        snapshot(
            method_instances=(
                instance(
                    "mi-root",
                    "t-root",
                    "o-root",
                    (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
                ),
                instance("mi-c", "t-c", "o-c", (("b1", "o-b1"),)),
                instance("mi-a", "t-a", "o-a", (("x", "o-b2"),)),
            )
        )


def test_method_instance_refining_an_occurrence_outside_the_network_is_refused() -> None:
    with pytest.raises(ContractError, match="does not contain"):
        snapshot(
            method_instances=(
                instance(
                    "mi-root",
                    "t-root",
                    "o-root",
                    (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
                ),
                instance("mi-c", "t-c", "o-elsewhere", (("b1", "o-b1"),)),
            )
        )


def test_occurrence_obligation_must_agree_with_the_semantic_binding() -> None:
    with pytest.raises(ContractError, match="obligation"):
        snapshot(
            occurrences=(
                occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
                occurrence("o-a", "t-a", obligation="ob-elsewhere"),
                occurrence("o-c", "t-c", form=TaskForm.COMPOUND),
                occurrence("o-d", "t-d"),
                occurrence("o-b1", "t-b1"),
                occurrence("o-b2", "t-b2"),
                occurrence("o-join", "t-join"),
            )
        )


def test_typed_edge_to_an_unknown_occurrence_is_refused() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(
            typed_edges=(
                TypedEdge(
                    relation=RelationKind.SUPERSEDES,
                    source=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-ghost"),
                    target=NetworkEndpoint(EndpointKind.OCCURRENCE, "o-b1"),
                ),
            )
        )


def test_root_occurrence_must_exist() -> None:
    with pytest.raises(ContractError, match="o-ghost"):
        snapshot(root_occurrence_ids=(OccurrenceId("o-ghost"),))


# --------------------------------------------------------------------------------------
# 6. One counterexample per problem kind
# --------------------------------------------------------------------------------------


def test_dangling_projection_endpoint_is_reported() -> None:
    projection = snapshot().execution_projection()
    broken = replace(
        projection,
        edges=projection.edges
        + (
            type(projection.edges[0])(
                source="ghost-node",
                target=projection.edges[0].target,
                kind=ProjectionEdgeKind.ORDER,
                origin="hand-made",
            ),
        ),
    )
    report = validate_execution_projection(broken, DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.DANGLING_ENDPOINT in kinds(report)
    assert "ghost-node" in report.of_kind(ProblemKind.DANGLING_ENDPOINT)[0].detail


def test_missing_refinement_close_edge_is_reported() -> None:
    projection = snapshot().execution_projection()
    exit_ = projection.exit_node_id(OccurrenceId("o-c"))
    b1 = projection.entry_node_id(OccurrenceId("o-b1"))
    broken = replace(
        projection,
        edges=tuple(
            edge for edge in projection.edges if not (edge.source == b1 and edge.target == exit_)
        ),
    )
    report = validate_execution_projection(broken, DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.MISSING_EDGE in kinds(report)


def test_order_constraint_on_an_unprojected_occurrence_is_reported() -> None:
    net = snapshot(
        occurrences=snapshot().occurrences + (occurrence("o-alt", "t-alt"),),
        task_bindings=snapshot().task_bindings + (binding("t-alt"),),
        order_constraints=snapshot().order_constraints + (order("o-alt", "o-d"),),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.MISSING_EDGE in kinds(report)


def test_one_method_filling_two_of_its_own_slots_with_one_occurrence_is_a_duplicate() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance(
                "mi-c",
                "t-c",
                "o-c",
                (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"), ("again", "o-b1")),
            ),
        ),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.DUPLICATE_SLOT in kinds(report)
    assert "o-b1" in report.of_kind(ProblemKind.DUPLICATE_SLOT)[0].detail


def test_single_valued_input_port_bound_twice_is_reported() -> None:
    net = snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding("t-d", inputs=(port("report"),)),
            binding("t-b1", outputs=(port("report"),)),
            binding("t-b2"),
            binding("t-join"),
        ),
        data_requirements=(
            data("dr-1", "o-a", "report", "o-d", "report"),
            data("dr-2", "o-b1", "report", "o-d", "report"),
        ),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.SINGLE_PORT_OVERBOUND in kinds(report)


def _set_port_snapshot(ordering: PortOrdering | None) -> TaskNetworkSnapshot:
    return snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding(
                "t-d",
                inputs=(port("report", cardinality=PortCardinality.SET, ordering=ordering),),
            ),
            binding("t-b1", outputs=(port("report"),)),
            binding("t-b2"),
            binding("t-join"),
        ),
        data_requirements=(
            data("dr-1", "o-a", "report", "o-d", "report"),
            data("dr-2", "o-b1", "report", "o-d", "report"),
        ),
    )


def test_set_port_without_a_declared_order_is_reported() -> None:
    report = validate_execution_projection(
        _set_port_snapshot(None).execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    assert ProblemKind.SET_PORT_UNORDERED in kinds(report)
    assert ProblemKind.SINGLE_PORT_OVERBOUND not in kinds(report)


def test_set_port_with_a_declared_order_is_accepted() -> None:
    report = validate_execution_projection(
        _set_port_snapshot(PortOrdering.BY_PRODUCER_ORDINAL).execution_projection(),
        DEFAULT_PROJECTION_BUDGET,
    )
    assert ProblemKind.SET_PORT_UNORDERED not in kinds(report)


def test_an_unbound_set_port_still_has_to_declare_its_order() -> None:
    """Admission is where the decision is due, not the first time two producers appear."""

    net = snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding("t-d", inputs=(port("report"),)),
            binding(
                "t-b1",
                outputs=(port("trace", cardinality=PortCardinality.SET, required=False),),
            ),
            binding("t-b2"),
            binding("t-join"),
        ),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    problems = report.of_kind(ProblemKind.SET_PORT_UNORDERED)
    assert problems and "trace" in problems[0].detail


def test_required_input_port_with_no_binding_is_unbound() -> None:
    net = snapshot(data_requirements=())
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.UNBOUND_PORT in kinds(report)
    assert "report" in report.of_kind(ProblemKind.UNBOUND_PORT)[0].detail


def test_optional_input_port_with_no_binding_is_fine() -> None:
    net = snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding("t-d", inputs=(port("report", required=False),)),
            binding("t-b1"),
            binding("t-b2"),
            binding("t-join"),
        ),
        data_requirements=(),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.UNBOUND_PORT not in kinds(report)


def test_data_requirement_naming_an_undeclared_output_port_is_unbound() -> None:
    net = snapshot(data_requirements=(data("dr-1", "o-a", "nowhere", "o-d", "report"),))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    problems = report.of_kind(ProblemKind.UNBOUND_PORT)
    assert any("nowhere" in problem.detail for problem in problems)


def test_required_obligation_without_any_occurrence_is_an_orphan() -> None:
    net = snapshot(
        required_obligations=(ObligationId("ob-t-root"), ObligationId("ob-never-planned"))
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    problems = report.of_kind(ProblemKind.ORPHAN_OBLIGATION)
    assert problems and "ob-never-planned" in problems[0].detail
    assert ProblemKind.UNREACHED_REQUIRED_OCCURRENCE not in kinds(report)


def test_required_occurrence_in_no_method_at_all_is_a_different_problem() -> None:
    net = snapshot(
        occurrences=snapshot().occurrences + (occurrence("o-loose", "t-loose"),),
        task_bindings=snapshot().task_bindings + (binding("t-loose"),),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    problems = report.of_kind(ProblemKind.UNREACHED_REQUIRED_OCCURRENCE)
    assert problems and "o-loose" in problems[0].detail
    assert ProblemKind.ORPHAN_OBLIGATION not in kinds(report)


def test_required_occurrence_held_only_by_an_alternative_method_is_not_an_orphan() -> None:
    net = snapshot(
        occurrences=snapshot().occurrences + (occurrence("o-alt", "t-alt"),),
        task_bindings=snapshot().task_bindings + (binding("t-alt"),),
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))),
            instance("mi-c-alt", "t-c", "o-c", (("only", "o-alt"),), method="meth-shortcut"),
        ),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.UNREACHED_REQUIRED_OCCURRENCE not in kinds(report)
    assert ProblemKind.ORPHAN_OBLIGATION not in kinds(report)


def test_root_coverage_gap_names_the_missing_criterion() -> None:
    net = snapshot(
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("o-a"),),
            ),
        )
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    gaps = report.of_kind(ProblemKind.ROOT_COVERAGE_GAP)
    assert gaps and "c2" in gaps[0].detail


def test_full_root_coverage_reports_no_gap() -> None:
    report = validate_execution_projection(
        snapshot().execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    assert ProblemKind.ROOT_COVERAGE_GAP not in kinds(report)


# -- resources ---------------------------------------------------------------------------

WORKSPACE = "ws-main"


def _resourced(
    b1: tuple[Sequence[ResourceRef], Sequence[ResourceRef]],
    b2: tuple[Sequence[ResourceRef], Sequence[ResourceRef]],
    **overrides: object,
) -> TaskNetworkSnapshot:
    return snapshot(
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1", "c2")),
            binding("t-a", outputs=(port("report"),)),
            binding("t-c", form=TaskForm.COMPOUND, adopted="mi-c"),
            binding("t-d", inputs=(port("report"),)),
            binding("t-b1", reads=b1[0], writes=b1[1]),
            binding("t-b2", reads=b2[0], writes=b2[1]),
            binding("t-join"),
        ),
        **overrides,
    )


def test_write_write_conflict_without_an_ordering_is_reported() -> None:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-x")
    net = _resourced(((), (ref,)), ((), (ref,)))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    conflicts = report.of_kind(ProblemKind.RESOURCE_CONFLICT)
    assert len(conflicts) == 1
    assert "obj-x" in conflicts[0].detail


def test_read_write_conflict_without_an_ordering_is_reported() -> None:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-x")
    net = _resourced(((ref,), ()), ((), (ref,)))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.RESOURCE_CONFLICT in kinds(report)


def test_read_read_on_the_same_object_is_not_a_conflict() -> None:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-x")
    net = _resourced(((ref,), ()), ((ref,), ()))
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.RESOURCE_CONFLICT not in kinds(report)


def test_same_object_id_in_another_namespace_is_another_object() -> None:
    net = _resourced(
        ((), (ResourceRef(namespace=WORKSPACE, object_id="obj-x"),)),
        ((), (ResourceRef(namespace="ws-other", object_id="obj-x"),)),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.RESOURCE_CONFLICT not in kinds(report)


def test_write_write_with_an_explicit_order_is_not_a_conflict() -> None:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-x")
    net = _resourced(
        ((), (ref,)),
        ((), (ref,)),
        order_constraints=snapshot().order_constraints + (order("o-b1", "o-b2"),),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert ProblemKind.RESOURCE_CONFLICT not in kinds(report)


def _crowded_resource_snapshot(writers: int) -> TaskNetworkSnapshot:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-hot")
    names = [f"o-w{seat:02d}" for seat in range(writers)]
    return TaskNetworkSnapshot(
        mission_id=MISSION,
        plan_revision=PlanRevision(1),
        occurrences=(occurrence("o-root", "t-root", form=TaskForm.COMPOUND),)
        + tuple(occurrence(name, f"t-w{seat:02d}") for seat, name in enumerate(names)),
        task_bindings=(binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root"),)
        + tuple(binding(f"t-w{seat:02d}", writes=(ref,)) for seat in range(writers)),
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                tuple((f"s{seat:02d}", name) for seat, name in enumerate(names)),
            ),
        ),
        adopted_instance_ids=(MethodInstanceId("mi-root"),),
        root_occurrence_ids=(OccurrenceId("o-root"),),
    )


def test_conflicting_pairs_are_bucketed_by_resource_and_capped_with_a_bound_reached() -> None:
    writers = 9  # 36 unordered pairs, above the per-resource listing cap
    report = validate_execution_projection(
        _crowded_resource_snapshot(writers).execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    listed = report.of_kind(ProblemKind.RESOURCE_CONFLICT)
    assert len(listed) == MAX_CONFLICTS_PER_RESOURCE
    truncation = [
        problem
        for problem in report.of_kind(ProblemKind.BOUND_REACHED)
        if "resource_conflict listing" in problem.detail
    ]
    assert len(truncation) == 1
    assert str(writers * (writers - 1) // 2) in truncation[0].detail
    assert "not absent" in truncation[0].detail


def test_a_small_bucket_is_listed_in_full_with_no_truncation_marker() -> None:
    report = validate_execution_projection(
        _crowded_resource_snapshot(3).execution_projection(), DEFAULT_PROJECTION_BUDGET
    )
    assert len(report.of_kind(ProblemKind.RESOURCE_CONFLICT)) == 3
    assert not [
        problem
        for problem in report.of_kind(ProblemKind.BOUND_REACHED)
        if "resource_conflict listing" in problem.detail
    ]


def test_each_problem_class_is_reported_separately() -> None:
    ref = ResourceRef(namespace=WORKSPACE, object_id="obj-x")
    net = _resourced(
        ((), (ref,)),
        ((), (ref,)),
        data_requirements=(),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("o-a"),),
            ),
        ),
    )
    report = validate_execution_projection(net.execution_projection(), DEFAULT_PROJECTION_BUDGET)
    assert {
        ProblemKind.UNBOUND_PORT,
        ProblemKind.ROOT_COVERAGE_GAP,
        ProblemKind.RESOURCE_CONFLICT,
    } <= kinds(report)
    assert report.ok is False


# --------------------------------------------------------------------------------------
# 7. Bounds: BOUND_REACHED, never a silent truncation
# --------------------------------------------------------------------------------------


def test_node_bound_is_reported_and_nothing_is_truncated() -> None:
    projection = snapshot().execution_projection()
    report = validate_execution_projection(projection, budget(max_nodes=3))
    assert ProblemKind.BOUND_REACHED in kinds(report)
    assert report.topological_order is not None
    assert len(report.topological_order) == len(projection.nodes)
    assert "max_nodes" in report.of_kind(ProblemKind.BOUND_REACHED)[0].detail


def test_edge_bound_is_reported() -> None:
    report = validate_execution_projection(snapshot().execution_projection(), budget(max_edges=2))
    assert any(
        "max_edges" in problem.detail for problem in report.of_kind(ProblemKind.BOUND_REACHED)
    )


def test_depth_bound_is_reported() -> None:
    report = validate_execution_projection(snapshot().execution_projection(), budget(max_depth=2))
    assert any(
        "max_depth" in problem.detail for problem in report.of_kind(ProblemKind.BOUND_REACHED)
    )


def test_fan_out_bound_is_reported() -> None:
    report = validate_execution_projection(snapshot().execution_projection(), budget(max_fan_out=1))
    assert any(
        "max_fan_out" in problem.detail for problem in report.of_kind(ProblemKind.BOUND_REACHED)
    )


def test_report_carries_the_budget_version_it_was_checked_against() -> None:
    report = validate_execution_projection(
        snapshot().execution_projection(), budget(budget_version=7)
    )
    assert report.budget_version == 7


def test_budget_is_frozen_and_rejects_nonsense_bounds() -> None:
    with pytest.raises(ContractError):
        budget(max_nodes=0)


# --------------------------------------------------------------------------------------
# 8. Refinement acyclicity (instantiated relations, one configuration at a time)
# --------------------------------------------------------------------------------------


def test_canonical_refinement_is_acyclic() -> None:
    assert validate_refinement_acyclic(snapshot()).problems == ()


def test_shared_compound_sub_goal_refinement_is_acyclic() -> None:
    assert validate_refinement_acyclic(shared_goal_snapshot()).problems == ()


def test_occurrence_that_is_its_own_ancestor_is_refused() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))),
            instance(
                "mi-loop",
                "t-c",
                "o-c",
                (("self", "o-c", Requiredness.REQUIRED, ReusePolicy.SHARE_ACTIVE),),
                method="meth-loop",
            ),
        ),
    )
    report = validate_refinement_acyclic(net)
    assert ProblemKind.CYCLE in kinds(report)
    assert "o-c" in report.of_kind(ProblemKind.CYCLE)[0].detail


def test_the_cycling_configuration_is_named() -> None:
    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"), ("b2", "o-b2"), ("join", "o-join"))),
            instance(
                "mi-loop",
                "t-c",
                "o-c",
                (("self", "o-c", Requiredness.REQUIRED, ReusePolicy.SHARE_ACTIVE),),
                method="meth-loop",
            ),
        ),
    )
    detail = validate_refinement_acyclic(net).of_kind(ProblemKind.CYCLE)[0].detail
    assert "mi-loop" in detail
    assert "the adopted configuration" not in detail


def test_mutually_exclusive_alternatives_are_never_merged_into_one_graph() -> None:
    """Two alternatives for one occurrence cannot both be in force, so neither can
    borrow an edge from the other to make a cycle."""

    net = snapshot(
        method_instances=(
            instance(
                "mi-root",
                "t-root",
                "o-root",
                (("analyse", "o-a"), ("build", "o-c"), ("deliver", "o-d")),
            ),
            instance("mi-c", "t-c", "o-c", (("b1", "o-b1"),)),
            instance(
                "mi-c-alt",
                "t-c",
                "o-c",
                (("b2", "o-b2"),),
                method="meth-shortcut",
            ),
        ),
        order_constraints=(order("o-a", "o-c"), order("o-c", "o-d")),
    )
    assert validate_refinement_acyclic(net).problems == ()


def test_two_occurrences_refining_each_other_are_refused() -> None:
    net = snapshot(
        occurrences=(
            occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
            occurrence("o-p", "t-p", form=TaskForm.COMPOUND),
            occurrence("o-q", "t-q", form=TaskForm.COMPOUND),
        ),
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1",)),
            binding("t-p", form=TaskForm.COMPOUND),
            binding("t-q", form=TaskForm.COMPOUND),
        ),
        method_instances=(
            instance("mi-root", "t-root", "o-root", (("p", "o-p"),)),
            instance("mi-p", "t-p", "o-p", (("q", "o-q"),)),
            instance(
                "mi-q",
                "t-q",
                "o-q",
                (("p", "o-p", Requiredness.REQUIRED, ReusePolicy.SHARE_ACTIVE),),
            ),
        ),
        adopted_instance_ids=(MethodInstanceId("mi-root"),),
        order_constraints=(),
        data_requirements=(),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("o-p"),),
            ),
        ),
    )
    report = validate_refinement_acyclic(net)
    assert ProblemKind.CYCLE in kinds(report)
    assert len(report.of_kind(ProblemKind.CYCLE)[0].path) >= 3


def test_the_same_method_definition_at_two_occurrences_is_not_recursion_in_this_check() -> None:
    net = snapshot(
        occurrences=(
            occurrence("o-root", "t-root", form=TaskForm.COMPOUND),
            occurrence("o-p", "t-p", form=TaskForm.COMPOUND),
            occurrence("o-q", "t-q"),
        ),
        task_bindings=(
            binding("t-root", form=TaskForm.COMPOUND, adopted="mi-root", criteria=("c1",)),
            binding("t-p", form=TaskForm.COMPOUND, adopted="mi-p"),
            binding("t-q"),
        ),
        method_instances=(
            instance("mi-root", "t-root", "o-root", (("p", "o-p"),), method="meth-recursive"),
            instance("mi-p", "t-p", "o-p", (("q", "o-q"),), method="meth-recursive"),
        ),
        adopted_instance_ids=(MethodInstanceId("mi-root"), MethodInstanceId("mi-p")),
        order_constraints=(),
        data_requirements=(),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("ob-t-root"),
                criterion_ids=("c1",),
                covered_by=(OccurrenceId("o-q"),),
            ),
        ),
    )
    assert validate_refinement_acyclic(net).problems == ()
    assert (
        validate_execution_projection(
            net.execution_projection(), DEFAULT_PROJECTION_BUDGET
        ).problems
        == ()
    )


# --------------------------------------------------------------------------------------
# 9. GraphIntegrityError: diagnose, never a patched-up order
# --------------------------------------------------------------------------------------


def test_require_topological_order_returns_an_order_for_a_dag() -> None:
    projection = snapshot().execution_projection()
    assert set(require_topological_order(projection)) == set(projection.node_ids())


def test_require_topological_order_raises_on_a_cycle_with_the_evidence() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    projection = net.execution_projection()
    with pytest.raises(GraphIntegrityError) as excinfo:
        require_topological_order(projection)
    error = excinfo.value
    assert error.cycle, "the error must carry a concrete cycle"
    assert set(error.cycle) <= set(error.remaining)


def test_diagnose_does_not_hand_out_a_patched_up_topological_order() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    projection = net.execution_projection()
    with pytest.raises(GraphIntegrityError) as excinfo:
        require_topological_order(projection)
    text = excinfo.value.diagnose()
    clean_prefix = projection.entry_node_id(OccurrenceId("o-a"))
    assert clean_prefix not in text, "an acyclic prefix is not evidence, it is a partial order"
    assert "no topological order" in text.lower()
    assert str(len(excinfo.value.remaining)) in text


def test_diagnose_names_the_cycle_members() -> None:
    net = snapshot(order_constraints=(order("o-b1", "o-b2"), order("o-b2", "o-b1")))
    with pytest.raises(GraphIntegrityError) as excinfo:
        require_topological_order(net.execution_projection())
    text = excinfo.value.diagnose()
    for node in excinfo.value.cycle:
        assert node in text


# --------------------------------------------------------------------------------------
# 10. Isolation: pure functions, zero persistence
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["task_network.py", "projection_validation.py"])
def test_new_graph_modules_import_no_persistence_layer(module: str) -> None:
    import agent_orchestrator.graph as graph_package

    source = Path(graph_package.__file__).with_name(module).read_text(encoding="utf-8")
    for banned in ("storage", "scheduling", "artifacts", "sqlite"):
        assert f"import {banned}" not in source
        assert f"from ..{banned}" not in source
        assert f"from .{banned}" not in source


def test_projection_is_a_pure_function_of_the_snapshot() -> None:
    net = snapshot()
    assert net.execution_projection() == net.execution_projection()


def test_execution_projection_type_is_frozen() -> None:
    projection = snapshot().execution_projection()
    assert isinstance(projection, ExecutionProjection)
    with pytest.raises(Exception):
        projection.nodes = ()  # type: ignore[misc]
