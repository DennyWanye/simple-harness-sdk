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
from dataclasses import dataclass, fields
from typing import Any

from ..contracts import Budget, ContractError, Mission
from ..contracts.models import STEP2_IMPLEMENTED_LAYERS, VERIFICATION_LAYERS
from .deduplicator import find_duplicates
from .dependency_checker import DependencyError, check_dependencies, roots_and_leaves

MAX_TASKS = 32


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
        for name in ("dependencies", "success_criteria", "verification_policy", "allowed_tools"):
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


def validate_graph(mission: Mission, proposal: TaskGraphProposal) -> ValidatedGraph:
    if not proposal.tasks:
        raise GraphRejected("empty", "a task graph needs at least one task")
    if len(proposal.tasks) > MAX_TASKS:
        raise GraphRejected("too_large", f"{len(proposal.tasks)} tasks > {MAX_TASKS}")
    duplicates = find_duplicates([(node.key, node.goal) for node in proposal.tasks])
    if duplicates:
        raise GraphRejected("duplicate", "; ".join(duplicates))
    try:
        order = check_dependencies(proposal.edges())
    except DependencyError as error:
        raise GraphRejected(error.reason, error.detail) from error
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
        undeployed = set(node.verification_policy) - STEP2_IMPLEMENTED_LAYERS
        if undeployed:
            raise GraphRejected("contract", f"{node.key}: layers not deployed {sorted(undeployed)}")
        extra_tools = set(node.allowed_tools) - set(mission.allowed_tools)
        if extra_tools:
            raise GraphRejected(
                "tools", f"{node.key}: tools outside the Mission {sorted(extra_tools)}"
            )
        if not node.budget.fits_within(mission.budget):
            raise GraphRejected("budget", f"{node.key}: budget exceeds the Mission budget (§18.2)")
    # §18.2: the children's budgets come from the parent — in sum, per limited dimension
    for name in ("max_tokens", "max_cost_micros"):
        parent = getattr(mission.budget, name)
        if parent is None:
            continue
        total = _sum_dimension(proposal.tasks, name)
        if total is None:
            raise GraphRejected(
                "budget", f"every task must bound {name} when the Mission bounds it"
            )
        if total > parent:
            raise GraphRejected(
                "budget", f"sum of task {name} ({total}) exceeds the Mission ({parent})"
            )
    roots, leaves = roots_and_leaves(proposal.edges())
    if not roots or not leaves:
        raise GraphRejected("shape", "graph needs a root and a terminal task")
    return ValidatedGraph(
        proposal=proposal, order=tuple(order), roots=tuple(roots), leaves=tuple(leaves)
    )


__all__ = (
    "MAX_TASKS",
    "GraphRejected",
    "TaskGraphProposal",
    "TaskNode",
    "ValidatedGraph",
    "validate_graph",
)
