"""Expired-key relay diagnostics preserve status without response disclosure."""
import asyncio

import httpx
import pytest

from simple_harness.contracts import RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import (
    CancelToken, OpenAICompatibleProvider, ProviderAuthenticationError,
    ProviderRequest, ProviderRequestRejectedError, Secret,
)


@pytest.mark.parametrize("nested", (False, True))
def test_expired_key_is_typed_and_generic_forbidden_stays_rejected(nested):
    async def exercise():
        calls = []
        def transport(request):
            calls.append(request.url.path)
            error = {"code": "API_KEY_EXPIRED" if len(calls) == 1 else "FORBIDDEN",
                     "message": "private-upstream-message private-api-key"}
            return httpx.Response(403, json={"error": error} if nested else error)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = OpenAICompatibleProvider(client, "https://fixture.invalid/v1",
                                                "fixture", Secret("private-api-key"))
            request = ProviderRequest(RequestId("expired-key"), (Message(MessageRole.USER, "OK"),))
            with pytest.raises(ProviderAuthenticationError) as caught:
                await provider.invoke(request, cancel=CancelToken())
            diagnostic = caught.value.to_dict()
            assert diagnostic["status_code"] == 403
            assert diagnostic["public_message"] == "Provider API key has expired."
            assert diagnostic["retryable"] is False
            assert "private-" not in repr(diagnostic)
            with pytest.raises(ProviderRequestRejectedError) as forbidden:
                await provider.invoke(request, cancel=CancelToken())
            assert forbidden.value.to_dict()["status_code"] == 403
            assert "private-" not in repr(forbidden.value.to_dict())
        assert calls == ["/v1/chat/completions"] * 2  # No automatic retry.

    asyncio.run(exercise())
