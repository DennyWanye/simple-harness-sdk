# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Artifact propagation and versioning across the DAG (§20.3, plan D3-7'/D3-8').

A downstream Attempt starts from the *accepted* artifacts of **all** its ancestors,
applied in topological order: a later producer overrides an earlier one only when
it depends (transitively) on that producer — it saw the earlier file and changed
it on purpose.  Two producers that are independent of each other (parallel
branches) giving the same path *different* content is an ``ArtifactConflict``
(never silently pick one); the same content is not a conflict.  The resulting
``(task_id, path, content_hash)`` set is frozen into the dispatch intent as the
Attempt's inputs, so a later inspection can tell exactly which upstream version a
result was built on — and the verifier re-materialises those inputs as protected
files (a downstream Worker may not rewrite what it was given).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import Artifact, Task


class ArtifactConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UpstreamInput:
    task_id: str
    path: str
    content_hash: str
    artifact_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "path": self.path,
            "content_hash": self.content_hash,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> UpstreamInput:
        return cls(
            str(value["task_id"]),
            str(value["path"]),
            str(value["content_hash"]),
            str(value["artifact_id"]),
        )


def ancestors(task_id: str, tasks_by_id: Mapping[str, Task]) -> list[Task]:
    """All transitive dependencies of ``task_id`` in topological order (step 5 D5-3': a
    BLOCKED Task rewired to a later-created dependency keeps its ordinal, so the order
    is computed from the edges; ordinal only breaks ties)."""

    seen: set[str] = set()

    def visit(current: str) -> None:
        task = tasks_by_id.get(current)
        if task is None:
            return
        for dep in task.dependency_ids:
            if dep not in seen:
                seen.add(dep)
                visit(dep)

    visit(task_id)
    # review P2-2 / D5-4: a superseded (CANCELLED) ancestor contributes nothing — its
    # accepted artifacts belong to the plan the Manager retired
    live = [tasks_by_id[t] for t in seen if str(tasks_by_id[t].status) != "CANCELLED"]
    return topological(live, tasks_by_id)


def topological(tasks: Sequence[Task], tasks_by_id: Mapping[str, Task]) -> list[Task]:
    """Kahn over the given tasks' dependency edges (restricted to the set), ordinal as
    the deterministic tie-break."""

    members = {task.id: task for task in tasks}
    indegree = {tid: sum(1 for d in t.dependency_ids if d in members) for tid, t in members.items()}
    ready = sorted((tid for tid, n in indegree.items() if n == 0), key=_ordinal)
    order: list[Task] = []
    while ready:
        current = ready.pop(0)
        order.append(members[current])
        for tid, task in members.items():
            if current in task.dependency_ids:
                indegree[tid] -= 1
                if indegree[tid] == 0:
                    ready.append(tid)
                    ready.sort(key=_ordinal)
    if len(order) != len(members):  # a cycle can only come from a corrupted library
        order.extend(
            sorted((t for t in members.values() if t not in order), key=lambda t: _ordinal(t.id))
        )
    return order


def _ordinal(task_id: str) -> int:
    try:
        return int(task_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def merge_accepted(
    producers: Sequence[Task],
    artifacts_by_task: Mapping[str, Sequence[Artifact]],
    *,
    tasks_by_id: Mapping[str, Task],
) -> list[UpstreamInput]:
    """Accepted artifacts of ``producers`` (topological order) merged by path.

    Override is legal only along a dependency chain; independent producers with
    different content for one path raise ``ArtifactConflict``.
    """

    closure = {task.id: {t.id for t in ancestors(task.id, tasks_by_id)} for task in producers}
    inputs: dict[str, UpstreamInput] = {}
    for task in topological(list(producers), tasks_by_id):
        accepted = set(task.accepted_artifacts)
        for artifact in artifacts_by_task.get(task.id, ()):
            if artifact.id not in accepted:
                continue
            existing = inputs.get(artifact.path)
            if existing is not None and existing.content_hash != artifact.content_hash:
                if existing.task_id not in closure.get(task.id, set()):
                    raise ArtifactConflict(
                        f"{artifact.path} is produced by both {existing.task_id} and {task.id}"
                        " (independent branches) with different content"
                    )
            inputs[artifact.path] = UpstreamInput(
                task.id, artifact.path, artifact.content_hash, artifact.id
            )
    return [inputs[path] for path in sorted(inputs)]


def collect_upstream_inputs(
    task: Task, tasks_by_id: Mapping[str, Task], artifacts_by_task: Mapping[str, Sequence[Artifact]]
) -> list[UpstreamInput]:
    """What a new Attempt of ``task`` starts from: every ancestor's accepted artifacts."""

    return merge_accepted(
        ancestors(task.id, tasks_by_id), artifacts_by_task, tasks_by_id=tasks_by_id
    )


def materialise_inputs(
    workspace_root: Path, inputs: Sequence[UpstreamInput], artifacts_by_id: Mapping[str, Artifact]
) -> list[str]:
    """Copy each upstream artifact file into the workspace (verifying its hash first)."""

    from .workspace import sha256_file

    written = []
    for item in inputs:
        artifact = artifacts_by_id[item.artifact_id]
        source = Path(artifact.storage_uri)
        if not source.is_file() or sha256_file(source) != item.content_hash:
            raise ArtifactConflict(
                f"upstream artifact {item.artifact_id} ({item.path}) is missing or changed"
            )
        target = workspace_root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        written.append(item.path)
    return written


def next_versions(existing: Sequence[Artifact]) -> dict[str, int]:
    """Highest recorded version per path (Mission-wide lineage, D3-8')."""

    versions: dict[str, int] = {}
    for artifact in existing:
        versions[artifact.path] = max(versions.get(artifact.path, 0), artifact.version)
    return versions


__all__ = (
    "ArtifactConflict",
    "UpstreamInput",
    "ancestors",
    "topological",
    "collect_upstream_inputs",
    "materialise_inputs",
    "merge_accepted",
    "next_versions",
)
