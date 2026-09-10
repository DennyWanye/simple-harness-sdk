# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Parse the single tagged JSON block an Agent must return (§13: no free prose).

``extract_block(text, tag)`` returns the parsed JSON object of the one
``<tag>…</tag>`` block or raises ``BlockError`` with a reason that is fed back to
the Agent on the repair Attempt (``envelope_invalid``).  Zero or several blocks,
non-object JSON and trailing garbage inside the block are all rejected; text
*outside* the block is tolerated but recorded (models add prose).
"""

from __future__ import annotations

import json
import re
from typing import Any


class BlockError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def extract_block(text: str, tag: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise BlockError("empty_output")
    pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
    matches = pattern.findall(text)
    if not matches:
        raise BlockError("block_missing", f"no <{tag}> block in the output")
    if len(matches) > 1:
        raise BlockError("block_ambiguous", f"{len(matches)} <{tag}> blocks")
    body = matches[0].strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise BlockError("invalid_json", f"{error.msg} at line {error.lineno}") from error
    if not isinstance(parsed, dict):
        raise BlockError("not_an_object", type(parsed).__name__)
    return parsed


def outside_text(text: str, tag: str) -> str:
    return re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL).strip()


__all__ = ("BlockError", "extract_block", "outside_text")
