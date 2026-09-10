# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T6 (AC10): per-turn limits derived from durable baselines; failed turn ≠ dead Agent."""

from __future__ import annotations

import asyncio

from provider_fixture import MODEL, ScriptedProvider
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import thaw_json
from simple_harness.execution.uow import RunState


def _ports(tmp_path, provider, executor=None, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="limits-owner",
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


def _checkpoint(uow, run_id):
    stored = uow.read_react_checkpoint(run_id)
    return None if stored is None else thaw_json(stored.checkpoint)


TOOL = ("echo", {})


def test_per_turn_model_call_limit_fails_the_turn_not_the_agent(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, TOOL, "下一轮正常"])
        executor = EchoToolExecutor()
        async with build_agent_runtime(_ports(tmp_path, provider, executor)) as runtime:
            agent = await runtime.create(_config(max_model_calls_per_turn=2), creation_key="m")
            failed = await agent.ask("循环", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "react_max_turns_exceeded"
            assert provider.calls == 2
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            checkpoint = _checkpoint(runtime.uow, agent.run_id)
            assert checkpoint["phase"] == "ready"
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED
            assert ok.public_output.content == "下一轮正常"
            assert provider.calls == 3
            # The failed turn's leftover tool call was never resumed under the new input.
            assert executor.calls == ["echo", "echo"]

    asyncio.run(case())


def test_per_turn_limits_do_not_reset_lifetime_totals(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, "第二轮"])
        async with build_agent_runtime(_ports(tmp_path, provider, EchoToolExecutor())) as runtime:
            agent = await runtime.create(_config(max_model_calls_per_turn=1), creation_key="t")
            first = await agent.ask("一", input_id="i1", timeout=5)
            assert first.state is AgentTurnState.FAILED
            after_first = _checkpoint(runtime.uow, agent.run_id)
            assert after_first["provider_turns_reserved_total"] == 1
            assert after_first["tool_calls_reserved_total"] == 1
            second = await agent.ask("二", input_id="i2", timeout=5)
            assert second.state is AgentTurnState.COMMITTED
            after_second = _checkpoint(runtime.uow, agent.run_id)
            assert after_second["provider_turns_reserved_total"] == 2
            s1 = agent.turn_snapshot(first.turn_id)
            s2 = agent.turn_snapshot(second.turn_id)
            assert s1.provider_turn_ordinal_from == 0 and s1.provider_turn_ordinal_to == 1
            assert s2.provider_turn_ordinal_from == 1 and s2.provider_turn_ordinal_to == 2
            assert s2.provider_turn_ordinal_from > s1.provider_turn_ordinal_from

    asyncio.run(case())


def test_per_turn_tool_call_limit_is_enforced(tmp_path):
    async def case():
        provider = ScriptedProvider([TOOL, TOOL, "好"])
        async with build_agent_runtime(_ports(tmp_path, provider, EchoToolExecutor())) as runtime:
            agent = await runtime.create(
                _config(max_tool_calls_per_turn=1, max_model_calls_per_turn=8), creation_key="c"
            )
            failed = await agent.ask("工具", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "react_max_tool_calls_exceeded"
            assert provider.calls == 2  # second response proposed the 2nd tool call
            assert _checkpoint(runtime.uow, agent.run_id)["phase"] == "ready"
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED and ok.public_output.content == "好"

    asyncio.run(case())


def test_turn_deadline_uses_first_durable_admission_and_restart_does_not_extend_it(tmp_path):
    async def case():
        clock = {"now": 10.0}
        provider = ScriptedProvider(["第一轮"])
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agent = await runtime.create(_config(turn_deadline_seconds=1.0), creation_key="d")
            first = await agent.ask("快", input_id="i1", timeout=5)
            assert first.state is AgentTurnState.COMMITTED

            async def no_wake(run_id: str) -> None:
                del run_id

            runtime.kernel._wake_continuation = no_wake  # type: ignore[method-assign]
            receipt = await agent.submit("慢", input_id="i2")
            assert agent.turn_state(receipt.turn_id) is AgentTurnState.QUEUED
        clock["now"] = 100.0  # lease expired and deadline long gone
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="limits-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            reopened = await restarted.open(agent.agent_id)
            result = await reopened.wait_turn(receipt.turn_id, timeout=5)
            assert result.state is AgentTurnState.FAILED
            assert result.error["error_code"] == "react_wall_clock_exceeded"
            assert provider.calls == 1  # no provider call for the expired turn
            snapshot = reopened.turn_snapshot(receipt.turn_id)
            assert snapshot.created_at == 10.0
        # Control: a generous deadline lets the same restart scenario succeed.
        clock["now"] = 10.0
        provider2 = ScriptedProvider(["慢也行"])
        runtime2 = build_agent_runtime(
            _ports(
                tmp_path,
                provider2,
                owner_id="limits-owner-3",
                clock=lambda: clock["now"],
                database_path=str(tmp_path / "second.db"),
            )
        )
        async with runtime2:
            agent2 = await runtime2.create(_config(turn_deadline_seconds=1000.0), creation_key="d")
            runtime2.kernel._wake_continuation = no_wake  # type: ignore[method-assign]
            receipt2 = await agent2.submit("慢", input_id="i1")
        clock["now"] = 100.0
        runtime3 = build_agent_runtime(
            _ports(
                tmp_path,
                provider2,
                owner_id="limits-owner-4",
                clock=lambda: clock["now"],
                database_path=str(tmp_path / "second.db"),
            )
        )
        async with runtime3:
            reopened2 = await runtime3.open(agent2.agent_id)
            ok = await reopened2.wait_turn(receipt2.turn_id, timeout=5)
            assert ok.state is AgentTurnState.COMMITTED

    asyncio.run(case())


def test_mid_turn_deadline_is_enforced_by_the_loop(tmp_path):
    async def case():
        clock = {"now": 10.0}

        def slow_tool():
            clock["now"] += 5.0

        provider = ScriptedProvider([TOOL, "不该到这"])
        executor = EchoToolExecutor(on_call=slow_tool)
        async with build_agent_runtime(
            _ports(tmp_path, provider, executor, clock=lambda: clock["now"])
        ) as runtime:
            agent = await runtime.create(_config(turn_deadline_seconds=2.0), creation_key="mid")
            # The wait deadline shares the fake clock the tool advances: keep it wide.
            failed = await agent.ask("慢工具", input_id="i1", timeout=60)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "react_wall_clock_exceeded"
            assert provider.calls == 1
            assert _checkpoint(runtime.uow, agent.run_id)["phase"] == "ready"

    asyncio.run(case())


def test_policy_fingerprint_is_unchanged_by_per_turn_limits(tmp_path):
    async def case():
        provider = ScriptedProvider(["a", "b"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            a = await runtime.create(_config(max_model_calls_per_turn=1), creation_key="a")
            b = await runtime.create(_config(max_model_calls_per_turn=8), creation_key="b")
            fingerprint = runtime.driver.policy_fingerprint
            assert fingerprint is not None
            for agent in (a, b):
                snapshot = runtime.uow.read_start_snapshot(agent.run_id)
                assert snapshot["policy_fingerprint"] == fingerprint
            await a.ask("问", input_id="i1", timeout=5)
            await b.ask("问", input_id="i1", timeout=5)
            for agent in (a, b):
                run = runtime.uow.read_run(agent.run_id)
                assert run is not None and run.state is RunState.WAITING

    asyncio.run(case())


def test_two_agents_with_different_per_turn_limits_do_not_interfere(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        executor = EchoToolExecutor()
        scripts = {}

        original = provider.invoke

        async def routed(request, *, cancel):
            # Route by the user text so concurrent agents get their own scripts.
            texts = [m.content for m in request.messages if isinstance(m.content, str)]
            key = "A" if any("给A" in t for t in texts) else "B"
            provider.script = scripts[key]
            return await original(request, cancel=cancel)

        provider.invoke = routed  # type: ignore[method-assign]
        async with build_agent_runtime(_ports(tmp_path, provider, executor)) as runtime:
            a = await runtime.create(_config(max_model_calls_per_turn=1), creation_key="A")
            b = await runtime.create(_config(max_model_calls_per_turn=8), creation_key="B")
            scripts["A"] = [TOOL, TOOL]
            scripts["B"] = [TOOL, TOOL, "B完成"]
            ra, rb = await asyncio.gather(
                a.ask("给A", input_id="i1", timeout=5), b.ask("给B", input_id="i1", timeout=5)
            )
            assert ra.state is AgentTurnState.FAILED
            assert rb.state is AgentTurnState.COMMITTED and rb.error is None
            assert rb.public_output.content == "B完成"

    asyncio.run(case())
