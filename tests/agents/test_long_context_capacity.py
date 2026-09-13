# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""LC6: real pinned-tokenizer capacity controls with a scripted SDK Provider."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from provider_fixture import ScriptedProvider, message_texts
from tool_fixture import ECHO_SCHEMA, EchoToolExecutor

from agent_orchestrator.runtime.deepseek_tokens import DeepSeekV41TokenEstimator
from simple_harness.agents import AgentConfig, AgentTurnState, build_agent_runtime
from simple_harness.agents.context import ContextPolicy
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.contracts import MessageRole
from simple_harness.runtime.termination import TerminationLimits

MODEL = "deepseek-flash"
INSTRUCTIONS = "你是LC6助手。必须保留本指令和本轮用户输入。"


class ControlledDeepSeekProvider(ScriptedProvider):
    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        return replace(await super().invoke(request, cancel=cancel), model=MODEL)


@pytest.fixture(scope="module")
def counter():
    path = os.environ.get("SH_TOKENIZER_PATH")
    if not path:
        pytest.skip("LC6 requires SH_TOKENIZER_PATH with the pinned V4.1 tokenizer")
    return DeepSeekV41TokenEstimator(Path(path), model=MODEL)


def _history_input(counter: DeepSeekV41TokenEstimator, capacity: int, marker: str) -> str:
    unit = " LC6历史资料：甲地在北方，乙地在南方；顺序不可交换。"
    sample_tokens = counter.count_text(unit * 100)
    repeats = round(capacity * 0.55 * 100 / sample_tokens)
    value = marker + unit * repeats
    # Both individual inputs fit, but two full inputs cannot fit together.
    assert capacity * 0.51 < counter.count_text(value) < capacity * 0.60
    return value


def _ports(db: Path, provider, counter, capacity: int, owner: str) -> AgentRuntimePorts:
    return AgentRuntimePorts(
        provider=provider,
        authorization=AllowAllAuthorization(),
        database_path=str(db),
        model=MODEL,
        owner_id=owner,
        tool_executor=EchoToolExecutor(),
        tool_names=("echo",),
        tool_schemas={"echo": ECHO_SCHEMA},
        tokenizer=counter,
        context_policy=ContextPolicy(
            max_input_tokens=capacity,
            output_reserve=32_768,
            safety_margin=0,
            render_slack_tokens=0,
        ),
        default_max_output_tokens=8_192,
        max_output_tokens_ceiling=32_768,
        termination_limits=TerminationLimits(
            max_turns=10_000,
            max_tool_calls=20_000,
            max_wall_seconds=365.0 * 86_400.0,
            max_cost_micros=10_000_000_000,
            max_consecutive_same_tool=100,
        ),
    )


def _assert_whole_tool_groups(request) -> None:
    calls = {
        call["id"]
        for message in request.messages
        for call in message.metadata.get("provider_tool_calls", ())
    }
    results = {
        message.call_id.value
        for message in request.messages
        if message.role is MessageRole.TOOL and message.call_id is not None
    }
    assert calls == results, "assembled context split an assistant/tool exchange"


@pytest.mark.parametrize("capacity", [262_144, 524_288])
def test_lc6_rotation_replay_and_protected_input(tmp_path, counter, capacity):
    first_input = _history_input(counter, capacity, "FIRST_ONLY:")
    second_input = _history_input(counter, capacity, "SECOND_ONLY:")
    assert counter.count_text(first_input + second_input) > capacity
    db = tmp_path / "lc6.db"
    provider = ControlledDeepSeekProvider([("echo", {}), "FIRST_DONE", "SECOND_DONE"])

    async def case():
        async with build_agent_runtime(
            _ports(db, provider, counter, capacity, "lc6-before-restart")
        ) as runtime:
            agent = await runtime.create(
                AgentConfig(
                    name="lc6", instructions=INSTRUCTIONS,
                    model_profile_ref=MODEL, tool_names=("echo",),
                ),
                creation_key="lc6-agent",
            )
            first = await agent.ask(first_input, input_id="first", timeout=120)
            assert first.state is AgentTurnState.COMMITTED, first.error
            assert provider.calls == 2
            before = runtime.uow.latest_agent_context_selection(agent.agent_id)
            assert before is not None and not before.dropped_ranges
            assert not runtime.uow.list_agent_summaries(agent.agent_id)
            assert capacity * 0.5 < counter.estimate_input_tokens(provider.requests[0]) <= capacity
            journal_before = agent.journal()
            assert any(row.kind == "tool_result" for row in journal_before)

            second = await agent.ask(second_input, input_id="second", timeout=120)
            assert second.state is AgentTurnState.COMMITTED, second.error
            assert second.public_output is not None
            assert second.public_output.content == "SECOND_DONE"
            assert provider.calls == 3
            selection = runtime.uow.latest_agent_context_selection(agent.agent_id)
            assert selection is not None and selection.dropped_ranges
            assert 2 in {
                seq for start, end in selection.dropped_ranges
                for seq in range(start, end + 1)
            }
            assert runtime.uow.list_agent_summaries(agent.agent_id)
            journal_after = agent.journal()
            assert journal_after[: len(journal_before)] == journal_before
            assert agent.read_journal_record(2).message_json["content"] == first_input
            assert any(
                row.kind == "user_input" and row.message_json["content"] == second_input
                for row in journal_after
            )

            for request in provider.requests:
                assert counter.estimate_input_tokens(request) <= capacity
                assert INSTRUCTIONS in message_texts(request)
                _assert_whole_tool_groups(request)
            assert all(first_input in message_texts(req) for req in provider.requests[:2])
            last_texts = message_texts(provider.requests[-1])
            assert second_input in last_texts
            assert first_input not in last_texts
            assert any("[历史已折叠]" in text for text in last_texts)
            agent_id = agent.agent_id

        async with build_agent_runtime(
            _ports(db, provider, counter, capacity, "lc6-after-restart")
        ) as restarted:
            reopened = await restarted.open(agent_id)
            replay = await reopened.ask(second_input, input_id="second", timeout=120)
            assert replay.to_json() == second.to_json()
            assert provider.calls == 3
            assert reopened.journal() == journal_after

            oversized = first_input + second_input
            guard_agent = await restarted.create(
                AgentConfig(name="lc6-over-cap", instructions=INSTRUCTIONS,
                            model_profile_ref=MODEL, tool_names=("echo",)),
                creation_key="lc6-over-cap",
            )
            failed = await guard_agent.ask(oversized, input_id="too-large", timeout=120)
            assert failed.state is AgentTurnState.FAILED
            assert failed.error["error_code"] == "context_required_content_too_large"
            assert failed.error["detail"]["required_over_budget"] is True
            assert provider.calls == 3
            rejected = restarted.uow.latest_agent_context_selection(guard_agent.agent_id)
            assert rejected is not None and rejected.required_over_budget

    asyncio.run(case())
