# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 5 independent-review regressions (K1 / C1 / E1 / E2 / A1).

K1: a driver exception inside an admitted turn is a visible failed turn and the Agent
survives; an exception before admission re-wakes the Run through the drain loop a
bounded number of times.  C1: a parent waiting on its child gives its tool permit
back, so ``max_concurrent_tool_calls=1`` cannot deadlock a delegation whose child
needs a tool.  E1: a cancel between two output-cap attempts is observed.  E2: the
escalation history is reported on a committed turn too.
"""

from __future__ import annotations

import asyncio

from provider_fixture import MODEL, ScriptedProvider
from test_provider_response_durability import LengthExhausted
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState, build_agent_runtime
from simple_harness.agents.execution import AgentExecutionDriver
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME
from simple_harness.execution.uow import RunState
from simple_harness.runtime.kernel import BASE_AGENT_DRIVER_EXCEPTION_WAKES


def _ports(tmp_path, provider, *, executor=None, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "review.db"),
        model=MODEL,
        owner_id="review-owner",
        child_instructions_template="你是被委派的工作 Agent。先用 echo 工具，再给结论。",
    )
    if executor is not None:
        base.update(
            tool_executor=executor, tool_names=("echo",), tool_schemas={"echo": ECHO_SCHEMA}
        )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(tools=(), **limits):
    return AgentConfig(
        name="w",
        instructions="你是助手。",
        model_profile_ref="p",
        tool_names=tools,
        limits=AgentLimits(**limits),
    )


def test_driver_exception_inside_a_turn_is_a_visible_failed_turn(tmp_path, monkeypatch):
    """K1: the Agent survives its own driver blowing up mid-turn."""

    async def case():
        provider = ScriptedProvider(["第二轮"])
        original = AgentExecutionDriver._run_turn
        blows = {"n": 0}

        async def blow_up_once(self, *args, **kwargs):
            if blows["n"] == 0:
                blows["n"] += 1
                raise RuntimeError("injected driver failure")
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(AgentExecutionDriver, "_run_turn", blow_up_once)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="k1")
            failed = await agent.ask("问", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "base_agent_driver_exception"
            assert failed.error["error_type"] == "RuntimeError"
            assert "injected" in failed.error["message"]
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            ok = await agent.ask("再问", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED
            assert ok.public_output.content == "第二轮"
            assert provider.calls == 1

    asyncio.run(case())


def test_driver_exception_escaping_the_driver_is_rewoken_not_stranded(tmp_path, monkeypatch):
    """K1: an exception the driver cannot turn into a failed turn re-wakes the Run."""

    async def case():
        provider = ScriptedProvider(["活着"])
        original = AgentExecutionDriver.start
        blows = {"n": 0}

        async def blow_up_once(self, invocation, **kwargs):
            if blows["n"] == 0 and invocation.continuations:
                blows["n"] += 1
                raise RuntimeError("escaped the driver boundary")
            return await original(self, invocation, **kwargs)

        monkeypatch.setattr(AgentExecutionDriver, "start", blow_up_once)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="k1b")
            result = await agent.ask("问", input_id="i1", timeout=5)
            assert blows["n"] == 1
            assert result.state is AgentTurnState.COMMITTED
            assert runtime.kernel._driver_exception_wakes.get(agent.run_id) is None

    asyncio.run(case())


def test_repeated_driver_exceptions_stop_rewaking_after_the_cap(tmp_path, monkeypatch):
    async def case():
        provider = ScriptedProvider([])
        calls = {"n": 0}
        original = AgentExecutionDriver.start

        async def guarded(self, invocation, **kwargs):
            if invocation.continuations:
                calls["n"] += 1
                raise RuntimeError("permanent")
            return await original(self, invocation, **kwargs)

        monkeypatch.setattr(AgentExecutionDriver, "start", guarded)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="k1c")
            await agent.submit("问", input_id="i1")
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if (
                    runtime.kernel._driver_exception_wakes.get(agent.run_id, 0)
                    > BASE_AGENT_DRIVER_EXCEPTION_WAKES
                ):
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            assert calls["n"] == BASE_AGENT_DRIVER_EXCEPTION_WAKES + 1
            assert agent.run_id not in runtime.kernel._pending_wakes
            run = runtime.uow.read_run(agent.run_id)
            # Authority dropped, input still claimed: non-terminal, recover() re-drives.
            assert run is not None and run.state in {RunState.RUNNING, RunState.WAITING}
            assert provider.calls == 0

    asyncio.run(case())


def test_delegation_completes_under_a_tool_cap_of_one(tmp_path):
    """C1: the parent's wait must not hold the only tool permit the child needs."""

    async def case():
        provider = ScriptedProvider(
            [
                (DELEGATE_TOOL_NAME, {"objective": "查一下", "delegation_id": "d-1"}),
                ("echo", {}),  # child turn-1 uses the (only) tool permit
                "结论：Y",  # child final
                "综合：Y",  # parent final
            ]
        )
        executor = EchoToolExecutor()
        async with build_agent_runtime(
            _ports(tmp_path, provider, executor=executor, max_concurrent_tool_calls=1)
        ) as runtime:
            main = await runtime.create(
                _config(tools=(DELEGATE_TOOL_NAME, "echo"), max_delegations_per_turn=1),
                creation_key="c1",
            )
            result = await asyncio.wait_for(main.ask("复杂任务", input_id="i1", timeout=10), 15)
            assert result.state is AgentTurnState.COMMITTED
            assert result.public_output.content == "综合：Y"
            assert result.delegation_count == 1
            assert executor.calls == ["echo"]
            registry = runtime.kernel._ports.tools._registry
            assert registry.max_tools_in_flight == 1 and registry.tools_in_flight == 0
            assert provider.calls == 4

    asyncio.run(case())


def test_cancel_between_output_cap_attempts_is_observed(tmp_path):
    """E1: cancel_turn landing between two escalation attempts fails the turn."""

    async def case():
        provider = LengthExhausted([], fail_times=10)
        original = provider.invoke
        seen = {"n": 0}
        runtime_ref = {}

        async def invoke(request, *, cancel):
            seen["n"] += 1
            response = await original(request, cancel=cancel)
            if seen["n"] == 1:
                agent = runtime_ref["agent"]
                receipt = await runtime_ref["runtime"].cancel_turn(
                    agent.agent_id, agent.turn_id_for("i1"), command_id="c-1", wait_timeout=0.0
                )
                assert receipt.state == "pending"
            return response

        provider.invoke = invoke  # type: ignore[method-assign]
        async with build_agent_runtime(
            _ports(tmp_path, provider, default_max_output_tokens=1024, empty_response_retries=2)
        ) as runtime:
            agent = await runtime.create(_config(), creation_key="e1")
            runtime_ref.update(runtime=runtime, agent=agent)
            failed = await agent.ask("综合", input_id="i1", timeout=5)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "agent_turn_cancelled"
            assert seen["n"] == 1  # no second provider call after the cancel
            ok_provider = ScriptedProvider(["下一轮"])
            runtime._assembled.wire._inner = ok_provider  # type: ignore[attr-defined]
            ok = await agent.ask("再来", input_id="i2", timeout=5)
            assert ok.state is AgentTurnState.COMMITTED

    asyncio.run(case())


def test_escalation_history_is_reported_on_success(tmp_path):
    """E2: a committed turn that needed a cap escalation says so in the drive payload."""

    async def case():
        provider = LengthExhausted(["终于有正文了"], fail_times=1)
        payloads = []
        async with build_agent_runtime(
            _ports(tmp_path, provider, default_max_output_tokens=1024)
        ) as runtime:
            driver = runtime._assembled.driver
            original = driver.start

            async def capture(invocation, **kwargs):
                result = await original(invocation, **kwargs)
                payloads.append(result.payload)
                return result

            driver.start = capture  # type: ignore[method-assign]
            agent = await runtime.create(_config(), creation_key="e2")
            result = await agent.ask("综合一下", input_id="i1", timeout=5)
            assert result.state is AgentTurnState.COMMITTED
            success = [p for p in payloads if p.get("response_present")]
            assert success and success[-1]["output_cap_escalations"] == [
                {"attempt": 1, "max_output_tokens": 2048}
            ]

    asyncio.run(case())
