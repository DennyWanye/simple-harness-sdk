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
        unresolved = self.root / candidate
        if unresolved.is_symlink():
            raise WorkspaceError(f"symlinks are not allowed: {relative}")
        target = unresolved.resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise WorkspaceError(f"path escapes the workspace: {relative}")
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
        self,
        attempt_id: str,
        *,
        seed: Mapping[str, str],
        previous: Path | None = None,
        inputs: Mapping[str, Path] | None = None,
    ) -> Workspace:
        """Fresh writable workspace; seeded from the Mission files, the upstream
        inputs (path → accepted artifact file, D3-7') and the previous Attempt's tree
        when this is a repair, so feedback refers to real files."""

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
        for relative, source in (inputs or {}).items():
            target = workspace.resolve(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(source).read_bytes())
        return workspace

    def integrated_copy(
        self, view_id: str, *, seed: Mapping[str, str], files: Mapping[str, Path]
    ) -> Workspace:
        """The Mission-level judgment tree (D3-9'): the seed plus every accepted
        artifact of every Task applied in topological order; rebuilt each time and
        exposed under ``<view_id>-verify`` so a Critic can be bound to it read-only."""

        target = self._root / f"{view_id}-verify"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        copy = Workspace(target, view_id, True)
        for relative, content in seed.items():
            copy.write_text(relative, content)
        for relative, source in files.items():
            resolved = copy.resolve(relative)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(Path(source).read_bytes())
        return copy

    def get(self, attempt_id: str, *, writable: bool = True) -> Workspace:
        root = self._root / attempt_id
        if not root.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        return Workspace(root, attempt_id, writable)

    def verification_copy(
        self, attempt_id: str, *, protected: Mapping[str, str] | None = None
    ) -> Workspace:
        """An independent copy for the Verifier (D12'); rebuilt from the current tree.

        ``protected`` (path → seed content) is re-materialised from the Mission seed
        so a Worker that rewrote its own acceptance tests is verified against the
        real ones (review P0-1).
        """

        source = self._root / attempt_id
        if not source.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        target = self._root / f"{attempt_id}-verify"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
        copy = Workspace(target, attempt_id, True)
        for relative, content in (protected or {}).items():
            copy.write_text(relative, content)
        return copy

    def tampered_protected(self, attempt_id: str, protected: Mapping[str, str]) -> list[str]:
        """Protected seed paths whose content in the Worker's tree differs from the seed."""

        workspace = self.get(attempt_id, writable=False)
        tampered = []
        for relative, content in protected.items():
            try:
                current = workspace.read_text(relative)
            except WorkspaceError:
                tampered.append(relative)
                continue
            if current != content:
                tampered.append(relative)
        return tampered

    def verification_view(self, attempt_id: str) -> Workspace:
        """The Critic's read-only view of the verification copy."""

        target = self._root / f"{attempt_id}-verify"
        if not target.is_dir():
            raise WorkspaceError(f"no verification copy for {attempt_id}")
        return Workspace(target, attempt_id, False)


__all__ = ("MAX_FILE_BYTES", "Workspace", "WorkspaceError", "WorkspaceManager", "sha256_file")
