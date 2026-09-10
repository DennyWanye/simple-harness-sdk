# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Isolated Workspaces and content-addressed Artifacts (§20, plan D12').

Layout under ``<evidence_root>/workspaces/``::

    <attempt_id>/          the Attempt's own writable tree (seeded from the Mission)
    <attempt_id>-verify/   an independent copy the Worker cannot reach; the
                           verification process owns it (may write caches)

Paths handed to tools are resolved against the workspace root and must stay
inside it (no ``..``, no absolute paths, no symlink escape).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Artifact, ids


class WorkspaceError(ValueError):
    pass


MAX_FILE_BYTES = 512 * 1024
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    attempt_id: str
    writable: bool

    def resolve(self, relative: str) -> Path:
        """A path inside the workspace or ``WorkspaceError``; never follows escapes."""

        if not isinstance(relative, str) or not relative.strip():
            raise WorkspaceError("path must be a non-empty string")
        candidate = Path(relative)
        if candidate.is_absolute() or any(
            part in {"..", ""} for part in candidate.parts if part != "."
        ):
            raise WorkspaceError(f"path escapes the workspace: {relative}")
        target = (self.root / candidate).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise WorkspaceError(f"path escapes the workspace: {relative}")
        if target.exists() and target.is_symlink():
            raise WorkspaceError(f"symlinks are not allowed: {relative}")
        return target

    def read_text(self, relative: str) -> str:
        target = self.resolve(relative)
        if not target.is_file():
            raise WorkspaceError(f"no such file: {relative}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"file too large to read: {relative}")
        return target.read_text(encoding="utf-8")

    def write_text(self, relative: str, content: str) -> Path:
        if not self.writable:
            raise WorkspaceError("workspace is read-only")
        if not isinstance(content, str):
            raise WorkspaceError("content must be a string")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkspaceError("content too large")
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def list_files(self) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self._walk())

    def _walk(self) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    yield path

    def snapshot(
        self,
        *,
        mission_id: str,
        task_id: str,
        produced_by: str,
        versions: Mapping[str, int] | None = None,
    ) -> list[Artifact]:
        """Every file as an Artifact record (content hash + per-path version)."""

        artifacts = []
        for relative in self.list_files():
            path = self.root / relative
            content_hash = sha256_file(path)
            version = 1 if versions is None else versions.get(relative, 0) + 1
            artifacts.append(
                Artifact(
                    id=ids.artifact_id(self.attempt_id, relative, content_hash),
                    mission_id=mission_id,
                    task_id=task_id,
                    attempt_id=self.attempt_id,
                    type="file",
                    path=relative,
                    version=version,
                    content_hash=content_hash,
                    size_bytes=path.stat().st_size,
                    produced_by=produced_by,
                    storage_uri=str(path),
                )
            )
        return artifacts


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def create(
        self, attempt_id: str, *, seed: Mapping[str, str], previous: Path | None = None
    ) -> Workspace:
        """Fresh writable workspace; seeded from the Mission files (and the previous
        Attempt's tree when this is a repair, so feedback refers to real files)."""

        root = self._root / attempt_id
        if root.exists():
            return Workspace(root, attempt_id, True)
        root.mkdir(parents=True)
        if previous is not None and previous.is_dir():
            shutil.copytree(
                previous, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS)
            )
        workspace = Workspace(root, attempt_id, True)
        for relative, content in seed.items():
            if not (workspace.root / relative).exists():
                workspace.write_text(relative, content)
        return workspace

    def get(self, attempt_id: str, *, writable: bool = True) -> Workspace:
        root = self._root / attempt_id
        if not root.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        return Workspace(root, attempt_id, writable)

    def verification_copy(self, attempt_id: str) -> Workspace:
        """An independent copy for the Verifier (D12'); rebuilt from the current tree."""

        source = self._root / attempt_id
        if not source.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        target = self._root / f"{attempt_id}-verify"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
        return Workspace(target, attempt_id, True)

    def verification_view(self, attempt_id: str) -> Workspace:
        """The Critic's read-only view of the verification copy."""

        target = self._root / f"{attempt_id}-verify"
        if not target.is_dir():
            raise WorkspaceError(f"no verification copy for {attempt_id}")
        return Workspace(target, attempt_id, False)


__all__ = ("MAX_FILE_BYTES", "Workspace", "WorkspaceError", "WorkspaceManager", "sha256_file")
