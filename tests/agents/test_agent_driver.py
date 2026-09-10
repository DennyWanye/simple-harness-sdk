# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T6: AgentExecutionDriver on the assembled runtime (no user Memory)."""

from __future__ import annotations

import asyncio

from kernel_fixture import create_agent, submit
from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness.agents.execution import build_agent_execution_driver
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.runtime import assemble_runtime
from simple_harness.contracts import RunId
from simple_harness.execution.budget import BudgetPolicy, FrozenPriceEstimator
from simple_harness.execution.uow import ContinuationState, RunState
from simple_harness.runtime.drivers.react import build_react_driver
from simple_harness.runtime.termination import TerminationLimits


def _ports(tmp_path, provider, **overrides):
    return AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "agents.db"),
        model=MODEL,
        owner_id="agent-runtime-1",
        **overrides,
    )


async def _settle(runtime, run_id: str) -> None:
    await asyncio.sleep(0.05)
    await runtime.wait_idle(RunId(run_id))


def test_create_without_input_returns_waiting_without_provider(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-idle")
            run = uow.read_run("agent-idle")
            assert run is not None and run.state is RunState.WAITING
            assert provider.calls == 0
            assert uow.list_agent_turns("agent-idle") == ()
            assert uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turn_results_v1"
            ).fetchone()[0] == 0

    asyncio.run(case())


def test_unexpected_continuation_kind_keeps_run_waiting_and_acks(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-odd")
            uow.enqueue_continuation(
                continuation_id="odd-1",
                run_id="agent-odd",
                payload={"kind": "child_terminal", "child_run_id": "x"},
                now=3.0,
            )
            asyncio.create_task(runtime._wake_continuation("agent-odd"))
            await _settle(runtime, "agent-odd")
            run = uow.read_run("agent-odd")
            assert run is not None and run.state is RunState.WAITING
            continuation = uow.read_continuation("odd-1")
            assert continuation is not None and continuation.state is ContinuationState.ACKED
            assert provider.calls == 0
            assert uow.list_agent_turns("agent-odd") == ()
            assert uow.database.connection.execute(
                "SELECT COUNT(*) FROM child_terminal_receipts"
            ).fetchone()[0] == 0
            kinds = [
                str(r[0]) for r in uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id='agent-odd'"
                )
            ]
            assert "run.failed" not in kinds and "run.completed" not in kinds

    asyncio.run(case())


def test_two_turns_same_agent_reuse_one_execution_identity(tmp_path):
    async def case():
        provider = ScriptedProvider(["第一轮回答", "第二轮回答"])
        assembled = assemble_runtime(_ports(tmp_path, provider))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-two")
            first = await submit(runtime, uow, agent_id="agent-two", input_id="i1", text="问题一")
            await _settle(runtime, "agent-two")
            second = await submit(runtime, uow, agent_id="agent-two", input_id="i2", text="问题二")
            await _settle(runtime, "agent-two")
            results = [uow.read_agent_turn_result(t.turn_id) for t in (first, second)]
            assert all(r is not None for r in results)
            assert [t.seq for t in uow.list_agent_turns("agent-two")] == [1, 2]
            assert provider.calls == 2
            second_texts = message_texts(provider.requests[1])
            assert "第一轮回答" in second_texts and "问题二" in second_texts
            kinds = [
                str(r[0]) for r in uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id='agent-two'"
                )
            ]
            assert not ({"run.completed", "run.failed", "run.cancelled"} & set(kinds))
            run = uow.read_run("agent-two")
            assert run is not None and run.state is RunState.WAITING
            from simple_harness.agents.contracts import AgentTurnResult

            decoded = AgentTurnResult.from_json(results[1].result_json)
            assert decoded.public_output is not None
            assert decoded.public_output.content == "第二轮回答"
            assert decoded.state.value == "committed" and decoded.delegation_count == 0

    asyncio.run(case())


def test_budget_exceeded_is_a_failed_turn_not_a_dead_agent(tmp_path):
    async def case():
        provider = ScriptedProvider(["只够一轮"])
        limits = TerminationLimits(max_turns=1, max_tool_calls=1)
        assembled = assemble_runtime(_ports(tmp_path, provider, termination_limits=limits))
        runtime, uow = assembled.runtime, assembled.uow
        async with runtime:
            await create_agent(runtime, uow, agent_id="agent-budget")
            first = await submit(runtime, uow, agent_id="agent-budget", input_id="i1", text="一")
            await _settle(runtime, "agent-budget")
            assert uow.read_agent_turn_result(first.turn_id) is not None
            second = await submit(runtime, uow, agent_id="agent-budget", input_id="i2", text="二")
            await _settle(runtime, "agent-budget")
            failed = uow.read_agent_turn_result(second.turn_id)
            assert failed is not None
            assert dict(failed.result_json)["state"] == "failed"
            turn = uow.read_agent_turn(second.turn_id)
            assert turn is not None and turn.phase == "failed"
            run = uow.read_run("agent-budget")
            assert run is not None and run.state is RunState.WAITING
            assert provider.calls == 1

    asyncio.run(case())


def test_agent_policy_fingerprint_differs_from_react():
    limits = TerminationLimits()
    budget = BudgetPolicy()
    estimator = FrozenPriceEstimator("v", "p", 0, 0)
    agent = build_agent_execution_driver(limits=limits, budget_policy=budget, estimator=estimator)
    react = build_react_driver(limits=limits, budget_policy=budget, estimator=estimator)
    assert agent.policy_fingerprint != react.policy_fingerprint
    assert agent.provider_budget_fingerprint == react.provider_budget_fingerprint
