# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Publishing a verified Artifact into a directory the user authorised (P3.2 plan v3 D6;
plan review round 2 P1-4 / P2-5).

One business action = one file that did not exist before.  The commit point is a single
``os.link`` into the user's directory: it fails if the name is taken, so nothing is ever
overwritten and the operation cannot half-happen.  Before that link the connector appends
its *intent* — key, final path, content hash — to a ledger it owns, outside the user's
directory, and after the read-back it appends ``COMMITTED``.  That makes every crash
decidable (:meth:`FilePublishConnector.lookup`):

* no intent for the key → the link never happened (``CONFIRMED_NOT_STARTED``);
* an intent and a file with the recorded hash → it happened, receipt rebuilt;
* an intent whose file is gone or changed → *not* "never started"; the connector raises,
  reconciliation keeps the action UNKNOWN and a person decides (the directory belongs to
  the user, who may have deleted or edited the file).

The bytes come from the content-addressed Artifact store; the connector re-checks the hash
it was given, never follows a symlink on the way, and refuses any target outside the root.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .connectors import (
    ConnectorRejected,
    ConnectorTransportError,
    OperationSpec,
    Receipt,
    params_hash,
)

PUBLISH_NAME = "file_publish"
LEDGER_FILE = "ledger.jsonl"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_nofollow(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ConnectorRejected(f"artifact_unavailable: {error.strerror}") from error
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _name_for(idempotency_key: str, path: PurePosixPath) -> str:
    """``<stem>.<key digest>.v<version><suffix>`` — stable for one action version and
    distinct from every other action's file.

    The digest covers the *whole* idempotency key (code review round 1 P1-1): slicing the
    hex out of ``action-<hex>`` dropped the ``#comp-<n>`` a compensation carries, so a
    compensation produced the same file name as the action it answers and could never be
    published at all.
    """

    head, _, version = idempotency_key.partition(":v")
    return f"{path.stem}.{_hash(head.encode('utf-8'))[:12]}.v{version or '1'}{path.suffix}"


class FilePublishConnector:
    """``publish`` (L2, state): put one Artifact's bytes at a new name under ``root``."""

    def __init__(self, root: Path | str, ledger_dir: Path | str) -> None:
        self._root = Path(root).resolve()
        self._ledger_dir = Path(ledger_dir)
        self.name = PUBLISH_NAME
        self.supports_idempotency = True
        self.supports_reconciliation = True
        # plan D7: this connector's lookup is authoritative — the ledger is its own, the
        # link is the only commit point, and the intent is always on disk before it
        self.lookup_authority = "authoritative"
        self.operations: Mapping[str, OperationSpec] = {
            "publish": OperationSpec(
                "publish", "L2", ("artifact_path", "content_hash", "storage_uri"), kind="state"
            )
        }
        self.fail_after: str | None = None  # test seam: "intent" | "link" | "commit"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def ledger_path(self) -> Path:
        return self._ledger_dir / LEDGER_FILE

    # ---------------------------------------------------------------- targets
    def normalize_target(self, target: str) -> str:
        parts = [part for part in str(target).strip().split("/") if part not in ("", ".")]
        return "/".join(parts)

    def _relative(self, target: str) -> PurePosixPath:
        text = str(target).strip()
        if not text or text.startswith("/") or "\\" in text:
            raise ConnectorRejected(f"target_outside_root: {text!r}")
        path = PurePosixPath(self.normalize_target(text))
        if not path.name or any(part == ".." for part in path.parts):
            raise ConnectorRejected(f"target_outside_root: {text!r}")
        return path

    def _open_directory(self, parts: tuple[str, ...], *, create: bool) -> int:
        """Walk the directories below the root, never following a symlink."""

        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as error:
                    raise ConnectorRejected(
                        f"target_outside_root: {part!r} is a symlink or not a directory"
                    ) from error
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        return fd

    # ---------------------------------------------------------------- ledger
    def _entries(self, key: str | None = None) -> list[dict[str, Any]]:
        path = self.ledger_path
        if not path.is_file():
            return []
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:  # a line that was not written whole is discarded (the intent is not there)
                entry = json.loads(line)
            except ValueError:
                continue
            if key is None or entry.get("key") == key:
                found.append(entry)
        return found

    def _append(self, entry: Mapping[str, Any]) -> None:
        self._ledger_dir.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            # plan D6: one writer at a time, and the line is on disk before we go on
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(dict(entry), sort_keys=True, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _published(self, entry: Mapping[str, Any]) -> Receipt:
        """The receipt of a key that reached the link — proven by the file, never by the
        ledger alone (code review round 1 P1-2).  A PREPARED line with no file means the
        process died between the intent and the link: that is unknown, not success."""

        try:
            data = _read_nofollow(self._root / str(entry["final_path"]))
        except ConnectorRejected as error:
            raise ConnectorTransportError(
                f"file_publish: {entry['final_path']} has an intent but no file ({error})"
            ) from error
        if _hash(data) != str(entry["content_hash"]):
            raise ConnectorTransportError(
                f"file_publish: {entry['final_path']} was published but has changed"
            )
        return self._receipt(entry)

    def _receipt(self, entry: Mapping[str, Any]) -> Receipt:
        published = self._root / str(entry["final_path"])
        return Receipt(
            idempotency_key=str(entry["key"]),
            connector=self.name,
            operation="publish",
            target=str(entry["target"]),
            params_hash=str(entry["params_hash"]),
            applied=True,
            before=None,
            after={
                "path": str(published),
                "content_hash": str(entry["content_hash"]),
                "bytes": int(entry["bytes"]),
            },
            service_ref=str(entry["final_path"]),
            applied_at=float(entry["applied_at"]),
        )

    # ---------------------------------------------------------------- the operation
    def execute(
        self, operation: str, target: str, params: Mapping[str, Any], *, idempotency_key: str
    ) -> Receipt:
        if operation != "publish":
            raise ConnectorRejected(f"unknown operation {operation}")
        relative = self._relative(target)
        missing = [p for p in self.operations["publish"].required_params if p not in params]
        if missing:
            raise ConnectorRejected(f"publish: missing params {missing}")
        content_hash = str(params["content_hash"])
        data = _read_nofollow(Path(str(params["storage_uri"])))
        if _hash(data) != content_hash:
            raise ConnectorRejected("artifact_bytes_mismatch")
        done = self._entries(idempotency_key)
        if done and done[-1].get("state") != "ABORTED":  # this key already reached the link
            return self._published(done[-1])
        final_name = _name_for(idempotency_key, relative)
        final_path = str(PurePosixPath(*relative.parts[:-1], final_name))
        entry = {
            "key": idempotency_key,
            "state": "PREPARED",
            "target": self.normalize_target(target),
            "final_path": final_path,
            "content_hash": content_hash,
            "bytes": len(data),
            "params_hash": params_hash(params),
            "applied_at": time.time(),
        }
        fd = self._open_directory(relative.parts[:-1], create=True)
        try:
            if self._existing(fd, final_name) is not None:
                # code review round 1 P2-3: a file this key did not create is never adopted,
                # whatever it holds — the key's own file is returned by the fast path above
                raise ConnectorRejected(f"conflict: {final_path} already exists")
            self._append(entry)  # the intent is on disk before the only commit point
            try:
                if self.fail_after == "intent":
                    raise ConnectorTransportError("file_publish: lost after writing the intent")
                temporary = f".publish-{uuid.uuid4().hex}"
                written = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=fd,
                )
                try:
                    with os.fdopen(written, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.link(temporary, final_name, src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            except BaseException:
                # this process knows the link did not happen: say so, so the action can be
                # handed off again instead of waiting for a person (the ledger is ours)
                self._append({**entry, "state": "ABORTED"})
                raise
            os.fsync(fd)
            if self.fail_after == "link":
                raise ConnectorTransportError("file_publish: lost after the link")
            written_back = self._existing(fd, final_name)
            if written_back is None or _hash(written_back) != content_hash:
                raise ConnectorTransportError("file_publish: the published file reads back wrong")
        finally:
            os.close(fd)
        self._append({**entry, "state": "COMMITTED"})
        if self.fail_after == "commit":
            raise ConnectorTransportError("file_publish: lost after the commit")
        return self._receipt(entry)

    def _existing(self, dir_fd: int, name: str) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ConnectorRejected(f"conflict: {name} is not a regular file") from error
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    # ---------------------------------------------------------------- reconciliation
    def lookup(self, idempotency_key: str) -> Receipt | None:
        entries = self._entries(idempotency_key)
        if not entries:
            return None  # the intent always precedes the link: this never started
        entry = entries[-1]
        if entry.get("state") == "ABORTED":
            return None  # the attempt gave up before the link; nothing was published
        published = self._root / str(entry["final_path"])
        try:
            data = _read_nofollow(published)
        except ConnectorRejected as error:
            raise ConnectorTransportError(
                f"file_publish: {entry['final_path']} was published but is gone ({error})"
            ) from error
        if _hash(data) != str(entry["content_hash"]):
            raise ConnectorTransportError(
                f"file_publish: {entry['final_path']} was published but has changed"
            )
        return self._receipt(entry)

    # ---------------------------------------------------------------- authorisation
    @staticmethod
    def supports_hardlinks(root: Path | str) -> bool:
        """Probed before a directory is authorised (review round 2 P2-5): a volume without
        hard links could never commit atomically, so it is never authorised."""

        directory = Path(root)
        if not directory.is_dir():
            return False
        source = directory / f".publish-probe-{uuid.uuid4().hex}"
        target = directory / f".publish-probe-{uuid.uuid4().hex}"
        try:
            source.write_bytes(b"probe")
            os.link(source, target)
            return True
        except OSError:
            return False
        finally:
            source.unlink(missing_ok=True)
            target.unlink(missing_ok=True)


__all__ = ("FilePublishConnector", "PUBLISH_NAME")
