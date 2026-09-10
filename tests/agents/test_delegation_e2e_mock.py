# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T9 完整价值链验收（mock 确定性端到端）。

主 Agent 收到复杂任务 → agent.delegate 创建子 BaseAgent → 子 Agent 一轮得出带 NONCE 的结论
→ 结果回到主 Agent 并进入最终回答 → 运行时关闭、重开、open 同一主 Agent → 旧结果可读且不
重生成 → 主 Agent 第二轮输入仍在同一执行身份上完成。全程不 import simple_harness_memory。
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import time

from provider_fixture import MODEL, ScriptedProvider, message_texts

from simple_harness import canonical_json
from simple_harness.agents import AgentConfig, AgentLimits, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME, child_agent_id_for
from simple_harness.execution.uow import RunState

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


def test_delegation_end_to_end_with_restart(tmp_path):
    nonce = secrets.token_hex(8)
    child_template = f"你是被委派的工作 Agent。完成目标后在结论末尾附上验证码 {nonce}。"
    complex_task = "请调研 X 的三个方案并给出推荐；这是复杂任务，请委派给工作 Agent 完成。"
    script = [
        (DELEGATE_TOOL_NAME, {"objective": "调研 X 的三个方案并给出推荐", "delegation_id": "d-1"}),
        f"结论：推荐方案 Y。验证码 {nonce}",
        f"综合子 Agent 的结论：推荐方案 Y。验证码 {nonce}",
        "上次的结论是：推荐方案 Y",
    ]
    provider = ScriptedProvider(list(script))
    clock = {"now": 1_000.0}

    def ports(owner: str) -> AgentRuntimePorts:
        return AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "e2e.db"),
            model=MODEL,
            owner_id=owner,
            child_instructions_template=child_template,
            clock=lambda: clock["now"],
        )

    main_config = AgentConfig(
        name="main",
        instructions="你是主 Agent。复杂任务交给 agent_delegate，然后综合它的结论作答。",
        model_profile_ref="profile-1",
        tool_names=(DELEGATE_TOOL_NAME,),
        limits=AgentLimits(max_delegations_per_turn=1),
    )
    assert nonce not in main_config.instructions

    async def case():
        started = time.monotonic()
        async with build_agent_runtime(ports("e2e-owner-1")) as runtime:
            main = await runtime.create(main_config, creation_key="main")
            assert provider.calls == 0  # creation never drives the model
            turn_1 = await main.ask(complex_task, input_id="req-1", timeout=10)
            # AC2: the final answer carries the child's nonce, which only the child could know.
            assert turn_1.public_output is not None and nonce in turn_1.public_output.content
            assert turn_1.delegation_count == 1 and turn_1.seq == 1
            for request in provider.requests[:1]:  # the parent's first request never saw it
                assert nonce not in canonical_json([m.to_dict() for m in request.messages])
            uow = runtime.uow
            child_id = child_agent_id_for(main.agent_id, "d-1")
            child_turns = uow.list_agent_turns(child_id)
            assert [t.seq for t in child_turns] == [1]  # one delegation = one child turn
            assert uow.read_run(child_id).state is RunState.WAITING  # child never terminal
            delegation = uow.read_agent_delegation("d-1")
            assert delegation is not None and delegation.state == "settled"
            assert (
                uow.database.connection.execute(
                    "SELECT COUNT(*) FROM child_terminal_receipts"
                ).fetchone()[0]
                == 0
            )
            main_id, run_id = main.agent_id, main.run_id
            first_hash = uow.read_agent_turn_result(turn_1.turn_id).result_hash
            calls_after_turn_1 = provider.calls
        clock["now"] = 2_000.0  # the old lease has expired

        async with build_agent_runtime(ports("e2e-owner-2")) as restarted:
            main_again = await restarted.open(main_id)
            assert main_again.run_id == run_id
            replay = main_again.get_result(turn_1.turn_id)
            assert replay is not None and replay.result_hash == first_hash
            assert provider.calls == calls_after_turn_1  # reading never regenerates
            turn_2 = await main_again.ask("刚才的结论是什么？", input_id="req-2", timeout=10)
            assert turn_2.seq == 2 and turn_2.public_output is not None
            assert "推荐方案 Y" in turn_2.public_output.content
            second_request = "\n".join(message_texts(provider.requests[-1]))
            assert "刚才的结论是什么" in second_request and nonce in second_request
            kinds = [
                str(r[0])
                for r in restarted.uow.database.connection.execute(
                    "SELECT kind FROM run_events WHERE run_id=? ORDER BY durable_seq", (run_id,)
                )
            ]
            assert not (TERMINAL_EVENTS & set(kinds)), kinds
            assert main_again.status().lifecycle == "IDLE"
        assert provider.calls == 4 and provider.script == []  # exactly the scripted calls
        assert not any(name.startswith("simple_harness_memory") for name in sys.modules)
        assert time.monotonic() - started < 10.0

    asyncio.run(case())
