# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E4 (real model): while the frozen query is still SCANNING, every SearchPage is a
PROGRESS page (items=[], has_more=true — CONTEXT-SEARCH C5).  Handed to the model one page
per call, the model read "nothing found", re-searched from scratch with new words and ran
into the call cap.  The model tool now consumes the progress cursors itself inside one call
(each step is still one spec page) and hands the model the first RESULTS page."""

from __future__ import annotations

import asyncio
import json

from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp.tools import READ_TOOL_NAME, SEARCH_TOOL_NAME
from simple_harness.agents.contracts import AgentTurnState
from simple_harness import MessageRole

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p", tool_names=(SEARCH_TOOL_NAME, READ_TOOL_NAME))
SECRET = "工程暗号 是 蓝鲸七号，请牢记。"
FILLERS = [f"第{i}条闲聊：今天天气不错，我们聊聊别的话题 {i}。" for i in range(10)]


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def test_one_search_call_returns_results_not_a_progress_page(tmp_path) -> None:
    async def case() -> None:
        script = ["记住了。"] + ["好的。"] * len(FILLERS) + [(SEARCH_TOOL_NAME, {"query": "工程暗号"}), "暗号是蓝鲸七号。"]
        provider = ScriptedProvider(script)
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, SECRET, "i0")).state is AgentTurnState.COMMITTED
            for i, filler in enumerate(FILLERS, 1):
                assert (await _turn(agent, filler, f"i{i}")).state is AgentTurnState.COMMITTED
            assert (await _turn(agent, "请用工具查一下工程暗号。", "ask")).state is AgentTurnState.COMMITTED
            tool_messages = [m for m in provider.requests[-1].messages if m.role is MessageRole.TOOL]
            assert tool_messages, "the search result must reach the model"
            body = json.loads(tool_messages[-1].content)
            page = body.get("value", body)
            receipt = page["receipt"]
            # More than one scan step happened, yet the model saw the RESULTS page.
            assert receipt["coverage"]["phase"] == "RESULTS" and page["page_semantics"] != "PROGRESS"
            # Items are references (text comes from session_history_read); rank 1 is the early secret.
            assert page["items"] and page["items"][0]["source_group_ids"][0].endswith(":input:i0:context:user")
            assert provider.calls == len(script)

    asyncio.run(case())


def test_advancing_yields_to_the_event_loop_between_steps(tmp_path) -> None:
    """Review blocker: the advance loop must not hold the event loop (lease heartbeats)."""

    async def case() -> None:
        script = ["记住了。"] + ["好的。"] * len(FILLERS) + [(SEARCH_TOOL_NAME, {"query": "工程暗号"}), "暗号是蓝鲸七号。"]
        runtime = build(tmp_path, ScriptedProvider(script))
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, SECRET, "i0")).state is AgentTurnState.COMMITTED
            for i, filler in enumerate(FILLERS, 1):
                assert (await _turn(agent, filler, f"i{i}")).state is AgentTurnState.COMMITTED
            ticks = 0
            stop = asyncio.Event()

            async def ticker() -> None:
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0)

            task = asyncio.create_task(ticker())
            before = ticks
            assert (await _turn(agent, "请用工具查一下工程暗号。", "ask")).state is AgentTurnState.COMMITTED
            stop.set(); await task
            assert ticks - before > 3, "other tasks must run while the search advances"

    asyncio.run(case())
