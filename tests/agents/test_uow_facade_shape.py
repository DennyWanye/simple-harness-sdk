# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T9 (L12): the two BaseAgent uow facades are thin; bodies live in base_agent.turns."""

from __future__ import annotations

import ast
import inspect
import textwrap

from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork


def _body_lines(function) -> tuple[int, int]:
    source = textwrap.dedent(inspect.getsource(function))
    tree = ast.parse(source)
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    lines = source.splitlines()
    first = body[0].lineno - 1
    last = max(getattr(stmt, "end_lineno", stmt.lineno) for stmt in body)
    code = [line for line in lines[first:last] if line.strip() and not line.strip().startswith("#")]
    transactions = sum("with self.database.transaction()" in line for line in code)
    return len(code), transactions


def test_submit_and_finalize_facades_are_thin():
    for name, limit in (
        ("submit_agent_input", 40),
        ("commit_agent_turn_result_and_idle", 40),
    ):
        lines, transactions = _body_lines(getattr(SqliteExecutionUnitOfWork, name))
        assert lines <= limit, (name, lines)
        assert transactions == 1, (name, transactions)


def test_fault_points_keep_their_slice_1_names():
    from simple_harness.execution.sqlite.base_agent import turns

    source = inspect.getsource(turns.finalize_turn)
    expected = [
        "agent_turn_finalize.result.before_write",
        "agent_turn_finalize.result.after_write",
        "agent_turn_finalize.continuation.before_write",
        "agent_turn_finalize.continuation.after_write",
        "agent_turn_finalize.run.before_write",
        "agent_turn_finalize.run.after_write",
    ]
    positions = [source.index(point) for point in expected]
    assert positions == sorted(positions)
    facade = inspect.getsource(SqliteExecutionUnitOfWork.commit_agent_turn_result_and_idle)
    assert "agent_turn_finalize.after_commit" in facade
