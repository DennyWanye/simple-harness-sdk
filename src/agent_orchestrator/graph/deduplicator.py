# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Duplicate-Task detection for a proposal (theory 10-13; ORCH §5.2).

Step 3 only recognises exact / normalised duplicates (same key, same normalised
goal).  Semantic near-duplicates are *not* merged automatically — "证明 X" and
"寻找 X 的反例" are different work — that is step 5's Manager territory.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence


def normalise_goal(goal: str) -> str:
    text = unicodedata.normalize("NFKC", goal).lower()
    text = re.sub(r"[\s\-_:：，,。.;；、（）()\[\]【】\"'`]+", "", text)
    return text


def find_duplicates(items: Sequence[tuple[str, str]]) -> list[str]:
    """``items`` = (key, goal); returns human-readable duplicate descriptions (empty = ok)."""

    problems: list[str] = []
    seen_keys: set[str] = set()
    seen_goals: dict[str, str] = {}
    for key, goal in items:
        if key in seen_keys:
            problems.append(f"duplicate task key {key!r}")
        seen_keys.add(key)
        norm = normalise_goal(goal)
        if norm in seen_goals:
            problems.append(f"task {key!r} duplicates the goal of {seen_goals[norm]!r}")
        else:
            seen_goals[norm] = key
    return problems


__all__ = ("find_duplicates", "normalise_goal")
