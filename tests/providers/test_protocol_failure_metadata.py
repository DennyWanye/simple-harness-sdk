# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Known-usage protocol failures disclose only bounded diagnostic metadata."""

import asyncio

import httpx
import pytest

from simple_harness.contracts import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderProtocolError,
    ProviderRequest,
    Secret,
)

VALID_USAGE = {
    "prompt_tokens": 20336,
    "completion_tokens": 8192,
    "total_tokens": 28528,
    "completion_tokens_details": {"reasoning_tokens": 6065},
}
RAW_ARGUMENT_SECRET = "raw-arguments-secret"
RAW_FINISH_SECRET = "raw-finish-secret"


@pytest.mark.parametrize(
    ("finish_reason", "expected_finish_reason"),
    [
        pytest.param("length", "length", id="length"),
        pytest.param("tool_calls", "tool_calls", id="tool-calls"),
        pytest.param(None, None, id="missing"),
        pytest.param(42, None, id="nonstring"),
        pytest.param(f"unknown-{RAW_FINISH_SECRET}", None, id="malicious-unknown"),
    ],
)
def test_known_usage_protocol_error_has_only_allowlisted_metadata(
    finish_reason, expected_finish_reason
):
    async def exercise():
        calls = []

        def transport(request):
            calls.append(request.url.path)
            choice = {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-bad",
                            "type": "function",
                            "function": {
                                "name": "probe",
                                "arguments": '{"secret": "' + RAW_ARGUMENT_SECRET,
                            },
                        }
                    ],
                }
            }
            if finish_reason is not None:
                choice["finish_reason"] = finish_reason
            return httpx.Response(
                200,
                json={"choices": [choice], "usage": VALID_USAGE},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://fixture.invalid/v1", "fixture", Secret("test-secret")
            )
            request = ProviderRequest(
                RequestId("protocol-failure-metadata"),
                (Message(MessageRole.USER, "Call probe."),),
            )
            with pytest.raises(ProviderProtocolError) as caught:
                await provider.invoke(request, cancel=CancelToken())

        error = caught.value
        assert calls == ["/v1/chat/completions"]
        assert error.code == "provider_protocol_error"
        assert error.detail == {
            "usage": {
                "input_tokens": 20336,
                "output_tokens": 8192,
                "total_tokens": 28528,
                "cache_tokens": None,
                "reasoning_tokens": 6065,
            },
            "parse_stage": "tool_parse",
            "tool_parse_reason": "arguments_json",
            **({"finish_reason": expected_finish_reason} if expected_finish_reason else {}),
        }
        public_error = repr(error) + str(error) + repr(error.detail)
        assert RAW_ARGUMENT_SECRET not in public_error
        assert RAW_FINISH_SECRET not in public_error

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("tool_calls", "reason"),
    [
        pytest.param("raw-arguments-secret", "shape", id="calls-shape"),
        pytest.param(["raw-arguments-secret"], "shape", id="call-shape"),
        pytest.param(
            [{"type": "raw-arguments-secret"}], "type", id="call-type"
        ),
        pytest.param(
            [{"type": "function", "id": "raw-arguments-secret\n", "function": {
                "name": "probe", "arguments": "{}"
            }}],
            "id",
            id="invalid-id",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": "raw-arguments-secret"}],
            "function",
            id="function-shape",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": {
                "name": "", "arguments": "raw-arguments-secret"
            }}],
            "name",
            id="missing-name",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": {
                "name": "   ", "arguments": "{}"
            }}],
            "name",
            id="blank-name",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": {
                "name": "probe", "arguments": '["raw-arguments-secret"]'
            }}],
            "arguments_non_object",
            id="json-array",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": {
                "name": "probe", "arguments": 42
            }}],
            "arguments_non_object",
            id="non-object",
        ),
        pytest.param(
            [{"type": "function", "id": "call-ok", "function": {
                "name": "probe", "arguments": '{"raw-arguments-secret": NaN}'
            }}],
            "normalization",
            id="invalid-normalization",
        ),
    ],
)
def test_known_usage_tool_parse_reason_is_bounded(tool_calls, reason):
    async def exercise():
        calls = []

        def transport(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": "", "tool_calls": tool_calls},
                }],
                "usage": VALID_USAGE,
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = OpenAICompatibleProvider(
                client, "https://fixture.invalid/v1", "fixture", Secret("test-secret")
            )
            with pytest.raises(ProviderProtocolError) as caught:
                await provider.invoke(
                    ProviderRequest(
                        RequestId("classified-tool-error"),
                        (Message(MessageRole.USER, "Call probe."),),
                    ),
                    cancel=CancelToken(),
                )

        error = caught.value
        assert calls == ["/v1/chat/completions"]
        assert error.code == "provider_protocol_error"
        assert error.detail == {
            "usage": {
                "input_tokens": 20336,
                "output_tokens": 8192,
                "total_tokens": 28528,
                "cache_tokens": None,
                "reasoning_tokens": 6065,
            },
            "finish_reason": "tool_calls",
            "parse_stage": "tool_parse",
            "tool_parse_reason": reason,
        }
        public_error = repr(error) + str(error) + repr(error.detail)
        assert RAW_ARGUMENT_SECRET not in public_error
        assert RAW_FINISH_SECRET not in public_error

    asyncio.run(exercise())


def test_invalid_usage_keeps_existing_protocol_error_without_metadata():
    async def exercise():
        payload = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-bad",
                                "type": "function",
                                "function": {
                                    "name": "probe",
                                    "arguments": '{"secret": "' + RAW_ARGUMENT_SECRET,
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {**VALID_USAGE, "prompt_tokens": True},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload)
        )) as client:
            provider = OpenAICompatibleProvider(
                client, "https://fixture.invalid/v1", "fixture", Secret("test-secret")
            )
            with pytest.raises(ProviderProtocolError) as caught:
                await provider.invoke(
                    ProviderRequest(
                        RequestId("invalid-usage"),
                        (Message(MessageRole.USER, "Call probe."),),
                    ),
                    cancel=CancelToken(),
                )

        error = caught.value
        assert not hasattr(error, "detail")
        assert RAW_ARGUMENT_SECRET not in repr(error) + str(error)

    asyncio.run(exercise())
