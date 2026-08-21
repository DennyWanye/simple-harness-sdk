# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from simple_harness.workflow.compiler import WorkflowCompileError, compile_workflow
from simple_harness.workflow.contracts import ChannelSpec, ReducerKind, StatePatch
from simple_harness.workflow.definition import (
    ConditionalEdge,
    Edge,
    NodeDefinition,
    WorkflowDefinition,
)


async def _noop(state, context):  # type: ignore[no-untyped-def]
    del state, context
    return StatePatch({})


def _definition(*, edges: tuple[Edge, ...] = (Edge("start", "finish"),), **changes):  # type: ignore[no-untyped-def]
    values = {
        "name": "demo",
        "version": "1",
        "state_schema_version": 1,
        "entry_node": "start",
        "nodes": (NodeDefinition("start", _noop), NodeDefinition("finish", _noop)),
        "channels": {
            "answer": ChannelSpec("string", ReducerKind.SINGLE_WRITER, frozenset({"finish"}))
        },
        "recursion_limit": 9,
        "max_supersteps": 8,
        "edges": edges,
    }
    values.update(changes)
    return WorkflowDefinition(**values)


def test_compile_is_deterministic_and_immutable() -> None:
    first = compile_workflow(_definition())
    second = compile_workflow(_definition())

    assert first.manifest.definition_hash == second.manifest.definition_hash
    assert first.manifest.implementation_bundle_hash == second.manifest.implementation_bundle_hash
    assert first.single_targets("start") == ("finish",)
    with pytest.raises(TypeError):
        first._nodes["other"] = first.node("start")  # type: ignore[index]


def test_compile_rejects_dangling_edges_and_duplicate_nodes() -> None:
    with pytest.raises(WorkflowCompileError, match="missing node"):
        compile_workflow(_definition(edges=(Edge("start", "missing"),)))
    with pytest.raises(WorkflowCompileError, match="Duplicate nodes"):
        compile_workflow(
            _definition(nodes=(NodeDefinition("start", _noop), NodeDefinition("start", _noop)))
        )


def test_compile_requires_explicit_budget_for_every_cycle() -> None:
    cyclic = _definition(edges=(Edge("start", "finish"), Edge("finish", "start")))
    with pytest.raises(WorkflowCompileError, match="requires explicit"):
        compile_workflow(cyclic)

    bounded = _definition(
        edges=(Edge("start", "finish"), Edge("finish", "start")),
        loop_budgets={"review": 2},
        loop_budget_bindings={"finish->start": "review"},
    )
    assert compile_workflow(bounded).is_cycle_edge("finish", "start")


def test_compile_validates_conditional_routes() -> None:
    def route(state, context):  # type: ignore[no-untyped-def]
        del state, context
        return "done"

    definition = _definition(
        edges=(),
        conditional_edges=(
            ConditionalEdge("start", route, {"done": "finish"}, selector_effect_policy="pure"),
        ),
    )
    compiled = compile_workflow(definition)
    assert compiled.conditional_for("start") is not None
