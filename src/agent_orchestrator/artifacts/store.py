# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Content-addressed Artifact bytes (P3.2 plan D3; plan review round 1 P1-1, round 2 P2-3).

Layout under ``<evidence_root>/artifacts/``::

    sha256/<content_hash>   the bytes of every recorded Artifact, written once, read-only

An Artifact's ``storage_uri`` names its file here, so the bytes outlive the workspace that
produced them, and every reader goes through :func:`read_verified`: the last path
component is never followed (``O_NOFOLLOW``), only a regular file is read, and the bytes
must hash to the recorded ``content_hash``.  An empty ``storage_uri`` means the bytes are
unavailable (a library upgraded after the original file was lost or changed); readers
refuse it rather than guess.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from ..contracts import Artifact

HASH_DIR = "sha256"
_HEX = frozenset("0123456789abcdef")


class ArtifactStoreError(ValueError):
    """Bytes that cannot be served: ``reason`` is ``symlink`` / ``missing`` /
    ``not_a_regular_file`` / ``hash_mismatch`` / ``unavailable`` / ``bad_address``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def open_nofollow(path: Path) -> BinaryIO:
    """Open a regular file for reading without following a symlink at the last component."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ArtifactStoreError("symlink", Path(path).name) from error
        raise ArtifactStoreError("missing", Path(path).name) from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArtifactStoreError("not_a_regular_file", Path(path).name)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def read_nofollow(path: Path) -> bytes:
    with open_nofollow(path) as handle:
        return handle.read()


def read_verified(artifact: Artifact) -> bytes:
    """The one way to read an Artifact's bytes (plan D3): no symlink, hash re-checked."""

    if not artifact.storage_uri:
        raise ArtifactStoreError("unavailable", artifact.path)
    data = read_nofollow(Path(artifact.storage_uri))
    if hashlib.sha256(data).hexdigest() != artifact.content_hash:
        raise ArtifactStoreError("hash_mismatch", artifact.path)
    return data


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or not set(content_hash) <= _HEX:
            raise ArtifactStoreError("bad_address", content_hash[:80])
        return self._root / HASH_DIR / content_hash

    def contains(self, path: Path) -> bool:
        """Whether ``path`` names a file of this store (not whether it exists)."""

        return Path(path).parent == self._root / HASH_DIR

    def put_bytes(self, data: bytes) -> str:
        """Write once: the same bytes land at the same address; a missing, tampered or
        substituted (symlink) file at that address is replaced atomically."""

        content_hash = hashlib.sha256(data).hexdigest()
        target = self.path_for(content_hash)
        try:
            if hashlib.sha256(read_nofollow(target)).hexdigest() == content_hash:
                return content_hash
        except ArtifactStoreError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".tmp-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return content_hash

    def put_file(self, source: Path) -> str:
        return self.put_bytes(read_nofollow(Path(source)))

    def read(self, content_hash: str) -> bytes:
        data = read_nofollow(self.path_for(content_hash))
        if hashlib.sha256(data).hexdigest() != content_hash:
            raise ArtifactStoreError("hash_mismatch", content_hash)
        return data


def backfill(artifacts: Iterable[Artifact], store: ArtifactStore) -> list[tuple[str, str]]:
    """Move pre-0.10 Artifacts into the store (plan D3 migration, review round 2 P2-3).

    Returns ``(artifact_id, new storage_uri)`` for every record that changes: the store's
    file when the original is still a regular file with the recorded hash, ``""``
    (unavailable) when it is missing, a symlink or changed.  Re-running is harmless: a
    record already in the store or already unavailable is left alone.
    """

    changes: list[tuple[str, str]] = []
    for artifact in artifacts:
        uri = artifact.storage_uri
        if not uri or store.contains(Path(uri)):
            continue
        try:
            data = read_nofollow(Path(uri))
        except ArtifactStoreError:
            changes.append((artifact.id, ""))
            continue
        if hashlib.sha256(data).hexdigest() != artifact.content_hash:
            changes.append((artifact.id, ""))
            continue
        store.put_bytes(data)
        changes.append((artifact.id, str(store.path_for(artifact.content_hash))))
    return changes


__all__ = (
    "ArtifactStore",
    "ArtifactStoreError",
    "backfill",
    "open_nofollow",
    "read_nofollow",
    "read_verified",
)
