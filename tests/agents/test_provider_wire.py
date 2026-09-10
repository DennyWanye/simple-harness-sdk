# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Slice 1 · T10: assistant tool_calls are restored on the wire from the effect ledger."""

from __future__ import annotations

import asyncio
import json

from provider_fixture import MODEL, ScriptedProvider

from simple_harness import Message, MessageRole
from simple_harness.agents import AgentConfig, build_agent_runtime
from simple_harness.agents.ports import AgentRuntimePorts, AllowAllAuthorization
from simple_harness.agents.tools.delegate import DELEGATE_TOOL_NAME
from simple_harness.agents.wire import PROVIDER_TOOL_CALLS_KEY, restore_tool_calls
from simple_harness.contracts import CallId
from simple_harness.providers.openai_compatible import OpenAICompatibleProvider


def test_openai_payload_emits_tool_calls_from_metadata():
    assistant = Message(
        MessageRole.ASSISTANT,
        "",
        metadata={
            PROVIDER_TOOL_CALLS_KEY: [
                {"id": "call_0", "name": "agent_delegate", "arguments": {"objective": "x"}}
            ]
        },
    )
    payload = OpenAICompatibleProvider._message_payload(assistant)
    assert payload["tool_calls"] == [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": "agent_delegate", "arguments": json.dumps({"objective": "x"})},
        }
    ]
    plain = OpenAICompatibleProvider._message_payload(Message(MessageRole.ASSISTANT, "hi"))
    assert "tool_calls" not in plain
    tool = OpenAICompatibleProvider._message_payload(
        Message(MessageRole.TOOL, "{}", name="agent_delegate", call_id=CallId("call_0"))
    )
    assert tool["tool_call_id"] == "call_0" and "tool_calls" not in tool


def test_restore_tool_calls_groups_by_turn_and_degrades_visibly():
    messages = (
        Message(MessageRole.USER, "q"),
        Message(MessageRole.ASSISTANT, ""),
        Message(MessageRole.TOOL, "{}", name="t", call_id=CallId("call_0")),
        Message(MessageRole.ASSISTANT, "answer"),
        Message(MessageRole.USER, "q2"),
        Message(MessageRole.ASSISTANT, ""),
        Message(MessageRole.TOOL, "{}", name="t", call_id=CallId("call_0")),  # id reused next turn
    )
    groups = [
        {"call_0": {"name": "t", "arguments": {"a": 1}}}
    ]  # only the first turn is in the ledger
    restored, fallbacks = restore_tool_calls(messages, groups)
    first = restored[1].metadata[PROVIDER_TOOL_CALLS_KEY]
    assert first[0]["arguments"] == {"a": 1} and first[0]["id"] == "call_0"
    assert restored[3].metadata == {}  # a plain answer gets nothing
    second = restored[5].metadata[PROVIDER_TOOL_CALLS_KEY]
    assert second[0]["arguments"] == {} and fallbacks == 1
    assert messages[1].metadata == {}  # the durable input is untouched


def test_second_request_after_delegation_carries_exact_tool_calls(tmp_path):
    async def case():
        provider = ScriptedProvider(
            [
                (DELEGATE_TOOL_NAME, {"objective": "调研 X", "delegation_id": "d-1"}),
                "子结论",
                "主结论",
            ]
        )
        ports = AgentRuntimePorts(
            provider=provider,
            authorization=AllowAllAuthorization(),
            database_path=str(tmp_path / "wire.db"),
            model=MODEL,
            owner_id="wire-owner",
        )
        async with build_agent_runtime(ports) as runtime:
            main = await runtime.create(
                AgentConfig(
                    name="main",
                    instructions="委派。",
                    model_profile_ref="p",
                    tool_names=(DELEGATE_TOOL_NAME,),
                ),
                creation_key="main",
            )
            await main.ask("任务", input_id="r1", timeout=10)
            final_request = provider.requests[-1]
            assistants = [
                m
                for m in final_request.messages
                if m.role is MessageRole.ASSISTANT and PROVIDER_TOOL_CALLS_KEY in m.metadata
            ]
            assert len(assistants) == 1
            calls = assistants[0].metadata[PROVIDER_TOOL_CALLS_KEY]
            assert calls[0]["name"] == DELEGATE_TOOL_NAME
            assert dict(calls[0]["arguments"]) == {"objective": "调研 X", "delegation_id": "d-1"}
            tool_messages = [m for m in final_request.messages if m.role is MessageRole.TOOL]
            assert tool_messages and tool_messages[0].call_id.value == calls[0]["id"]
            assert runtime._assembled.wire.fallback_total == 0
            # Durable Context stays metadata-free.
            from simple_harness.contracts import RunId
            from simple_harness.runtime.context import SqliteContextPort

            stored = SqliteContextPort(runtime.uow.database).load(RunId(main.run_id))
            assert all(PROVIDER_TOOL_CALLS_KEY not in m.metadata for m in stored.messages)

    asyncio.run(case())
