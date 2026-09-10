# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T8 (AC9 / AC11): cooperative cancel_turn; a failed turn keeps session and cost."""

from __future__ import annotations

import asyncio
import json

from provider_fixture import MODEL, ScriptedProvider, message_texts
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.execution.uow import ContinuationState, RunState
from simple_harness.runtime.context import SqliteContextPort

TOOL = ("echo", {})


def _ports(tmp_path, provider, executor=None, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="cancel-owner",
    )
    if executor is not None:
        base.update(
            tool_executor=executor, tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA}
        )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(**limits):
    return AgentConfig(
        name="w",
        instructions="你是助手。",
        model_profile_ref="p",
        tool_names=("echo",),
        limits=AgentLimits(**limits),
    )


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


def _generation(uow, agent_id):
    return _rows(
        uow, "SELECT control_generation FROM base_agent_bindings_v1 WHERE agent_id=?", agent_id
    )[0][0]


def test_cancel_turn_ends_the_turn_without_killing_the_agent(tmp_path):
    async def case():
        # Cooperative cancel is observed at the loop's cancel points (before a provider
        # call and after a tool batch), never by interrupting an in-flight call.
        provider = ScriptedProvider([TOOL, "下一轮"], blocked=True)
        executor = EchoToolExecutor()
        async with build_agent_runtime(_ports(tmp_path, provider, executor)) as runtime:
            agent = await runtime.create(_config(), creation_key="c1")
            turn = await agent.submit("慢一点", input_id="i1")
            await asyncio.sleep(0.05)  # the first provider call is in flight (blocked)
            receipt_task = asyncio.create_task(agent.cancel_turn(turn.turn_id, command_id="x1"))
            await asyncio.sleep(0.05)
            provider.allow.set()  # the call returns a tool call; the loop then sees the cancel
            receipt = await receipt_task
            assert receipt.state == "cancelled"
            result = await agent.wait_turn(turn.turn_id, timeout=5)
            assert result.state is AgentTurnState.FAILED
            assert result.error["error_code"] == "agent_turn_cancelled"
            assert provider.calls == 1
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            assert runtime.uow.read_continuation(turn.turn_id).state is ContinuationState.ACKED
            assert agent.status().lifecycle == "IDLE"
            provider.blocked = False
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED and ok.public_output.content == "下一轮"
            assert provider.calls == 2


def test_cancel_that_loses_the_race_to_a_final_answer_is_already_settled(tmp_path):
    async def case():
        provider = ScriptedProvider(["最终答案"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="race")
            turn = await agent.submit("问", input_id="i1")
            await asyncio.sleep(0.05)
            task = asyncio.create_task(agent.cancel_turn(turn.turn_id, command_id="x1"))
            await asyncio.sleep(0.02)
            provider.allow.set()  # the final answer lands before any cancel point
            receipt = await task
            assert receipt.state == "already_settled"
            result = await agent.wait_turn(turn.turn_id, timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "最终答案"


def test_cancel_turn_is_idempotent_by_command_id(tmp_path):
    async def case():
        provider = ScriptedProvider(["答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c2")
            turn = await agent.submit("问", input_id="i1")
            await asyncio.sleep(0.05)
            first_task = asyncio.create_task(agent.cancel_turn(turn.turn_id, command_id="x1"))
            await asyncio.sleep(0.02)
            provider.allow.set()
            first = await first_task
            second = await agent.cancel_turn(turn.turn_id, command_id="x1")
            assert first == second
            commands = _rows(runtime.uow, "SELECT * FROM base_agent_control_commands_v1")
            assert len(commands) == 1
            assert _generation(runtime.uow, agent.agent_id) == 1
            assert first.control_generation == 1

    asyncio.run(case())


def test_cancel_after_result_pending_does_not_rewrite_the_fact(tmp_path):
    async def case():
        provider = ScriptedProvider(["原始成功结果"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c3")
            result = await agent.ask("问", input_id="i1", timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            receipt = await agent.cancel_turn(result.turn_id, command_id="late")
            assert receipt.state == "already_settled"
            assert agent.get_result(result.turn_id) == result
            stored = runtime.uow.read_agent_control_command("late")
            assert stored is not None and stored.kind == "cancel_turn"
            assert dict(stored.receipt)["state"] == "already_settled"
            assert stored.target_turn_id == result.turn_id

    asyncio.run(case())


def test_cancel_does_not_claim_to_undo_real_effects(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, TOOL, "不该到这"])
        executor = EchoToolExecutor()
        ready = asyncio.Event()
        release = asyncio.Event()
        original = provider.invoke

        async def gate_second_call(request, *, cancel):
            if provider.calls == 1:
                ready.set()
                await release.wait()  # hold the 2nd call until the cancel intent is durable
            return await original(request, cancel=cancel)

        provider.invoke = gate_second_call  # type: ignore[method-assign]
        async with build_agent_runtime(_ports(tmp_path, provider, executor)) as runtime:
            agent = await runtime.create(_config(), creation_key="c4")
            turn = await agent.submit("用工具", input_id="i1")
            await asyncio.wait_for(ready.wait(), 5)
            assert executor.calls == ["echo"]
            effects_before = _rows(
                runtime.uow,
                "SELECT effect_id, state FROM execution_effects WHERE run_id=?",
                agent.run_id,
            )
            assert len(effects_before) == 1
            task = asyncio.create_task(
                agent.cancel_turn(turn.turn_id, command_id="x1", wait_timeout=5)
            )
            await asyncio.sleep(0.02)
            release.set()
            receipt = await task
            assert receipt.state == "cancelled"
            result = await agent.wait_turn(turn.turn_id, timeout=5)
            assert result.state is AgentTurnState.FAILED
            assert "rolled_back" not in json.dumps(result.to_json())
            effects_after = _rows(
                runtime.uow,
                "SELECT effect_id, state FROM execution_effects WHERE run_id=?",
                agent.run_id,
            )
            assert effects_after == effects_before

    asyncio.run(case())


def test_cancel_bumps_control_generation(tmp_path):
    async def case():
        provider = ScriptedProvider(["答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c5")
            assert _generation(runtime.uow, agent.agent_id) == 0
            turn = await agent.submit("问", input_id="i1")
            await asyncio.sleep(0.05)
            task = asyncio.create_task(agent.cancel_turn(turn.turn_id, command_id="x1"))
            await asyncio.sleep(0.02)
            provider.allow.set()
            await task
            assert _generation(runtime.uow, agent.agent_id) == 1
            assert agent.status().control_generation == 1

    asyncio.run(case())


def test_durable_cancel_intent_survives_restart(tmp_path):
    async def case():
        provider = ScriptedProvider(["不该被调用"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agent = await runtime.create(_config(), creation_key="c6")
            for _ in range(100):  # let the creation drive go idle and release authority
                if agent.run_id not in runtime.kernel._leases:
                    break
                await asyncio.sleep(0.01)

            async def no_wake(run_id: str) -> None:
                del run_id

            runtime.kernel._wake_continuation = no_wake  # type: ignore[method-assign]
            turn = await agent.submit("排队", input_id="i1")
            receipt = await agent.cancel_turn(turn.turn_id, command_id="x1", wait_timeout=0)
            assert receipt.state == "pending"  # nothing is executing it yet
            assert agent.turn_state(turn.turn_id) is AgentTurnState.QUEUED
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="cancel-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            reopened = await restarted.open(agent.agent_id)
            result = await reopened.wait_turn(turn.turn_id, timeout=5)
            assert result.state is AgentTurnState.FAILED
            assert result.error["error_code"] == "agent_turn_cancelled"
            assert provider.calls == 0

    asyncio.run(case())


def test_turn_failure_keeps_session_and_cost(tmp_path):
    async def case():
        provider = ScriptedProvider(["第一轮答", TOOL, TOOL, "第三轮答"])
        async with build_agent_runtime(_ports(tmp_path, provider, EchoToolExecutor())) as runtime:
            agent = await runtime.create(_config(max_model_calls_per_turn=2), creation_key="c7")
            first = await agent.ask("第一轮", input_id="i1", timeout=5)
            assert first.state is AgentTurnState.COMMITTED
            context = SqliteContextPort(runtime.uow.database)
            revision_after_first = context.load(RunId(agent.run_id)).revision
            budget_after_first = runtime.kernel._ports.provider.read_provider_budget(
                RunId(agent.run_id)
            )
            second = await agent.ask("第二轮", input_id="i2", timeout=5)
            assert second.state is AgentTurnState.FAILED
            # (a) history keeps the first result; (b) Context did not roll back.
            assert [h["turn_id"] for h in agent.history()] == [first.turn_id, second.turn_id]
            snapshot = context.load(RunId(agent.run_id))
            assert snapshot.revision >= revision_after_first
            texts = [m.content for m in snapshot.messages if isinstance(m.content, str)]
            assert "第一轮" in texts and "第一轮答" in texts
            # (c) committed cost never decreases.
            budget_after_second = runtime.kernel._ports.provider.read_provider_budget(
                RunId(agent.run_id)
            )
            assert budget_after_second.committed_micros >= budget_after_first.committed_micros
            # (d) the third turn's request still carries the first conversation; (e) seqs.
            third = await agent.ask("第三轮", input_id="i3", timeout=5)
            assert third.state is AgentTurnState.COMMITTED
            last_request = message_texts(provider.requests[-1])
            assert "第一轮" in last_request and "第一轮答" in last_request
            seqs = [
                row[0]
                for row in _rows(
                    runtime.uow,
                    "SELECT seq FROM base_agent_turns_v1 WHERE agent_id=? ORDER BY seq",
                    agent.agent_id,
                )
            ]
            assert seqs == [1, 2, 3]

    asyncio.run(case())
