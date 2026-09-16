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


#: What to say back to the Agent for each way a block can be unreadable (§18.5 C8).
#: The repair is *bounded*: the same Attempt is told precisely what was wrong and
#: asked again, and a malformed block never becomes a second request that quietly
#: launders the failure into a fresh identity.
REPAIR_HINTS: dict[str, str] = {
    "empty_output": "你没有输出任何内容。只输出一个 <{tag}> 块。",
    "block_missing": "没有找到 <{tag}> 块。只输出一个 <{tag}>…</{tag}> 块，块内是 JSON 对象。",
    "block_ambiguous": "输出里有多个 <{tag}> 块。只保留一个。",
    "invalid_json": "<{tag}> 块内不是合法 JSON（{detail}）。重新输出完整的 JSON 对象。",
    "not_an_object": "<{tag}> 块内必须是 JSON 对象，不是 {detail}。",
}


def repair_hint(error: BlockError, tag: str) -> str:
    """The one instruction the repair Attempt is given for ``error``.

    A single sentence naming the tag and the defect, because the model is being
    asked to fix *this* output — not to be told the whole contract again.
    """

    template = REPAIR_HINTS.get(
        error.reason, "<{tag}> 块无法解析（{detail}）。按契约重新输出该块。"
    )
    return template.format(tag=tag, detail=error.detail or error.reason)


__all__ = ("REPAIR_HINTS", "BlockError", "extract_block", "outside_text", "repair_hint")
