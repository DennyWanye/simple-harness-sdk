# SPDX-License-Identifier: Apache-2.0
"""AppWorld sandbox command journal; ambiguous execution is never resent.

This connector is only for an owned AppWorld benchmark episode. A receipt proves
execution provenance, not that an API mutation or the user's task succeeded.
Business completion needs a separate registered state observer and outcome review.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json
from ..runtime.connectors import ConnectorRejected, ConnectorTransportError, OperationSpec, Receipt, params_hash
from .appworld import AppWorldEpisode


class AppWorldOperationConnector:
    name = "appworld_sandbox"
    supports_idempotency = True
    supports_reconciliation = True
    lookup_authority = "authoritative"
    # This is an isolated benchmark world, not credentials to a user's real app.
    # Arbitrary approved application code can append/send, so it must never be
    # advertised as a convergent state assignment. The deployment explicitly
    # opts this journalled sandbox event into its action policy.
    operations = {"execute": OperationSpec("execute", "L1", ("artifact_path", "content_hash", "storage_uri", "size"), kind="event")}

    def __init__(self, episode: AppWorldEpisode, directory: Path, *, create: bool = False):
        if not isinstance(episode, AppWorldEpisode):
            raise TypeError("AppWorld connector requires the actual owned episode")
        self.episode = episode
        self.root = directory.resolve()
        if create == self.root.exists():
            raise ConnectorTransportError("AppWorld journal needs explicit creation or its original directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "operations.sqlite"
        self._lock = threading.RLock()
        self.identity: dict[str, Any] = {"run_id": episode.run_id, "episode_id": episode.episode_id,
                         "task_id": episode.config.task_id}
        self._creating = create
        with self._exclusive() as db:
            if create:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('STARTED','COMMITTED')), receipt TEXT);
                """)
            original = db.execute("SELECT body FROM identity WHERE id=1").fetchone()
            body = canonical_json(self.identity)
            if (not create and original is None) or (original is not None and original[0] != body):
                raise ConnectorTransportError("original AppWorld episode identity is unavailable")
            if create:
                db.execute("INSERT INTO identity VALUES(1,?)", (body,))
                db.commit()
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        self._creating = False

    @contextmanager
    def _exclusive(self) -> Iterator[sqlite3.Connection]:
        with self._lock, (self.root / "operations.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                db = sqlite3.connect(self.database if self._creating else f"file:{self.database}?mode=rw",
                                     uri=not self._creating)
            except sqlite3.Error as error:
                fcntl.flock(lock, fcntl.LOCK_UN)
                raise ConnectorTransportError("original AppWorld journal is unavailable") from error
            try:
                db.execute("PRAGMA synchronous=FULL")
                yield db
            finally:
                db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def normalize_target(self, target: str) -> str:
        if target != self.episode.config.task_id:
            raise ConnectorRejected("target is outside the frozen AppWorld task")
        return target

    def lookup(self, idempotency_key: str) -> Receipt | None:
        with self._exclusive() as db:
            row = db.execute("SELECT state,receipt FROM operations WHERE key=?", (idempotency_key,)).fetchone()
            if row is None:
                return None  # All dispatches reserve their key before execution.
            if row[0] != "COMMITTED":
                raise ConnectorTransportError("AppWorld execution may have occurred; replay is forbidden")
            return Receipt.from_json(json.loads(row[1]))

    def execute(self, operation: str, target: str, params: Mapping[str, Any], *, idempotency_key: str) -> Receipt:
        if operation != "execute" or not idempotency_key:
            raise ConnectorRejected("unknown AppWorld operation or missing key")
        target = self.normalize_target(target)
        digest = params_hash(params)
        request_hash = params_hash({"operation": operation, "target": target, "params_hash": digest})
        with self._exclusive() as db:
            old = db.execute("SELECT request_hash,state,receipt FROM operations WHERE key=?", (idempotency_key,)).fetchone()
            if old is not None:
                if old[0] != request_hash:
                    raise ConnectorRejected("AppWorld idempotency key conflicts with frozen parameters")
                if old[1] != "COMMITTED":
                    raise ConnectorTransportError("AppWorld execution is unresolved; replay is forbidden")
                return Receipt.from_json(json.loads(old[2]))
            try:
                fd = os.open(str(params["storage_uri"]), os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    raw = stream.read()
                if hashlib.sha256(raw).hexdigest() != params["content_hash"] or len(raw) != params["size"]:
                    raise ConnectorRejected("approved AppWorld program bytes changed")
                code = raw.decode("utf-8")
            except (OSError, KeyError, UnicodeError) as error:
                raise ConnectorRejected("approved AppWorld program is unavailable") from error
            db.execute("INSERT INTO operations VALUES(?,?,'STARTED',NULL)", (idempotency_key, request_hash))
            db.commit()  # FULL synchronous, before the one possible external call.
            # A crash/error after this point leaves STARTED and cannot buy a retry.
            before_ids = {r.execution_id for r in self.episode.list_execution_receipts()}
            before_version = self.episode.world_version
            try:
                output = self.episode.agent.execute(code)
                candidates = [r for r in self.episode.list_execution_receipts()
                              if r.execution_id not in before_ids and r.code_sha256 == params["content_hash"]]
                if len(candidates) != 1:
                    raise ConnectorTransportError("execution provenance is missing or ambiguous")
                execution = candidates[0]
                if not self.episode.verify_execution_observation(execution, **self.identity,
                        execution_id=execution.execution_id, code=code, output=output["output"],
                        world_version=execution.world_version):
                    raise ConnectorTransportError("AppWorld execution provenance is not current")
            except BaseException as error:
                raise ConnectorTransportError("AppWorld execution outcome requires reconciliation") from error
            receipt = Receipt(idempotency_key, self.name, operation, target, digest, True,
                before={"world_version": before_version},
                after={"execution": asdict(execution), "execution_only": True,
                       "output_sha256": execution.output_sha256},
                service_ref=canonical_json(self.identity), applied_at=time.time())
            db.execute("UPDATE operations SET state='COMMITTED',receipt=? WHERE key=?",
                       (canonical_json(receipt.to_json()), idempotency_key))
            db.commit()
            return receipt
