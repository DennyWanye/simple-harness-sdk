# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``TokenizerPort``: the caller injects the model's real counter (BA13 / BA22).

Counts are never "characters / 4".  ``UpperBoundTokenizer`` is the explicit,
conservative fallback (an upper bound for every tokenizer this SDK has met); its
fingerprint marks every selection it produced so a later real tokenizer never
reuses those counts (BA22).  ``TiktokenTokenizer`` wraps ``tiktoken`` when the
caller has it installed; it is an optional dependency, not an SDK requirement.
"""

from __future__ import annotations

import math
from typing import Protocol, cast, runtime_checkable

from simple_harness.contracts import FrozenJsonValue, JsonValue, canonical_json, thaw_json
from simple_harness.contracts.messages import Message
from simple_harness.providers import ProviderToolSpec


@runtime_checkable
class TokenizerPort(Protocol):
    @property
    def fingerprint(self) -> str:
        """Stable identity of the counting rule (model / encoding / version)."""

    def count_text(self, text: str) -> int: ...


class UpperBoundTokenizer:
    """Conservative bound: one token per two UTF-8 bytes, plus per-item overhead.

    Latin text averages ~4 characters per token and CJK ~1.5 bytes-per-token
    ratios stay under 2 bytes per token for the BPE encodings we target, so this
    never under-counts; it over-counts by up to 2x for English prose, which is
    the safe direction for a budget.
    """

    fingerprint = "upper-bound-utf8-bytes-div-2:v1"

    def count_text(self, text: str) -> int:
        return int(math.ceil(len(text.encode("utf-8")) / 2.0))


class TiktokenTokenizer:
    """Real BPE counting through ``tiktoken`` (optional dependency)."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken  # noqa: PLC0415 - optional dependency resolved at construction

        self._encoding = tiktoken.get_encoding(encoding_name)
        self.fingerprint = f"tiktoken:{encoding_name}:{tiktoken.__version__}"

    def count_text(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


# Per-message framing overhead used by chat templates (role markers, separators).
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_OVERHEAD_TOKENS = 8


def message_text(message: Message) -> str:
    """The text a chat template renders for one message (content + call ids + names)."""

    parts: list[str] = []
    if isinstance(message.content, str):
        parts.append(message.content)
    else:
        parts.append(canonical_json([block.to_dict() for block in message.content]))
    if message.name:
        parts.append(message.name)
    if message.call_id is not None:
        parts.append(message.call_id.value)
    calls = message.metadata.get("provider_tool_calls") if message.metadata else None
    if calls:
        parts.append(canonical_json(thaw_json(cast(FrozenJsonValue, calls))))
    return "\n".join(parts)


def count_message(tokenizer: TokenizerPort, message: Message) -> int:
    return tokenizer.count_text(message_text(message)) + MESSAGE_OVERHEAD_TOKENS


def count_tools(tokenizer: TokenizerPort, tools: tuple[ProviderToolSpec, ...]) -> int:
    total = 0
    for tool in tools:
        rendered: dict[str, JsonValue] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": thaw_json(cast(FrozenJsonValue, tool.parameters)),
        }
        total += tokenizer.count_text(canonical_json(rendered)) + TOOL_OVERHEAD_TOKENS
    return total


__all__ = (
    "MESSAGE_OVERHEAD_TOKENS",
    "TOOL_OVERHEAD_TOKENS",
    "TiktokenTokenizer",
    "TokenizerPort",
    "UpperBoundTokenizer",
    "count_message",
    "count_tools",
    "message_text",
)
