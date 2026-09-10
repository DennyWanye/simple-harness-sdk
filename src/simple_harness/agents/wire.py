# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Restore assistant ``tool_calls`` on the wire from the effect ledger.

Durable Context keeps a provider assistant message free of provider metadata, so a
``tool`` message would follow an assistant message without ``tool_calls`` and an
OpenAI-compatible endpoint rejects the request.  The facts are durable elsewhere:
every executed call lives in ``execution_effects`` with its raw provider call id,
tool name and canonical arguments.  This wrapper re-attaches them as
``metadata["provider_tool_calls"]`` on a *request copy* (never on the stored
Context), which ``OpenAICompatibleProvider`` serializes as ``tool_calls``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from simple_harness.contracts import JsonValue, Message, MessageRole
from simple_harness.providers import ProviderRequest, ProviderResponse
from simple_harness.providers.errors import ProviderProtocolError

PROVIDER_TOOL_CALLS_KEY = "provider_tool_calls"
REQUEST_ID_MARKER = ":provider-turn:"


def run_id_from_request(request_id: str) -> str | None:
    if REQUEST_ID_MARKER not in request_id:
        return None
    return request_id.split(REQUEST_ID_MARKER, 1)[0]


def _ledger_groups(connection: sqlite3.Connection, run_id: str) -> list[dict[str, dict]]:
    """Executed calls grouped by provider turn, in issue order."""

    groups: dict[int, dict[str, dict]] = {}
    for row in connection.execute(
        "SELECT raw_call_id, turn_ordinal, call_ordinal, tool_name, arguments_json"
        " FROM execution_effects WHERE run_id=? AND raw_call_id IS NOT NULL"
        " ORDER BY turn_ordinal, call_ordinal",
        (run_id,),
    ):
        try:
            arguments = json.loads(str(row[4]))
        except (TypeError, ValueError):
            arguments = {}
        groups.setdefault(int(row[1]), {})[str(row[0])] = {
            "name": str(row[3]),
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
    return [groups[key] for key in sorted(groups)]


def restore_tool_calls(
    messages: tuple[Message, ...], groups: list[dict[str, dict]]
) -> tuple[tuple[Message, ...], int]:
    """Return a wire copy of ``messages`` plus the number of calls that fell back to ``{}``."""

    restored: list[Message] = []
    fallbacks = 0
    group_index = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        followers: list[Message] = []
        if message.role is MessageRole.ASSISTANT:
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].role is MessageRole.TOOL:
                followers.append(messages[cursor])
                cursor += 1
        if not followers or PROVIDER_TOOL_CALLS_KEY in message.metadata:
            restored.append(message)
            index += 1
            continue
        group = groups[group_index] if group_index < len(groups) else {}
        group_index += 1
        calls: list[JsonValue] = []
        for follower in followers:
            raw_id = follower.call_id.value if follower.call_id is not None else ""
            fact = group.get(raw_id)
            if fact is None:
                fallbacks += 1
                fact = {"name": follower.name or "unknown", "arguments": {}}
            calls.append({"id": raw_id, "name": fact["name"], "arguments": fact["arguments"]})
        metadata: dict[str, JsonValue] = {**dict(message.metadata), PROVIDER_TOOL_CALLS_KEY: calls}
        restored.append(
            Message(message.role, message.content, name=message.name, metadata=metadata)
        )
        index += 1
    return tuple(restored), fallbacks


class ProviderEmptyResponseError(ProviderProtocolError):
    """The model returned neither public text nor tool calls (F-BA-1 / T10).

    Raised at the wire so the turn fails with a readable code instead of tripping the
    durable-state gate (``provider_response_not_durable``) deep in dispatch.  It is a
    definite failure: the invocation is settled, the turn fails, the Agent lives on.
    """

    __slots__ = ()
    error_code = "provider_empty_response"
    default_message = "Provider returned an empty response without tool calls."


def _is_empty_final(response: ProviderResponse) -> bool:
    if response.tool_calls:
        return False
    content = response.message.content
    if isinstance(content, str):
        return not content.strip()
    return not content


class AgentProviderWire:
    """Consumer ``ProviderPort`` decorator used by ``assemble_runtime``."""

    def __init__(self, inner, database) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self._database = database
        self.fallback_total = 0
        self.last_request: ProviderRequest | None = None

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:  # type: ignore[no-untyped-def]
        run_id = run_id_from_request(request.request_id.value)
        wire_request = request
        if run_id is not None and any(m.role is MessageRole.TOOL for m in request.messages):
            groups = _ledger_groups(self._database.connection, run_id)
            messages, fallbacks = restore_tool_calls(request.messages, groups)
            self.fallback_total += fallbacks
            wire_request = replace(request, messages=messages)
        self.last_request = wire_request
        response = await self._inner.invoke(wire_request, cancel=cancel)
        if _is_empty_final(response):
            finish = getattr(response, "finish_reason", None)
            raise ProviderEmptyResponseError(
                public_message=(
                    "Provider returned an empty response without tool calls"
                    + (f" (finish_reason={finish})." if finish else ".")
                )
            )
        return response


__all__ = (
    "PROVIDER_TOOL_CALLS_KEY",
    "AgentProviderWire",
    "ProviderEmptyResponseError",
    "restore_tool_calls",
    "run_id_from_request",
)
