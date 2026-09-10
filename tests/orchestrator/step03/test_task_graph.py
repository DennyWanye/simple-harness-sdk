# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · slice A: the Graph Manager validates a whole proposal — order, cycles,
missing/self dependencies, duplicates, per-task and summed budgets, tools, shape."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import Budget, ContractError, Mission, MissionStatus
from agent_orchestrator.graph.dependency_checker import DependencyError, check_dependencies
from agent_orchestrator.graph.task_graph import GraphRejected, TaskGraphProposal, validate_graph

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")


def mission(**overrides):
    base = dict(
        id="mission-g",
        goal="实现两个功能并交付",
        success_criteria=("pytest:tests",),
        stop_conditions=(),
        allowed_tools=TOOLS,
        risk_level="sandbox",
        budget=Budget(max_tokens=100_000, max_attempts=3),
        tenant_id="t",
        status=MissionStatus.PLANNING,
        created_at=1.0,
        version=2,
        idempotency_key="g",
    )
    base.update(overrides)
    return Mission(**base)


def node(key, deps=(), tokens=10_000, **overrides):
    base = dict(
        key=key,
        goal=f"任务 {key}",
        rationale=f"{key} 是计划的一部分",
        dependencies=list(deps),
        success_criteria=[f"file:{key}.py"],
        verification_policy=["format_check", "rule_check", "code_test"],
        allowed_tools=list(TOOLS),
        budget={"max_tokens": tokens, "max_attempts": 2},
    )
    base.update(overrides)
    return base


DAG = {
    "tasks": [
        node("A"),
        node("B", ["A"]),
        node("C", ["A"]),
        node("D", ["B", "C"]),
        node("E", ["D"]),
    ]
}


def test_valid_dag_orders_roots_and_terminal():
    graph = validate_graph(mission(), TaskGraphProposal.from_json(DAG))
    assert graph.order == ("A", "B", "C", "D", "E")
    assert graph.roots == ("A",) and graph.leaves == ("E",) and graph.terminal_key == "E"
    # order is deterministic for the proposal order, not alphabetical
    shuffled = {"tasks": [node("C", ["A"]), node("B", ["A"]), node("A"), node("D", ["B", "C"])]}
    assert validate_graph(mission(), TaskGraphProposal.from_json(shuffled)).order == (
        "A",
        "C",
        "B",
        "D",
    )


def test_cycle_missing_and_self_dependencies_are_rejected():
    with pytest.raises(DependencyError) as exc:
        check_dependencies({"A": ("B",), "B": ("A",)})
    assert exc.value.reason == "cycle" and "A" in exc.value.detail
    with pytest.raises(DependencyError) as exc:
        check_dependencies({"A": ("Z",)})
    assert exc.value.reason == "missing_dependency"
    with pytest.raises(DependencyError) as exc:
        check_dependencies({"A": ("A",)})
    assert exc.value.reason == "self_dependency"
    bad = {"tasks": [node("A", ["B"]), node("B", ["A"])]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(bad))
    assert exc.value.reason == "cycle"


def test_duplicates_budget_sum_tools_and_shape():
    dup = {"tasks": [node("A"), node("A2", goal="任务 A")]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(dup))
    assert exc.value.reason == "duplicate"
    over = {"tasks": [node("A", tokens=60_000), node("B", ["A"], tokens=60_000)]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(over))
    assert exc.value.reason == "budget" and "sum" in exc.value.detail
    single_over = {"tasks": [node("A", tokens=200_000)]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(single_over))
    assert exc.value.reason == "budget"
    unbounded = {"tasks": [node("A", budget={"max_attempts": 1})]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(unbounded))
    assert exc.value.reason == "budget"  # unlimited child under a bounded parent (§18.2)
    tools = {"tasks": [node("A", allowed_tools=["shell"])]}
    with pytest.raises(GraphRejected) as exc:
        validate_graph(mission(), TaskGraphProposal.from_json(tools))
    assert exc.value.reason == "tools"
    with pytest.raises(GraphRejected):
        validate_graph(mission(), TaskGraphProposal.from_json({"tasks": []}))
    with pytest.raises(ContractError):
        TaskGraphProposal.from_json({"tasks": [{"key": "A"}]})
    with pytest.raises(ContractError):
        TaskGraphProposal.from_json({"tasks": [node("A")], "extra": 1})
