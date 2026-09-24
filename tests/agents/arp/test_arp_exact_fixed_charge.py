# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E4 (real-model finding): the planner charged the fixed part (instructions + tool
schemas + request framing) with the generic word heuristic while the final meter used the
deployment's exact rendered-request counter.  The real DeepSeek template adds ~270 tokens
of tool-calling preamble, so every plan that filled the window overflowed at the final
measurement and step 8 dropped the recall (F) first — recall never survived in exactly the
long-context case it exists for.  With an exact counter the fixed part is now measured on
the same counter, so the plan fits and the early fact comes back."""

from __future__ import annotations

import asyncio

import arp_fixture
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.context.tokenizer import count_message, count_tools
from simple_harness.agents.contracts import AgentTurnState

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
SECRET = "工程暗号 是 蓝鲸七号，请牢记。"
FILLERS = [f"第{i}条闲聊：今天天气不错，我们聊聊别的话题 {i}。" for i in range(6)]
FRAMING = 6  # tokens the real template adds that no per-message heuristic sees
SMALL = dict(
    input_limit=72,
    max_output=16,
    policy_overrides=dict(
        max_context_tokens=72, output_reserve_tokens=16, safety_reserve_tokens=2, tool_headroom_tokens=2,
        recent_min_tokens=12, recall_max_tokens=24, fixed_soft_max_tokens=32,
    ),
)


class FramedWordTokenizer(arp_fixture.ExactWordTokenizer):
    """Exact rendered-request counter: the word heuristic plus a fixed request framing."""

    fingerprint = "test-exact-words-framed:v1"

    def count_request_tokens(self, request) -> int:  # type: ignore[no-untyped-def]
        return sum(count_message(self, m) for m in request.messages) + count_tools(self, tuple(request.tools)) + FRAMING


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def test_an_exact_counter_charges_the_fixed_part_so_recall_survives_the_final_measurement(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(arp_fixture, "ExactWordTokenizer", FramedWordTokenizer)

    async def case() -> None:
        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["暗号是蓝鲸七号。"])
        runtime = build(tmp_path, provider, **SMALL)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, SECRET, "i0")).state is AgentTurnState.COMMITTED
            for i, filler in enumerate(FILLERS, 1):
                assert (await _turn(agent, filler, f"i{i}")).state is AgentTurnState.COMMITTED
            assert (await _turn(agent, "请问工程暗号 是什么？", "ask")).state is AgentTurnState.COMMITTED
            connection = runtime.uow.database.connection
            key = connection.execute(
                "SELECT original_request_key FROM arp_context_requests ORDER BY created_at_ms DESC, provider_request_ordinal DESC LIMIT 1"
            ).fetchone()[0]
            body = store.read_context_by_request_key(connection, str(key)).manifest
            assert body["recalled_chunk_ids"], "the early secret must survive the final measurement"
            texts = [m.content for m in provider.requests[-1].messages if isinstance(m.content, str)]
            assert any("recalled_history" in t and "蓝鲸七号" in t for t in texts)
            # The frozen manifest's fixed charge is the exact one: the planned total equals the measured wire.
            assert int(body["input_token_charge"]) <= int(body["effective_input_budget"])

    asyncio.run(case())
