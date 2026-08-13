# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import httpx
import pytest

from simple_harness.contracts.identity import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderAuthenticationError,
    ProviderCancelledError,
    ProviderPaymentRequiredError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderRequestRejectedError,
    ProviderResponse,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderToolSpec,
    ProviderTransportError,
    Secret,
)


def _request() -> ProviderRequest:
    return ProviderRequest(
        request_id=RequestId("request-1"),
        messages=(Message(role=MessageRole.USER, content="summarize"),),
        tools=(
            ProviderToolSpec(
                name="project_summary_read",
                description="Read a public project summary",
                parameters={"type": "object", "additionalProperties": False},
            ),
        ),
    )


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "provider-request-1"},
        json={
            "id": "completion-1",
            "model": "fixture-model",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "project_summary_read",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        },
    )


def test_provider_contract_is_stateless_and_performs_exactly_one_call() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _response()

    async def exercise() -> ProviderResponse:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            assert (
                set(vars(provider)) == set() if hasattr(provider, "__dict__") else True
            )
            return await provider.invoke(_request(), cancel=CancelToken())

    result = asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0].url == "https://provider.invalid/v1/chat/completions"
    assert result.request_id == RequestId("request-1")
    assert result.tool_calls[0].call_id.value == "call-1"
    assert result.tool_calls[0].name == "project_summary_read"
    assert dict(result.tool_calls[0].arguments) == {}
    assert result.usage is not None
    assert result.usage.total_tokens == 14
    assert result.provider_request_id == "provider-request-1"


def test_provider_values_are_immutable() -> None:
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.temperature = 1.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.tools[0].parameters["type"] = "array"  # type: ignore[index]


@pytest.mark.parametrize(
    ("status", "expected", "code", "retryable"),
    [
        (400, ProviderRequestRejectedError, "provider_request_rejected", False),
        (401, ProviderAuthenticationError, "provider_authentication_failed", False),
        (402, ProviderPaymentRequiredError, "provider_payment_required", False),
        (408, ProviderTimeoutError, "provider_timeout", True),
        (429, ProviderRateLimitError, "provider_rate_limited", True),
        (500, ProviderServerError, "provider_server_error", True),
        (503, ProviderServerError, "provider_server_error", True),
    ],
)
def test_http_errors_are_typed_and_never_retried(
    status: int, expected: type[Exception], code: str, retryable: bool
) -> None:
    count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(status, text="private provider body")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            with pytest.raises(expected) as caught:
                await provider.invoke(_request(), cancel=CancelToken())
            assert "private provider body" not in str(caught.value)
            assert caught.value.status_code == status
            assert caught.value.code == code
            assert caught.value.retryable is retryable

    asyncio.run(exercise())
    assert count == 1


def test_timeout_and_transport_errors_are_typed_and_single_attempt() -> None:
    attempts = 0

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    async def exercise(handler, expected) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            with pytest.raises(expected):
                await provider.invoke(_request(), cancel=CancelToken())

    asyncio.run(exercise(timeout_handler, ProviderTimeoutError))
    asyncio.run(exercise(transport_handler, ProviderTransportError))
    assert attempts == 2


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")


def test_cancel_before_handoff_makes_no_request() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response()

    async def exercise() -> None:
        token = CancelToken()
        token.cancel()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            with pytest.raises(ProviderCancelledError):
                await provider.invoke(_request(), cancel=token)

    asyncio.run(exercise())
    assert calls == 0


def test_cancel_after_handoff_cancels_the_only_request() -> None:
    async def exercise() -> tuple[int, int]:
        transport = _BlockingTransport()
        token = CancelToken()
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            invocation = asyncio.create_task(provider.invoke(_request(), cancel=token))
            await transport.started.wait()
            token.cancel()
            with pytest.raises(ProviderCancelledError):
                await invocation
        return transport.calls, transport.cancelled

    calls, cancelled = asyncio.run(exercise())
    assert (calls, cancelled) == (1, 1)


def test_invalid_base_url_and_embedded_credentials_are_rejected() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(client, "relative", "model", Secret("secret"))
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(
                    client, "http://provider.invalid/v1", "model", Secret("secret")
                )
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(
                    client,
                    "https://user:password@example.test/v1",
                    "model",
                    Secret("secret"),
                )
            with pytest.raises(ValueError):
                OpenAICompatibleProvider(
                    client,
                    "https://provider.invalid/v1",
                    "model",
                    Secret("secret"),
                    timeout=0,
                )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"choices": []},
        {"choices": [{"message": {"content": []}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "tool", "arguments": "not-json"},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 1},
        },
    ],
)
def test_malformed_provider_payload_is_a_stable_protocol_error(payload: object) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://provider.invalid/v1", "fixture-model", Secret("secret")
            )
            with pytest.raises(ProviderProtocolError) as caught:
                await provider.invoke(_request(), cancel=CancelToken())
            assert caught.value.code == "provider_protocol_error"

    asyncio.run(exercise())
    assert calls == 1
