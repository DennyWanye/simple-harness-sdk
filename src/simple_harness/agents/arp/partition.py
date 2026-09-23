# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Session partition: one derived ``index.sqlite3`` per logical Session (§5, C4).

The partition holds only derived data (chunks, vectors, frozen query snapshots,
cursor pages); the Journal in the execution library stays the single original.
Every table's identity carries ``index_generation`` so a view / chunker /
embedding change is isolated in a new generation.  Physical access is serialised
by the ``FileGuard`` (an OS lock file under ``<root>/locks``), taken *after* the
execution UOW is released and released before anything slow runs (lock order
catalogue → orch → FileGuard → exec → partition).
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import ArpError
from .migration import apply_ddl, partition_ddl
from .pins import Pin
from .ports import RootIdentity
from .rules import validate_vector
from .strict import digest

PARTITION_FILE = "index.sqlite3"
PARTITION_SCHEMA_VERSION = 2
MAX_CHUNK_BYTES = 16384


class FileGuard:
    """Exclusive OS lock per Session, kept outside every SQLite transaction."""

    def __init__(self, root: RootIdentity, session_id: str, *, timeout_s: float = 5.0) -> None:
        self._path = root.locks / f"{session_id}.lock"
        self._timeout = timeout_s
        self._fd: int | None = None
        self.depth = 0

    @contextmanager
    def held(self) -> Iterator[None]:
        if self.depth > 0:
            # Re-entry from the same coordinator is a programming error under the
            # declared lock order; refuse instead of silently nesting.
            raise ArpError("LOCK_TIMEOUT", "FileGuard is not re-entrant")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ArpError("FILE_BUSY", "session file guard is held elsewhere") from None
                    time.sleep(0.01)
            self._fd = fd
            self.depth = 1
            yield
        finally:
            self.depth = 0
            self._fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


@dataclass(frozen=True, slots=True)
class GenerationRow:
    index_generation: int
    view_policy_hash: str
    chunker_fingerprint: str
    embedding_fingerprint: str
    state: str
    next_commit_seq: int
    generation_manifest_hash: str | None

    @property
    def pin(self) -> Pin:
        return Pin(
            "index_generation",
            f"gen:{self.index_generation}",
            self.index_generation,
            self.generation_manifest_hash or digest(self.manifest()),
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "index_generation": self.index_generation,
            "view_policy_hash": self.view_policy_hash,
            "chunker_fingerprint": self.chunker_fingerprint,
            "embedding_fingerprint": self.embedding_fingerprint,
        }


NO_EMBEDDING_FINGERPRINT = digest("no-embedding")


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.execute("PRAGMA busy_timeout = 250")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ArpError("SQL_PRAGMA_UNSUPPORTED", "foreign_keys")


@dataclass(slots=True)
class Partition:
    """An open Session partition (one connection, explicit transactions)."""

    path: Path
    connection: sqlite3.Connection
    session_id: str
    partition_id: str

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            raise RuntimeError("partition transactions never nest")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # ---- generations ---------------------------------------------------------

    def read_generation(self, index_generation: int) -> GenerationRow | None:
        row = self.connection.execute(
            "SELECT index_generation,view_policy_hash,chunker_fingerprint,embedding_fingerprint,state,"
            "next_commit_seq,generation_manifest_hash FROM index_generations WHERE index_generation=?",
            (index_generation,),
        ).fetchone()
        return None if row is None else GenerationRow(int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]), None if row[6] is None else str(row[6]))

    def find_generation(self, *, view_policy_hash: str, chunker_fingerprint: str, embedding_fingerprint: str) -> GenerationRow | None:
        row = self.connection.execute(
            "SELECT index_generation FROM index_generations WHERE view_policy_hash=? AND chunker_fingerprint=?"
            " AND embedding_fingerprint=? AND state IN ('BUILDING','READY') ORDER BY index_generation DESC LIMIT 1",
            (view_policy_hash, chunker_fingerprint, embedding_fingerprint),
        ).fetchone()
        return None if row is None else self.read_generation(int(row[0]))

    def ensure_generation_locked(
        self, connection: sqlite3.Connection, *, view_policy_hash: str, chunker_fingerprint: str, embedding_fingerprint: str
    ) -> GenerationRow:
        """Same fingerprints → same generation; any change starts a new READY generation."""

        existing = self.find_generation(
            view_policy_hash=view_policy_hash, chunker_fingerprint=chunker_fingerprint, embedding_fingerprint=embedding_fingerprint
        )
        if existing is not None:
            return existing
        number = int(connection.execute("SELECT COALESCE(MAX(index_generation),0)+1 FROM index_generations").fetchone()[0])
        manifest = {
            "index_generation": number,
            "view_policy_hash": view_policy_hash,
            "chunker_fingerprint": chunker_fingerprint,
            "embedding_fingerprint": embedding_fingerprint,
        }
        connection.execute(
            "INSERT INTO index_generations(index_generation,view_policy_hash,chunker_fingerprint,embedding_fingerprint,"
            "state,next_commit_seq,generation_manifest_hash) VALUES (?,?,?,?,'BUILDING',1,?)",
            (number, view_policy_hash, chunker_fingerprint, embedding_fingerprint, digest(manifest)),
        )
        connection.execute("UPDATE index_generations SET state='READY' WHERE index_generation=?", (number,))
        row = self.read_generation(number)
        assert row is not None
        return row

    def upper_commit(self, index_generation: int) -> int:
        row = self.connection.execute(
            "SELECT next_commit_seq FROM index_generations WHERE index_generation=?", (index_generation,)
        ).fetchone()
        if row is None:
            raise ArpError("GENERATION_STALE", "unknown index generation")
        return int(row[0]) - 1

    # ---- materialisation -----------------------------------------------------

    def group_source(self, group_id: str) -> tuple[str, int, int] | None:
        row = self.connection.execute(
            "SELECT source_hash,seq_from,seq_to FROM group_sources WHERE group_id=?", (group_id,)
        ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]), int(row[2]))

    def is_materialized(self, index_generation: int, group_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM indexed_groups WHERE index_generation=? AND group_id=?", (index_generation, group_id)
            ).fetchone()
            is not None
        )

    def materialize_group_locked(
        self,
        connection: sqlite3.Connection,
        *,
        generation: GenerationRow,
        group_id: str,
        source_hash: str,
        seq_from: int,
        seq_to: int,
        closed_receipt_ref: Pin,
        view_hash: str,
        chunks: Sequence[Mapping[str, Any]],
        vectors: Mapping[str, Sequence[float]] | None,
        embedding_receipt_ref: Pin | None,
    ) -> Mapping[str, Any]:
        """Append one closed group as one immutable batch under one ``commit_seq``.

        Returns the materialisation receipt body.  ``chunks`` items:
        ``{chunk_id, record_id, utf8_start, utf8_end, text_view, provenance, validity_epoch}``.
        """

        existing = self.group_source(group_id)
        if existing is None:
            connection.execute(
                "INSERT INTO group_sources(group_id,source_hash,seq_from,seq_to,closed_receipt_ref_json) VALUES (?,?,?,?,?)",
                (group_id, source_hash, seq_from, seq_to, _json(closed_receipt_ref.to_json())),
            )
        elif existing[0] != source_hash:
            raise ArpError("GROUP_HASH_CONFLICT", "closed group source differs from the recorded source")
        if self.is_materialized(generation.index_generation, group_id):
            raise ArpError("DUPLICATE_GROUP", "group already materialised in this generation")
        commit_seq = int(
            connection.execute(
                "SELECT next_commit_seq FROM index_generations WHERE index_generation=?", (generation.index_generation,)
            ).fetchone()[0]
        )
        ids = [str(c["chunk_id"]) for c in chunks]
        if len(set(ids)) != len(ids):
            raise ArpError("DUPLICATE_ITEM", "duplicate chunk id in one batch")
        vector_ids = [] if vectors is None else [i for i in ids if i in vectors]
        if vector_ids and embedding_receipt_ref is None:
            raise ArpError("INVALID_EMBEDDING", "vectors need their real embedding receipt")
        receipt = {
            "kind": "partition-materialization-v1",
            "index_generation": generation.index_generation,
            "group_id": group_id,
            "source_hash": source_hash,
            "view_hash": view_hash,
            "commit_seq": commit_seq,
            "chunk_ids": ids,
            "vector_chunk_ids": vector_ids,
            "embedding_receipt_ref": None if embedding_receipt_ref is None else embedding_receipt_ref.to_json(),
        }
        connection.execute(
            "INSERT INTO indexed_groups(index_generation,group_id,source_hash,view_hash,chunk_count,vector_ready_count,"
            "commit_seq,materialization_receipt_json) VALUES (?,?,?,?,?,?,?,?)",
            (generation.index_generation, group_id, source_hash, view_hash, len(chunks), len(vector_ids), commit_seq, _json(receipt)),
        )
        for chunk in chunks:
            text = str(chunk["text_view"])
            if len(text.encode("utf-8")) > MAX_CHUNK_BYTES:
                raise ArpError("DOCUMENT_TOO_LARGE", "chunk view exceeds 16KiB")
            chunk_id = str(chunk["chunk_id"])
            connection.execute(
                "INSERT INTO session_chunks(chunk_id,index_generation,group_id,record_id,utf8_start,utf8_end,source_hash,"
                "view_hash,chunker_fingerprint,text_view,provenance,validity_epoch,commit_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    chunk_id, generation.index_generation, group_id, str(chunk["record_id"]),
                    int(chunk["utf8_start"]), int(chunk["utf8_end"]), source_hash, view_hash,
                    generation.chunker_fingerprint, text, str(chunk["provenance"]), int(chunk.get("validity_epoch", 0)), commit_seq,
                ),
            )
            if chunk_id in vector_ids:
                raw = vectors[chunk_id]  # type: ignore[index]
                values = validate_vector(raw, len(raw))
                connection.execute(
                    "INSERT INTO session_vectors(chunk_id,index_generation,embedding_fingerprint,dim,vector_le_f32,source_hash,"
                    "commit_seq,embedding_receipt_ref_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        chunk_id, generation.index_generation, generation.embedding_fingerprint, len(values),
                        pack_vector(values), source_hash, commit_seq, _json(embedding_receipt_ref.to_json()),  # type: ignore[union-attr]
                    ),
                )
        connection.execute(
            "UPDATE index_generations SET next_commit_seq=? WHERE index_generation=?",
            (commit_seq + 1, generation.index_generation),
        )
        return receipt

    # ---- coverage counters (all bounded by upper_commit) ---------------------

    def coverage(self, index_generation: int, upper_commit: int, journal_highwater: int) -> dict[str, int]:
        indexed = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM indexed_groups g JOIN group_sources s ON s.group_id=g.group_id AND s.source_hash=g.source_hash"
                " WHERE g.index_generation=? AND g.commit_seq<=? AND s.seq_to<=?",
                (index_generation, upper_commit, journal_highwater),
            ).fetchone()[0]
        )
        vector_ready = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM indexed_groups g JOIN group_sources s ON s.group_id=g.group_id AND s.source_hash=g.source_hash"
                " WHERE g.index_generation=? AND g.commit_seq<=? AND s.seq_to<=? AND g.chunk_count=("
                " SELECT COUNT(*) FROM session_chunks c JOIN session_vectors v ON v.chunk_id=c.chunk_id AND v.index_generation=c.index_generation"
                " WHERE c.index_generation=g.index_generation AND c.group_id=g.group_id AND v.commit_seq<=?)",
                (index_generation, upper_commit, journal_highwater, upper_commit),
            ).fetchone()[0]
        )
        chunks = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM session_chunks c JOIN group_sources s ON s.group_id=c.group_id AND s.source_hash=c.source_hash"
                " WHERE c.index_generation=? AND c.commit_seq<=? AND s.seq_to<=?",
                (index_generation, upper_commit, journal_highwater),
            ).fetchone()[0]
        )
        return {"indexed_groups": indexed, "vector_ready_groups": vector_ready, "snapshot_chunks": chunks}

    def indexed_group_set_hash(self, index_generation: int, upper_commit: int, journal_highwater: int) -> str:
        rows = self.connection.execute(
            "SELECT g.group_id,g.source_hash FROM indexed_groups g JOIN group_sources s ON s.group_id=g.group_id AND s.source_hash=g.source_hash"
            " WHERE g.index_generation=? AND g.commit_seq<=? AND s.seq_to<=? ORDER BY g.group_id",
            (index_generation, upper_commit, journal_highwater),
        ).fetchall()
        return digest([[str(r[0]), str(r[1])] for r in rows])


def pack_vector(values: Sequence[float]) -> bytes:
    return struct.pack("<%df" % len(values), *values)


def unpack_vector(blob: bytes, dim: int) -> tuple[float, ...]:
    if len(blob) != 4 * dim:
        raise ArpError("EMBEDDING_DIM_MISMATCH")
    return struct.unpack("<%df" % dim, blob)


def _json(value: Any) -> str:
    from .strict import canonical

    return canonical(value).decode("utf-8")


def partition_path(directory: Path) -> Path:
    return directory / PARTITION_FILE


def open_partition(
    directory: Path,
    *,
    session_id: str,
    agent_id: str,
    root: RootIdentity,
    control_generation: int,
    marker_hash: str,
    source_from: int = 1,
) -> Partition:
    """Open (creating on first use) the Session's partition and verify its identity row."""

    directory.mkdir(parents=True, exist_ok=True)
    path = partition_path(directory)
    fresh = not path.exists()
    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    try:
        _configure(connection)
        partition_id = f"{session_id}:partition"
        if fresh:
            connection.execute("BEGIN IMMEDIATE")
            try:
                apply_ddl(connection, partition_ddl())
                connection.execute(
                    "INSERT INTO session_partition(singleton,session_id,agent_id,root_id,root_incarnation,partition_id,"
                    "control_generation,source_from,schema_version,marker_hash) VALUES (1,?,?,?,?,?,?,?,?,?)",
                    (session_id, agent_id, root.root_id, root.root_incarnation, partition_id, control_generation, source_from, PARTITION_SCHEMA_VERSION, marker_hash),
                )
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        row = connection.execute(
            "SELECT session_id,agent_id,root_id,root_incarnation,partition_id,schema_version,marker_hash FROM session_partition WHERE singleton=1"
        ).fetchone()
        if row is None or (str(row[0]), str(row[1]), str(row[2]), str(row[3])) != (session_id, agent_id, root.root_id, root.root_incarnation):
            raise ArpError("SESSION_IDENTITY_MISMATCH", "partition identity differs from the session")
        if int(row[5]) != PARTITION_SCHEMA_VERSION:
            raise ArpError("GENERATION_STALE", "partition schema version unsupported")
        if str(row[6]) != marker_hash:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "partition marker differs from the session marker")
        return Partition(path, connection, session_id, str(row[4]))
    except BaseException:
        connection.close()
        raise


__all__ = (
    "FileGuard",
    "GenerationRow",
    "MAX_CHUNK_BYTES",
    "NO_EMBEDDING_FINGERPRINT",
    "PARTITION_FILE",
    "Partition",
    "open_partition",
    "pack_vector",
    "partition_path",
    "unpack_vector",
)
