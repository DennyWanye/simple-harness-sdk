"""Observer retains billed usage from rejected replies while keeping failures visible."""
import asyncio

import httpx
import pytest
from test_real_search_value import _ObservedProvider

from simple_harness.contracts import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken,
    OpenAICompatibleProvider,
    ProviderProtocolError,
    ProviderRequest,
    Secret,
)


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 19400, "completion_tokens": 8192, "total_tokens": 27592},
    None,
    {"prompt_tokens": -1, "completion_tokens": 8192, "total_tokens": 8191},
])
def test_malformed_tool_response_keeps_known_usage_and_never_turns_into_success(usage):
    async def exercise():
        payload = {"model": "fixture", "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [{"id": "bad", "type": "function",
            "function": {"name": "workspace_read_file", "arguments": "{invalid json"}}],
        }}]}
        if usage is not None:
            payload["usage"] = usage
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload),
        )) as client:
            observer = _ObservedProvider(OpenAICompatibleProvider(
                client, "https://fixture.invalid", "fixture", Secret("fake-test-secret"),
            ))
            request = ProviderRequest(RequestId("usage-rejected-1"), (
                Message(MessageRole.USER, "## attempt\n{\"attempt_id\":\"A\"}"),
            ))
            with pytest.raises(ProviderProtocolError):
                await observer.invoke(request, cancel=CancelToken())
            assert len(observer.calls) == 1 and observer.calls[0]["error_type"]
            assert observer.writes == []
            recorded = observer.calls[0]["usage"]
            if usage is not None and usage["prompt_tokens"] >= 0:
                assert recorded["total_tokens"] == 27592
                assert recorded["input_tokens"] == 19400 and recorded["output_tokens"] == 8192
            else:
                assert recorded is None
            assert "fake-test-secret" not in str(observer.calls)

    asyncio.run(exercise())


@pytest.mark.parametrize("content,finish,empty", [
    ("", "length", True), ("   ", "stop", True), ("done", "stop", False),
])
def test_empty_parsed_response_is_visible_without_fabricating_adapter_exception(
    content, finish, empty,
):
    """The old observer's error_type=None alone missed a real SDK empty-response failure."""
    async def exercise():
        payload = {"model": "fixture", "choices": [{"finish_reason": finish,
            "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 4929, "completion_tokens": 8192,
                "total_tokens": 13121,
                "completion_tokens_details": {"reasoning_tokens": 8192}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload),
        )) as client:
            observer = _ObservedProvider(OpenAICompatibleProvider(
                client, "https://fixture.invalid", "fixture", Secret("fake-test-secret"),
            ))
            request = ProviderRequest(RequestId("empty-observation"), (
                Message(MessageRole.USER, '## attempt\n{"attempt_id":"A"}'),
            ))
            response = await observer.invoke(request, cancel=CancelToken())
            assert response.message.content == content  # observer does not repair or reject it
            row, = observer.calls
            assert row["error_type"] is None  # no exception at the provider-adapter boundary
            assert row["response_empty"] is empty
            assert row["response_finish_reason"] == finish
            assert row["usage"]["reasoning_tokens"] == 8192
            assert row["usage"]["total_tokens"] == 13121
            assert "fake-test-secret" not in repr(row)

    asyncio.run(exercise())
