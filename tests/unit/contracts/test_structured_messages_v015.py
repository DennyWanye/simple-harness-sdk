# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

import json

import httpx
import pytest

from simple_harness import ContentBlock, Message, MessageRole, RequestId
from simple_harness.execution.provider_invocations import (
    provider_request_from_json,
    provider_request_json,
)
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderRequest,
    Secret,
)
from simple_harness.runtime.context import _message
from simple_harness.runtime.drivers.react import _messages


def test_structured_content_round_trips_without_string_coercion() -> None:
    content = (
        ContentBlock("text", {"text": "你好"}),
        ContentBlock("image_url", {"image_url": {"url": "data:image/png;base64,AA=="}}),
    )
    request = ProviderRequest(RequestId("request-structured"), (Message("user", content),))

    stored = provider_request_json(request)
    restored = provider_request_from_json(request.request_id, stored)

    assert restored == request
    assert stored["messages"][0]["content"] == [block.to_dict() for block in content]  # type: ignore[index]


def test_list_content_is_rejected_instead_of_stringified() -> None:
    with pytest.raises(Exception, match="text or a tuple"):
        Message(MessageRole.USER, [{"type": "text", "text": "unsafe"}])  # type: ignore[arg-type]


def test_react_accepts_frozen_tuple_blocks_from_start_snapshot() -> None:
    messages = _messages(
        (
            {
                "role": "user",
                "content": (
                    {"type": "text", "text": "hello"},
                    {"type": "input_text", "data": "body"},
                ),
            },
        )
    )
    assert not isinstance(messages[0].content, str)
    assert [block.to_dict() for block in messages[0].content] == [
        {"type": "text", "text": "hello"},
        {"type": "input_text", "data": "body"},
    ]


def test_durable_context_rebuilds_structured_blocks() -> None:
    message = _message(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "input_text", "data": "body"},
            ],
            "metadata": {},
        }
    )
    assert not isinstance(message.content, str)
    assert [block.to_dict() for block in message.content] == [
        {"type": "text", "text": "hello"},
        {"type": "input_text", "data": "body"},
    ]


@pytest.mark.anyio
async def test_openai_transport_emits_content_blocks_and_extended_usage() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "model": "model-a",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            client, "https://provider.example/v1", "model-a", Secret("secret")
        )
        response = await provider.invoke(
            ProviderRequest(
                RequestId("request-wire"),
                (Message("user", (ContentBlock("text", {"text": "hello"}),)),),
            ),
            cancel=CancelToken(),
        )

    assert captured["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]
    assert response.usage is not None
    assert response.usage.cache_tokens == 4
    assert response.usage.reasoning_tokens == 3
