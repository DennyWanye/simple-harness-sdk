# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Scripted, deterministic Provider for BaseAgent tests (no network, no randomness)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from simple_harness import Message, MessageRole
from simple_harness.contracts import CallId
from simple_harness.providers import ProviderRequest, ProviderResponse, ProviderToolCall

MODEL = "agent-model"


class ScriptedProvider:
    """Returns the scripted responses in order; records every request it saw."""

    def __init__(self, script: Sequence[object], *, blocked: bool = False) -> None:
        self.script = list(script)
        self.requests: list[ProviderRequest] = []
        self.blocked = blocked
        self.allow = asyncio.Event()

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        del cancel
        self.requests.append(request)
        if self.blocked:
            await self.allow.wait()
        if not self.script:
            raise AssertionError("scripted provider exhausted: unexpected model call")
        step = self.script.pop(0)
        if isinstance(step, str):
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, step),
                model=MODEL,
                finish_reason="stop",
            )
        name, arguments = step  # type: ignore[misc]
        call = ProviderToolCall(CallId(f"call-{len(self.requests)}"), name, dict(arguments))
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, ""),
            tool_calls=(call,),
            model=MODEL,
            finish_reason="tool_calls",
        )


def message_texts(request: ProviderRequest) -> list[str]:
    texts = []
    for message in request.messages:
        content = message.content
        texts.append(content if isinstance(content, str) else str(content))
    return texts
