# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Isolated Workspaces and content-addressed Artifacts (§20, plan D12', P3.2 plan D3).

Layout under ``<evidence_root>/workspaces/``::

    <attempt_id>/            the Attempt's own writable tree (seeded from the Mission);
                             only the Tool Gateway's file tools write it
    <attempt_id>-verify/     the Verifier's copy, rebuilt from the Mission seed, the
                             upstream inputs and the Attempt's recorded Artifacts (P3.2
                             review round 2 P1-3: what is verified is what was recorded)
    <attempt_id>-exec-<id>/  a throw-away copy model-written code runs in (``run_tests``),
                             removed when the run ends (P3.2 review round 2 P1-1)

Paths handed to tools are resolved against the workspace root and must stay inside it
(no ``..``, no absolute paths, no symlink escape).  No copy the system makes ever follows
a symlink, and a tree that holds one is refused (``workspace_symlink``) instead of being
copied or recorded (P3.2 review round 1 P0-1): a link into the host would otherwise turn
into host bytes in a later copy.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Artifact, ids
from .store import ArtifactStore, ArtifactStoreError, read_nofollow, read_verified


class WorkspaceError(ValueError):
    pass


MAX_FILE_BYTES = 512 * 1024
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git"}
EXEC_COPY_MARK = "-exec-"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_symlinks(root: Path) -> list[str]:
    """Every symlink (file or directory) under ``root``, relative, without following any."""

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        kept = []
        for name in dirnames:
            if name in IGNORED_DIRS:
                continue
            if (base / name).is_symlink():
                found.append(str((base / name).relative_to(root)))
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if (base / name).is_symlink():
                found.append(str((base / name).relative_to(root)))
    return sorted(found)


def _symlink_refusal(links: Sequence[str]) -> WorkspaceError:
    # the link paths only — never where they point
    return WorkspaceError(
        "workspace_symlink: symlinks are not allowed in a workspace: " + ", ".join(links)
    )


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        symlinks=True,  # a link is copied as a link, never dereferenced into host bytes
        ignore=shutil.ignore_patterns(*IGNORED_DIRS),
    )
    links = scan_symlinks(target)
    if links:
        shutil.rmtree(target, ignore_errors=True)
        raise _symlink_refusal(links)


def _source_bytes(source: Path, label: str) -> bytes:
    try:
        return read_nofollow(Path(source))
    except ArtifactStoreError as error:
        if error.reason == "symlink":
            raise _symlink_refusal([label]) from error
        raise WorkspaceError(f"{error.reason}: {label}") from error


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    attempt_id: str
    writable: bool
    store: ArtifactStore | None = None  # P3.2 D3: where snapshot() puts the bytes

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

    def write_bytes(self, relative: str, data: bytes) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def list_files(self) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self._walk())

    def symlinks(self) -> list[str]:
        return scan_symlinks(self.root)

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
        """Every file as an Artifact record (content hash + per-path version); with a
        store, the bytes are written to it and ``storage_uri`` names the stored file.  A
        tree holding a symlink is refused, never recorded without it (P3.2 D3)."""

        links = self.symlinks()
        if links:
            raise _symlink_refusal(links)
        artifacts = []
        for relative in self.list_files():
            data = _source_bytes(self.root / relative, relative)
            content_hash = hashlib.sha256(data).hexdigest()
            storage_uri = str(self.root / relative)
            if self.store is not None:
                self.store.put_bytes(data)
                storage_uri = str(self.store.path_for(content_hash))
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
                    size_bytes=len(data),
                    produced_by=produced_by,
                    storage_uri=storage_uri,
                    workspace=self.attempt_id,  # §20.2: one writable workspace per Attempt
                )
            )
        return artifacts


class WorkspaceManager:
    def __init__(self, root: Path, *, artifact_store: ArtifactStore | None = None) -> None:
        self._root = Path(root)
        self._store = (
            artifact_store
            if artifact_store is not None
            else ArtifactStore(self._root.parent / "artifacts")
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._store

    def _workspace(self, root: Path, attempt_id: str, writable: bool) -> Workspace:
        return Workspace(root, attempt_id, writable, self._store)

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
        when this is a repair, so feedback refers to real files.  A previous tree that
        holds a symlink is refused (P3.2 D3) and nothing is left behind."""

        root = self._root / attempt_id
        if root.exists():
            return self._workspace(root, attempt_id, True)
        root.mkdir(parents=True)
        try:
            if previous is not None and previous.is_dir():
                _copy_tree(previous, root)
            workspace = self._workspace(root, attempt_id, True)
            for relative, content in seed.items():
                if not (workspace.root / relative).exists():
                    workspace.write_text(relative, content)
            for relative, source in (inputs or {}).items():
                workspace.write_bytes(relative, _source_bytes(source, relative))
        except WorkspaceError:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return workspace

    def integrated_copy(
        self, view_id: str, *, seed: Mapping[str, str], files: Mapping[str, Path | bytes]
    ) -> Workspace:
        """The Mission-level judgment tree (D3-9'): the seed plus every accepted
        artifact of every Task applied in topological order; rebuilt each time and
        exposed under ``<view_id>-verify`` so a Critic can be bound to it read-only."""

        target = self._root / f"{view_id}-verify"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        copy = self._workspace(target, view_id, True)
        try:
            for relative, content in seed.items():
                copy.write_text(relative, content)
            for relative, source in files.items():
                data = source if isinstance(source, bytes) else _source_bytes(source, relative)
                copy.write_bytes(relative, data)
        except WorkspaceError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return copy

    def get(self, attempt_id: str, *, writable: bool = True) -> Workspace:
        root = self._root / attempt_id
        if not root.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        return self._workspace(root, attempt_id, writable)

    def verification_copy(
        self,
        attempt_id: str,
        *,
        protected: Mapping[str, str] | None = None,
        seed: Mapping[str, str] | None = None,
        inputs: Mapping[str, Path | bytes] | None = None,
        artifacts: Sequence[Artifact] | None = None,
    ) -> Workspace:
        """An independent copy for the Verifier (D12').

        With ``artifacts`` (P3.2 review round 2 P1-3) the copy is rebuilt from the Mission
        ``seed``, the upstream ``inputs`` and the Attempt's recorded Artifacts read back
        from the store — later changes to the live tree cannot reach it.  Without, it is a
        copy of the live tree that refuses any symlink.  ``protected`` (path → seed
        content) is re-materialised last so a Worker that rewrote its own acceptance
        tests is verified against the real ones (review P0-1).
        """

        source = self._root / attempt_id
        if not source.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        target = self._root / f"{attempt_id}-verify"
        if target.exists():
            shutil.rmtree(target)
        if artifacts is None:
            _copy_tree(source, target)
        else:
            target.mkdir(parents=True)
            rebuilt = self._workspace(target, attempt_id, True)
            try:
                for relative, content in (seed or {}).items():
                    rebuilt.write_text(relative, content)
                for relative, source in (inputs or {}).items():
                    data = source if isinstance(source, bytes) else _source_bytes(source, relative)
                    rebuilt.write_bytes(relative, data)
                for artifact in artifacts:
                    try:
                        data = read_verified(artifact)
                    except ArtifactStoreError as error:
                        raise WorkspaceError(f"artifact_unreadable: {error}") from error
                    rebuilt.write_bytes(artifact.path, data)
            except WorkspaceError:
                shutil.rmtree(target, ignore_errors=True)
                raise
        copy = self._workspace(target, attempt_id, True)
        for relative, content in (protected or {}).items():
            copy.write_text(relative, content)
        return copy

    def exec_copy(self, attempt_id: str) -> Workspace:
        """A throw-away copy of the live tree for running model-written code (P3.2
        review round 2 P1-1): whatever that code writes — caches, temp files, symlinks —
        never reaches the Attempt's own tree.  Remove it with :meth:`discard`."""

        source = self._root / attempt_id
        if not source.is_dir():
            raise WorkspaceError(f"no workspace for {attempt_id}")
        target = self._root / f"{attempt_id}{EXEC_COPY_MARK}{uuid.uuid4().hex[:12]}"
        _copy_tree(source, target)
        return self._workspace(target, attempt_id, True)

    def discard(self, copy: Workspace) -> None:
        if EXEC_COPY_MARK not in copy.root.name or copy.root.parent != self._root:
            raise WorkspaceError(f"not an execution copy: {copy.root.name}")
        shutil.rmtree(copy.root, ignore_errors=True)

    def sweep_exec_copies(self, *, older_than: float = 3600.0) -> list[str]:
        """Execution copies left by a crash; a copy younger than ``older_than`` seconds may
        belong to another live instance and is left alone."""

        removed = []
        if not self._root.is_dir():
            return removed
        cutoff = time.time() - older_than
        for entry in self._root.iterdir():
            if EXEC_COPY_MARK in entry.name and entry.is_dir() and not entry.is_symlink():
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed.append(entry.name)
        return sorted(removed)

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
        return self._workspace(target, attempt_id, False)


__all__ = (
    "EXEC_COPY_MARK",
    "MAX_FILE_BYTES",
    "Workspace",
    "WorkspaceError",
    "WorkspaceManager",
    "scan_symlinks",
    "sha256_file",
)
