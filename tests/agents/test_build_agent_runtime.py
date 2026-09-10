# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T7: build_agent_runtime / AgentRuntime / BaseAgent without any user Memory."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from provider_fixture import MODEL, ScriptedProvider, message_texts

import simple_harness
from simple_harness.agents import (
    AgentConfig,
    AgentTurnState,
    AgentTurnTimeout,
    build_agent_runtime,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import RunId
from simple_harness.runtime.context import SqliteContextPort

SRC = Path(__file__).resolve().parents[2] / "src" / "simple_harness"


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="build-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config(name="worker", instructions="你是助手。"):
    return AgentConfig(name=name, instructions=instructions, model_profile_ref="profile-1")


def test_no_memory_entrypoint_is_called(tmp_path):
    async def case():
        provider = ScriptedProvider(["回答"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="m-1")
            result = await agent.ask("你好", input_id="r-1", timeout=5)
            assert result.public_output is not None and result.public_output.content == "回答"
            kernel_ports = runtime.kernel._ports
            assert kernel_ports.agent_memory is None
            assert kernel_ports.conversation_memory_enabled is False
            assert kernel_ports.memory_dispatcher is None
            assert kernel_ports.context_staging is None
            assert kernel_ports.context_preparation_mode is None
        assert "simple_harness_memory" not in sys.modules
        assert not any(name.startswith("simple_harness_memory") for name in sys.modules)
        for path in (SRC / "agents").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "import simple_harness_memory" not in source, path
            assert "from simple_harness_memory" not in source, path

    asyncio.run(case())


def test_create_returns_independent_agents(tmp_path):
    async def case():
        provider = ScriptedProvider(["只有 0 号回答"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agents = await runtime.create_many([_config()] * 3, batch_key="b1")
            ids = {agent.agent_id for agent in agents}
            runs = {agent.run_id for agent in agents}
            assert len(ids) == 3 and len(runs) == 3
            assert provider.calls == 0
            assert all(agent.status().lifecycle == "IDLE" for agent in agents)
            await agents[0].ask("问", input_id="i1", timeout=5)
            context = SqliteContextPort(runtime.uow.database)
            assert context.load(RunId(agents[0].run_id)).revision >= 1
            for other in agents[1:]:
                assert context.load(RunId(other.run_id)).revision == 0
                assert runtime.uow.list_agent_turns(other.agent_id) == ()
            # Replaying the same batch returns the same identities (no new Agents).
            again = await runtime.create_many([_config()] * 3, batch_key="b1")
            assert [a.agent_id for a in again] == [a.agent_id for a in agents]
            with pytest.raises(ValueError):
                await runtime.create(_config(instructions="其它"), creation_key="b1:0")

    asyncio.run(case())


def test_create_does_not_call_provider(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agents = await runtime.create_many([_config()] * 3, batch_key="quiet")
            await asyncio.sleep(0.05)
            for agent in agents:
                await runtime.kernel.wait_idle(RunId(agent.run_id))
            assert provider.calls == 0
            assert runtime.uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turns_v1"
            ).fetchone()[0] == 0

    asyncio.run(case())


def test_submit_reschedules_the_run(tmp_path):
    async def case():
        provider = ScriptedProvider(["好"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="s-1")
            kernel = runtime.kernel
            schedules: list[str] = []
            wakes: list[str] = []
            original_schedule = kernel._schedule
            original_wake = kernel._wake_continuation

            def spy_schedule(run_id: str) -> None:
                schedules.append(run_id)
                original_schedule(run_id)

            async def spy_wake(run_id: str) -> None:
                wakes.append(run_id)
                await original_wake(run_id)

            kernel._schedule = spy_schedule  # type: ignore[method-assign]
            kernel._wake_continuation = spy_wake  # type: ignore[method-assign]
            receipt = await agent.submit("问", input_id="i1")
            assert receipt.state is AgentTurnState.QUEUED and receipt.seq == 1
            result = await agent.wait_turn(receipt.turn_id, timeout=5)
            assert result.public_output.content == "好"
            assert wakes.count(agent.run_id) == 1
            assert schedules.count(agent.run_id) >= 1
            # Same input_id replays the receipt; different content under it conflicts.
            replay = await agent.submit("问", input_id="i1")
            assert replay.turn_id == receipt.turn_id
            from simple_harness.agents import AgentInputConflict

            with pytest.raises(AgentInputConflict):
                await agent.submit("另一个问题", input_id="i1")

    asyncio.run(case())


def test_queued_turn_survives_restart(tmp_path):
    async def case():
        provider = ScriptedProvider(["重启后才回答"])
        clock = {"now": 10.0}
        ports = _ports(tmp_path, provider, clock=lambda: clock["now"])
        runtime = build_agent_runtime(ports)
        async with runtime:
            agent = await runtime.create(_config(), creation_key="q-1")

            async def no_wake(run_id: str) -> None:  # the process dies before waking
                del run_id

            runtime.kernel._wake_continuation = no_wake  # type: ignore[method-assign]
            receipt = await agent.submit("等我回来", input_id="i1")
            assert agent.turn_state(receipt.turn_id) is AgentTurnState.QUEUED
            assert provider.calls == 0
        clock["now"] = 100.0  # lease expired
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="build-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            reopened = await restarted.open(agent.agent_id)
            result = await reopened.wait_turn(receipt.turn_id, timeout=5)
            assert result.public_output.content == "重启后才回答"
            assert provider.calls == 1
            assert reopened.status().lifecycle == "IDLE"

    asyncio.run(case())


def test_ask_timeout_does_not_cancel_the_turn(tmp_path):
    async def case():
        provider = ScriptedProvider(["慢回答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="t-1")
            with pytest.raises(AgentTurnTimeout) as info:
                await agent.ask("慢一点", input_id="i1", timeout=0.05)
            turn_id = info.value.turn_id
            assert agent.turn_state(turn_id) in {AgentTurnState.QUEUED, AgentTurnState.RUNNING}
            provider.allow.set()
            result = await agent.wait_turn(turn_id, timeout=5)
            assert result.public_output.content == "慢回答"
            assert provider.calls == 1

    asyncio.run(case())


def test_instructions_reach_the_model_and_history_is_per_agent(tmp_path):
    async def case():
        provider = ScriptedProvider(["A1", "B1"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            a = await runtime.create(_config(instructions="你是 A。"), creation_key="a")
            b = await runtime.create(_config(instructions="你是 B。"), creation_key="b")
            await a.ask("问 A", input_id="i1", timeout=5)
            await b.ask("问 B", input_id="i1", timeout=5)
            first, second = provider.requests
            assert "你是 A。" in message_texts(first) and "你是 B。" not in message_texts(first)
            assert "你是 B。" in message_texts(second) and "问 A" not in message_texts(second)
            assert len(a.history()) == 1 and len(b.history()) == 1
            assert a.turn_id_for("i1") != b.turn_id_for("i1")

    asyncio.run(case())


def test_import_purity_still_holds():
    assert simple_harness.__all__[0] == "__version__"
    assert len(simple_harness.__all__) == len(set(simple_harness.__all__))
    code = (
        "import sys, simple_harness\n"
        "assert 'simple_harness.agents' not in sys.modules\n"
        "assert 'simple_harness.execution.sqlite' not in sys.modules\n"
        "simple_harness.BaseAgent\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                            cwd=str(SRC.parent))
    assert result.returncode == 0, result.stderr
