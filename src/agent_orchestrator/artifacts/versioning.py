# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Artifact propagation and versioning across the DAG (§20.3, plan D3-7/D3-8).

A downstream Attempt starts from the *accepted* artifacts of its direct
dependencies: the files are materialised into its workspace and their
``(task_id, path, content_hash)`` are frozen into the dispatch intent as the
Attempt's inputs, so a later inspection can tell exactly which upstream version a
result was built on.  Two dependencies producing the same path with different
content is an ``ArtifactConflict`` (never silently pick one).
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


def collect_upstream_inputs(
    dependencies: Sequence[Task], artifacts_by_task: Mapping[str, Sequence[Artifact]]
) -> list[UpstreamInput]:
    """Accepted artifacts of the direct dependencies, conflict-checked by path."""

    inputs: dict[str, UpstreamInput] = {}
    for task in dependencies:
        accepted = set(task.accepted_artifacts)
        for artifact in artifacts_by_task.get(task.id, ()):
            if artifact.id not in accepted:
                continue
            existing = inputs.get(artifact.path)
            if existing is not None and existing.content_hash != artifact.content_hash:
                raise ArtifactConflict(
                    f"{artifact.path} is produced by both {existing.task_id} and {task.id}"
                    " with different content"
                )
            inputs[artifact.path] = UpstreamInput(
                task.id, artifact.path, artifact.content_hash, artifact.id
            )
    return [inputs[path] for path in sorted(inputs)]


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
    """Highest recorded version per path for one Attempt (input to ``Workspace.snapshot``)."""

    versions: dict[str, int] = {}
    for artifact in existing:
        versions[artifact.path] = max(versions.get(artifact.path, 0), artifact.version)
    return versions


__all__ = (
    "ArtifactConflict",
    "UpstreamInput",
    "collect_upstream_inputs",
    "materialise_inputs",
    "next_versions",
)
