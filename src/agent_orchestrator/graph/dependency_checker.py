# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Dependency checks for a Task DAG proposal (§6.1, §19.3; theory 02-10).

Pure functions over ``key → dependencies`` maps.  A cycle, a self-dependency or
an unknown dependency invalidates the *whole* proposal (§24 step 3: the Graph
Manager rejects, the formal graph does not change).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class DependencyError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def check_dependencies(edges: Mapping[str, Sequence[str]]) -> list[str]:
    """Return the keys in a topological order or raise ``DependencyError``.

    Kahn's algorithm; ties are broken by the proposal order so the assigned
    ordinals are deterministic for the same proposal.
    """

    keys = list(edges)
    known = set(keys)
    for key in keys:
        for dependency in edges[key]:
            if dependency == key:
                raise DependencyError("self_dependency", key)
            if dependency not in known:
                raise DependencyError("missing_dependency", f"{key} -> {dependency}")
        if len(set(edges[key])) != len(edges[key]):
            raise DependencyError("duplicate_dependency", key)
    indegree = {key: len(set(edges[key])) for key in keys}
    dependents: dict[str, list[str]] = {key: [] for key in keys}
    for key in keys:
        for dependency in edges[key]:
            dependents[dependency].append(key)
    ready = [key for key in keys if indegree[key] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort(key=keys.index)
    if len(order) != len(keys):
        remaining = [key for key in keys if key not in order]
        raise DependencyError("cycle", " -> ".join(_cycle_path(edges, remaining)))
    return order


def _cycle_path(edges: Mapping[str, Sequence[str]], remaining: Sequence[str]) -> list[str]:
    """One concrete cycle among the remaining (all-in-cycles) nodes, for the rejection detail."""

    start = remaining[0]
    path = [start]
    seen = {start}
    current = start
    while True:
        nxt = next((d for d in edges[current] if d in remaining), None)
        if nxt is None:
            return path
        if nxt in seen:
            return path[path.index(nxt) :] + [nxt]
        path.append(nxt)
        seen.add(nxt)
        current = nxt


def roots_and_leaves(edges: Mapping[str, Sequence[str]]) -> tuple[list[str], list[str]]:
    dependents = {key: 0 for key in edges}
    for key, dependencies in edges.items():
        for dependency in dependencies:
            dependents[dependency] += 1
    roots = [key for key, dependencies in edges.items() if not dependencies]
    leaves = [key for key, count in dependents.items() if count == 0]
    return roots, leaves


__all__ = ("DependencyError", "check_dependencies", "roots_and_leaves")
