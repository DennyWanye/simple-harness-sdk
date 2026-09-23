# SPDX-License-Identifier: Apache-2.0
"""Atomic immutable per-Attempt input binding and exact recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Callable
from typing import Any, Iterator, NoReturn

from simple_harness.contracts import canonical_json

from ..contracts.models import Attempt, ContractError
from ..contracts.htn import TaskSemanticBindingV1
from ..artifacts.taskgraph_inputs import decode_frozen_manifest
from ..graph.attempt_inputs import AttemptInputBinding, FrozenAttemptInputs
from ..graph.revision_records import HistoricalRevision
from .store import Store


class AttemptInputIntegrityError(ContractError):
    pass


def _fail(message: str) -> NoReturn:
    raise AttemptInputIntegrityError(f"TASKGRAPH_ATTEMPT_INPUT_INTEGRITY: {message}")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class TaskGraphAttemptInputStore:
    def __init__(
        self,
        store: Store,
        *,
        revision_reader: Callable[[str, int], HistoricalRevision] | None = None,
    ) -> None:
        if not isinstance(store, Store):
            _fail("TaskGraphAttemptInputStore requires Store")
        self._store = store
        self._revision_reader = revision_reader

    def _revision(self, mission_id: str, revision: int) -> HistoricalRevision:
        reader = self._revision_reader
        if reader is None:
            _fail("the validated TaskGraph revision reader is not installed")
        result = reader(mission_id, revision)
        if not isinstance(result, HistoricalRevision):
            _fail("the TaskGraph revision reader returned the wrong contract")
        return result

    @contextmanager
    def _savepoint(self) -> Iterator[sqlite3.Connection]:
        connection = self._store.connection
        if not connection.in_transaction:
            _fail("insert_attempt_inputs must join the outer dispatch transaction")
        with self._store.transaction() as owned_connection:
            name = "taskgraph_attempt_inputs"
            owned_connection.execute(f"SAVEPOINT {name}")
            try:
                yield owned_connection
            except BaseException:
                owned_connection.execute(f"ROLLBACK TO {name}")
                owned_connection.execute(f"RELEASE {name}")
                raise
            else:
                owned_connection.execute(f"RELEASE {name}")

    @staticmethod
    def _row(row: sqlite3.Row) -> AttemptInputBinding:
        return AttemptInputBinding(**dict(row))

    @staticmethod
    def _attempt(connection: sqlite3.Connection, attempt_id: str) -> tuple[Attempt, sqlite3.Row]:
        row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            _fail("the real Attempt row is unavailable")
        try:
            attempt = Attempt.from_json(json.loads(row["json"]))
        except (ContractError, ValueError, TypeError):
            _fail("the real Attempt row does not decode")
        if (attempt.id != row["attempt_id"] or attempt.mission_id != row["mission_id"]
                or attempt.task_id != row["task_id"]):
            _fail("Attempt columns and immutable body disagree")
        return attempt, row

    @staticmethod
    def _semantic(connection: sqlite3.Connection, task_id: str,
                  binding_revision: int) -> tuple[TaskSemanticBindingV1, sqlite3.Row]:
        row = connection.execute(
            "SELECT * FROM task_semantics WHERE task_id=? AND binding_revision=?",
            (task_id, binding_revision),).fetchone()
        if row is None:
            _fail("the exact Task semantic binding is unavailable")
        try:
            binding = TaskSemanticBindingV1.from_json(json.loads(row["binding_json"]))
        except (ContractError, ValueError, TypeError):
            _fail("the exact Task semantic binding does not decode")
        if (str(binding.task_id) != task_id or int(binding.contract_revision) != binding_revision
                or binding.content_hash() != row["content_hash"]):
            _fail("Task semantic binding identity/hash mismatch")
        return binding, row

    def insert_attempt_inputs(self, *, attempt_id: str, occurrence_id: str,
                              source_revision: int, binding_revision: int,
                              manifest_hash: str, created_at: float) -> AttemptInputBinding:
        with self._savepoint() as connection:
            attempt, attempt_row = self._attempt(connection, attempt_id)
            intent = connection.execute(
                "SELECT * FROM dispatch_intents WHERE subject_id=?", (attempt_id,)).fetchone()
            if intent is None or intent["mission_id"] != attempt.mission_id:
                _fail("the real DispatchIntent is unavailable or belongs to another Mission")
            if (intent["creation_key"] != attempt.creation_key or intent["input_id"] != attempt.input_id
                    or attempt.input_hash is None or intent["input_hash"] != attempt.input_hash):
                _fail("Attempt and DispatchIntent execution identity disagree")
            membership = connection.execute(
                "SELECT task_id,form FROM plan_memberships WHERE mission_id=? AND revision=? "
                "AND occurrence_id=?", (attempt.mission_id, source_revision, occurrence_id)).fetchone()
            if membership is None or membership[0] != attempt.task_id or membership[1] != "primitive":
                _fail("Attempt is not bound to the stated primitive occurrence/revision")
            binding, semantic_row = self._semantic(connection, attempt.task_id, binding_revision)
            if semantic_row["mission_id"] != attempt.mission_id or str(binding.form) != "primitive":
                _fail("Task semantic binding has the wrong Mission or form")
            manifest = connection.execute(
                "SELECT origin_mission_id,manifest_json FROM input_manifests WHERE manifest_hash=?",
                (manifest_hash,),).fetchone()
            if manifest is None:
                _fail("the frozen InputManifest is unavailable")
            try:
                manifest_body = json.loads(manifest["manifest_json"])
            except (ValueError, TypeError):
                _fail("the frozen InputManifest does not decode")
            if canonical_json(manifest_body) != manifest["manifest_json"] or _hash(manifest_body) != manifest_hash:
                _fail("InputManifest bytes/hash mismatch")
            decoded = decode_frozen_manifest(manifest_body)
            if str(decoded.consumer_task_ref) != attempt.task_id:
                _fail("InputManifest consumer differs from the actual Attempt")
            manifest_binding = connection.execute(
                "SELECT input_binding_revision FROM input_manifest_bindings WHERE mission_id=? "
                "AND task_id=? AND manifest_hash=?",
                (attempt.mission_id, attempt.task_id, manifest_hash),).fetchone()
            if manifest_binding is None or manifest_binding[0] != int(binding.input_binding_revision):
                _fail("InputManifest is not bound to the exact Task input revision")
            record = connection.execute(
                "SELECT source_kind,admission_check_id FROM taskgraph_revision_records "
                "WHERE mission_id=? AND revision=?", (attempt.mission_id, source_revision)).fetchone()
            if record is None:
                _fail("the source TaskGraph revision record is unavailable")
            validated_revision = self._revision(attempt.mission_id, source_revision).record
            self._require_pin(validated_revision, occurrence_id, attempt.task_id, binding_revision)
            if (validated_revision.document.revision != source_revision
                    or validated_revision.source_kind != record["source_kind"]
                    or validated_revision.admission_check_id != record["admission_check_id"]):
                _fail("validated revision identity disagrees with the source record")
            admission = record["admission_check_id"]
            if record["source_kind"] == "CAPTURED_BASELINE":
                if admission is not None:
                    _fail("CAPTURED_BASELINE must not carry an admission check")
            else:
                check = connection.execute(
                    "SELECT c.phase,r.mission_id FROM planning_admission_checks c JOIN planning_requests r "
                    "ON r.request_id=c.request_id WHERE c.check_id=?", (admission,)).fetchone()
                if check is None or check[0] != "APPLIED" or check[1] != attempt.mission_id:
                    _fail("source revision does not name a real APPLIED admission check")
            identity = {
                "attempt_id": attempt_id, "mission_id": attempt.mission_id,
                "task_id": attempt.task_id, "occurrence_id": occurrence_id,
                "source_revision": source_revision, "binding_revision": binding_revision,
                "contract_hash": binding.contract_hash,
                "input_binding_revision": int(binding.input_binding_revision),
                "dispatch_generation": int(binding.dispatch_generation),
                "manifest_hash": manifest_hash, "intent_id": intent["intent_id"],
                "creation_key": intent["creation_key"], "input_id": intent["input_id"],
                "frozen_input_hash": intent["input_hash"], "admission_check_id": admission,
            }
            candidate = AttemptInputBinding(**identity, origin_hash=_hash(identity), created_at=created_at)
            existing = connection.execute(
                "SELECT * FROM taskgraph_attempt_inputs WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing is not None:
                stored = self._row(existing)
                if stored.identity_json() != candidate.identity_json() or stored.origin_hash != candidate.origin_hash:
                    _fail("Attempt already has a conflicting immutable input binding")
                return stored
            connection.execute(
                "INSERT INTO taskgraph_attempt_inputs(attempt_id,mission_id,task_id,occurrence_id,"
                "source_revision,binding_revision,contract_hash,input_binding_revision,"
                "dispatch_generation,manifest_hash,intent_id,creation_key,input_id,frozen_input_hash,"
                "admission_check_id,origin_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(candidate.identity_json().values()) + (candidate.origin_hash, candidate.created_at))
            return candidate

    @staticmethod
    def _require_pin(record: Any, occurrence_id: str, task_id: str, binding_revision: int) -> None:
        pins = [pin for pin in record.pins.member_pins if pin.occurrence_id == occurrence_id]
        if (len(pins) != 1 or pins[0].task_id != task_id
                or pins[0].binding_revision != binding_revision):
            _fail("Attempt semantic revision differs from the immutable occurrence pin")

    def get_attempt_inputs(self, mission_id: str, attempt_id: str) -> FrozenAttemptInputs:
        with self._store.read_view() as connection:
            row = connection.execute(
                "SELECT b.*,m.origin_mission_id,m.manifest_json FROM taskgraph_attempt_inputs b "
                "JOIN input_manifests m ON m.manifest_hash=b.manifest_hash "
                "WHERE b.mission_id=? AND b.attempt_id=?", (mission_id, attempt_id)).fetchone()
            if row is None:
                _fail("immutable Attempt inputs are unavailable")
            binding = AttemptInputBinding(**{field: row[field] for field in AttemptInputBinding.__dataclass_fields__})
            validated_revision = self._revision(mission_id, binding.source_revision).record
            self._require_pin(validated_revision, binding.occurrence_id, binding.task_id,
                              binding.binding_revision)
            if validated_revision.admission_check_id != binding.admission_check_id:
                _fail("Attempt binding admission identity disagrees with its validated revision")
            attempt, _ = self._attempt(connection, attempt_id)
            intent = connection.execute("SELECT * FROM dispatch_intents WHERE intent_id=?", (binding.intent_id,)).fetchone()
            if (attempt.mission_id != binding.mission_id or attempt.task_id != binding.task_id
                    or attempt.creation_key != binding.creation_key or attempt.input_id != binding.input_id
                    or attempt.input_hash != binding.frozen_input_hash
                    or intent is None or intent["subject_id"] != attempt_id
                    or intent["kind"] != "attempt" or intent["mission_id"] != mission_id
                    or intent["creation_key"] != binding.creation_key
                    or intent["input_id"] != binding.input_id
                    or intent["input_hash"] != binding.frozen_input_hash):
                _fail("stored Attempt/DispatchIntent identity no longer matches the binding")
            membership = connection.execute(
                "SELECT task_id,form FROM plan_memberships WHERE mission_id=? AND revision=? "
                "AND occurrence_id=?",
                (mission_id, binding.source_revision, binding.occurrence_id),
            ).fetchone()
            if membership is None or tuple(membership) != (binding.task_id, "primitive"):
                _fail("stored occurrence identity no longer matches the Attempt binding")
            semantic, semantic_row = self._semantic(connection, binding.task_id, binding.binding_revision)
            if (semantic_row["mission_id"] != mission_id or semantic.contract_hash != binding.contract_hash
                    or int(semantic.input_binding_revision) != binding.input_binding_revision
                    or int(semantic.dispatch_generation) != binding.dispatch_generation):
                _fail("stored Task semantic identity no longer matches the binding")
            manifest = json.loads(row["manifest_json"])
            decoded_manifest = decode_frozen_manifest(manifest)
            if str(decoded_manifest.consumer_task_ref) != binding.task_id:
                _fail("frozen manifest consumer disagrees with the Attempt binding")
            manifest_binding = connection.execute(
                "SELECT input_binding_revision FROM input_manifest_bindings WHERE mission_id=? "
                "AND task_id=? AND manifest_hash=?",
                (mission_id, binding.task_id, binding.manifest_hash),
            ).fetchone()
            if manifest_binding is None or manifest_binding[0] != binding.input_binding_revision:
                _fail("stored manifest association no longer matches the Attempt binding")
            if (canonical_json(manifest) != row["manifest_json"] or _hash(manifest) != binding.manifest_hash
                    or _hash(binding.identity_json()) != binding.origin_hash):
                _fail("stored InputManifest or origin identity is inconsistent")
            return FrozenAttemptInputs(binding=binding, manifest=manifest)


__all__ = ["AttemptInputIntegrityError", "TaskGraphAttemptInputStore"]
