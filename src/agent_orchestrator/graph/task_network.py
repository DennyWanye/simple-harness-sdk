# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The immutable TaskNetwork snapshot and its semantic views (TG §3–§6, §24.2).

One network, several readings.  ``TaskNetworkSnapshot`` is the single frozen fact
for one Mission at one ``plan_revision``; the views below are *queries* over it,
never second write authorities:

``refinement_view()``
    Task → MethodInstance → child occurrence.  AND inside one method, OR between
    the methods offered for the same task (TG §4.1, implementation design §3.2).
``execution_projection()``
    the completion-dependency DAG of the **currently adopted** methods.  A
    compound occurrence compiles to an ``entry``/``exit`` pair of logical gates
    (plan §24.1 decision 2): the parent's entry opens each child, each *gating*
    child's exit closes the parent, and an external ORDER ``P before Q`` becomes
    ``P.exit → Q.entry``.  A composition review waits on its *siblings*, not on
    the parent — which is precisely why "parent waits for child" and "child is
    opened by parent" stop being a cycle once they are compiled instead of
    unioned (TG §6).
``support_view()`` / ``supervision_view()``
    the evidence and coordination readings.  Neither ever contributes an edge to
    the execution projection (TG §5: the four graphs are not merged).

Two house rules the types enforce rather than document:

* every public edge points ``producer / predecessor → consumer / successor``.
  The legacy ``key → dependencies[]`` shape of :mod:`.dependency_checker` is the
  opposite direction, so the conversion goes through the explicit
  :func:`flip_to_dependency_map` / :func:`flip_from_dependency_map` pair and
  never through an implicit re-reading of the same dict.
* a gate is a logical boundary, not an agent.  ``ProjectionNode.billable`` is
  ``False`` for gates, they carry no operator and nothing here fabricates an
  Attempt for one.  An adopted method whose children are *all* optional still
  gets an ``entry → exit`` span, so its exit can never be reached before its
  entry; the missing gate is reported rather than papered over.

Everything in this module is a pure function of its inputs: no storage, no
scheduler, no artifact store, no clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..contracts.htn import (
    ChildBinding,
    DataRequirement,
    EndpointKind,
    GraphStructureBudget,
    MethodInstanceDraft,
    MethodInstanceId,
    MissionRef,
    NetworkEndpoint,
    ObligationCoverage,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    RelationKind,
    Requiredness,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    TypedEdge,
    assert_method_instances_match_occurrences,
)
from ..contracts.models import ContractError

#: Requirednesses whose child must be satisfied before the parent gate opens.
#: ``CONDITIONAL`` is "required by *this* method" (§6.1), so it gates too.
GATING_REQUIREDNESS = frozenset({Requiredness.REQUIRED, Requiredness.CONDITIONAL})


# --------------------------------------------------------------------------------------
# Size bounds
# --------------------------------------------------------------------------------------

#: The bound a new-mode Mission is checked against unless the caller supplies one.
#:
#: ``GraphStructureBudget`` (ADR-08) is the versioned budget of record and carries both
#: the planning dimensions and the three projection ones this module reads
#: (``max_nodes`` / ``max_edges`` / ``max_fan_out``, alongside ``max_depth``).  It is not
#: ``contracts.htn.StructureBudget``, which is a mutable node counter for one condition
#: parse, and it does not touch ``graph/task_graph.py``'s legacy ``MAX_TASKS`` /
#: ``MAX_GRAPH_DEPTH``.
DEFAULT_PROJECTION_BUDGET = GraphStructureBudget(
    budget_version=1,
    max_live_tasks=256,
    max_depth=64,
    max_expanded_nodes=4096,
    max_candidates=16,
    max_recursion_fuel=32,
    max_nodes=2048,
    max_edges=8192,
    max_fan_out=256,
)


# --------------------------------------------------------------------------------------
# Relation rows: one flat shape for querying every relation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationRow:
    """One relation, flattened for display and cross-view queries.

    This is a *reading*, not a storable fact.  The storable forms are
    ``ChildBinding`` (refinement), ``OrderConstraint``, ``DataRequirement`` and
    ``contracts.htn.TypedEdge`` (the five relations with no dedicated type) — and
    the contract ``TypedEdge`` deliberately refuses to carry refinement / ORDER /
    DATA, so those three are flattened into this row rather than smuggled into it.
    """

    relation: RelationKind
    source: NetworkEndpoint
    target: NetworkEndpoint
    label: str = ""


# --------------------------------------------------------------------------------------
# The execution projection
# --------------------------------------------------------------------------------------


class ProjectionNodeKind(StrEnum):
    OCCURRENCE = "occurrence"
    COMPOUND_ENTRY = "compound_entry"
    COMPOUND_EXIT = "compound_exit"


class ProjectionEdgeKind(StrEnum):
    REFINEMENT_OPEN = "refinement_open"
    REFINEMENT_CLOSE = "refinement_close"
    COMPOUND_SPAN = "compound_span"
    ORDER = "order"
    DATA = "data"


def _node_id(occurrence: str, suffix: str) -> str:
    """A collision-free node id.

    An ``OccurrenceId`` is free-form printable text, so ``f"{occ}:entry"`` is not
    injective.  The length prefix makes it so, at the cost of a little noise in
    the diagnostics.
    """

    return f"{len(occurrence)}:{occurrence}:{suffix}"


@dataclass(frozen=True, slots=True)
class ProjectionNode:
    node_id: str
    kind: ProjectionNodeKind
    occurrence_id: OccurrenceId
    task_id: TaskRef
    obligation_id: ObligationId
    ordinal: int
    billable: bool

    @property
    def is_gate(self) -> bool:
        return self.kind is not ProjectionNodeKind.OCCURRENCE


@dataclass(frozen=True, slots=True)
class ProjectionEdge:
    source: str
    target: str
    kind: ProjectionEdgeKind
    origin: str


@dataclass(frozen=True, slots=True)
class ExecutionProjection:
    """The completion-dependency DAG of the adopted plan.

    ``snapshot`` is kept so the validator can ask semantic questions (ports,
    coverage, slots, resources) about the same frozen network the projection was
    compiled from; the projection itself never reads anything else.
    """

    snapshot: TaskNetworkSnapshot
    mission_id: MissionRef
    plan_revision: PlanRevision
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]
    projected_occurrences: frozenset[OccurrenceId]
    unprojected_endpoints: tuple[tuple[str, OccurrenceId], ...] = ()
    _by_node_id: Mapping[str, ProjectionNode] = field(
        default_factory=dict, repr=False, compare=False
    )
    _entry_of: Mapping[OccurrenceId, str] = field(default_factory=dict, repr=False, compare=False)
    _exit_of: Mapping[OccurrenceId, str] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_id = {node.node_id: node for node in self.nodes}
        entry: dict[OccurrenceId, str] = {}
        exit_: dict[OccurrenceId, str] = {}
        for node in self.nodes:
            if node.kind is not ProjectionNodeKind.COMPOUND_EXIT:
                entry[node.occurrence_id] = node.node_id
            if node.kind is not ProjectionNodeKind.COMPOUND_ENTRY:
                exit_[node.occurrence_id] = node.node_id
        object.__setattr__(self, "_by_node_id", by_id)
        object.__setattr__(self, "_entry_of", entry)
        object.__setattr__(self, "_exit_of", exit_)

    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes)

    def node(self, node_id: str) -> ProjectionNode:
        return self._by_node_id[node_id]

    def entry_node_id(self, occurrence: OccurrenceId) -> str:
        return self._entry_of[occurrence]

    def exit_node_id(self, occurrence: OccurrenceId) -> str:
        return self._exit_of[occurrence]

    def successors(self) -> Mapping[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            out.setdefault(edge.source, []).append(edge.target)
            out.setdefault(edge.target, [])
        return {key: tuple(value) for key, value in out.items()}

    def predecessors(self) -> Mapping[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            out.setdefault(edge.target, []).append(edge.source)
            out.setdefault(edge.source, [])
        return {key: tuple(value) for key, value in out.items()}

    def to_dependency_map(self) -> dict[str, list[str]]:
        """The legacy ``key → dependencies[]`` shape — an explicit flip, not a re-read."""

        out = flip_to_dependency_map((edge.source, edge.target) for edge in self.edges)
        for node in self.nodes:
            out.setdefault(node.node_id, [])
        return out

    def ordinals(self) -> Mapping[str, int]:
        return {node.node_id: node.ordinal for node in self.nodes}


def flip_to_dependency_map(edges: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """``(predecessor, successor)`` pairs → ``successor → [predecessors]``.

    The public direction is producer → consumer; the legacy checker wants the
    reverse.  Both modules go through this function so neither has to guess which
    way the arrows in a given dict point.
    """

    out: dict[str, list[str]] = {}
    for source, target in edges:
        out.setdefault(source, [])
        out.setdefault(target, [])
        if source not in out[target]:
            out[target].append(source)
    return out


def flip_from_dependency_map(
    dependencies: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, str], ...]:
    """``successor → [predecessors]`` → ``(predecessor, successor)`` pairs."""

    return tuple((source, target) for target in dependencies for source in dependencies[target])


# --------------------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MethodAlternative:
    """One method offered for one task.  AND inside; OR against its siblings."""

    instance_id: MethodInstanceId
    goal_task_id: TaskRef
    goal_occurrence_id: OccurrenceId
    adopted: bool
    slots: Mapping[str, OccurrenceId]
    required_children: tuple[OccurrenceId, ...]
    optional_children: tuple[OccurrenceId, ...]


@dataclass(frozen=True, slots=True)
class RefinementView:
    """``parents_of`` is multi-valued on purpose.

    TG §12 allows a shared sub-goal: one occurrence bound by two adopted slots in
    two different methods.  A single-parent map would have to lose one of those
    two responsibilities, which is exactly what the annex says not to do.
    """

    alternatives: Mapping[TaskRef, tuple[MethodAlternative, ...]]
    parents_of: Mapping[OccurrenceId, tuple[OccurrenceId, ...]]

    def alternatives_for(self, task_id: TaskRef) -> tuple[MethodAlternative, ...]:
        return self.alternatives.get(task_id, ())

    def adopted_for(self, task_id: TaskRef) -> MethodAlternative | None:
        for alternative in self.alternatives_for(task_id):
            if alternative.adopted:
                return alternative
        return None

    def ancestors_of(self, occurrence: OccurrenceId) -> frozenset[OccurrenceId]:
        seen: set[OccurrenceId] = set()
        pending = list(self.parents_of.get(occurrence, ()))
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self.parents_of.get(current, ()))
        return frozenset(seen)


@dataclass(frozen=True, slots=True)
class SupportView:
    """SUPPORT / ASSUMPTION readings.  One set is AND; sets are OR (TG §4.4)."""

    #: keyed by the *subject* the evidence supports.  The contract points a SUPPORT
    #: edge evidence → subject, so the subject is the target, not the source.
    by_subject: Mapping[str, tuple[TypedEdge, ...]]
    by_support_set: Mapping[str, tuple[TypedEdge, ...]]


@dataclass(frozen=True, slots=True)
class SupervisionView:
    by_supervisor: Mapping[str, tuple[OccurrenceId, ...]]
    supervisor_of: Mapping[OccurrenceId, tuple[str, ...]]


# --------------------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskNetworkSnapshot:
    """One Mission's network, frozen at one ``plan_revision`` (TG §3).

    Construction checks referential integrity only — every endpoint names
    something the snapshot contains.  Anything that is a *judgement* about the
    plan (cycles, coverage, ports, conflicts, size) belongs to
    :mod:`.projection_validation`, which reports each class separately instead of
    collapsing them into one refusal.
    """

    mission_id: MissionRef
    plan_revision: PlanRevision
    occurrences: tuple[OccurrenceSpec, ...]
    task_bindings: tuple[TaskSemanticBindingV1, ...]
    method_instances: tuple[MethodInstanceDraft, ...] = ()
    adopted_instance_ids: tuple[MethodInstanceId, ...] = ()
    root_occurrence_ids: tuple[OccurrenceId, ...] = ()
    order_constraints: tuple[OrderConstraint, ...] = ()
    data_requirements: tuple[DataRequirement, ...] = ()
    typed_edges: tuple[TypedEdge, ...] = ()
    obligation_coverage: tuple[ObligationCoverage, ...] = ()
    required_obligations: tuple[ObligationId, ...] = ()
    _by_occurrence: Mapping[OccurrenceId, OccurrenceSpec] = field(
        default_factory=dict, repr=False, compare=False
    )
    _by_task: Mapping[TaskRef, TaskSemanticBindingV1] = field(
        default_factory=dict, repr=False, compare=False
    )
    _by_instance: Mapping[MethodInstanceId, MethodInstanceDraft] = field(
        default_factory=dict, repr=False, compare=False
    )
    _adopted_by_occurrence: Mapping[OccurrenceId, MethodInstanceDraft] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        occurrences: dict[OccurrenceId, OccurrenceSpec] = {}
        for spec in self.occurrences:
            if spec.occurrence_id in occurrences:
                raise ContractError(f"occurrence {spec.occurrence_id!r} is declared twice")
            occurrences[spec.occurrence_id] = spec
        bindings: dict[TaskRef, TaskSemanticBindingV1] = {}
        for item in self.task_bindings:
            if item.task_id in bindings:
                raise ContractError(f"task {item.task_id!r} has two semantic bindings")
            bindings[item.task_id] = item
        instances: dict[MethodInstanceId, MethodInstanceDraft] = {}
        for draft in self.method_instances:
            if draft.instance_id in instances:
                raise ContractError(f"method instance {draft.instance_id!r} is declared twice")
            instances[draft.instance_id] = draft

        for spec in self.occurrences:
            binding = bindings.get(spec.task_id)
            if binding is None:
                raise ContractError(
                    f"occurrence {spec.occurrence_id!r} names task {spec.task_id!r}, "
                    "which has no semantic binding (§18.5: a missing binding is corruption, "
                    "not a legacy fallback)"
                )
            if binding.form is not spec.form:
                raise ContractError(
                    f"occurrence {spec.occurrence_id!r} says form {spec.form!s} but its "
                    f"binding says {binding.form!s}"
                )
            if binding.obligation_id != spec.obligation_id:
                raise ContractError(
                    f"occurrence {spec.occurrence_id!r} claims obligation "
                    f"{spec.obligation_id!r}, its binding claims {binding.obligation_id!r}"
                )

        object.__setattr__(self, "_by_occurrence", occurrences)
        object.__setattr__(self, "_by_task", bindings)
        object.__setattr__(self, "_by_instance", instances)

        adopted = set(self.adopted_instance_ids)
        for instance_id in adopted:
            if instance_id not in instances:
                raise ContractError(f"adopted method instance {instance_id!r} does not exist")
        # TG §12: a refinement is pinned to (goal_id, goal_occurrence_id), so a shared
        # compound sub-goal may sit at several occurrences without either consumer's
        # refinement being read as the other's.
        assert_method_instances_match_occurrences(self.method_instances, self.occurrences)
        adopted_by_occurrence: dict[OccurrenceId, MethodInstanceDraft] = {}
        for draft in self.method_instances:
            if draft.goal_id not in bindings:
                raise ContractError(
                    f"method instance {draft.instance_id!r} refines unknown task {draft.goal_id!r}"
                )
            parent = draft.effective_goal_occurrence_id
            if parent not in occurrences:
                raise ContractError(
                    f"method instance {draft.instance_id!r} refines occurrence {parent!r}, "
                    "which this network does not contain"
                )
            if draft.instance_id in adopted:
                if parent in adopted_by_occurrence:
                    raise ContractError(
                        f"occurrence {parent!r} has two adopted method instances "
                        f"({adopted_by_occurrence[parent].instance_id!r} and "
                        f"{draft.instance_id!r}); alternatives are OR, not AND"
                    )
                adopted_by_occurrence[parent] = draft
            for child in draft.child_bindings:
                if child.occurrence_id not in occurrences:
                    raise ContractError(
                        f"method instance {draft.instance_id!r} binds slot "
                        f"{child.slot_key!r} to unknown occurrence {child.occurrence_id!r}"
                    )
        object.__setattr__(self, "_adopted_by_occurrence", adopted_by_occurrence)

        for item in self.task_bindings:
            claimed = item.adopted_method_instance_id
            if claimed is None:
                continue
            if claimed not in instances:
                raise ContractError(
                    f"task {item.task_id!r} adopts unknown method instance {claimed!r}"
                )
            if claimed not in adopted:
                raise ContractError(
                    f"task {item.task_id!r} adopts method instance {claimed!r}, which is not in "
                    "the active membership of this plan revision"
                )

        for constraint in self.order_constraints:
            for endpoint in (constraint.before, constraint.after):
                if endpoint not in occurrences:
                    raise ContractError(f"order constraint names unknown occurrence {endpoint!r}")
        seen_requirements: set[str] = set()
        for requirement in self.data_requirements:
            for endpoint in (requirement.producer_occurrence, requirement.consumer_occurrence):
                if endpoint not in occurrences:
                    raise ContractError(
                        f"data requirement {requirement.requirement_id!r} names unknown "
                        f"occurrence {endpoint!r}"
                    )
            if requirement.requirement_id in seen_requirements:
                raise ContractError(
                    f"data requirement {requirement.requirement_id!r} is declared twice"
                )
            seen_requirements.add(requirement.requirement_id)

        for root in self.root_occurrence_ids:
            if root not in occurrences:
                raise ContractError(f"root occurrence {root!r} does not exist")
        for coverage in self.obligation_coverage:
            for covered in coverage.covered_by:
                if covered not in occurrences:
                    raise ContractError(f"obligation coverage names unknown occurrence {covered!r}")
        for edge in self.typed_edges:
            for side in (edge.source, edge.target):
                if side.kind is EndpointKind.OCCURRENCE:
                    if OccurrenceId(side.id) not in occurrences:
                        raise ContractError(
                            f"{edge.relation!s} edge names unknown occurrence {side.id!r}"
                        )
                elif side.kind is EndpointKind.TASK:
                    if TaskRef(side.id) not in bindings:
                        raise ContractError(
                            f"{edge.relation!s} edge names unknown task {side.id!r}"
                        )
                elif side.kind is EndpointKind.METHOD_INSTANCE:
                    if MethodInstanceId(side.id) not in instances:
                        raise ContractError(
                            f"{edge.relation!s} edge names unknown method instance {side.id!r}"
                        )

    # -- lookups ------------------------------------------------------------------

    def occurrence(self, occurrence_id: OccurrenceId) -> OccurrenceSpec:
        return self._by_occurrence[occurrence_id]

    def binding_for_task(self, task_id: TaskRef) -> TaskSemanticBindingV1:
        return self._by_task[task_id]

    def binding_for_occurrence(self, occurrence_id: OccurrenceId) -> TaskSemanticBindingV1:
        return self._by_task[self._by_occurrence[occurrence_id].task_id]

    def instance(self, instance_id: MethodInstanceId) -> MethodInstanceDraft:
        return self._by_instance[instance_id]

    def is_adopted(self, instance_id: MethodInstanceId) -> bool:
        return instance_id in set(self.adopted_instance_ids)

    def adopted_instance_for(self, occurrence_id: OccurrenceId) -> MethodInstanceDraft | None:
        """The adopted method of one *occurrence* — sharing makes this per-occurrence."""

        return self._adopted_by_occurrence.get(occurrence_id)

    def adopted_children(self, occurrence_id: OccurrenceId) -> tuple[ChildBinding, ...]:
        """The child bindings the adopted method opens under this occurrence."""

        spec = self._by_occurrence[occurrence_id]
        if spec.form is not TaskForm.COMPOUND:
            return ()
        draft = self._adopted_by_occurrence.get(occurrence_id)
        return () if draft is None else draft.child_bindings

    def relations(self, kind: RelationKind) -> tuple[RelationRow, ...]:
        """Every edge of one relation, derived ones included (§6.5: one meaning each)."""

        if kind is RelationKind.REFINEMENT:
            derived: list[RelationRow] = []
            for draft in sorted(self.method_instances, key=lambda item: str(item.instance_id)):
                derived.append(
                    RelationRow(
                        relation=RelationKind.REFINEMENT,
                        source=NetworkEndpoint(EndpointKind.TASK, str(draft.goal_id)),
                        target=NetworkEndpoint(
                            EndpointKind.METHOD_INSTANCE, str(draft.instance_id)
                        ),
                        label="adopted" if self.is_adopted(draft.instance_id) else "alternative",
                    )
                )
                for child in draft.child_bindings:
                    derived.append(
                        RelationRow(
                            relation=RelationKind.REFINEMENT,
                            source=NetworkEndpoint(
                                EndpointKind.METHOD_INSTANCE, str(draft.instance_id)
                            ),
                            target=NetworkEndpoint(
                                EndpointKind.OCCURRENCE, str(child.occurrence_id)
                            ),
                            label=child.slot_key,
                        )
                    )
            return tuple(derived)
        if kind is RelationKind.ORDER:
            return tuple(
                RelationRow(
                    relation=RelationKind.ORDER,
                    source=NetworkEndpoint(EndpointKind.OCCURRENCE, str(constraint.before)),
                    target=NetworkEndpoint(EndpointKind.OCCURRENCE, str(constraint.after)),
                    label=str(constraint.release_condition),
                )
                for constraint in self.order_constraints
            )
        if kind is RelationKind.DATA:
            return tuple(
                RelationRow(
                    relation=RelationKind.DATA,
                    source=NetworkEndpoint(
                        EndpointKind.OCCURRENCE, str(requirement.producer_occurrence)
                    ),
                    target=NetworkEndpoint(
                        EndpointKind.OCCURRENCE, str(requirement.consumer_occurrence)
                    ),
                    label=requirement.requirement_id,
                )
                for requirement in self.data_requirements
            )
        return tuple(
            RelationRow(
                relation=edge.relation,
                source=edge.source,
                target=edge.target,
                label=edge.label,
            )
            for edge in self.typed_edges
            if edge.relation is kind
        )

    # -- views --------------------------------------------------------------------

    def refinement_view(self) -> RefinementView:
        alternatives: dict[TaskRef, list[MethodAlternative]] = {}
        parents: dict[OccurrenceId, list[OccurrenceId]] = {}
        for draft in sorted(self.method_instances, key=lambda item: str(item.instance_id)):
            slots = {child.slot_key: child.occurrence_id for child in draft.child_bindings}
            required = tuple(
                child.occurrence_id
                for child in draft.child_bindings
                if child.requiredness in GATING_REQUIREDNESS
            )
            optional = tuple(
                child.occurrence_id
                for child in draft.child_bindings
                if child.requiredness not in GATING_REQUIREDNESS
            )
            adopted = self.is_adopted(draft.instance_id)
            parent = draft.effective_goal_occurrence_id
            alternatives.setdefault(draft.goal_id, []).append(
                MethodAlternative(
                    instance_id=draft.instance_id,
                    goal_task_id=draft.goal_id,
                    goal_occurrence_id=parent,
                    adopted=adopted,
                    slots=slots,
                    required_children=required,
                    optional_children=optional,
                )
            )
            if not adopted:
                continue
            for child in draft.child_bindings:
                parents.setdefault(child.occurrence_id, []).append(parent)
        return RefinementView(
            alternatives={key: tuple(value) for key, value in alternatives.items()},
            parents_of={key: tuple(value) for key, value in parents.items()},
        )

    def support_view(self) -> SupportView:
        by_subject: dict[str, list[TypedEdge]] = {}
        by_set: dict[str, list[TypedEdge]] = {}
        for edge in self.typed_edges:
            if edge.relation not in (RelationKind.SUPPORT, RelationKind.ASSUMPTION):
                continue
            by_subject.setdefault(edge.target.id, []).append(edge)
            by_set.setdefault(edge.label, []).append(edge)
        return SupportView(
            by_subject={key: tuple(value) for key, value in by_subject.items()},
            by_support_set={key: tuple(value) for key, value in by_set.items()},
        )

    def supervision_view(self) -> SupervisionView:
        by_supervisor: dict[str, list[OccurrenceId]] = {}
        supervisor_of: dict[OccurrenceId, list[str]] = {}
        for edge in self.typed_edges:
            if edge.relation is not RelationKind.SUPERVISION:
                continue
            subject = OccurrenceId(edge.target.id)
            by_supervisor.setdefault(edge.source.id, []).append(subject)
            supervisor_of.setdefault(subject, []).append(edge.source.id)
        return SupervisionView(
            by_supervisor={key: tuple(value) for key, value in by_supervisor.items()},
            supervisor_of={key: tuple(value) for key, value in supervisor_of.items()},
        )

    # -- the execution projection -------------------------------------------------

    def execution_projection(self) -> ExecutionProjection:
        """Compile the adopted plan into a completion-dependency DAG (TG §6, §5.1)."""

        reachable = self._reachable_occurrences()
        nodes = self._compile_nodes(reachable)
        entry = {
            node.occurrence_id: node.node_id
            for node in nodes
            if node.kind is not ProjectionNodeKind.COMPOUND_EXIT
        }
        exit_ = {
            node.occurrence_id: node.node_id
            for node in nodes
            if node.kind is not ProjectionNodeKind.COMPOUND_ENTRY
        }
        edges: list[ProjectionEdge] = []
        unprojected: list[tuple[str, OccurrenceId]] = []

        for occurrence_id in sorted(reachable, key=str):
            spec = self._by_occurrence[occurrence_id]
            if spec.form is not TaskForm.COMPOUND:
                continue
            children = self.adopted_children(occurrence_id)
            gating = [child for child in children if child.requiredness in GATING_REQUIREDNESS]
            if not gating:
                # No child closes this boundary — either the method is unexpanded, or every
                # slot is optional.  Without this span the exit could be scheduled before
                # the entry, which would make every ORDER through this compound vacuous.
                # The second case is also reported as NO_GATING_CHILDREN; the span keeps the
                # graph honest, the report keeps the plan from passing unnoticed.
                edges.append(
                    ProjectionEdge(
                        source=entry[occurrence_id],
                        target=exit_[occurrence_id],
                        kind=ProjectionEdgeKind.COMPOUND_SPAN,
                        origin=(
                            f"span:{occurrence_id}:"
                            + ("no_gating_children" if children else "unexpanded")
                        ),
                    )
                )
            for child in children:
                edges.append(
                    ProjectionEdge(
                        source=entry[occurrence_id],
                        target=entry[child.occurrence_id],
                        kind=ProjectionEdgeKind.REFINEMENT_OPEN,
                        origin=f"slot:{child.slot_key}",
                    )
                )
                if child.requiredness in GATING_REQUIREDNESS:
                    edges.append(
                        ProjectionEdge(
                            source=exit_[child.occurrence_id],
                            target=exit_[occurrence_id],
                            kind=ProjectionEdgeKind.REFINEMENT_CLOSE,
                            origin=f"slot:{child.slot_key}",
                        )
                    )

        for constraint in self.order_constraints:
            if constraint.before not in reachable or constraint.after not in reachable:
                for endpoint in (constraint.before, constraint.after):
                    if endpoint not in reachable:
                        unprojected.append(
                            (f"order:{constraint.before}->{constraint.after}", endpoint)
                        )
                continue
            edges.append(
                ProjectionEdge(
                    source=exit_[constraint.before],
                    target=entry[constraint.after],
                    kind=ProjectionEdgeKind.ORDER,
                    origin=(
                        f"order:{constraint.before}->{constraint.after}"
                        f":{constraint.release_condition!s}"
                    ),
                )
            )

        for requirement in self.data_requirements:
            producer = requirement.producer_occurrence
            consumer = requirement.consumer_occurrence
            if producer not in reachable or consumer not in reachable:
                for endpoint in (producer, consumer):
                    if endpoint not in reachable:
                        unprojected.append((f"data:{requirement.requirement_id}", endpoint))
                continue
            edges.append(
                ProjectionEdge(
                    source=exit_[producer],
                    target=entry[consumer],
                    kind=ProjectionEdgeKind.DATA,
                    origin=f"data:{requirement.requirement_id}",
                )
            )

        ordered_edges = tuple(
            sorted(edges, key=lambda edge: (edge.source, edge.target, str(edge.kind), edge.origin))
        )
        return ExecutionProjection(
            snapshot=self,
            mission_id=self.mission_id,
            plan_revision=self.plan_revision,
            nodes=nodes,
            edges=ordered_edges,
            projected_occurrences=frozenset(reachable),
            unprojected_endpoints=tuple(sorted(unprojected)),
        )

    def _reachable_occurrences(self) -> set[OccurrenceId]:
        """Roots plus everything the *adopted* methods open beneath them."""

        seen: set[OccurrenceId] = set()
        pending = list(self.root_occurrence_ids)
        while pending:
            occurrence_id = pending.pop()
            if occurrence_id in seen or occurrence_id not in self._by_occurrence:
                continue
            seen.add(occurrence_id)
            for child in self.adopted_children(occurrence_id):
                pending.append(child.occurrence_id)
        return seen

    def _compile_nodes(self, reachable: set[OccurrenceId]) -> tuple[ProjectionNode, ...]:
        raw: list[tuple[str, ProjectionNodeKind, OccurrenceSpec]] = []
        for occurrence_id in reachable:
            spec = self._by_occurrence[occurrence_id]
            if spec.form is TaskForm.COMPOUND:
                raw.append(
                    (_node_id(str(occurrence_id), "entry"), ProjectionNodeKind.COMPOUND_ENTRY, spec)
                )
                raw.append(
                    (_node_id(str(occurrence_id), "exit"), ProjectionNodeKind.COMPOUND_EXIT, spec)
                )
            else:
                raw.append(
                    (_node_id(str(occurrence_id), "node"), ProjectionNodeKind.OCCURRENCE, spec)
                )
        raw.sort(key=lambda item: item[0])
        return tuple(
            ProjectionNode(
                node_id=node_id,
                kind=kind,
                occurrence_id=spec.occurrence_id,
                task_id=spec.task_id,
                obligation_id=spec.obligation_id,
                ordinal=ordinal,
                billable=kind is ProjectionNodeKind.OCCURRENCE,
            )
            for ordinal, (node_id, kind, spec) in enumerate(raw)
        )


__all__ = (
    "DEFAULT_PROJECTION_BUDGET",
    "GATING_REQUIREDNESS",
    "ExecutionProjection",
    "MethodAlternative",
    "ProjectionEdge",
    "ProjectionEdgeKind",
    "ProjectionNode",
    "ProjectionNodeKind",
    "RefinementView",
    "RelationRow",
    "SupervisionView",
    "SupportView",
    "TaskNetworkSnapshot",
    "flip_from_dependency_map",
    "flip_to_dependency_map",
)
