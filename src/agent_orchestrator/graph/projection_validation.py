# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Structural checks on a compiled execution projection (TG §14.2–14.3, §24.1/10–11).

Only the *execution projection* is checked for cycles.  The other readings of the
same network answer different questions and are deliberately not merged into one
``DiGraph`` for a single ``is_acyclic`` call (TG §5):

======================================  ==================================================
method definition graph                 may recurse; bounded expansion is checked elsewhere
instantiated refinement relations       :func:`validate_refinement_acyclic` — no occurrence
                                        may be its own ancestor, checked per *configuration*
adopted execution projection            :func:`validate_execution_projection` — must be a DAG
evidence support graph                  anchor-free SCCs, checked by the knowledge layer
supersedes / event history              causal order, never execution order
live resource wait-for graph            deadlock analysis, not a Task DAG property
======================================  ==================================================

Each class of problem is reported on its own: a caller that wants to know whether
the ports are bound must not have to infer it from a single boolean that also
covered cycles and coverage.  Size limits are reported as ``BOUND_REACHED`` — the
projection is never silently truncated to fit, and a report that had to stop
listing says so with a ``BOUND_REACHED`` of its own.

The topological sort is a heap-based Kahn over an explicit ``nodes + adjacency +
indegree``.  Ties break on the immutable node ordinal, which exists for
determinism and carries no business meaning.  ``graphlib.TopologicalSorter`` is a
fine cross-check on a frozen snapshot and is used as one in the tests; it is not
used here, because its ``prepare`` closes the graph and it silently invents nodes
it has not been given.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..contracts.htn import (
    GraphStructureBudget,
    MethodInstanceId,
    OccurrenceId,
    PortCardinality,
    Requiredness,
    TaskForm,
    resource_conflicts,
    undeclared_set_ports,
)
from ..contracts.models import ContractError
from .task_network import (
    GATING_REQUIREDNESS,
    ExecutionProjection,
    TaskNetworkSnapshot,
)

#: How many conflicting pairs are listed per resource before the report says it stopped.
MAX_CONFLICTS_PER_RESOURCE = 32


class ProblemKind(StrEnum):
    """One value per class of structural defect (§24.1 decision 10)."""

    CYCLE = "cycle"
    DANGLING_ENDPOINT = "dangling_endpoint"
    MISSING_EDGE = "missing_edge"
    NO_GATING_CHILDREN = "no_gating_children"
    DUPLICATE_SLOT = "duplicate_slot"
    UNBOUND_PORT = "unbound_port"
    SINGLE_PORT_OVERBOUND = "single_port_overbound"
    SET_PORT_UNORDERED = "set_port_unordered"
    ORPHAN_OBLIGATION = "orphan_obligation"
    UNREACHED_REQUIRED_OCCURRENCE = "unreached_required_occurrence"
    ROOT_COVERAGE_GAP = "root_coverage_gap"
    RESOURCE_CONFLICT = "resource_conflict"
    BOUND_REACHED = "bound_reached"
    PARTIAL_CHECK = "partial_check"


@dataclass(frozen=True, slots=True)
class ProjectionProblem:
    kind: ProblemKind
    detail: str
    nodes: tuple[str, ...] = ()
    path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectionReport:
    """What the checks found.  ``ok`` is a convenience, never the whole answer."""

    budget_version: int
    problems: tuple[ProjectionProblem, ...]
    topological_order: tuple[str, ...] | None

    @property
    def ok(self) -> bool:
        return not self.problems

    def of_kind(self, kind: ProblemKind) -> tuple[ProjectionProblem, ...]:
        return tuple(problem for problem in self.problems if problem.kind is kind)

    @property
    def kinds(self) -> frozenset[ProblemKind]:
        return frozenset(problem.kind for problem in self.problems)


class GraphIntegrityError(RuntimeError):
    """The projection could not be ordered (TG §14.3).

    Raised on the execution / materialisation path, which then stops dispatching
    in the affected scope.  The error carries the nodes that could not be ordered
    and one concrete cycle as evidence — and deliberately **no** topological
    order: a partial order over the healthy prefix is not a plan, and handing one
    out invites someone to execute it.
    """

    def __init__(self, remaining: Sequence[str], cycle: Sequence[str]) -> None:
        self.remaining = tuple(remaining)
        self.cycle = tuple(cycle)
        super().__init__(
            f"{len(self.remaining)} node(s) could not be ordered; "
            f"cycle: {' -> '.join(self.cycle) if self.cycle else 'unknown'}"
        )

    def diagnose(self) -> str:
        """A description a person can act on.  Never an order a machine can run."""

        cycle = " -> ".join(self.cycle) if self.cycle else "not isolated"
        unordered = ", ".join(sorted(self.remaining))
        return (
            f"graph integrity failure: {len(self.remaining)} node(s) remain unordered "
            f"[{unordered}]; one concrete cycle: {cycle}. "
            "No topological order is reported for a damaged projection: the healthy "
            "prefix is a partial order, not a plan."
        )


# --------------------------------------------------------------------------------------
# Topological machinery
# --------------------------------------------------------------------------------------


def kahn_order(
    successors: Mapping[str, Sequence[str]],
    ordinals: Mapping[str, int],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Heap-based Kahn.  Returns ``(placed, remaining)``.

    ``remaining`` is empty exactly on a DAG, in which case ``placed`` is a full
    topological order.  Otherwise ``placed`` is the healthy prefix: callers may
    use it to keep checking the acyclic part, but it is **not** an order to
    execute, and no public result ever presents it as one.

    Cost is ``O(E + V log V)``.  The heap key is ``(ordinal, node)`` so the result
    is a deterministic *choice* of linearisation, not a claim that the business
    wants this order.
    """

    indegree = {key: 0 for key in successors}
    for key in successors:
        for target in successors[key]:
            if target not in indegree:
                raise ContractError(
                    f"adjacency names {target!r}, which is not a node of this graph; "
                    "a topological sort does not invent the nodes it was not given"
                )
            indegree[target] += 1
    ready: list[tuple[int, str]] = [
        (ordinals.get(key, 0), key) for key in successors if indegree[key] == 0
    ]
    heapq.heapify(ready)
    placed: list[str] = []
    while ready:
        _, current = heapq.heappop(ready)
        placed.append(current)
        for target in successors[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, (ordinals.get(target, 0), target))
    if len(placed) == len(successors):
        return tuple(placed), ()
    seated = set(placed)
    remaining = tuple(sorted(key for key in successors if key not in seated))
    return tuple(placed), remaining


def find_cycle(
    successors: Mapping[str, Sequence[str]],
    remaining: Sequence[str],
) -> tuple[str, ...]:
    """One concrete closed walk among the nodes Kahn could not place.

    The walk runs *backwards*.  Every node Kahn left behind still has an unplaced
    predecessor, so a backward walk can never dead-end, while a forward one can
    wander off into the blocked-but-acyclic tail below the cycle and report a
    path that closes nothing.
    """

    if not remaining:
        return ()
    pool = set(remaining)
    predecessors: dict[str, list[str]] = {key: [] for key in pool}
    for key in successors:
        if key not in pool:
            continue
        for target in successors[key]:
            if target in pool:
                predecessors[target].append(key)

    start = min(pool)
    path = [start]
    seen = {start}
    current = start
    while True:
        nxt = next(iter(sorted(predecessors[current])), None)
        if nxt is None:  # pragma: no cover - Kahn guarantees a predecessor
            return ()
        if nxt in seen:
            seat = path.index(nxt)
            return (nxt,) + tuple(reversed(path[seat:]))
        path.append(nxt)
        seen.add(nxt)
        current = nxt


def require_topological_order(projection: ExecutionProjection) -> tuple[str, ...]:
    """The execution-path entry point: an order, or :class:`GraphIntegrityError`."""

    successors = _declared_successors(projection)
    placed, remaining = kahn_order(successors, projection.ordinals())
    if remaining:
        raise GraphIntegrityError(remaining, find_cycle(successors, remaining))
    return placed


def _declared_successors(projection: ExecutionProjection) -> dict[str, list[str]]:
    """Adjacency over declared nodes only; a dangling edge is reported, not followed."""

    known = set(projection.node_ids())
    out: dict[str, list[str]] = {node_id: [] for node_id in known}
    for edge in projection.edges:
        if edge.source in known and edge.target in known:
            out[edge.source].append(edge.target)
    return out


def _induced(successors: Mapping[str, Sequence[str]], keep: Sequence[str]) -> dict[str, list[str]]:
    """The sub-graph over ``keep`` — used to keep checking the acyclic part."""

    pool = set(keep)
    return {
        key: [target for target in successors[key] if target in pool]
        for key in successors
        if key in pool
    }


def _longest_path_depth(successors: Mapping[str, Sequence[str]], order: Sequence[str]) -> int:
    """Depth in nodes along the longest chain of the DAG."""

    depth = {key: 1 for key in successors}
    for key in order:
        for target in successors[key]:
            depth[target] = max(depth[target], depth[key] + 1)
    return max(depth.values(), default=0)


def _reachable_from(successors: Mapping[str, Sequence[str]], start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for target in successors.get(current, ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


# --------------------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------------------


def validate_execution_projection(
    projection: ExecutionProjection,
    budget: GraphStructureBudget,
) -> ProjectionReport:
    """Check one compiled projection and report every class of defect separately."""

    problems: list[ProjectionProblem] = []
    snapshot = projection.snapshot
    known = set(projection.node_ids())

    problems.extend(_check_bounds(projection, budget))
    problems.extend(_check_dangling(projection, known))

    successors = _declared_successors(projection)
    placed, remaining = kahn_order(successors, projection.ordinals())
    topological: tuple[str, ...] | None
    if remaining:
        topological = None
        cycle = find_cycle(successors, remaining)
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.CYCLE,
                detail=(
                    f"the execution projection is not a DAG: {' -> '.join(cycle)} "
                    f"({len(remaining)} node(s) unordered)"
                ),
                nodes=tuple(remaining),
                path=cycle,
            )
        )
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.PARTIAL_CHECK,
                detail=(
                    f"depth and resource-ordering were checked on the acyclic part only "
                    f"({len(placed)} of {len(projection.nodes)} nodes); the {len(remaining)} "
                    "node(s) inside or below the cycle were not"
                ),
                nodes=tuple(remaining),
            )
        )
    else:
        topological = placed

    healthy = _induced(successors, placed)
    problems.extend(_check_depth(healthy, placed, budget))
    problems.extend(_check_refinement_wiring(projection))
    problems.extend(_check_duplicate_slots(snapshot))
    problems.extend(_check_ports(snapshot, projection))
    problems.extend(_check_orphans(snapshot, projection))
    problems.extend(_check_root_coverage(snapshot, projection))
    problems.extend(_check_resource_conflicts(snapshot, projection, healthy))

    return ProjectionReport(
        budget_version=budget.budget_version,
        problems=_sorted(problems),
        topological_order=topological,
    )


def validate_refinement_acyclic(snapshot: TaskNetworkSnapshot) -> ProjectionReport:
    """No occurrence may be its own ancestor through instantiated refinement.

    Alternatives for the *same goal occurrence* are mutually exclusive, so putting
    them all in one graph invents cycles that no plan could ever have.  The check
    runs per **configuration**: one method instance per goal occurrence.  The base
    configuration takes the adopted instance for every occurrence that has one and
    the first candidate for every occurrence that does not; each further
    configuration varies exactly one occurrence.  That covers the adopted plan and
    every single alternative substitution without enumerating the cross product.

    The unit is the occurrence, not the task: TG §12 lets one shared compound goal
    sit at several occurrences, and two of those are not alternatives to each other.

    Recursion in a method *definition* is legitimate and is not checked here — a
    definition is not an expansion (TG §5, implementation design §3.3).
    """

    problems: list[ProjectionProblem] = []
    for label, chosen in _configurations(snapshot):
        successors: dict[str, list[str]] = {
            str(spec.occurrence_id): [] for spec in snapshot.occurrences
        }
        for instance_id in chosen:
            draft = snapshot.instance(instance_id)
            parent = str(draft.effective_goal_occurrence_id)
            if parent not in successors:
                continue
            for child in draft.child_bindings:
                successors[parent].append(str(child.occurrence_id))
        ordinals = {key: seat for seat, key in enumerate(sorted(successors))}
        _, remaining = kahn_order(successors, ordinals)
        if not remaining:
            continue
        cycle = find_cycle(successors, remaining)
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.CYCLE,
                detail=(
                    f"instantiated refinement makes an occurrence its own ancestor under "
                    f"{label}: {' -> '.join(cycle)}"
                ),
                nodes=tuple(remaining),
                path=cycle,
            )
        )
    return ProjectionReport(budget_version=1, problems=_sorted(problems), topological_order=None)


def _configurations(
    snapshot: TaskNetworkSnapshot,
) -> tuple[tuple[str, tuple[MethodInstanceId, ...]], ...]:
    """One method instance per goal occurrence: the base choice, then one swap at a time."""

    candidates: dict[OccurrenceId, list[MethodInstanceId]] = {}
    for draft in sorted(snapshot.method_instances, key=lambda item: str(item.instance_id)):
        candidates.setdefault(draft.effective_goal_occurrence_id, []).append(draft.instance_id)
    base: dict[OccurrenceId, MethodInstanceId] = {}
    for occurrence_id, offered in candidates.items():
        adopted = [item for item in offered if snapshot.is_adopted(item)]
        base[occurrence_id] = adopted[0] if adopted else offered[0]
    if not base:
        return ()
    configurations = [("the adopted configuration", tuple(sorted(base.values(), key=str)))]
    for occurrence_id, offered in candidates.items():
        for alternative in offered:
            if alternative == base[occurrence_id]:
                continue
            swapped = dict(base)
            swapped[occurrence_id] = alternative
            configurations.append(
                (
                    f"the configuration that refines {occurrence_id!r} with {alternative!r}",
                    tuple(sorted(swapped.values(), key=str)),
                )
            )
    return tuple(configurations)


def _sorted(problems: Iterable[ProjectionProblem]) -> tuple[ProjectionProblem, ...]:
    return tuple(sorted(problems, key=lambda item: (str(item.kind), item.detail, item.nodes)))


def _check_bounds(
    projection: ExecutionProjection, bounds: GraphStructureBudget
) -> list[ProjectionProblem]:
    problems: list[ProjectionProblem] = []
    version = bounds.budget_version
    if len(projection.nodes) > bounds.max_nodes:
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.BOUND_REACHED,
                detail=(
                    f"max_nodes: the projection has {len(projection.nodes)} nodes, the "
                    f"v{version} budget allows {bounds.max_nodes}"
                ),
            )
        )
    if len(projection.edges) > bounds.max_edges:
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.BOUND_REACHED,
                detail=(
                    f"max_edges: the projection has {len(projection.edges)} edges, the "
                    f"v{version} budget allows {bounds.max_edges}"
                ),
            )
        )
    fan_out: dict[str, int] = {}
    for edge in projection.edges:
        fan_out[edge.source] = fan_out.get(edge.source, 0) + 1
    widest = max(fan_out.values(), default=0)
    limit = bounds.max_fan_out
    if widest > limit:
        offenders = sorted(key for key, count in fan_out.items() if count > limit)
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.BOUND_REACHED,
                detail=(
                    f"max_fan_out: a node opens {widest} successors, the v{version} budget "
                    f"allows {limit}"
                ),
                nodes=tuple(offenders),
            )
        )
    return problems


def _check_depth(
    successors: Mapping[str, Sequence[str]],
    order: Sequence[str],
    bounds: GraphStructureBudget,
) -> list[ProjectionProblem]:
    depth = _longest_path_depth(successors, order)
    limit = bounds.max_depth
    if depth <= limit:
        return []
    return [
        ProjectionProblem(
            kind=ProblemKind.BOUND_REACHED,
            detail=(
                f"max_depth: the longest chain is {depth} nodes, the "
                f"v{bounds.budget_version} budget allows {limit}"
            ),
        )
    ]


def _check_dangling(projection: ExecutionProjection, known: set[str]) -> list[ProjectionProblem]:
    problems: list[ProjectionProblem] = []
    for edge in projection.edges:
        for endpoint in (edge.source, edge.target):
            if endpoint in known:
                continue
            problems.append(
                ProjectionProblem(
                    kind=ProblemKind.DANGLING_ENDPOINT,
                    detail=(
                        f"edge {edge.origin!r} names {endpoint!r}, which is not a node of "
                        "this projection"
                    ),
                    nodes=(endpoint,),
                )
            )
    return problems


def _check_refinement_wiring(projection: ExecutionProjection) -> list[ProjectionProblem]:
    """Every adopted child must be opened by its parent's entry and close its exit."""

    snapshot = projection.snapshot
    pairs = {(edge.source, edge.target) for edge in projection.edges}
    problems: list[ProjectionProblem] = []
    for occurrence_id in sorted(projection.projected_occurrences, key=str):
        spec = snapshot.occurrence(occurrence_id)
        if spec.form is not TaskForm.COMPOUND:
            continue
        children = snapshot.adopted_children(occurrence_id)
        if not children:
            continue
        entry = projection.entry_node_id(occurrence_id)
        exit_ = projection.exit_node_id(occurrence_id)
        if not any(child.requiredness in GATING_REQUIREDNESS for child in children):
            problems.append(
                ProjectionProblem(
                    kind=ProblemKind.NO_GATING_CHILDREN,
                    detail=(
                        f"the adopted method of {occurrence_id!r} has {len(children)} slot(s), "
                        "all optional: nothing the method produces has to be accepted before "
                        "its exit opens. The compiler must mark a required slot or say "
                        "explicitly that this boundary closes on entry"
                    ),
                    nodes=(entry, exit_),
                )
            )
        for child in children:
            if child.occurrence_id not in projection.projected_occurrences:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.MISSING_EDGE,
                        detail=(
                            f"slot {child.slot_key!r} under {occurrence_id!r} binds "
                            f"{child.occurrence_id!r}, which the projection does not contain"
                        ),
                    )
                )
                continue
            child_entry = projection.entry_node_id(child.occurrence_id)
            child_exit = projection.exit_node_id(child.occurrence_id)
            if (entry, child_entry) not in pairs:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.MISSING_EDGE,
                        detail=(
                            f"the entry gate of {occurrence_id!r} does not open slot "
                            f"{child.slot_key!r} ({child.occurrence_id!r})"
                        ),
                        nodes=(entry, child_entry),
                    )
                )
            if child.requiredness in GATING_REQUIREDNESS and (child_exit, exit_) not in pairs:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.MISSING_EDGE,
                        detail=(
                            f"required slot {child.slot_key!r} ({child.occurrence_id!r}) does "
                            f"not gate the exit of {occurrence_id!r}"
                        ),
                        nodes=(child_exit, exit_),
                    )
                )
    for origin, endpoint in projection.unprojected_endpoints:
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.MISSING_EDGE,
                detail=(
                    f"{origin} names occurrence {endpoint!r}, which the adopted plan does "
                    "not project"
                ),
            )
        )
    return problems


def _check_duplicate_slots(snapshot: TaskNetworkSnapshot) -> list[ProjectionProblem]:
    """One method may not fill two of its own positions with the same occurrence.

    Two *different* adopted methods binding the same occurrence is a shared
    sub-goal (TG §12) and is legitimate — the responsibility is held twice on
    purpose, and erasing one of the two holders is the bug the annex warns about.
    """

    problems: list[ProjectionProblem] = []
    for draft in sorted(snapshot.method_instances, key=lambda item: str(item.instance_id)):
        if not snapshot.is_adopted(draft.instance_id):
            continue
        slots_by_occurrence: dict[OccurrenceId, list[str]] = {}
        for child in draft.child_bindings:
            slots_by_occurrence.setdefault(child.occurrence_id, []).append(child.slot_key)
        for occurrence_id in sorted(slots_by_occurrence, key=str):
            slots = sorted(slots_by_occurrence[occurrence_id])
            if len(slots) > 1:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.DUPLICATE_SLOT,
                        detail=(
                            f"method instance {draft.instance_id!r} fills slots "
                            f"{', '.join(slots)} with the same occurrence {occurrence_id!r}; "
                            "one method's positions are distinct work, not one handle reused"
                        ),
                    )
                )
    return problems


def _check_ports(
    snapshot: TaskNetworkSnapshot, projection: ExecutionProjection
) -> list[ProjectionProblem]:
    problems: list[ProjectionProblem] = []
    projected = projection.projected_occurrences
    bound: dict[tuple[OccurrenceId, str], list[str]] = {}
    for requirement in snapshot.data_requirements:
        bound.setdefault((requirement.consumer_occurrence, requirement.input_port), []).append(
            requirement.requirement_id
        )

    for requirement in snapshot.data_requirements:
        producer = requirement.producer_occurrence
        consumer = requirement.consumer_occurrence
        if producer in projected:
            outputs = {
                spec.port_key for spec in snapshot.binding_for_occurrence(producer).output_ports
            }
            if requirement.output_port not in outputs:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.UNBOUND_PORT,
                        detail=(
                            f"data requirement {requirement.requirement_id!r} reads output port "
                            f"{requirement.output_port!r}, which {producer!r} does not declare"
                        ),
                    )
                )
        if consumer in projected:
            inputs = {
                spec.port_key for spec in snapshot.binding_for_occurrence(consumer).input_ports
            }
            if requirement.input_port not in inputs:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.UNBOUND_PORT,
                        detail=(
                            f"data requirement {requirement.requirement_id!r} writes input port "
                            f"{requirement.input_port!r}, which {consumer!r} does not declare"
                        ),
                    )
                )

    for occurrence_id in sorted(projected, key=str):
        binding = snapshot.binding_for_occurrence(occurrence_id)
        for spec in binding.input_ports:
            requirements = sorted(bound.get((occurrence_id, spec.port_key), []))
            if not requirements:
                if spec.required:
                    problems.append(
                        ProjectionProblem(
                            kind=ProblemKind.UNBOUND_PORT,
                            detail=(
                                f"required input port {spec.port_key!r} of {occurrence_id!r} has "
                                "no data requirement"
                            ),
                        )
                    )
                continue
            if spec.cardinality is PortCardinality.SINGLE and len(requirements) > 1:
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.SINGLE_PORT_OVERBOUND,
                        detail=(
                            f"single-valued input port {spec.port_key!r} of {occurrence_id!r} "
                            f"has {len(requirements)} bindings ({', '.join(requirements)})"
                        ),
                    )
                )
    problems.extend(_check_set_port_ordering(snapshot, projection))
    return problems


def _check_set_port_ordering(
    snapshot: TaskNetworkSnapshot, projection: ExecutionProjection
) -> list[ProjectionProblem]:
    """TG §4.3: a set port must declare how its bindings are ordered.

    ``PortSpec.ordering`` is optional in the contract so a port list can be built
    before that decision is made; admitting a *network* is where the decision
    becomes due, so this is the layer that refuses an undeclared one rather than
    letting "whoever wrote last" settle it at run time.
    """

    problems: list[ProjectionProblem] = []
    for occurrence_id in sorted(projection.projected_occurrences, key=str):
        binding = snapshot.binding_for_occurrence(occurrence_id)
        for label, ports in (
            ("input", binding.input_ports),
            ("output", binding.output_ports),
        ):
            for port_key in undeclared_set_ports(ports):
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.SET_PORT_UNORDERED,
                        detail=(
                            f"set {label} port {port_key!r} of {occurrence_id!r} declares no "
                            "ordering; a set port defines its order, it is not "
                            "last-writer-wins"
                        ),
                    )
                )
    return problems


def _check_orphans(
    snapshot: TaskNetworkSnapshot, projection: ExecutionProjection
) -> list[ProjectionProblem]:
    """Two different failures, reported as two kinds.

    A duty nobody plans for and a planned occurrence nobody adopted are different
    mistakes with different fixes, and a caller that can only repair one of them
    should not have to read the detail string to tell which it got.
    """

    problems: list[ProjectionProblem] = []
    projected = projection.projected_occurrences
    covered_obligations = {
        snapshot.occurrence(occurrence_id).obligation_id for occurrence_id in projected
    }
    for obligation in snapshot.required_obligations:
        if obligation not in covered_obligations:
            problems.append(
                ProjectionProblem(
                    kind=ProblemKind.ORPHAN_OBLIGATION,
                    detail=(
                        f"required obligation {obligation!r} has no occurrence in the adopted plan"
                    ),
                )
            )
    held_by_some_method: set[OccurrenceId] = set()
    for draft in snapshot.method_instances:
        for child in draft.child_bindings:
            held_by_some_method.add(child.occurrence_id)
    for spec in snapshot.occurrences:
        if spec.requiredness is not Requiredness.REQUIRED:
            continue
        if spec.occurrence_id in projected or spec.occurrence_id in held_by_some_method:
            continue
        problems.append(
            ProjectionProblem(
                kind=ProblemKind.UNREACHED_REQUIRED_OCCURRENCE,
                detail=(
                    f"required occurrence {spec.occurrence_id!r} belongs to no method instance "
                    "at all, adopted or alternative"
                ),
            )
        )
    return problems


def _check_root_coverage(
    snapshot: TaskNetworkSnapshot, projection: ExecutionProjection
) -> list[ProjectionProblem]:
    problems: list[ProjectionProblem] = []
    projected = projection.projected_occurrences
    for root in snapshot.root_occurrence_ids:
        spec = snapshot.occurrence(root)
        wanted = tuple(snapshot.binding_for_task(spec.task_id).goal_signature.coverage_criteria)
        if not wanted:
            continue
        covered: set[str] = set()
        for claim in snapshot.obligation_coverage:
            if claim.obligation_id != spec.obligation_id:
                continue
            if any(item not in projected for item in claim.covered_by):
                continue
            covered.update(claim.criterion_ids)
        missing = sorted(set(wanted) - covered)
        if missing:
            problems.append(
                ProjectionProblem(
                    kind=ProblemKind.ROOT_COVERAGE_GAP,
                    detail=(
                        f"root occurrence {root!r} leaves criteria {', '.join(missing)} of "
                        f"obligation {spec.obligation_id!r} uncovered"
                    ),
                )
            )
    return problems


def _check_resource_conflicts(
    snapshot: TaskNetworkSnapshot,
    projection: ExecutionProjection,
    successors: Mapping[str, Sequence[str]],
) -> list[ProjectionProblem]:
    """A symmetric read/write clash is one problem, not two opposite dependencies.

    Only primitives declare resources (the contract refuses them on a compound),
    and pairs where one occurrence is a refinement ancestor of the other are
    skipped: an ancestor and its descendant are phases of the same work, not two
    things running at once.

    The comparison is bucketed by resource, so a mission with many occurrences
    touching disjoint objects costs ``O(Σ|bucket|²)`` rather than ``O(C²)``, and
    each bucket stops listing after :data:`MAX_CONFLICTS_PER_RESOURCE` pairs —
    with a ``BOUND_REACHED`` saying so, never a silent truncation.

    The projection cannot repair a conflict either: naming an order here would be
    inventing a business decision (TG §3.1, §6.6).
    """

    buckets: dict[tuple[str, str], list[OccurrenceId]] = {}
    for occurrence_id in sorted(projection.projected_occurrences, key=str):
        binding = snapshot.binding_for_occurrence(occurrence_id)
        for ref in (*binding.resource_reads, *binding.resource_writes):
            buckets.setdefault(ref.key, []).append(occurrence_id)

    view = snapshot.refinement_view()
    reach: dict[str, set[str]] = {}
    problems: list[ProjectionProblem] = []
    for key in sorted(buckets):
        members = buckets[key]
        listed = 0
        found = 0
        for seat, left in enumerate(members):
            for right in members[seat + 1 :]:
                if left == right:
                    continue
                left_binding = snapshot.binding_for_occurrence(left)
                right_binding = snapshot.binding_for_occurrence(right)
                clashes = resource_conflicts(
                    left_binding.resource_reads,
                    left_binding.resource_writes,
                    right_binding.resource_reads,
                    right_binding.resource_writes,
                )
                if not any(ref.key == key for ref in clashes):
                    continue
                if right in view.ancestors_of(left) or left in view.ancestors_of(right):
                    continue
                if _ordered(projection, successors, reach, left, right):
                    continue
                if _ordered(projection, successors, reach, right, left):
                    continue
                found += 1
                if listed >= MAX_CONFLICTS_PER_RESOURCE:
                    continue
                listed += 1
                problems.append(
                    ProjectionProblem(
                        kind=ProblemKind.RESOURCE_CONFLICT,
                        detail=(
                            f"{left!r} and {right!r} both touch {key[0]}/{key[1]} (at least "
                            "one writes) with no ordering between them"
                        ),
                    )
                )
        if found > listed:
            problems.append(
                ProjectionProblem(
                    kind=ProblemKind.BOUND_REACHED,
                    detail=(
                        f"resource_conflict listing: {key[0]}/{key[1]} has {found} unordered "
                        f"pairs, {listed} listed; the rest are real and unreported, not absent"
                    ),
                )
            )
    return problems


def _ordered(
    projection: ExecutionProjection,
    successors: Mapping[str, Sequence[str]],
    reach: dict[str, set[str]],
    first: OccurrenceId,
    second: OccurrenceId,
) -> bool:
    source = projection.exit_node_id(first)
    target = projection.entry_node_id(second)
    if source not in successors:
        return False
    if source not in reach:
        reach[source] = _reachable_from(successors, source)
    return target in reach[source]


__all__ = (
    "MAX_CONFLICTS_PER_RESOURCE",
    "GraphIntegrityError",
    "ProblemKind",
    "ProjectionProblem",
    "ProjectionReport",
    "find_cycle",
    "kahn_order",
    "require_topological_order",
    "validate_execution_projection",
    "validate_refinement_acyclic",
)
