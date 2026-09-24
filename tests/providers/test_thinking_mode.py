# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Thinking mode on the OpenAI-compatible adapter (user decision 2026-09-24: support both).

Enabled: the switch is sent, the returned reasoning is kept privately on the response and
replayed from assistant metadata; disabled: the switch is sent and reasoning is dropped;
None: the legacy request, unchanged.  The mode is part of the target identity and the
counted payload, and selects the continuation capability.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from simple_harness.contracts import Message, MessageRole, RequestId
from simple_harness.providers import OpenAICompatibleProvider, ProviderRequest, Secret
from simple_harness.providers.base import PROVIDER_REASONING_KEY, ProviderContinuationMode
from simple_harness.providers.chat_stream import ChatStream
from simple_harness.providers.openai_compatible import openai_chat_request_payload

URL = "https://example.test/v1"


def _provider(client, **kwargs):  # type: ignore[no-untyped-def]
    return OpenAICompatibleProvider(client, URL, "deepseek-v4.1-flash", Secret("fixture"), **kwargs)


def _response(reasoning: str | None):  # type: ignore[no-untyped-def]
    message = {"role": "assistant", "content": "ok"}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"id": "x", "model": "deepseek-v4.1-flash", "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}


def test_the_switch_is_sent_counted_and_part_of_the_identity() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            request = ProviderRequest(RequestId("r1"), (Message(MessageRole.USER, "hi"),), max_output_tokens=8)
            legacy, off, on = _provider(client), _provider(client, thinking="disabled"), _provider(client, thinking="enabled", reasoning_effort="high")
            assert "thinking" not in legacy._request_payload(request)
            assert off._request_payload(request)["thinking"] == {"type": "disabled"}
            body = on._request_payload(request)
            assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
            # The counter renders exactly this body when given the same mode.
            assert openai_chat_request_payload(request, model="deepseek-v4.1-flash", thinking="enabled", reasoning_effort="high") == body
            assert len({legacy.target, off.target, on.target}) == 3
            assert legacy.continuation_capability.mode is ProviderContinuationMode.REASONING_DISABLED
            assert off.continuation_capability.mode is ProviderContinuationMode.REASONING_DISABLED
            assert on.continuation_capability.mode is ProviderContinuationMode.REASONING_REPLAY
            with pytest.raises(ValueError):
                _provider(client, thinking="auto")
            with pytest.raises(ValueError):
                _provider(client, thinking="disabled", reasoning_effort="high")

    asyncio.run(exercise())


def test_reasoning_is_kept_privately_only_when_replayed() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            request = ProviderRequest(RequestId("r1"), (Message(MessageRole.USER, "hi"),), max_output_tokens=8)
            for mode, expected in ((None, None), ("disabled", None), ("enabled", "先想一想")):
                parsed = _provider(client, thinking=mode)._parse_response(request, _response("先想一想"), httpx.Response(200))
                assert parsed.reasoning_content == expected
                assert parsed.message.content == "ok" and dict(parsed.message.metadata) == {}  # never public

    asyncio.run(exercise())


def test_streamed_reasoning_deltas_are_accumulated() -> None:
    stream = ChatStream()
    for event in (
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "先"}, "finish_reason": None}]},
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"reasoning_content": "想一想"}, "finish_reason": None}]},
        {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ):
        stream.line("data: " + json.dumps(event, ensure_ascii=False))
        stream.line("")
    stream.line("data: [DONE]")
    stream.line("")
    message = stream.payload()["choices"][0]["message"]
    assert message["reasoning_content"] == "先想一想" and message["content"] == "ok"


def test_replayed_reasoning_is_serialized_for_assistant_messages_only() -> None:
    assistant = Message(MessageRole.ASSISTANT, "", metadata={PROVIDER_REASONING_KEY: "我要查一下"})
    user = Message(MessageRole.USER, "hi", metadata={PROVIDER_REASONING_KEY: "never"})
    body = openai_chat_request_payload(ProviderRequest(RequestId("r1"), (user, assistant), max_output_tokens=8), model="m", thinking="enabled")
    assert "reasoning_content" not in body["messages"][0]
    assert body["messages"][1]["reasoning_content"] == "我要查一下"
