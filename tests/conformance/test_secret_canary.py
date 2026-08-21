# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import httpx
import pytest

from simple_harness.contracts.identity import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderRequest,
    ProviderServerError,
    ProviderTransportError,
    Secret,
    SecretRedactor,
)

CANARY = "provider-canary-DO-NOT-LEAK"


def _request() -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId("request-canary"),
        messages=(Message(role=MessageRole.USER, content="hello"),),
    )


def test_secret_string_forms_and_nested_diagnostics_are_redacted() -> None:
    secret = Secret(CANARY)
    redactor = SecretRedactor.from_secrets(secret)
    assert CANARY not in str(secret)
    assert CANARY not in repr(secret)
    diagnostic = redactor.value(
        {"authorization": f"Bearer {CANARY}", "nested": [CANARY, {CANARY: CANARY}]}
    )
    assert CANARY not in repr(diagnostic)
    assert "[REDACTED]" in repr(diagnostic)


def test_error_response_body_and_secret_do_not_enter_public_exception() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == f"Bearer {CANARY}"
        return httpx.Response(500, text=f"upstream body contains {CANARY}")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret(CANARY)
            )
            with pytest.raises(ProviderServerError) as caught:
                await provider.invoke(_request(), cancel=CancelToken())
            rendered = str(caught.value) + repr(caught.value) + repr(caught.value.to_dict())
            assert CANARY not in rendered
            assert "upstream body" not in rendered

    asyncio.run(exercise())
    assert calls == 1


def test_transport_private_cause_is_redacted_without_retry() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"failed with {CANARY}", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret(CANARY)
            )
            with pytest.raises(ProviderTransportError) as caught:
                await provider.invoke(_request(), cancel=CancelToken())
            rendered = str(caught.value) + repr(caught.value) + repr(caught.value.to_dict())
            assert CANARY not in rendered

    asyncio.run(exercise())
    assert calls == 1
