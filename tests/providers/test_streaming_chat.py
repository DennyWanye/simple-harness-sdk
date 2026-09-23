"""One physical request, complete tool calls and honest billing across SSE failures."""

import asyncio
import json

import httpx
import pytest

from simple_harness.contracts import Message, RequestId
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderCancelledError,
    ProviderProtocolError,
    ProviderRequest,
    ProviderServerError,
    Secret,
)

USAGE = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
REQUEST = ProviderRequest(RequestId("stream-test"), (Message("user", "Use the tool."),))


def chunk(delta=None, *, finish=None, usage=None):
    return {
        "id": "response-1",
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }


def event(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data, *, disconnect=False):
        self.data, self.disconnect, self.closed = data.encode(), disconnect, False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 7):
            yield self.data[offset : offset + 7]
        if self.disconnect:
            raise httpx.ReadError("private-response-or-key")

    async def aclose(self):
        self.closed = True


async def invoke(data, *, disconnect=False):
    stream = Bytes(data, disconnect=disconnect)
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = OpenAICompatibleProvider(
            client, "https://fixture.invalid/v1", "fixture", Secret("secret"), stream=True
        )
        try:
            return await provider.invoke(REQUEST, cancel=CancelToken())
        finally:
            assert len(calls) == 1
            assert calls[0]["stream"] is True
            assert calls[0]["stream_options"] == {"include_usage": True}
            assert stream.closed


@pytest.mark.parametrize("separate_usage", [False, True])
def test_split_utf8_tool_arguments_and_final_usage_are_returned_once(separate_usage):
    data = ": keep-alive\n\n" + event(
        chunk({"role": "assistant", "reasoning_content": "private reasoning"})
    )
    data += event(
        chunk(
            {
                "content": "已准备",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "save", "arguments": '{"name":'},
                    }
                ],
            }
        )
    )
    data += event(chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"报告"}'}}]}))
    data += event(chunk(finish="tool_calls", usage=None if separate_usage else USAGE))
    if separate_usage:
        data += event({"choices": [], "usage": USAGE})
    response = asyncio.run(invoke(data + "data: [DONE]\n\n"))
    assert response.message.content == "已准备"
    assert response.tool_calls[0].name == "save"
    assert response.tool_calls[0].arguments == {"name": "报告"}
    assert response.usage.total_tokens == 14
    assert response.provider_request_id == "response-1"


@pytest.mark.parametrize("ending", ["eof", "done_without_finish", "identity_change", "error"])
def test_partial_output_never_becomes_an_accepted_response(ending):
    data = event(chunk({"content": "partial"}))
    data += {
        "eof": "",
        "done_without_finish": "data: [DONE]\n\n",
        "identity_change": event({**chunk(), "id": "other-response"}),
        "error": event({"error": {"message": "private-response-or-key"}}),
    }[ending]
    with pytest.raises(ProviderProtocolError) as caught:
        asyncio.run(invoke(data))
    assert not hasattr(caught.value, "detail")
    assert "private-response-or-key" not in str(caught.value)


def test_relay_empty_finish_is_not_terminal_and_requires_a_later_finish():
    data = event(chunk({"role": "assistant"}, finish=""))
    data += event(chunk({"content": "OK"}, finish=""))
    response = asyncio.run(
        invoke(data + event(chunk(finish="stop", usage=USAGE)) + "data: [DONE]\n\n")
    )
    assert response.message.content == "OK"
    with pytest.raises(ProviderProtocolError):
        asyncio.run(invoke(data + "data: [DONE]\n\n"))


@pytest.mark.parametrize("disconnect", [False, True])
def test_missing_done_preserves_final_billed_usage_without_accepting_output(disconnect):
    data = event(chunk({"content": "result"}, finish="stop", usage=USAGE))
    with pytest.raises(ProviderProtocolError) as caught:
        asyncio.run(invoke(data, disconnect=disconnect))
    assert caught.value.detail["usage"]["total_tokens"] == 14


def test_invalid_tool_json_preserves_final_usage():
    data = event(
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "save", "arguments": "{broken"},
                    }
                ]
            },
            finish="tool_calls",
            usage=USAGE,
        )
    )
    with pytest.raises(ProviderProtocolError) as caught:
        asyncio.run(invoke(data + "data: [DONE]\n\n"))
    assert caught.value.detail["usage"]["total_tokens"] == 14


def test_524_is_typed_and_never_retried_or_switched_to_nonstream():
    async def check():
        calls = []

        def transport(request):
            calls.append(request)
            return httpx.Response(524, text="private upstream details")

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            p = OpenAICompatibleProvider(
                client, "https://fixture.invalid/v1", "fixture", Secret("secret"), stream=True
            )
            with pytest.raises(ProviderServerError) as error:
                await p.invoke(REQUEST, cancel=CancelToken())
            assert error.value.to_dict()["status_code"] == 524
            assert len(calls) == 1

    asyncio.run(check())


def test_cancellation_closes_stream_without_returning_partial_content():
    async def check():
        started = asyncio.Event()

        class Waiting(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield event(chunk({"content": "partial"})).encode()
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Waiting()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        ) as client:
            p = OpenAICompatibleProvider(
                client, "https://fixture.invalid/v1", "fixture", Secret("secret"), stream=True
            )
            cancel = CancelToken()
            task = asyncio.create_task(p.invoke(REQUEST, cancel=cancel))
            await started.wait()
            cancel.cancel()
            with pytest.raises(ProviderCancelledError):
                await task
            assert stream.closed

    asyncio.run(check())
