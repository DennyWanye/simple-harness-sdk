# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T3: immutable configuration, turn contracts and the AgentId/RunId bridge."""

from __future__ import annotations

import ast
import dataclasses
import importlib
import sys
from pathlib import Path

import pytest

from simple_harness import Message, MessageRole, RunId
from simple_harness.agents import (
    AGENT_CONFIG_FIELDS,
    AgentConfig,
    AgentId,
    AgentLimits,
    AgentTurnResult,
    AgentTurnState,
    agent_id_for_run,
    config_hash,
    run_id_for_agent,
)
from simple_harness.runtime.agent_turn import AgentTurnOutcome, result_json_hash

SRC = Path(__file__).resolve().parents[2] / "src" / "simple_harness"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_model_calls_per_turn": True},
        {"max_model_calls_per_turn": -1},
        {"max_model_calls_per_turn": 0},
        {"turn_deadline_seconds": float("nan")},
        {"turn_deadline_seconds": -5.0},
        {"delegation_wait_seconds": float("inf")},
        {"max_delegations_per_turn": 0},
        {"lifetime_cost_limit_micros": True},
        {"lifetime_model_calls": 1, "max_model_calls_per_turn": 2},
    ],
)
def test_limits_reject_bool_negative_nan(kwargs):
    with pytest.raises((ValueError, TypeError)):
        AgentLimits(**kwargs)


def test_config_hash_is_stable_and_order_insensitive():
    a = AgentConfig(
        name="worker", instructions="do", model_profile_ref="p", tool_names=("x", "y"),
        limits=AgentLimits(max_tool_calls_per_turn=5),
    )
    b = AgentConfig(
        limits=AgentLimits(max_tool_calls_per_turn=5), tool_names=("x", "y"),
        model_profile_ref="p", instructions="do", name="worker",
    )
    assert config_hash(a) == config_hash(b)
    assert len(config_hash(a)) == 64
    c = AgentConfig(name="worker", instructions="do!", model_profile_ref="p", tool_names=("x", "y"),
                    limits=AgentLimits(max_tool_calls_per_turn=5))
    assert config_hash(c) != config_hash(a)
    assert AgentConfig.from_json(a.to_json()) == a


def test_config_rejects_user_memory_fields():
    names = {field.name for field in dataclasses.fields(AgentConfig)}
    assert names == AGENT_CONFIG_FIELDS
    assert not ({"user_memory", "mission_id", "task_id"} & names)
    with pytest.raises(TypeError):
        AgentConfig(  # type: ignore[call-arg]
            name="a", instructions="", model_profile_ref="p", mission_id="m"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "x" * 129},
        {"tool_names": ("a", "a")},
        {"tool_names": "not-a-tuple"},
        {"short_memory_mode": "ram"},
        {"model_profile_ref": " "},
    ],
)
def test_config_rejects_invalid_values(kwargs):
    base = {"name": "n", "instructions": "", "model_profile_ref": "p"}
    base.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        AgentConfig(**base)  # type: ignore[arg-type]


def test_agent_id_run_id_roundtrip():
    agent = AgentId("agent-00000001")
    assert agent_id_for_run(run_id_for_agent(agent)) == agent
    assert run_id_for_agent(agent) == RunId("agent-00000001")
    with pytest.raises(TypeError):
        run_id_for_agent("agent-00000001")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        agent_id_for_run("agent-00000001")  # type: ignore[arg-type]


def test_agent_turn_outcome_has_no_agents_dependency():
    for name in [key for key in sys.modules if key.startswith("simple_harness.agents")]:
        del sys.modules[name]
    importlib.import_module("simple_harness.runtime.agent_turn")
    assert not any(key.startswith("simple_harness.agents") for key in sys.modules)
    for relative in ("runtime/agent_turn.py", "runtime/kernel.py"):
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("simple_harness.agents") for alias in node.names
                )
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("simple_harness.agents"), relative


def test_result_hash_roundtrip():
    result = AgentTurnResult(
        turn_id="t-1", agent_id="a-1", seq=1, state=AgentTurnState.COMMITTED,
        public_output=Message(MessageRole.ASSISTANT, "结论：Y"), usage_refs=("u-1",),
        delegation_count=1,
    )
    outcome = result.to_outcome(input_id="i-1", input_hash="0" * 64)
    assert outcome.result_hash == result.result_hash == result_json_hash(result.to_json())
    restored = AgentTurnResult.from_outcome(outcome)
    assert restored == result
    assert restored.to_json() == result.to_json()
    with pytest.raises(ValueError):
        AgentTurnOutcome("a", "t", "i", "0" * 64, "1" * 64, {"x": 1})
    with pytest.raises(ValueError):
        AgentTurnResult(turn_id="t", agent_id="a", seq=1, state=AgentTurnState.FAILED)
    with pytest.raises(ValueError):
        AgentTurnResult(turn_id="t", agent_id="a", seq=1, state=AgentTurnState.QUEUED)


def test_termination_limits_come_from_lifetime_totals():
    limits = AgentLimits(lifetime_model_calls=50, lifetime_tool_calls=70, max_model_calls_per_turn=5)
    termination = limits.termination_limits()
    assert (termination.max_turns, termination.max_tool_calls) == (50, 70)
