# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Task Graph proposal (§6, §15, plan D3-1/D3-2): parse, validate as a whole, order.

A ``TaskGraphProposal`` is what the Planner emits inside ``<task_graph_proposal>``.
``validate_graph`` performs the Graph Manager checks of §24 step 3 — missing /
self / cyclic dependencies, duplicates, budgets within the Mission on every
limited dimension **and** in sum, tools within the Mission, at least one root and
one terminal task — and returns the deterministic topological order used to
assign formal ids.  Any failure rejects the whole proposal (``GraphRejected``);
nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Any

from ..contracts import Budget, ContractError, Mission
from ..contracts.models import STEP2_IMPLEMENTED_LAYERS, VERIFICATION_LAYERS
from ..planning.manager import inherit_limits, system_reserve_tokens
from .deduplicator import find_duplicates
from .dependency_checker import DependencyError, check_dependencies, roots_and_leaves

MAX_TASKS = 32
MAX_GRAPH_DEPTH = 6  # step 5 (R19): the same bound the change path enforces


class GraphRejected(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TaskNode:
    key: str
    goal: str
    rationale: str
    dependencies: tuple[str, ...]
    success_criteria: tuple[str, ...]
    verification_policy: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    priority: float = 1.0
    outputs: tuple[str, ...] = ()  # D3-7': declared output paths (static sibling check)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "goal": self.goal,
            "rationale": self.rationale,
            "dependencies": list(self.dependencies),
            "success_criteria": list(self.success_criteria),
            "verification_policy": list(self.verification_policy),
            "allowed_tools": list(self.allowed_tools),
            "budget": self.budget.to_json(),
            "priority": self.priority,
            "outputs": list(self.outputs),
        }

    @classmethod
    def from_json(cls, value: object) -> TaskNode:
        if not isinstance(value, Mapping):
            raise ContractError("task node must be an object")
        allowed = {f.name for f in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ContractError(f"task node has unknown fields: {sorted(unknown)}")
        missing = {"key", "goal", "rationale", "success_criteria", "verification_policy"} - set(
            value
        )
        if missing:
            raise ContractError(f"task node is missing fields: {sorted(missing)}")
        key = str(value["key"]).strip()
        if not key:
            raise ContractError("task node key must not be blank")
        for name in (
            "dependencies",
            "success_criteria",
            "verification_policy",
            "allowed_tools",
            "outputs",
        ):
            raw = value.get(name, ())
            if isinstance(raw, str) or not isinstance(raw, Sequence):
                raise ContractError(f"task node {key!r}: {name} must be a list")
        priority = value.get("priority", 1.0)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            raise ContractError(f"task node {key!r}: priority must be a number")
        return cls(
            key=key,
            goal=str(value["goal"]),
            rationale=str(value["rationale"]),
            dependencies=tuple(str(d) for d in value.get("dependencies", ())),
            success_criteria=tuple(str(c) for c in value["success_criteria"]),
            verification_policy=tuple(str(layer) for layer in value["verification_policy"]),
            allowed_tools=tuple(str(t) for t in value.get("allowed_tools", ())),
            budget=Budget.from_json(value.get("budget", {})),
            priority=float(priority),
            outputs=tuple(str(o) for o in value.get("outputs", ())),
        )


@dataclass(frozen=True, slots=True)
class TaskGraphProposal:
    tasks: tuple[TaskNode, ...]

    def to_json(self) -> dict[str, Any]:
        return {"tasks": [node.to_json() for node in self.tasks]}

    @classmethod
    def from_json(cls, value: object) -> TaskGraphProposal:
        if not isinstance(value, Mapping) or "tasks" not in value:
            raise ContractError("task graph proposal must be an object with a tasks list")
        raw = value["tasks"]
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ContractError("tasks must be a list")
        unknown = set(value) - {"tasks"}
        if unknown:
            raise ContractError(f"task graph proposal has unknown fields: {sorted(unknown)}")
        return cls(tasks=tuple(TaskNode.from_json(item) for item in raw))

    def edges(self) -> dict[str, tuple[str, ...]]:
        return {node.key: node.dependencies for node in self.tasks}


@dataclass(frozen=True, slots=True)
class ValidatedGraph:
    proposal: TaskGraphProposal
    order: tuple[str, ...]  # topological, deterministic
    roots: tuple[str, ...]
    leaves: tuple[str, ...]
    warnings: tuple[str, ...] = ()  # suspected duplicates etc. (D3-16)

    @property
    def terminal_key(self) -> str:
        """The Mission-level judgment target: the last leaf in topological order (D3-9)."""

        return [key for key in self.order if key in set(self.leaves)][-1]

    def node(self, key: str) -> TaskNode:
        return next(node for node in self.proposal.tasks if node.key == key)


def _sum_dimension(nodes: Sequence[TaskNode], name: str) -> int | None:
    """Sum of a budget dimension over the nodes; None when any node leaves it unlimited."""

    total = 0
    for node in nodes:
        value = getattr(node.budget, name)
        if value is None:
            return None
        total += value
    return total


@dataclass(frozen=True)
class TaskBudgetFloor:
    """P3.1 fix F-ORCH-1: the least a Task's token budget must hold for its first Attempt
    and that Attempt's Critic to be *reserved* — per candidate, because every candidate
    reserves a turn and has its own result reviewed.  ``base`` is what one turn of a
    routable profile may emit, ``critic`` the Critic's reservation (counted only when the
    policy names critic_review).  ``base == 0`` switches the floor off.

    A necessary condition at reservation time, and no more (review round 1 P2-2): it holds
    only while a turn settles within its reservation, since the ledger books what a turn
    actually spent — a real turn also pays for its input tokens and may call the model more
    than once.  A Task whose budget is exactly the floor can therefore still run out before
    its Critic is reserved; the floor never promises that repairs will be affordable."""

    base: int
    critic: int = 0

    def floor_for(self, policy: Sequence[str], candidates: int = 1) -> int:
        if self.base <= 0:
            return 0
        per_candidate = self.base + (self.critic if "critic_review" in policy else 0)
        return max(1, int(candidates)) * per_candidate


def floor_refusal(
    floor: TaskBudgetFloor | None, policy: Sequence[str], granted: int | None, candidates: int = 1
) -> str | None:
    """Why ``granted`` tokens cannot carry a Task under ``floor`` — or None when it can
    (no floor, an unlimited budget, or enough)."""

    if floor is None or granted is None:
        return None
    need = floor.floor_for(policy, candidates)
    if granted >= need:
        return None
    critic = floor.critic if "critic_review" in policy else 0
    return (
        f"task_budget_below_floor: max_tokens={granted} < {need} "
        f"(= {max(1, int(candidates))} candidate(s) × "
        f"({floor.base} for one turn + {critic} for its Critic)); "
        f"a first Attempt and its Critic could not both be reserved — give at least {need}"
    )


def normalise_budgets(mission: Mission, proposal: TaskGraphProposal) -> TaskGraphProposal:
    """D3-2': deterministic budget completion before validation.

    A task that leaves ``max_tokens`` / ``max_cost_micros`` unset while the Mission
    bounds it receives an even share of the Mission pool (integer division; the
    remainder stays with the Mission); ``max_attempts`` / ``max_concurrency`` /
    ``max_runtime_seconds`` are inherited from the Mission when unset.  Explicit
    values are never changed, so the hard checks below still apply to them.
    """

    count = max(1, len(proposal.tasks))
    reserve = system_reserve_tokens(mission)  # D4-20: synthesis + conflict reserve
    nodes = []
    for node in proposal.tasks:
        changes: dict[str, Any] = {}
        for name in ("max_tokens", "max_cost_micros", "max_tool_calls"):
            parent = getattr(mission.budget, name)
            if getattr(node.budget, name) is None and parent is not None:
                pool = max(0, parent - reserve) if name == "max_tokens" else parent
                # review P1-2: a pool dimension is shared out, never copied to every Task
                changes[name] = max(1, pool // count) if name == "max_tool_calls" else pool // count
        for name in ("max_attempts", "max_concurrency", "max_runtime_seconds"):
            parent = getattr(mission.budget, name)
            if getattr(node.budget, name) is None and parent is not None:
                changes[name] = parent
        if changes:
            node = replace(node, budget=replace(node.budget, **changes))
        nodes.append(node)
    return TaskGraphProposal(tasks=tuple(nodes))


def ancestors_of(edges: Mapping[str, Sequence[str]]) -> dict[str, frozenset[str]]:
    """Transitive dependency closure per key (edges must already be acyclic)."""

    memo: dict[str, frozenset[str]] = {}

    def visit(key: str) -> frozenset[str]:
        if key in memo:
            return memo[key]
        found: set[str] = set()
        for dep in edges.get(key, ()):
            found.add(dep)
            found |= visit(dep)
        memo[key] = frozenset(found)
        return memo[key]

    for key in edges:
        visit(key)
    return memo


def _sibling_output_conflicts(proposal: TaskGraphProposal) -> list[str]:
    """D3-7': two tasks neither of which depends on the other may not both declare
    the same output path (their results could never be merged deterministically)."""

    edges = proposal.edges()
    ancestors = ancestors_of(edges)
    conflicts = []
    nodes = list(proposal.tasks)
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            if left.key in ancestors[right.key] or right.key in ancestors[left.key]:
                continue
            shared = sorted(set(left.outputs) & set(right.outputs))
            if shared:
                conflicts.append(f"{left.key} and {right.key} both declare outputs {shared}")
    return conflicts


def pytest_criteria(criteria: Sequence[str]) -> list[str]:
    """The ``pytest:`` criteria among ``criteria`` — judged only by running code here."""

    return [c for c in criteria if c.startswith("pytest:")]


def validate_graph(
    mission: Mission,
    proposal: TaskGraphProposal,
    *,
    deployed_layers: frozenset[str] = STEP2_IMPLEMENTED_LAYERS,
    task_floor: TaskBudgetFloor | None = None,
    candidates: int = 1,
) -> ValidatedGraph:
    if not proposal.tasks:
        raise GraphRejected("empty", "a task graph needs at least one task")
    if len(proposal.tasks) > MAX_TASKS:
        raise GraphRejected("too_large", f"{len(proposal.tasks)} tasks > {MAX_TASKS}")
    proposal = normalise_budgets(mission, proposal)
    report = find_duplicates(
        [(node.key, node.goal, node.dependencies, node.success_criteria) for node in proposal.tasks]
    )
    if report.problems:
        raise GraphRejected("duplicate", "; ".join(report.problems))
    try:
        order = check_dependencies(proposal.edges())
    except DependencyError as error:
        raise GraphRejected(error.reason, error.detail) from error
    conflicts = _sibling_output_conflicts(proposal)
    if conflicts:
        raise GraphRejected("artifact_conflict", "; ".join(conflicts))
    depth: dict[str, int] = {}
    for key in order:
        depth[key] = 1 + max((depth[d] for d in proposal.edges()[key]), default=0)
    if depth and max(depth.values()) > MAX_GRAPH_DEPTH:
        raise GraphRejected("depth", f"graph depth {max(depth.values())} > {MAX_GRAPH_DEPTH}")
    for node in proposal.tasks:
        if not node.goal.strip():
            raise GraphRejected("contract", f"{node.key}: goal is blank")
        if not node.rationale.strip():
            raise GraphRejected("contract", f"{node.key}: no rationale (§19.5 goal drift)")
        if not node.success_criteria:
            raise GraphRejected("contract", f"{node.key}: no success criteria (§6.3)")
        if not node.verification_policy:
            raise GraphRejected("contract", f"{node.key}: no verification policy")
        unknown_layers = set(node.verification_policy) - set(VERIFICATION_LAYERS)
        if unknown_layers:
            raise GraphRejected("contract", f"{node.key}: unknown layers {sorted(unknown_layers)}")
        undeployed = set(node.verification_policy) - deployed_layers  # host support 0.9.8
        if undeployed:
            raise GraphRejected(
                "verification_policy_undeployed",
                f"{node.key}: layers not deployed {sorted(undeployed)}",
            )
        if "code_test" not in deployed_layers and pytest_criteria(node.success_criteria):
            # review round 1 P1-1: nobody could judge such a criterion here
            raise GraphRejected(
                "verification_policy_undeployed",
                f"{node.key}: pytest criteria need local code execution, which this "
                f"deployment has turned off: {pytest_criteria(node.success_criteria)}",
            )
        extra_tools = set(node.allowed_tools) - set(mission.allowed_tools)
        if extra_tools:
            raise GraphRejected(
                "tools", f"{node.key}: tools outside the Mission {sorted(extra_tools)}"
            )
        if not node.budget.fits_within(mission.budget):
            raise GraphRejected(
                "budget",
                f"{node.key}: budget exceeds the Mission budget (§18.2); "
                f"mission={mission.budget.to_json()}",
            )
        # P3.1 fix F-ORCH-1: the effective budget (explicit, or the pool share) must carry
        # a first Attempt and its Critic; the proposal is refused, never silently raised
        refusal = floor_refusal(
            task_floor, node.verification_policy, node.budget.max_tokens, candidates
        )
        if refusal:
            raise GraphRejected("budget", f"{node.key}: {refusal}")
    # §18.2: the children's budgets come from the parent — in sum, per limited dimension;
    # the system tasks' reserve (D4-20) is part of the sum on the token dimension
    for name in ("max_tokens", "max_cost_micros"):
        parent = getattr(mission.budget, name)
        if parent is None:
            continue
        total = _sum_dimension(proposal.tasks, name)
        if total is None:  # cannot happen after normalisation; kept as a guard
            raise GraphRejected(
                "budget", f"every task must bound {name} when the Mission bounds it"
            )
        reserve = system_reserve_tokens(mission) if name == "max_tokens" else 0
        if total + reserve > parent:
            raise GraphRejected(
                "budget",
                f"sum of task {name} ({total}) plus the system reserve ({reserve}) exceeds "
                f"the Mission ({parent}); dimension={name} remaining={max(0, parent - reserve)}",
            )
    template = (mission.final_report or {}).get("synthesis")
    if template:  # P1-2: the synthesis Task's own budget must fit the Mission too
        try:
            synthesis_budget = inherit_limits(
                Budget.from_json(dict(template).get("budget", {})), mission.budget
            )
        except ContractError as error:
            raise GraphRejected("budget", f"synthesis template budget invalid: {error}") from error
        if not synthesis_budget.fits_within(mission.budget):
            raise GraphRejected(
                "budget",
                f"synthesis task budget {synthesis_budget.to_json()} exceeds the Mission "
                "budget (§18.2)",
            )
    roots, leaves = roots_and_leaves(proposal.edges())
    if not roots or not leaves:
        raise GraphRejected("shape", "graph needs a root and a terminal task")
    return ValidatedGraph(
        proposal=proposal,
        order=tuple(order),
        roots=tuple(roots),
        leaves=tuple(leaves),
        warnings=report.suspected,
    )


__all__ = (
    "MAX_TASKS",
    "GraphRejected",
    "TaskBudgetFloor",
    "TaskGraphProposal",
    "TaskNode",
    "ValidatedGraph",
    "ancestors_of",
    "floor_refusal",
    "normalise_budgets",
    "validate_graph",
)
