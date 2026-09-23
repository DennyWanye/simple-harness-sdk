# SPDX-License-Identifier: Apache-2.0
"""Offline replay of verified TaskGraph structure into a non-runnable graph database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..graph.network_codec import decode
from ..graph.revision_pins import build_revision_pins
from ..graph.revision_records import HistoricalRevision
from ..storage.store import Store
from ..storage.taskgraph_store import RefVerifier, TaskGraphStore


def _fail(message: str) -> NoReturn:
    raise ContractError(f"TASKGRAPH_REPLAY_INVALID: {message}")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE replay_metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL) STRICT;
CREATE TABLE revisions(
 mission_id TEXT NOT NULL, revision INTEGER NOT NULL, source_kind TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, parent_revision INTEGER, parent_manifest_hash TEXT,
 codec_manifest_hash TEXT NOT NULL, network_json TEXT NOT NULL, projection_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision)
) STRICT;
CREATE TABLE members(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,occurrence_id TEXT NOT NULL,
 task_id TEXT NOT NULL,obligation_id TEXT NOT NULL,form TEXT NOT NULL,object_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,occurrence_id)
) STRICT;
CREATE TABLE methods(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,instance_id TEXT NOT NULL,
 goal_occurrence_id TEXT NOT NULL,adopted INTEGER NOT NULL,draft_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,instance_id)
) STRICT;
CREATE TABLE member_pins(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,occurrence_id TEXT NOT NULL,
 task_id TEXT NOT NULL,binding_revision INTEGER NOT NULL,binding_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,occurrence_id),
 FOREIGN KEY(mission_id,revision,occurrence_id)
 REFERENCES members(mission_id,revision,occurrence_id)
) STRICT;
CREATE TABLE demands(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,consumer_instance_id TEXT NOT NULL,
 slot_key TEXT NOT NULL,slot_occurrence_id TEXT NOT NULL,producer_occurrence_id TEXT NOT NULL,
 obligation_id TEXT NOT NULL,mode TEXT NOT NULL,requiredness TEXT NOT NULL,source_slot_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,consumer_instance_id,slot_key)
) STRICT;
CREATE TABLE order_edges(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,before_occurrence TEXT NOT NULL,
 after_occurrence TEXT NOT NULL,object_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,before_occurrence,after_occurrence)
) STRICT;
CREATE TABLE data_edges(
 mission_id TEXT NOT NULL,revision INTEGER NOT NULL,requirement_id TEXT NOT NULL,
 producer_occurrence TEXT NOT NULL,consumer_occurrence TEXT NOT NULL,object_hash TEXT NOT NULL,
 PRIMARY KEY(mission_id,revision,requirement_id)
) STRICT;
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplayReport:
    mission_id: str
    through_revision: int
    revision_count: int
    coverage_start_revision: int
    coverage_start_kind: str
    target_path: str
    status: str = "GRAPH_PROJECTION_VERIFIED"
    runtime_status: str = "RUNTIME_RESUME_NOT_AUTHORIZED"


def _projection(revision: HistoricalRevision) -> dict[str, Any]:
    record = revision.record
    network = record.document
    decoded = decode(network.to_json()).snapshot
    # Replay the versioned reducer from the frozen document. Source history was
    # already checked, but copying its persisted pins would not reconstruct them.
    pins = build_revision_pins(network)
    object_hashes = {(item.kind, item.identity): item.sha256 for item in network.objects}
    return {
        "network_document": network.to_json(),
        "members": [
            [str(item.occurrence_id), str(item.task_id), str(item.obligation_id), str(item.form),
             object_hashes[("occurrence", str(item.occurrence_id))]]
            for item in decoded.occurrences
        ],
        "methods": [
            [row.instance_id, row.goal_occurrence_id, row.adopted, row.draft_hash]
            for row in pins.method_pins
        ],
        "member_pins": [
            [row.occurrence_id, row.task_id, row.binding_revision, row.binding_hash]
            for row in pins.member_pins
        ],
        "demands": [
            [row.consumer_instance_id, row.slot_key, row.slot_occurrence_id,
             row.producer_occurrence_id, row.obligation_id, row.mode, row.requiredness,
             row.source_slot_hash]
            for row in pins.demand_refs
        ],
        "order": [
            [str(item.before), str(item.after),
             object_hashes[("order_constraint", canonical_json([str(item.before), str(item.after)]))]]
            for item in decoded.order_constraints
        ],
        "data": [
            [item.requirement_id, str(item.producer_occurrence), str(item.consumer_occurrence),
             object_hashes[("data_requirement", item.requirement_id)]]
            for item in decoded.data_requirements
        ],
    }


def _readback(connection: sqlite3.Connection, mission: str, revision: int) -> dict[str, Any]:
    specifications = {
        "members": ("occurrence_id,task_id,obligation_id,form,object_hash", "members"),
        "member_pins": ("occurrence_id,task_id,binding_revision,binding_hash", "member_pins"),
        "methods": ("instance_id,goal_occurrence_id,adopted,draft_hash", "methods"),
        "demands": ("consumer_instance_id,slot_key,slot_occurrence_id,producer_occurrence_id,"
                    "obligation_id,mode,requiredness,source_slot_hash", "demands"),
        "order": ("before_occurrence,after_occurrence,object_hash", "order_edges"),
        "data": ("requirement_id,producer_occurrence,consumer_occurrence,object_hash", "data_edges"),
    }
    projection = {
        label: [list(row) for row in connection.execute(
            f"SELECT {columns} FROM {table} WHERE mission_id=? AND revision=? ORDER BY 1,2",
            (mission, revision),)]
        for label, (columns, table) in specifications.items()
    }
    network_json = connection.execute(
        "SELECT network_json FROM revisions WHERE mission_id=? AND revision=?",
        (mission, revision),
    ).fetchone()
    if network_json is None:
        _fail("target revision document is missing")
    projection["network_document"] = json.loads(str(network_json[0]))
    return projection


def replay_taskgraph(
    source: Store,
    *,
    mission_id: str,
    through_revision: int,
    target_path: str | Path,
    external_ref_verifier: RefVerifier | None = None,
) -> ReplayReport:
    """Verify and replay structure only; never creates a runnable SDK Store."""

    if not isinstance(source, Store) or type(through_revision) is not int or through_revision < 0:
        _fail("source and through_revision are invalid")
    target = Path(target_path).expanduser().resolve()
    source_row = source.connection.execute("PRAGMA database_list").fetchone()
    source_path = Path(str(source_row[2])).resolve() if source_row and source_row[2] else None
    if source_path is not None and target == source_path:
        _fail("target must not be the source database")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created_target = False
    connection: sqlite3.Connection | None = None
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        created_target = True
        os.close(descriptor)
        descriptor = None
        with source.read_view():
            reader = TaskGraphStore(source, external_ref_verifier=external_ref_verifier)
            rows = source.connection.execute(
                "SELECT revision,source_kind FROM taskgraph_revision_records WHERE mission_id=? "
                "AND revision<=? ORDER BY revision", (mission_id, through_revision),).fetchall()
            if not rows or int(rows[-1][0]) != through_revision:
                _fail("the requested through revision is unavailable")
            start_revision, start_kind = int(rows[0][0]), str(rows[0][1])
            if start_kind not in {"SEED_COMMIT", "CAPTURED_BASELINE"}:
                _fail("history does not begin at an explicit seed or captured baseline")
            expected_revisions = tuple(range(start_revision, through_revision + 1))
            if tuple(int(row[0]) for row in rows) != expected_revisions:
                _fail("revision history is incomplete")
            verified = [reader.read_revision(mission_id, revision) for revision in expected_revisions]
        connection = sqlite3.connect(target, isolation_level=None)
        connection.executescript(_DDL)
        connection.execute("BEGIN IMMEDIATE")
        for historical in verified:
            record = historical.record
            revision = record.document.revision
            projection = _projection(historical)
            projection = {
                key: sorted(value) if isinstance(value, list) else value
                for key, value in projection.items()
            }
            projection_hash = _hash(projection)
            prefix = (mission_id, revision)
            connection.executemany("INSERT INTO members VALUES (?,?,?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["members"]])
            connection.executemany("INSERT INTO member_pins VALUES (?,?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["member_pins"]])
            connection.executemany("INSERT INTO methods VALUES (?,?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["methods"]])
            connection.executemany("INSERT INTO demands VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["demands"]])
            connection.executemany("INSERT INTO order_edges VALUES (?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["order"]])
            connection.executemany("INSERT INTO data_edges VALUES (?,?,?,?,?,?)",
                                   [prefix + tuple(row) for row in projection["data"]])
            connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)", (
                mission_id, revision, record.source_kind, record.manifest_hash,
                record.parent_revision, record.parent_manifest_hash,
                record.document.codec_manifest_hash,
                canonical_json(record.document.to_json()), projection_hash))
            if _hash(_readback(connection, mission_id, revision)) != projection_hash:
                _fail("target projection hash differs from the verified source projection")
        metadata: dict[str, Any] = {
            "format": "taskgraph-offline-graph-replay-v2",
            "mission_id": mission_id,
            "through_revision": through_revision,
            "coverage_start_revision": start_revision,
            "coverage_start_kind": start_kind,
            "status": "GRAPH_PROJECTION_VERIFIED",
            "runtime_status": "RUNTIME_RESUME_NOT_AUTHORIZED",
        }
        for key, value in metadata.items():
            connection.execute("INSERT INTO replay_metadata VALUES (?,?)",
                               (key, canonical_json(value)))
        connection.execute("COMMIT")
        connection.close()
        connection = None
        return ReplayReport(mission_id=mission_id, through_revision=through_revision,
                            revision_count=len(verified), coverage_start_revision=start_revision,
                            coverage_start_kind=start_kind, target_path=str(target))
    except BaseException:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if descriptor is not None:
            os.close(descriptor)
        if created_target:
            for candidate in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm"),
                              Path(str(target) + "-journal")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
        raise


__all__ = ["ReplayReport", "replay_taskgraph"]
