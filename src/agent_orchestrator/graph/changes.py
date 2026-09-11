# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Task DAG change proposals (§6.2 dynamic Task Graph, §15 Proposal/Commit, ORCH §7.3;
plan D5-1 / D5-2).

A ``TaskGraphChange`` is what a Manager proposes after execution evidence: a base
graph version, the basis (result / attempt / task that triggered it), a rationale
and a list of operations from a *system-defined* vocabulary.  ``validate_change``
merges the proposal with the current formal graph and checks it as a whole —
cycles, missing dependencies, duplicates, depth, task count, proposals per source
Attempt, the Mission budget pool, tools, deployed layers, goal drift and the
legality of every operation against §25.1 (no back-edges: an executing Task is
never rewired, it is superseded by a new entity; only a BLOCKED Task may have its
dependencies rewritten in place).  Any failure rejects the whole proposal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Budget, ContractError, Mission, Task, TaskStatus
from ..contracts.models import (
    STEP2_IMPLEMENTED_LAYERS,
    SYSTEM_DEFAULT_POLICY,
    VERIFICATION_LAYERS,
    default_change_policy,
    sha256_hex,
)
from ..planning.manager import inherit_limits, system_reserve_tokens
from .deduplicator import find_duplicates
from .dependency_checker import DependencyError, check_dependencies
from .task_graph import MAX_TASKS

OPERATIONS = (
    "add_task",
    "supersede_task",
    "retarget_dependencies",
    "set_priority",
    "pause_task",
    "resume_task",
    "cancel_task",
    "set_role",
)
TASK_ROLES = ("worker", "explorer", "exploiter", "simplifier", "connector", "failure_analyst")
SUPERSEDABLE = frozenset({TaskStatus.READY, TaskStatus.ACTIVE, TaskStatus.VERIFYING})


class GraphChangeRejected(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ChangeLimits:
    max_graph_depth: int = 6
    max_proposals_per_agent: int = 3
    max_tasks: int = MAX_TASKS
    max_supersede_chain: int = 2
    admit_new_tasks: bool = True  # step 6 (D6-3 ③): False while backpressure is raised


@dataclass(frozen=True, slots=True)
class NewTaskNode:
    key: str
    goal: str
    rationale: str
    dependencies: tuple[str, ...]  # existing task ids or keys of this proposal
    success_criteria: tuple[str, ...]
    verification_policy: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    priority: float = 1.0
    outputs: tuple[str, ...] = ()
    parent_task_ids: tuple[str, ...] = ()
    role: str = "worker"

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
            "parent_task_ids": list(self.parent_task_ids),
            "role": self.role,
        }

    @classmethod
    def from_json(
        cls, value: object, *, default_policy: Sequence[str] = SYSTEM_DEFAULT_POLICY
    ) -> NewTaskNode:
        if not isinstance(value, Mapping):
            raise ContractError("add_task must be an object")
        allowed = {
            "key",
            "goal",
            "rationale",
            "dependencies",
            "success_criteria",
            "verification_policy",
            "allowed_tools",
            "budget",
            "priority",
            "outputs",
            "parent_task_ids",
            "role",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ContractError(f"add_task has unknown fields: {sorted(unknown)}")
        missing = {"key", "goal", "rationale", "success_criteria"} - set(value)
        if missing:
            raise ContractError(f"add_task is missing fields: {sorted(missing)}")
        key = str(value["key"]).strip()
        if not key:
            raise ContractError("add_task key must not be blank")
        for name in (
            "dependencies",
            "success_criteria",
            "verification_policy",
            "allowed_tools",
            "outputs",
            "parent_task_ids",
        ):
            raw = value.get(name, ())
            if isinstance(raw, str) or not isinstance(raw, Sequence):
                raise ContractError(f"add_task {key!r}: {name} must be a list")
        priority = value.get("priority", 1.0)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            raise ContractError(f"add_task {key!r}: priority must be a number")
        role = str(value.get("role", "worker") or "worker")
        if role not in TASK_ROLES:
            raise ContractError(f"add_task {key!r}: unknown role {role!r}")
        return cls(
            key=key,
            goal=str(value["goal"]),
            rationale=str(value["rationale"]),
            dependencies=tuple(str(d) for d in value.get("dependencies", ())),
            success_criteria=tuple(str(c) for c in value["success_criteria"]),
            verification_policy=tuple(
                str(layer) for layer in value.get("verification_policy", default_policy)
            ),
            allowed_tools=tuple(str(t) for t in value.get("allowed_tools", ())),
            budget=Budget.from_json(value.get("budget", {})),
            priority=float(priority),
            outputs=tuple(str(o) for o in value.get("outputs", ())),
            parent_task_ids=tuple(str(p) for p in value.get("parent_task_ids", ())),
            role=role,
        )


@dataclass(frozen=True, slots=True)
class Operation:
    op: str
    args: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, **{k: v for k, v in self.args.items()}}

    @classmethod
    def from_json(cls, value: object) -> Operation:
        if not isinstance(value, Mapping) or "op" not in value:
            raise ContractError("operation must be an object with an 'op'")
        op = str(value["op"])
        if op not in OPERATIONS:
            raise ContractError(f"unknown operation {op!r} (allowed: {list(OPERATIONS)})")
        args = {k: v for k, v in value.items() if k != "op"}
        if op == "add_task":
            NewTaskNode.from_json(args)  # validate shape now; parsed again in validate_change
        elif op in {
            "supersede_task",
            "retarget_dependencies",
            "set_priority",
            "pause_task",
            "resume_task",
            "cancel_task",
            "set_role",
        }:
            if not str(args.get("task_id", "")).strip():
                raise ContractError(f"{op} needs task_id")
        if op == "supersede_task" and not str(args.get("replacement_key", "")).strip():
            raise ContractError(
                "supersede_task needs replacement_key (an add_task of this proposal)"
            )
        if op == "retarget_dependencies":
            deps = args.get("dependencies")
            if isinstance(deps, str) or not isinstance(deps, Sequence):
                raise ContractError("retarget_dependencies needs a dependencies list")
        if op == "set_priority":
            p = args.get("priority")
            if isinstance(p, bool) or not isinstance(p, (int, float)):
                raise ContractError("set_priority needs a numeric priority")
        if op == "set_role" and str(args.get("role")) not in TASK_ROLES:
            raise ContractError(f"set_role role must be one of {list(TASK_ROLES)}")
        return cls(op=op, args=dict(args))


@dataclass(frozen=True, slots=True)
class TaskGraphChange:
    base_graph_version: int
    basis: Mapping[str, Any]  # {"trigger": ..., "result_id"?, "attempt_id"?, "task_id"?}
    rationale: str
    operations: tuple[Operation, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "base_graph_version": self.base_graph_version,
            "basis": dict(self.basis),
            "rationale": self.rationale,
            "operations": [op.to_json() for op in self.operations],
        }

    @property
    def proposal_hash(self) -> str:
        body = self.to_json()
        body.pop("base_graph_version")
        return sha256_hex(body)

    @classmethod
    def from_json(cls, value: object) -> TaskGraphChange:
        if not isinstance(value, Mapping):
            raise ContractError("graph change proposal must be an object")
        unknown = set(value) - {"base_graph_version", "basis", "rationale", "operations"}
        if unknown:
            raise ContractError(f"graph change proposal has unknown fields: {sorted(unknown)}")
        base = value.get("base_graph_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 1:
            raise ContractError("base_graph_version must be a positive integer")
        raw_ops = value.get("operations", ())
        if isinstance(raw_ops, str) or not isinstance(raw_ops, Sequence):
            raise ContractError("operations must be a list")
        basis = value.get("basis", {})
        if not isinstance(basis, Mapping):
            raise ContractError("basis must be an object")
        return cls(
            base_graph_version=base,
            basis=dict(basis),
            rationale=str(value.get("rationale", "")),
            operations=tuple(Operation.from_json(item) for item in raw_ops),
        )

    def add_tasks(
        self, *, default_policy: Sequence[str] = SYSTEM_DEFAULT_POLICY
    ) -> list[NewTaskNode]:
        return [
            NewTaskNode.from_json(op.args, default_policy=default_policy)
            for op in self.operations
            if op.op == "add_task"
        ]

    def referenced_task_ids(self) -> set[str]:
        ids: set[str] = set()
        for op in self.operations:
            if op.op != "add_task" and op.args.get("task_id"):
                ids.add(str(op.args["task_id"]))
            if op.op == "add_task":
                ids.update(str(d) for d in op.args.get("dependencies", ()))
                ids.update(str(p) for p in op.args.get("parent_task_ids", ()))
        return ids


@dataclass(frozen=True, slots=True)
class ValidatedChange:
    change: TaskGraphChange
    new_nodes: tuple[NewTaskNode, ...]
    order: tuple[str, ...]  # topological order of the new keys only
    superseded: Mapping[str, str]  # old task id → replacement key
    retargets: Mapping[str, tuple[str, ...]]  # BLOCKED task id → new dependency refs
    priorities: Mapping[str, float]
    pauses: Mapping[str, str]
    resumes: tuple[str, ...]
    cancels: Mapping[str, str]
    roles: Mapping[str, str]
    affected_task_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    depth: int = 0
    budget_pool: Mapping[str, int] = field(default_factory=dict)


def _depth(edges: Mapping[str, Sequence[str]], order: Sequence[str]) -> int:
    depth: dict[str, int] = {}
    for key in order:
        depth[key] = 1 + max((depth[d] for d in edges[key]), default=0)
    return max(depth.values(), default=0)


def validate_change(
    mission: Mission,
    tasks: Sequence[Task],
    change: TaskGraphChange,
    *,
    limits: ChangeLimits,
    proposals_by_attempt: Mapping[str, int],
    committed_tokens_by_task: Mapping[str, int],
    deployed_layers: frozenset[str] = STEP2_IMPLEMENTED_LAYERS,
) -> ValidatedChange:
    """Graph Manager checks for one change proposal against the current formal graph.
    ``deployed_layers`` (host support 0.9.8) is what this deployment can run: an
    ``add_task`` without a policy gets the system default narrowed to it, one that names
    an undeployed layer is refused."""

    by_id = {task.id: task for task in tasks}
    live = {task.id: task for task in tasks if task.status is not TaskStatus.CANCELLED}
    if not limits.admit_new_tasks and any(op.op == "add_task" for op in change.operations):
        # §18.5 "禁止新任务继续分裂": while the pipeline is backed up no proposal may grow
        # the graph; the Manager is told so and may still change roles or priorities
        raise GraphChangeRejected(
            "backpressure", "no new Task may be added while backpressure is raised (§18.5)"
        )
    nodes = change.add_tasks(default_policy=default_change_policy(deployed_layers))
    keys = [node.key for node in nodes]
    if len(set(keys)) != len(keys):
        raise GraphChangeRejected("duplicate", f"repeated add_task keys {keys}")
    key_set = set(keys)
    superseded: dict[str, str] = {}
    retargets: dict[str, tuple[str, ...]] = {}
    priorities: dict[str, float] = {}
    pauses: dict[str, str] = {}
    resumes: list[str] = []
    cancels: dict[str, str] = {}
    roles: dict[str, str] = {}

    def existing(task_id: str, op: str) -> Task:
        task = by_id.get(task_id)
        if task is None:
            raise GraphChangeRejected("missing_task", f"{op} refers to unknown task {task_id!r}")
        return task

    for op in change.operations:
        if op.op == "add_task":
            continue
        task_id = str(op.args["task_id"])
        task = existing(task_id, op.op)
        if op.op == "supersede_task":
            if task.status not in SUPERSEDABLE:
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"supersede_task {task_id}: a {task.status} Task cannot be superseded (§25.1)",
                )
            replacement = str(op.args["replacement_key"])
            if replacement not in key_set:
                raise GraphChangeRejected(
                    "missing_task",
                    f"supersede_task {task_id}: replacement {replacement!r} is not an add_task of this proposal",
                )
            if task.kind != "work":
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"supersede_task {task_id}: system Tasks ({task.kind}) are not superseded by a Manager",
                )
            chain = int(task.context.get("supersede_depth", 0)) + 1
            if chain > limits.max_supersede_chain:
                raise GraphChangeRejected(
                    "supersede_chain",
                    f"{task_id} was already superseded {chain - 1} time(s); max_supersede_chain={limits.max_supersede_chain}",
                )
            superseded[task_id] = replacement
        elif op.op == "retarget_dependencies":
            if task.status is not TaskStatus.BLOCKED:
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"retarget_dependencies {task_id}: only a BLOCKED Task may have its dependencies rewritten in place; a {task.status} Task must be superseded",
                )
            if task.kind == "synthesis":
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"retarget_dependencies {task_id}: the synthesis Task's dependencies never change (D4-8')",
                )
            retargets[task_id] = tuple(str(d) for d in op.args["dependencies"])
        elif op.op == "set_priority":
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise GraphChangeRejected(
                    "illegal_transition", f"set_priority {task_id}: Task is terminal"
                )
            priorities[task_id] = float(op.args["priority"])
        elif op.op in {"pause_task", "resume_task"}:
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise GraphChangeRejected(
                    "illegal_transition", f"{op.op} {task_id}: Task is terminal"
                )
            if op.op == "pause_task":
                if task.status not in {TaskStatus.READY, TaskStatus.BLOCKED}:
                    # review P0-1: an executing route cannot be parked — it would never reach
                    # a terminal state and the judge would wait forever; supersede or cancel it
                    raise GraphChangeRejected(
                        "illegal_transition",
                        f"pause_task {task_id}: only a READY/BLOCKED Task can be paused; "
                        f"a {task.status} Task must be superseded or cancelled",
                    )
                pauses[task_id] = str(op.args.get("reason", ""))
            else:
                resumes.append(task_id)
        elif op.op == "cancel_task":
            if task.status not in SUPERSEDABLE:
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"cancel_task {task_id}: a {task.status} Task cannot be cancelled (§25.1)",
                )
            if task.kind != "work":
                raise GraphChangeRejected(
                    "illegal_transition",
                    f"cancel_task {task_id}: system Tasks are not cancelled by a Manager",
                )
            cancels[task_id] = str(op.args.get("reason", ""))
        elif op.op == "set_role":
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise GraphChangeRejected(
                    "illegal_transition", f"set_role {task_id}: Task is terminal"
                )
            roles[task_id] = str(op.args["role"])
    # ---- R10: a Task appears in at most one structural operation per proposal
    structural = [set(superseded), set(retargets), set(cancels)]
    for index, left in enumerate(structural):
        for right in structural[index + 1 :]:
            clash = left & right
            if clash:
                raise GraphChangeRejected(
                    "conflicting_operations", f"tasks in two structural operations: {sorted(clash)}"
                )
    # ---- the merged graph: live tasks (minus superseded/cancelled) + new nodes
    removed = set(superseded) | set(cancels)
    edges: dict[str, list[str]] = {}
    for task in live.values():
        if task.id in removed:
            continue
        deps = list(retargets.get(task.id, task.dependency_ids))
        deps = [
            superseded.get(d, d) for d in deps
        ]  # a dependent of a superseded task follows the replacement
        edges[task.id] = deps
    for node in nodes:
        edges[node.key] = list(node.dependencies)
    for key, deps in edges.items():
        for dep in deps:
            if dep in removed and dep not in key_set:
                raise GraphChangeRejected(
                    "missing_dependency", f"{key} depends on {dep}, which this proposal removes"
                )
            if dep not in edges:
                raise GraphChangeRejected("missing_dependency", f"{key} -> {dep}")
            if dep in by_id and by_id[dep].status is TaskStatus.CANCELLED:
                raise GraphChangeRejected(
                    "missing_dependency", f"{key} depends on CANCELLED task {dep}"
                )
    try:
        order = check_dependencies(edges)
    except DependencyError as error:
        raise GraphChangeRejected(error.reason, error.detail) from error
    if len(edges) > limits.max_tasks:
        raise GraphChangeRejected("too_large", f"{len(edges)} tasks > max_tasks={limits.max_tasks}")
    depth = _depth(edges, order)
    if depth > limits.max_graph_depth:
        raise GraphChangeRejected(
            "depth", f"graph depth {depth} > max_graph_depth={limits.max_graph_depth}"
        )
    # ---- proposals per source Attempt
    source_attempt = str(change.basis.get("attempt_id") or "")
    if source_attempt and nodes:
        already = int(proposals_by_attempt.get(source_attempt, 0))
        if already + len(nodes) > limits.max_proposals_per_agent:
            raise GraphChangeRejected(
                "proposals",
                f"attempt {source_attempt} would have {already + len(nodes)} added tasks > max_proposals_per_agent={limits.max_proposals_per_agent}",
            )
    # ---- duplicates across the merged graph
    items = [
        (t.id, t.goal, tuple(edges[t.id]), t.success_criteria)
        for t in live.values()
        if t.id not in removed
    ]
    items += [(n.key, n.goal, tuple(n.dependencies), n.success_criteria) for n in nodes]
    report = find_duplicates(items)
    if report.problems:
        raise GraphChangeRejected("duplicate", "; ".join(report.problems))
    # ---- D3-7' on the merged graph: independent tasks may not declare the same output path
    outputs_of: dict[str, set[str]] = {
        t.id: set(t.outputs) for t in live.values() if t.id not in removed
    }
    for node in nodes:
        outputs_of[node.key] = set(node.outputs)
    closure: dict[str, set[str]] = {}

    def anc(key: str) -> set[str]:
        if key not in closure:
            found: set[str] = set()
            for dep in edges.get(key, ()):
                found.add(dep)
                found |= anc(dep)
            closure[key] = found
        return closure[key]

    for node in nodes:
        for other, outs in outputs_of.items():
            if other == node.key or other in anc(node.key) or node.key in anc(other):
                continue
            shared = sorted(outputs_of[node.key] & outs)
            if shared:
                raise GraphChangeRejected(
                    "artifact_conflict",
                    f"{node.key} and {other} are independent but both declare outputs {shared}",
                )
    # ---- contracts of the new nodes
    for node in nodes:
        if not node.goal.strip() or not node.success_criteria:
            raise GraphChangeRejected(
                "contract", f"{node.key}: goal and success_criteria are required (§6.3)"
            )
        if not node.rationale.strip():
            raise GraphChangeRejected(
                "goal_drift",
                f"{node.key}: no rationale — every Task must say how it serves the root goal (§19.5)",
            )
        unknown_layers = set(node.verification_policy) - set(VERIFICATION_LAYERS)
        if unknown_layers or not node.verification_policy:
            raise GraphChangeRejected(
                "contract", f"{node.key}: bad verification_policy {sorted(unknown_layers)}"
            )
        undeployed = set(node.verification_policy) - deployed_layers  # host support 0.9.8
        if undeployed:
            raise GraphChangeRejected(
                "verification_policy_undeployed",
                f"{node.key}: layers not deployed {sorted(undeployed)}",
            )
        extra_tools = set(node.allowed_tools) - set(mission.allowed_tools)
        if extra_tools:
            raise GraphChangeRejected(
                "tools", f"{node.key}: tools outside the Mission {sorted(extra_tools)}"
            )
        for parent in node.parent_task_ids:
            if parent not in by_id:
                raise GraphChangeRejected(
                    "missing_task", f"{node.key}: parent_task_ids refers to unknown task {parent!r}"
                )
    # ---- goal drift: a new Task must feed something (a dependent), replace something or refine something
    depended_on = {dep for deps in edges.values() for dep in deps}
    replacements = set(superseded.values())
    for node in nodes:
        if node.key in depended_on or node.key in replacements or node.parent_task_ids:
            continue
        raise GraphChangeRejected(
            "goal_drift",
            f"{node.key}: nothing depends on it, it replaces nothing and refines nothing — it cannot be traced to the root goal (§19.5)",
        )
    # ---- the Mission budget pool (D5-2 / 30-13)
    pool = mission.budget.max_tokens
    budgets: dict[str, int] = {}
    if pool is not None:
        committed = 0
        for task in tasks:
            if task.status is TaskStatus.CANCELLED or task.id in removed:
                # R3 / ORCH §12.2: a cancelled task keeps what it settled *and* what is
                # still reserved in flight — nothing is freed by cancelling
                committed += int(committed_tokens_by_task.get(task.id, 0))
            else:
                committed += int(task.budget.max_tokens or 0)
        reserve = system_reserve_tokens(mission)
        # a superseded/cancelled task's unused allocation returns to the pool once its
        # reservations settle; settled + in-flight reserved stay committed
        remaining = pool - reserve - committed
        # review P2-7: a node that names no budget gets a bounded share (at most a quarter
        # of the pool, or the pool split over live + new Tasks), never the whole remainder
        default_share = (
            max(1, min(remaining // max(1, len(nodes)), pool // max(4, len(tasks) + len(nodes))))
            if nodes
            else 0
        )
        for node in nodes:
            wanted = node.budget.max_tokens
            budgets[node.key] = int(wanted if wanted is not None else default_share)
        total_new = sum(budgets.values())
        if total_new > remaining:
            raise GraphChangeRejected(
                "budget",
                f"new tasks ask {total_new} tokens but the Mission pool has {max(0, remaining)} left "
                f"(pool={pool}, committed={committed}, system_reserve={reserve}); dimension=max_tokens remaining={max(0, remaining)}",
            )
    affected = sorted(
        set(superseded)
        | set(retargets)
        | set(priorities)
        | set(pauses)
        | set(resumes)
        | set(cancels)
        | set(roles)
        | set(keys)
    )
    return ValidatedChange(
        change=change,
        new_nodes=tuple(nodes),
        order=tuple(key for key in order if key in key_set),
        superseded=superseded,
        retargets=retargets,
        priorities=priorities,
        pauses=pauses,
        resumes=tuple(resumes),
        cancels=cancels,
        roles=roles,
        affected_task_ids=tuple(affected),
        warnings=report.suspected,
        depth=depth,
        budget_pool=budgets,
    )


def node_budget(node: NewTaskNode, validated: ValidatedChange, mission: Mission) -> Budget:
    tokens = validated.budget_pool.get(node.key, node.budget.max_tokens)
    budget = Budget.from_json({**node.budget.to_json(), "max_tokens": tokens})
    return inherit_limits(budget, mission.budget)


__all__ = (
    "OPERATIONS",
    "SUPERSEDABLE",
    "TASK_ROLES",
    "ChangeLimits",
    "GraphChangeRejected",
    "NewTaskNode",
    "Operation",
    "TaskGraphChange",
    "ValidatedChange",
    "node_budget",
    "validate_change",
)
