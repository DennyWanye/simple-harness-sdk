# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 2 · T3.5 value milestone: ordered inputs, then close, then restart, still closed.

One command is the milestone::

    .venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py
"""

from __future__ import annotations

import asyncio

import pytest
from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness.agents import AgentClosed, AgentConfig, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.execution.uow import RunState


def _ports(tmp_path, provider, **overrides):
    base = dict(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id="lifecycle-owner",
    )
    base.update(overrides)
    return AgentRuntimePorts(**base)


def test_ordered_inputs_then_close_then_restart_still_closed(tmp_path):
    async def case():
        provider = ScriptedProvider(["A1", "B1"])
        clock = {"now": 10.0}
        runtime = build_agent_runtime(_ports(tmp_path, provider, clock=lambda: clock["now"]))
        async with runtime:
            agent = await runtime.create(
                AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p"),
                creation_key="life-1",
            )
            # 2. strict order
            r1, r2 = await asyncio.gather(
                agent.submit("第一条", input_id="i1"), agent.submit("第二条", input_id="i2")
            )
            assert (r1.seq, r2.seq) == (1, 2)
            out1 = await agent.wait_turn(r1.turn_id, timeout=5)
            out2 = await agent.wait_turn(r2.turn_id, timeout=5)
            assert (out1.public_output.content, out2.public_output.content) == ("A1", "B1")
            assert provider.calls == 2
            second = message_texts(provider.requests[1])
            assert "A1" in second and "第一条" in second and "第二条" in second
            # 3./4. close refuses new inputs
            receipt = await agent.close(command_id="c1")
            assert receipt.state == "closed"
            with pytest.raises(AgentClosed):
                await agent.submit("第三条", input_id="i3")
            turns = runtime.uow.database.connection.execute(
                "SELECT COUNT(*) FROM base_agent_turns_v1 WHERE agent_id=?", (agent.agent_id,)
            ).fetchone()[0]
            assert turns == 2
            # 5. not dead
            run = runtime.uow.read_run(agent.run_id)
            assert run is not None and run.state is RunState.WAITING
            kinds = {
                str(row[0])
                for row in runtime.uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id=?", (agent.run_id,)
                ).fetchall()
            }
            assert not (kinds & {"run.completed", "run.failed", "run.cancelled"}), kinds
            assert "run.started" in kinds or len(kinds) > 0
        # 6. restart: still closed, history intact, no extra model calls
        clock["now"] = 100.0
        restarted = build_agent_runtime(
            _ports(tmp_path, provider, owner_id="lifecycle-owner-2", clock=lambda: clock["now"])
        )
        async with restarted:
            reopened = await restarted.open(agent.agent_id)
            with pytest.raises(AgentClosed):
                await reopened.submit("第三条", input_id="i3")
            assert len(reopened.history()) == 2
            assert reopened.status().lifecycle == "CLOSED"
            await restarted.recover_pending_turns()
            assert provider.calls == 2

    asyncio.run(case())
