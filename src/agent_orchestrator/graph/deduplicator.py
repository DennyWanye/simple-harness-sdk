# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Duplicate-Task detection for a proposal (theory 10-13; ORCH §5.2; plan D3-16).

A proposal is *rejected* only for a repeated ``key`` or for two nodes that are
identical in every respect that matters (normalised goal, dependency set and
success criteria).  Two nodes that merely share a goal text but differ in
dependencies or criteria are *suspected* duplicates: reported, never merged —
"证明 X" and "寻找 X 的反例" are different work; that is step 5's Manager territory.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


def normalise_goal(goal: str) -> str:
    text = unicodedata.normalize("NFKC", goal).lower()
    text = re.sub(r"[\s\-_:：，,。.;；、（）()\[\]【】\"'`]+", "", text)
    return text


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    problems: tuple[str, ...]  # reject the proposal
    suspected: tuple[str, ...]  # keep going, but say so


def find_duplicates(
    items: Sequence[tuple[str, str, Sequence[str], Sequence[str]]],
) -> DuplicateReport:
    """``items`` = (key, goal, dependencies, success_criteria)."""

    problems: list[str] = []
    suspected: list[str] = []
    seen_keys: set[str] = set()
    seen_goals: dict[str, tuple[str, frozenset[str], tuple[str, ...]]] = {}
    for key, goal, dependencies, criteria in items:
        if key in seen_keys:
            problems.append(f"duplicate task key {key!r}")
        seen_keys.add(key)
        norm = normalise_goal(goal)
        signature = (frozenset(dependencies), tuple(sorted(criteria)))
        previous = seen_goals.get(norm)
        if previous is None:
            seen_goals[norm] = (key, *signature)
            continue
        other_key, other_deps, other_criteria = previous
        if (other_deps, other_criteria) == signature:
            problems.append(f"task {key!r} duplicates task {other_key!r}")
        else:
            suspected.append(f"task {key!r} shares its goal text with {other_key!r}")
    return DuplicateReport(tuple(problems), tuple(suspected))


__all__ = ("DuplicateReport", "find_duplicates", "normalise_goal")
