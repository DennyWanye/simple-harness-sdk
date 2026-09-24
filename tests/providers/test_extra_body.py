# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Provider-specific request fields ride along with the body without touching the counted request."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from simple_harness.contracts import Message, MessageRole, RequestId
from simple_harness.providers import OpenAICompatibleProvider, ProviderRequest, Secret
from simple_harness.providers.openai_compatible import openai_chat_request_payload


def test_extra_body_is_merged_into_the_body_but_never_overrides_the_counted_request() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            provider = OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), extra_body={"user": "tenant-a"})
            request = ProviderRequest(RequestId("r1"), (Message(MessageRole.USER, "hi"),), max_output_tokens=8)
            payload = provider._request_payload(request)
            assert payload["user"] == "tenant-a" and payload["max_tokens"] == 8
            assert "user" not in openai_chat_request_payload(request, model="m")  # the counted body is unchanged
            plain = OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"))
            assert "user" not in plain._request_payload(request)
            # Thinking is a first-class, counted, identity-bearing switch — never an extra field.
            for field in ("thinking", "reasoning_effort"):
                with pytest.raises(ValueError):
                    OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), extra_body={field: {"type": "disabled"}})
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), extra_body={"max_tokens": 1})
            with pytest.raises(TypeError):
                OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), extra_body=["thinking"])  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), extra_body={"": 1})

    asyncio.run(exercise())


def test_a_declared_response_model_alias_is_normalised_to_the_bound_model() -> None:
    """The kernel trusts usage only when ``response.model`` echoes the bound model; a relay
    that echoes a vendor-prefixed spelling (observed 2026-09-24) is declared as an alias."""

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            provider = OpenAICompatibleProvider(client, "https://example.test/v1", "deepseek-v4.1-flash", Secret("fixture"), response_model_aliases=("deepseek-ai/DeepSeek-V4.1-Flash",))
            request = ProviderRequest(RequestId("r1"), (Message(MessageRole.USER, "hi"),), max_output_tokens=8)

            def parse(model: str | None):  # type: ignore[no-untyped-def]
                payload = {"id": "x", "model": model, "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
                return provider._parse_response(request, payload, httpx.Response(200, headers={}))

            assert parse("deepseek-ai/DeepSeek-V4.1-Flash").model == "deepseek-v4.1-flash"
            assert parse("deepseek-v4.1-flash").model == "deepseek-v4.1-flash"
            assert parse("some-other-model").model == "some-other-model"  # an undeclared echo stays untrusted
            with pytest.raises(TypeError):
                OpenAICompatibleProvider(client, "https://example.test/v1", "m", Secret("fixture"), response_model_aliases="alias")  # type: ignore[arg-type]

    asyncio.run(exercise())
