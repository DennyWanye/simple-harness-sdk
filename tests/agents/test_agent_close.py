# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T3 (AC1 / BA12): close refuses new inputs, never kills the Run, survives restart."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents import (
    AgentClosed,
    AgentClosingReceipt,
    AgentConfig,
    AgentNotFound,
    AgentTurnState,
    build_agent_runtime,
)
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.execution.uow import RunState

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="close-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def _config():
    return AgentConfig(name="worker", instructions="你是助手。", model_profile_ref="profile-1")


def _rows(uow, sql, *params):
    return uow.database.connection.execute(sql, params).fetchall()


def _terminal_event_kinds(uow, run_id):
    rows = _rows(uow, "SELECT kind FROM run_events WHERE run_id=?", run_id)
    return {str(row[0]) for row in rows} & {f"run.{name}" for name in TERMINAL_EVENTS}


def _binding_row(uow, agent_id):
    return _rows(
        uow,
        "SELECT lifecycle, control_generation FROM base_agent_bindings_v1 WHERE agent_id=?",
        agent_id,
    )[0]


def test_close_rejects_new_input_and_keeps_the_agent_non_terminal(tmp_path):
    async def case():
        provider = ScriptedProvider(["回答一"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-1")
            await agent.ask("先问一句", input_id="i1", timeout=5)
            receipt = await agent.close(command_id="cmd-1")
            assert isinstance(receipt, AgentClosingReceipt)
            assert receipt.state == "closed" and receipt.open_turn_id is None
            assert receipt.control_generation == 1
            with pytest.raises(AgentClosed):
                await agent.submit("再问一句", input_id="i2")
            assert len(_rows(runtime.uow, "SELECT 1 FROM base_agent_turns_v1")) == 1
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            assert _terminal_event_kinds(runtime.uow, agent.run_id) == set()
            assert agent.status().lifecycle == "CLOSED"
            assert agent.status().lifecycle_state == "closed"
            assert provider.calls == 1

    asyncio.run(case())


def test_close_is_idempotent_by_command_id(tmp_path):
    async def case():
        provider = ScriptedProvider([])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-2")
            first = await agent.close(command_id="cmd-1")
            second = await agent.close(command_id="cmd-1")
            assert first == second
            commands = _rows(runtime.uow, "SELECT * FROM base_agent_control_commands_v1")
            assert len(commands) == 1
            assert tuple(_binding_row(runtime.uow, agent.agent_id)) == ("closed", 1)
            third = await agent.close(command_id="cmd-2")
            assert third.state == "closed" and third.control_generation == 1
            assert tuple(_binding_row(runtime.uow, agent.agent_id)) == ("closed", 1)
            assert len(_rows(runtime.uow, "SELECT * FROM base_agent_control_commands_v1")) == 2
            stored = runtime.uow.read_agent_control_command("cmd-2")
            assert stored is not None and stored.kind == "close"

    asyncio.run(case())


def test_close_drains_the_open_turn_and_returns_a_queryable_receipt(tmp_path):
    async def case():
        provider = ScriptedProvider(["慢答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-3")
            turn = await agent.submit("慢一点", input_id="i1")
            await asyncio.sleep(0.05)  # let the driver claim the turn
            receipt = await agent.close(command_id="cmd-1", drain_timeout=0.05)
            assert receipt.state == "closing"
            assert receipt.open_turn_id == turn.turn_id
            assert agent.status().lifecycle == "RUNNING"
            assert agent.status().lifecycle_state == "closing"
            with pytest.raises(AgentClosed):
                await agent.submit("插队", input_id="i2")
            provider.allow.set()
            result = await agent.wait_turn(turn.turn_id, timeout=5)
            assert result.public_output.content == "慢答"
            assert agent.turn_state(turn.turn_id) is AgentTurnState.COMMITTED
            again = await agent.close(command_id="cmd-2")
            assert again.state == "closed" and again.open_turn_id is None
            assert again.control_generation == 1
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING

    asyncio.run(case())


def test_closed_agent_survives_restart(tmp_path):
    async def case():
        provider = ScriptedProvider(["第一答"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agent = await runtime.create(_config(), creation_key="c-4")
            first = await agent.ask("问一", input_id="i1", timeout=5)
            await agent.close(command_id="cmd-1")
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="close-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            reopened = await restarted.open(agent.agent_id)
            with pytest.raises(AgentClosed):
                await reopened.submit("问二", input_id="i2")
            assert reopened.get_result(first.turn_id) == first
            assert len(reopened.history()) == 1
            assert reopened.status().lifecycle == "CLOSED"
            assert provider.calls == 1
            await restarted.recover_pending_turns()
            assert provider.calls == 1

    asyncio.run(case())


def test_shutdown_does_not_close_any_agent(tmp_path):
    async def case():
        provider = ScriptedProvider(["a", "b", "c"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agents = await runtime.create_many([_config()] * 3, batch_key="s-1")
            await runtime.shutdown()
        with sqlite3.connect(str(tmp_path / "runtime.db")) as raw:
            rows = raw.execute(
                "SELECT lifecycle, control_generation FROM base_agent_bindings_v1 ORDER BY agent_id"
            ).fetchall()
        assert rows == [("open", 0)] * 3
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="close-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            for index, agent in enumerate(agents):
                reopened = await restarted.open(agent.agent_id)
                result = await reopened.ask("问", input_id=f"i{index}", timeout=5)
                assert result.public_output.content == "abc"[index]

    asyncio.run(case())


def test_close_keeps_history_readable(tmp_path):
    async def case():
        provider = ScriptedProvider(["答一", "答二"])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-5")
            r1 = await agent.ask("问一", input_id="i1", timeout=5)
            r2 = await agent.ask("问二", input_id="i2", timeout=5)
            await agent.close(command_id="cmd-1")
            assert len(agent.history()) == 2
            assert agent.get_result(r1.turn_id) == r1
            assert agent.get_result(r2.turn_id) == r2
            assert agent.turn_state(r2.turn_id) is AgentTurnState.COMMITTED
            assert agent.status().committed_turns == 2

    asyncio.run(case())


def test_close_unknown_agent_is_not_found(tmp_path):
    async def case():
        async with build_agent_runtime(_ports(tmp_path, ScriptedProvider([]))) as runtime:
            with pytest.raises(AgentNotFound):
                await runtime.close_agent("agent-does-not-exist", command_id="cmd-1")

    asyncio.run(case())


def test_closing_converges_to_closed_when_the_open_turn_finalizes(tmp_path):
    """Challenge finding closing-to-closed-has-no-owner: no second close call is needed."""

    async def case():
        provider = ScriptedProvider(["慢答"], blocked=True)
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-6")
            turn = await agent.submit("慢一点", input_id="i1")
            await asyncio.sleep(0.05)
            receipt = await agent.close(command_id="cmd-1", drain_timeout=0.05)
            assert receipt.state == "closing"
            provider.allow.set()
            await agent.wait_turn(turn.turn_id, timeout=5)
            assert tuple(_binding_row(runtime.uow, agent.agent_id)) == ("closed", 1)
            assert agent.status().lifecycle == "CLOSED"

    asyncio.run(case())


def test_close_drain_conflict_is_bounded_not_a_spin(tmp_path):
    """Review F3: a conflicting mark_agent_closed must fall through to the bounded wait."""

    async def case():
        provider = ScriptedProvider([])
        async with build_agent_runtime(_ports(tmp_path, provider)) as runtime:
            agent = await runtime.create(_config(), creation_key="c-7")
            klass = type(runtime.uow)
            original = klass.mark_agent_closed
            calls = {"n": 0}

            def always_conflict(self, **kwargs):
                calls["n"] += 1
                from simple_harness.execution.uow import UnitOfWorkConflict

                raise UnitOfWorkConflict("agent still has an open turn")

            klass.mark_agent_closed = always_conflict  # type: ignore[method-assign]
            try:
                receipt = await asyncio.wait_for(
                    agent.close(command_id="cmd-1", drain_timeout=0.2), timeout=5
                )
            finally:
                klass.mark_agent_closed = original  # type: ignore[method-assign]
            assert receipt.state == "closing"
            assert calls["n"] >= 1
            again = await agent.close(command_id="cmd-2")
            assert again.state == "closed"

    asyncio.run(case())
