# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Planning-model replies: keys a contract does not name are dropped, not refused.

User decision 2026-09-26: DeepSeek adds keys such as ``rationale`` or
``review_feedback_note`` to otherwise valid planner decisions and method-synthesis
proposals, and the strict refusal ended a seven-step Mission in planning.  The
contracts themselves stay strict (their sources are frozen codec inputs), so the
reply is decoded, and each "has unknown fields" refusal removes exactly those keys
at the place the refusal names before the reply is decoded again.  A dropped key
reaches no contract object, so it carries no authority; callers still run their
system-field and authority-claim scans on the raw reply first.  Review replies and
Worker claims never come through here.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any, TypeVar

from ..contracts.models import ContractError

T = TypeVar("T")

MAX_DROPS = 16
_UNKNOWN = re.compile(r"^(?P<name>.+?) has unknown fields: \[(?P<keys>.*)\]$")
_SEGMENT = re.compile(r"^(?P<key>[^\[\]]*)(?P<indexes>(?:\[\d+\])*)$")


def decode_dropping_unknown(
    decode: Callable[[Any], T], raw: Any, *, root_names: tuple[str, ...]
) -> T:
    """``decode(raw)``, dropping unknown keys named by its refusals (bounded)."""

    data = copy.deepcopy(raw)
    for _ in range(MAX_DROPS):
        try:
            return decode(data)
        except ContractError as error:
            match = _UNKNOWN.match(str(error))
            if match is None:
                raise
            keys = [part.strip().strip("'\"") for part in match["keys"].split(",") if part.strip()]
            if not keys or not _drop(data, match["name"], keys, root_names):
                raise
    return decode(data)


def _drop(data: Any, name: str, keys: list[str], root_names: tuple[str, ...]) -> bool:
    target = _resolve(data, name, root_names)
    if isinstance(target, dict) and all(key in target for key in keys):
        for key in keys:
            del target[key]
        return True
    # A contract that names its objects without the full path: only a unique
    # holder of every refused key is unambiguous.
    holders = [item for item in _objects(data) if all(key in item for key in keys)]
    if len(holders) != 1:
        return False
    for key in keys:
        del holders[0][key]
    return True


def _resolve(data: Any, name: str, root_names: tuple[str, ...]) -> Any:
    segments = name.split(".")
    head = _SEGMENT.match(segments[0])
    if head is None or head["key"] not in root_names:
        return None
    node = _index(data, head["indexes"])
    for segment in segments[1:]:
        match = _SEGMENT.match(segment)
        if match is None or not isinstance(node, dict) or match["key"] not in node:
            return None
        node = _index(node[match["key"]], match["indexes"])
    return node


def _index(node: Any, indexes: str) -> Any:
    for position in re.findall(r"\[(\d+)\]", indexes):
        if not isinstance(node, list) or int(position) >= len(node):
            return None
        node = node[int(position)]
    return node


def _objects(node: Any) -> list[dict]:
    found: list[dict] = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found
