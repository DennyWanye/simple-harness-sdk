# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``ContextPolicy``: the fixed working-context budget (BA-v1.0 §7.2).

``B_input = min(max_input_tokens, max_total_tokens - output_reserve - safety_margin)``
when the deployment shares one window; a deployment with a separate input limit
sets ``max_total_tokens=None`` and only ``max_input_tokens`` applies.  Everything
that enters the request is counted against ``B_input``: instructions, tool
schemas, the current input, protocol groups, summaries and (S4) recalls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields

from simple_harness.contracts import JsonValue, canonical_json


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    max_input_tokens: int = 32_768
    max_total_tokens: int | None = None
    output_reserve: int = 4_096
    safety_margin: int = 256
    # Tool results above this size are kept whole in the Journal and shown to the
    # model as a preview plus a read-back reference (BA-v1.0 §7.5).
    max_tool_result_tokens: int = 2_048
    tool_result_preview_chars: int = 1_024
    # The rendered request may not exceed the input budget by more than this
    # slack (chat-template framing differs per provider); 0 = exact.
    render_slack_tokens: int = 64

    def __post_init__(self) -> None:
        for name in (
            "max_input_tokens",
            "output_reserve",
            "max_tool_result_tokens",
            "tool_result_preview_chars",
        ):
            _positive_int(getattr(self, name), name)
        for name in ("safety_margin", "render_slack_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_total_tokens is not None:
            _positive_int(self.max_total_tokens, "max_total_tokens")
        if self.input_budget() < 1:
            raise ValueError("context policy leaves no input budget")

    def input_budget(self) -> int:
        """``B_input`` of BA-v1.0 §7.2."""

        budget = self.max_input_tokens
        if self.max_total_tokens is not None:
            budget = min(budget, self.max_total_tokens - self.output_reserve - self.safety_margin)
        return budget

    def to_json(self) -> dict[str, JsonValue]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def policy_hash(policy: ContextPolicy, *, tokenizer_fingerprint: str, model: str) -> str:
    """Identity of a counting regime; a change invalidates every cached count (BA22)."""

    payload: dict[str, JsonValue] = {
        "protocol": "base-agent-context-policy-v1",
        "policy": policy.to_json(),
        "tokenizer": tokenizer_fingerprint,
        "model": model,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ("ContextPolicy", "policy_hash")
