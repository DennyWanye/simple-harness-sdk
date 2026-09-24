# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Accumulate one chat-completions SSE response; never expose partial tool calls."""

from __future__ import annotations

import json
from typing import Any

from .errors import ProviderProtocolError


def _billed(usage: Any) -> bool:
    """A usage object that states a charge: any positive token count."""

    return isinstance(usage, dict) and any(
        type(v) is int and v > 0 for v in usage.values()
    )


def _placeholder(value: dict[str, Any]) -> bool:
    """A chunk with no content, no tool-call delta, no finish and no billing."""

    if _billed(value.get("usage")):
        return False
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return False
    choice = choices[0]
    finish = choice.get("finish_reason")
    if finish not in (None, ""):
        return False
    delta = choice.get("delta")
    if delta is None:
        return True
    if not isinstance(delta, dict):
        return False
    return not delta.get("content") and not delta.get("tool_calls") and not delta.get("reasoning_content")


class ChatStream:
    """Single-choice deltas, with an explicit finish and transport terminator.

    The adapter still returns exactly one ProviderResponse. Reasoning deltas and
    SSE keepalives are consumed without being presented as assistant content.
    """

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict[str, Any]] = {}
        self.identity: dict[str, str] = {}
        self.usage: Any = None
        self.finish: str | None = None
        self.done = False
        self._data: list[str] = []
        self._size = 0

    def line(self, line: str) -> None:
        # Bound even ignored fields/keepalives, without retaining their content.
        self._size += len(line.encode("utf-8"))
        if self._size > 16 * 1024 * 1024:
            raise ProviderProtocolError()
        if not line:
            if self._data:
                data, self._data = "\n".join(self._data), []
                self._event(data)
        elif line.startswith("data:"):
            self._data.append(line[5:].removeprefix(" "))

    def _event(self, data: str) -> None:
        if self.done:
            raise ProviderProtocolError()
        if data == "[DONE]":
            if self.finish is None:
                raise ProviderProtocolError()
            self.done = True
            return
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ProviderProtocolError() from None
        if not isinstance(value, dict) or "error" in value:
            raise ProviderProtocolError()
        if _placeholder(value):
            # Some compatible relays open the stream with an empty delta under a
            # provisional id and repeat a 0/0/0 usage on every delta.  Such a chunk
            # carries nothing, so it pins neither the response identity nor billing.
            return
        for name in ("id", "model"):
            part = value.get(name)
            if part is not None:
                if not isinstance(part, str) or (
                    name in self.identity and self.identity[name] != part
                ):
                    raise ProviderProtocolError()
                self.identity[name] = part
        usage = value.get("usage")
        if usage is not None and not _billed(usage):
            usage = None  # an all-zero usage is a placeholder, never a billing statement
        if usage is not None:
            if self.usage is not None and self.usage != usage:
                self.usage = None  # contradictory billing must remain unknown
                raise ProviderProtocolError()
            self.usage = usage
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProviderProtocolError()
        if not choices:  # OpenAI's trailing usage-only chunk
            return
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
        ):
            raise ProviderProtocolError()
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise ProviderProtocolError()
        content, calls = delta.get("content"), delta.get("tool_calls")
        reasoning = delta.get("reasoning_content")
        if self.finish is not None and (content or calls or reasoning):
            raise ProviderProtocolError()
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise ProviderProtocolError()
            self.reasoning.append(reasoning)
        if content is not None:
            if not isinstance(content, str):
                raise ProviderProtocolError()
            self.content.append(content)
        if calls is not None:
            if not isinstance(calls, list):
                raise ProviderProtocolError()
            for call in calls:
                self._tool(call)
        finish = choice.get("finish_reason")
        # Some compatible relays emit an empty string on intermediate deltas.
        # It carries no terminal meaning; [DONE] still requires a real finish.
        if finish == "":
            finish = None
        if finish is not None:
            if not isinstance(finish, str) or not finish or self.finish is not None:
                raise ProviderProtocolError()
            self.finish = finish

    def _tool(self, delta: Any) -> None:
        if not isinstance(delta, dict):
            raise ProviderProtocolError()
        index = delta.get("index")
        if type(index) is not int or not 0 <= index < 128:
            raise ProviderProtocolError()
        call = self.tools.setdefault(index, {"function": {"name": "", "arguments": ""}})
        for name in ("id", "type"):
            value = delta.get(name)
            if value is not None:
                if not isinstance(value, str) or (name in call and call[name] != value):
                    raise ProviderProtocolError()
                call[name] = value
        function = delta.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ProviderProtocolError()
            for name in ("name", "arguments"):
                value = function.get(name)
                if value is not None:
                    if not isinstance(value, str):
                        raise ProviderProtocolError()
                    call["function"][name] += value

    def payload(self) -> dict[str, Any]:
        if not self.done or self._data or set(self.tools) != set(range(len(self.tools))):
            raise ProviderProtocolError()
        return {
            **self.identity,
            "usage": self.usage,
            "choices": [
                {
                    "finish_reason": self.finish,
                    "message": {
                        "role": "assistant",
                        "content": "".join(self.content),
                        "tool_calls": [self.tools[i] for i in sorted(self.tools)],
                        **({"reasoning_content": "".join(self.reasoning)} if self.reasoning else {}),
                    },
                }
            ],
        }
