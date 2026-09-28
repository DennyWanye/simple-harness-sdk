# SPDX-License-Identifier: Apache-2.0
"""Atomic TaskGraph revision history persistence over the existing Store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import astuple
from typing import Iterator, NoReturn

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..graph.network_codec import NetworkDocumentV1, decode
from ..graph.revision_events import revision_event_payload
from ..graph.revision_pins import DemandRef, MemberPin, MethodPin, RevisionPins, build_revision_pins, verify_revision_pins
from ..graph.revision_records import BaselineProofContext, CapturedBaselineCertificate, HistoricalRevision, PlanAdmissionCertificate, RevisionCertificate, RevisionRecord, SourceRef, certificate_from_json
from .store import Store
from .htn_store import HtnStore
from .taskgraph_history_sources import validate_revision_sources


class GraphIntegrityError(ContractError):
    pass


RefVerifier = Callable[[sqlite3.Connection, SourceRef, BaselineProofContext], bool]


def _fail(message: str) -> NoReturn:
    raise GraphIntegrityError(f"TASKGRAPH_HISTORY_INTEGRITY: {message}")


def _document_hash(document: NetworkDocumentV1) -> str:
    return hashlib.sha256(canonical_json(document.to_json()).encode("utf-8")).hexdigest()


class TaskGraphStore:
    def __init__(self, store: Store, *, external_ref_verifier: RefVerifier | None = None) -> None:
        if not isinstance(store, Store):
            _fail("TaskGraphStore requires Store")
        self._store = store
        self._external_ref_verifier = external_ref_verifier
        # (mission, revision) -> (read generation it was verified under, result)
        self._verified: dict[tuple[str, int], tuple[tuple[int, int, int], HistoricalRevision]] = {}

    @property
    def store(self) -> Store:
        return self._store

    @contextmanager
    def _savepoint(self) -> Iterator[sqlite3.Connection]:
        connection = self._store.connection
        if not connection.in_transaction:
            _fail("insert_revision_record must join the outer Commit transaction")
        with self._store.transaction() as owned_connection:
            name = "taskgraph_revision_record"
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
    def _pins(connection: sqlite3.Connection, mission: str, revision: int) -> RevisionPins:
        members = tuple(MemberPin(**dict(row)) for row in connection.execute(
            "SELECT mission_id,revision,occurrence_id,task_id,binding_revision,binding_hash "
            "FROM taskgraph_member_pins WHERE mission_id=? AND revision=?", (mission, revision)))
        methods = tuple(MethodPin(**dict(row)) for row in connection.execute(
            "SELECT mission_id,revision,instance_id,goal_occurrence_id,adopted,draft_hash "
            "FROM taskgraph_method_pins WHERE mission_id=? AND revision=?", (mission, revision)))
        demands = tuple(DemandRef(**dict(row)) for row in connection.execute(
            "SELECT mission_id,revision,consumer_instance_id,slot_key,slot_occurrence_id,"
            "producer_occurrence_id,obligation_id,mode,requiredness,source_slot_hash "
            "FROM taskgraph_demand_refs WHERE mission_id=? AND revision=?", (mission, revision)))
        return RevisionPins(member_pins=members, method_pins=methods, demand_refs=demands)

    def _verify_ref(self, connection: sqlite3.Connection, reference: SourceRef,
                    context: BaselineProofContext) -> None:
        if reference.channel == "taskgraph_baseline_quiescence":
            from ..orchestrator.taskgraph_baseline_proof import verify_baseline_proof
            if not verify_baseline_proof(connection, reference, context):
                _fail("original baseline quiescence proof does not bind this capture")
            return
        if self._external_ref_verifier is None or self._external_ref_verifier(connection, reference, context) is not True:
            _fail(f"required certificate source channel {reference.channel!r} is unavailable")

    def _verify_policy(self, connection: sqlite3.Connection, mission: str) -> None:
        row = connection.execute("SELECT * FROM taskgraph_policy_bindings WHERE mission_id=?", (mission,)).fetchone()
        if row is None or row["kernel_version"] != "taskgraph-exec-v2":
            _fail("TaskGraph policy is not enabled")
        raw = str(row["policy_json"])
        if canonical_json(json.loads(raw)) != raw or hashlib.sha256(raw.encode()).hexdigest() != row["policy_hash"]:
            _fail("TaskGraph policy bytes/hash mismatch")
        receipt = self._store.get_receipt(row["enabling_command_id"])
        if (receipt is None or receipt.get("mission_id") != mission
                or receipt.get("command_id") != row["enabling_command_id"]
                or receipt.get("policy_hash") != row["policy_hash"]
                or receipt.get("kind") != "TaskGraphContractEnabled"
                or hashlib.sha256(canonical_json(dict(receipt)).encode()).hexdigest() != row["enabling_receipt_hash"]):
            _fail("TaskGraph enabling receipt does not match its immutable policy")
        acceptance = SourceRef.from_json(receipt.get("deployment_acceptance_ref"))
        if acceptance.channel != "h1h_deployment_acceptance":
            _fail("TaskGraph enabling receipt lacks actual deployment acceptance identity")

    def _verify_certificate(self, connection: sqlite3.Connection, mission: str, command: str,
                            admission: str | None, certificate: RevisionCertificate,
                            document: NetworkDocumentV1) -> None:
        self._verify_policy(connection, mission)
        if isinstance(certificate, PlanAdmissionCertificate):
            if (certificate.preview.candidate_hash != _document_hash(document)
                    or certificate.preview.base_revision + 1 != document.revision):
                _fail("PLAN_ADMISSION preview does not bind this exact document")
            if admission is None or certificate.preview.mission_id != mission:
                _fail("PLAN_ADMISSION certificate identity mismatch")
            if certificate.admission_check_ref.identity != admission:
                _fail("admission certificate does not name the stored check")
            row = connection.execute(
                "SELECT c.*,r.mission_id AS source_mission_id FROM planning_admission_checks c "
                "JOIN planning_requests r ON r.request_id=c.request_id WHERE c.check_id=?",
                (admission,),).fetchone()
            if row is None or row["phase"] != "APPLIED" or row["source_mission_id"] != mission:
                _fail("the referenced admission check is not a real APPLIED check")
            if (row["request_id"] != certificate.preview.request_id
                    or row["decision_id"] != certificate.preview.decision_id
                    or row["decision_hash"] != certificate.preview.decision_hash
                    or row["delta_hash"] != certificate.preview.delta_hash):
                _fail("APPLIED check does not bind the supplied preview")
            if certificate.admission_check_ref.channel != "planning_admission_check":
                _fail("admission check reference has the wrong channel")
            admission_body = {key: row[key] for key in row.keys() if key != "source_mission_id"}
            admission_digest = hashlib.sha256(canonical_json(admission_body).encode()).hexdigest()
            if admission_digest != certificate.admission_check_ref.digest:
                _fail("admission check digest mismatch")
            if certificate.commit_receipt_ref.identity != command:
                _fail("commit receipt reference does not name command_id")
            if certificate.commit_receipt_ref.channel != "commit_receipt":
                _fail("commit receipt reference has the wrong channel")
            if (certificate.commit_receipt_ref.revision != document.revision
                    or certificate.admission_check_ref.revision != document.revision):
                _fail("PLAN_ADMISSION source revision mismatch")
            receipt_row = connection.execute(
                "SELECT mission_id,new_plan_revision FROM plan_commit_receipts WHERE command_id=?",
                (command,),
            ).fetchone()
            if (receipt_row is None or receipt_row["mission_id"] != mission
                    or receipt_row["new_plan_revision"] != document.revision):
                _fail("the referenced commit receipt is unavailable")
            receipt = HtnStore(self._store).get_commit_receipt(command)
            digest = hashlib.sha256(canonical_json(receipt.to_json()).encode()).hexdigest()
            if digest != certificate.commit_receipt_ref.digest:
                _fail("commit receipt digest mismatch")
            if row["check_schema"] != "taskgraph-h1-applied/v1" or row["snapshot_hash"] != certificate.preview.candidate_hash:
                _fail("APPLIED check is not the original TaskGraph H1 producer result")
            detail = json.loads(row["detail_json"])
            if (not isinstance(detail, dict) or set(detail) != {"version", "command_id", "commit_receipt_hash",
                    "preview_hash", "source_reads", "runtime_work_hash", "read_set_hash", "compilation_hash"}
                    or type(detail["version"]) is not int or detail["version"] != 1
                    or canonical_json(detail) != row["detail_json"]
                    or detail["command_id"] != command or detail["commit_receipt_hash"] != digest
                    or detail["preview_hash"] != certificate.preview.canonical_hash()
                    or detail["source_reads"] != [item.to_json() for item in certificate.preview.source_reads]
                    or detail["read_set_hash"] != certificate.preview.read_set_hash):
                _fail("APPLIED check does not bind original H1 reads and exact commit result")
            for name in ("runtime_work_hash", "compilation_hash"):
                value = detail[name]
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    _fail("APPLIED check has an invalid H1 source digest")
        else:
            if admission is not None or certificate.codec_manifest_hash != document.codec_manifest_hash:
                _fail("CAPTURED_BASELINE certificate identity mismatch")
            reference = certificate.baseline_command_ref
            baseline_receipt = self._store.get_receipt(command)
            policy = connection.execute("SELECT * FROM taskgraph_policy_bindings WHERE mission_id=?", (mission,)).fetchone()
            if (reference.channel != "taskgraph_baseline_capture" or reference.identity != command
                    or reference.revision != document.revision or baseline_receipt is None
                    or baseline_receipt.get("schema_version") != 1 or baseline_receipt.get("kind") != "TaskGraphBaselineCaptured"
                    or baseline_receipt.get("command_id") != command or baseline_receipt.get("mission_id") != mission
                    or baseline_receipt.get("revision") != document.revision
                    or baseline_receipt.get("manifest_hash") != _document_hash(document)
                    or baseline_receipt.get("captured_through_seq") != certificate.captured_through_seq
                    or baseline_receipt.get("codec_manifest_hash") != document.codec_manifest_hash
                    or baseline_receipt.get("quiescence_ref") != certificate.quiescence_ref.to_json()
                    or baseline_receipt.get("policy_ref") != certificate.policy_ref.to_json()
                    or hashlib.sha256(canonical_json(dict(baseline_receipt)).encode()).hexdigest() != reference.digest):
                _fail("baseline command receipt does not bind this capture")
            if (policy is None or baseline_receipt.get("enabling_command_id") != policy["enabling_command_id"]
                    or certificate.policy_ref != SourceRef(channel="taskgraph_policy",
                        identity=policy["enabling_command_id"], revision=1, digest=policy["policy_hash"])):
                _fail("baseline policy reference does not bind this Mission")
            plan = connection.execute("SELECT snapshot_hash FROM plan_revisions WHERE mission_id=? AND revision=?",
                                      (mission, document.revision)).fetchone()
            if plan is None or baseline_receipt.get("sdk_snapshot_hash") != plan[0]:
                _fail("baseline receipt SDK snapshot mismatch")
            # Checking only a reference hash would accept a valid proof from a
            # different Mission or capture. Require its actual producer to bind
            # all capture coordinates, including the event boundary.
            self._verify_ref(connection, certificate.quiescence_ref, BaselineProofContext(
                mission_id=mission, revision=document.revision, command_id=command,
                enabling_command_id=policy["enabling_command_id"], manifest_hash=_document_hash(document),
                captured_through_seq=certificate.captured_through_seq))

    def insert_revision_record(self, *, document: NetworkDocumentV1, source_kind: str,
                               sdk_snapshot_hash: str, certificate: RevisionCertificate,
                               admission_check_id: str | None, event_id: str, command_id: str,
                               created_at: float) -> RevisionRecord:
        decoded = decode(document.to_json())
        mission, revision = document.mission_id, document.revision
        manifest = _document_hash(document)
        pins = build_revision_pins(document)
        if source_kind not in {"SEED_COMMIT", "COMMIT", "CAPTURED_BASELINE"}:
            _fail("source_kind is invalid")
        if (source_kind == "CAPTURED_BASELINE") != isinstance(
            certificate, CapturedBaselineCertificate
        ):
            _fail("source_kind and certificate kind disagree")
        with self._savepoint() as connection:
            plan = connection.execute("SELECT snapshot_hash FROM plan_revisions WHERE mission_id=? AND revision=?", (mission, revision)).fetchone()
            if plan is None or plan[0] != sdk_snapshot_hash:
                _fail("exact plan revision/sdk snapshot hash mismatch")
            requirement = connection.execute("SELECT revision_id,content_hash FROM requirements_revisions WHERE mission_id=? AND revision=?", (mission, document.requirements_ref.revision)).fetchone()
            if requirement is None or tuple(requirement) != (document.requirements_ref.id, document.requirements_ref.content_hash):
                _fail("exact requirements revision mismatch")
            previous = connection.execute("SELECT revision,manifest_hash FROM taskgraph_revision_records WHERE mission_id=? ORDER BY revision DESC LIMIT 1", (mission,)).fetchone()
            parent_revision = parent_hash = None
            if source_kind == "COMMIT":
                if previous is None or revision != previous[0] + 1:
                    _fail("COMMIT requires the immediately preceding revision record")
                parent_revision, parent_hash = int(previous[0]), str(previous[1])
                self.read_revision(mission, parent_revision)
                if (not isinstance(certificate, PlanAdmissionCertificate)
                        or certificate.preview.base_revision != parent_revision):
                    _fail("COMMIT preview does not bind the parent revision")
            elif previous is not None:
                _fail("seed/baseline cannot be inserted after revision history exists")
            if source_kind == "SEED_COMMIT" and (
                    revision != 1 or not isinstance(certificate, PlanAdmissionCertificate)
                    or certificate.preview.base_revision != 0):
                _fail("SEED_COMMIT must start at revision one from base zero")
            self._verify_certificate(connection, mission, command_id, admission_check_id, certificate, document)
            event = connection.execute("SELECT mission_id,payload_json,type,seq FROM events WHERE event_id=?", (event_id,)).fetchone()
            if event is None or event[0] != mission or event[2] != "TaskGraphRevisionRecorded":
                _fail("revision event is unavailable or belongs to another mission")
            if isinstance(certificate, CapturedBaselineCertificate) and certificate.captured_through_seq >= event[3]:
                _fail("baseline capture boundary must precede its revision event")
            payload = json.loads(event[1])
            parent_document = None if parent_revision is None else self.read_revision(mission, parent_revision).record.document
            required_event = revision_event_payload(document, source_kind=source_kind,
                sdk_snapshot_hash=sdk_snapshot_hash, command_id=command_id,
                decision_id=certificate.preview.decision_id if isinstance(certificate, PlanAdmissionCertificate) else None,
                parent=parent_document)
            if canonical_json(payload) != canonical_json(required_event):
                _fail("revision event does not bind the exact document and command")
            connection.execute("INSERT INTO taskgraph_revision_records(mission_id,revision,parent_revision,parent_manifest_hash,source_kind,requirements_revision,manifest_hash,sdk_snapshot_hash,network_json,certificate_json,admission_check_id,event_id,command_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mission, revision, parent_revision, parent_hash, source_kind, document.requirements_ref.revision, manifest, sdk_snapshot_hash, canonical_json(document.to_json()), canonical_json(certificate.to_json()), admission_check_id, event_id, command_id, created_at))
            connection.executemany("INSERT INTO taskgraph_member_pins VALUES (?,?,?,?,?,?)", [astuple(row) for row in pins.member_pins])
            connection.executemany("INSERT INTO taskgraph_method_pins VALUES (?,?,?,?,?,?)", [astuple(row) for row in pins.method_pins])
            connection.executemany("INSERT INTO taskgraph_demand_refs VALUES (?,?,?,?,?,?,?,?,?,?)", [astuple(row) for row in pins.demand_refs])
            stored_pins = self._pins(connection, mission, revision)
            verify_revision_pins(document, stored_pins.member_pins, stored_pins.method_pins,
                                 stored_pins.demand_refs)
            validate_revision_sources(self._store, document)
        return RevisionRecord(document=document, manifest_hash=manifest, sdk_snapshot_hash=sdk_snapshot_hash,
            source_kind=source_kind, parent_revision=parent_revision, parent_manifest_hash=parent_hash,
            certificate=certificate, admission_check_id=admission_check_id, event_id=event_id,
            command_id=command_id, created_at=created_at, pins=pins)

    def read_revision(self, mission_id: str, revision: int) -> HistoricalRevision:
        """Validate every ancestor under one snapshot; never trust a parent's hash column alone.

        A result verified while nothing in the store changed is the result: it is reused
        only under an equal :meth:`Store.read_generation` (any write, anywhere, re-verifies).
        """
        generation = self._store.read_generation()
        key = (mission_id, int(revision))
        cached = self._verified.get(key)
        if generation is not None and cached is not None and cached[0] == generation:
            return cached[1]
        selected = self._read_revision_verified(mission_id, revision)
        if generation is not None and self._store.read_generation() == generation:
            if len(self._verified) >= 256:
                self._verified.clear()
            self._verified[key] = (generation, selected)
        return selected

    def _read_revision_verified(self, mission_id: str, revision: int) -> HistoricalRevision:
        with self._store.read_view():
            selected = self._read_one_revision(mission_id, revision)
            child = selected.record
            visited = {revision}
            while child.parent_revision is not None:
                parent_revision = child.parent_revision
                if parent_revision in visited or parent_revision != child.document.revision - 1:
                    _fail("broken revision parent chain")
                visited.add(parent_revision)
                parent = self._read_one_revision(mission_id, parent_revision).record
                if parent.manifest_hash != child.parent_manifest_hash:
                    _fail("revision parent document hash mismatch")
                child = parent
            return selected

    def _read_one_revision(self, mission_id: str, revision: int) -> HistoricalRevision:
        with self._store.read_view() as connection:
            row = connection.execute("SELECT * FROM taskgraph_revision_records WHERE mission_id=? AND revision=?", (mission_id, revision)).fetchone()
            if row is None:
                _fail("HISTORICAL_STRUCTURE_UNAVAILABLE")
            raw = str(row["network_json"])
            parsed = json.loads(raw)
            if canonical_json(parsed) != raw:
                _fail("stored NetworkDocument is not canonical")
            document = decode(parsed).document
            if document.mission_id != mission_id or document.revision != revision or _document_hash(document) != row["manifest_hash"]:
                _fail("revision record document identity mismatch")
            plan = connection.execute("SELECT snapshot_hash FROM plan_revisions WHERE mission_id=? AND revision=?", (mission_id, revision)).fetchone()
            if plan is None or plan[0] != row["sdk_snapshot_hash"]:
                _fail("revision record sdk snapshot mismatch")
            if row["requirements_revision"] != document.requirements_ref.revision:
                _fail("revision record requirements mismatch")
            if row["parent_revision"] is None:
                if row["parent_manifest_hash"] is not None or row["source_kind"] not in {"SEED_COMMIT", "CAPTURED_BASELINE"}:
                    _fail("invalid root revision parent")
                other = connection.execute(
                    "SELECT 1 FROM taskgraph_revision_records WHERE mission_id=? AND revision<>? "
                    "AND parent_revision IS NULL",
                    (mission_id, revision),
                ).fetchone()
                if other is not None:
                    _fail("a root revision cannot coexist with another parentless history")
            else:
                if row["source_kind"] != "COMMIT":
                    _fail("a non-root revision must be a COMMIT")
                parent = connection.execute("SELECT manifest_hash FROM taskgraph_revision_records WHERE mission_id=? AND revision=?", (mission_id, row["parent_revision"])).fetchone()
                if parent is None or parent[0] != row["parent_manifest_hash"] or revision != row["parent_revision"] + 1:
                    _fail("broken revision parent chain")
            certificate = certificate_from_json(json.loads(row["certificate_json"]))
            if row["source_kind"] == "SEED_COMMIT" and (
                    revision != 1 or not isinstance(certificate, PlanAdmissionCertificate)
                    or certificate.preview.base_revision != 0):
                _fail("invalid seed revision origin")
            if (row["source_kind"] == "CAPTURED_BASELINE") != isinstance(
                certificate, CapturedBaselineCertificate
            ):
                _fail("source_kind and certificate kind disagree")
            if (row["source_kind"] == "COMMIT"
                    and isinstance(certificate, PlanAdmissionCertificate)
                    and certificate.preview.base_revision != row["parent_revision"]):
                _fail("COMMIT preview does not bind the parent revision")
            self._verify_certificate(connection, mission_id, row["command_id"], row["admission_check_id"], certificate, document)
            event = connection.execute("SELECT mission_id,payload_json,type,seq FROM events WHERE event_id=?", (row["event_id"],)).fetchone()
            if event is None or event[0] != mission_id or event[2] != "TaskGraphRevisionRecorded":
                _fail("revision event is unavailable or belongs to another mission")
            if isinstance(certificate, CapturedBaselineCertificate) and certificate.captured_through_seq >= event[3]:
                _fail("baseline capture boundary must precede its revision event")
            parent_document = None
            if row["parent_revision"] is not None:
                parent_row = connection.execute("SELECT network_json FROM taskgraph_revision_records WHERE mission_id=? AND revision=?",
                    (mission_id, row["parent_revision"])).fetchone()
                if parent_row is None:
                    _fail("revision event parent is unavailable")
                parent_document = decode(json.loads(parent_row[0])).document
            expected_event = revision_event_payload(document, source_kind=row["source_kind"],
                sdk_snapshot_hash=row["sdk_snapshot_hash"], command_id=row["command_id"],
                decision_id=certificate.preview.decision_id if isinstance(certificate, PlanAdmissionCertificate) else None,
                parent=parent_document)
            event_payload = json.loads(event[1])
            if canonical_json(event_payload) != canonical_json(expected_event):
                _fail("revision event does not bind the exact document and command")
            pins = self._pins(connection, mission_id, revision)
            verify_revision_pins(document, pins.member_pins, pins.method_pins, pins.demand_refs)
            validate_revision_sources(self._store, document)
            record = RevisionRecord(document=document, manifest_hash=row["manifest_hash"], sdk_snapshot_hash=row["sdk_snapshot_hash"], source_kind=row["source_kind"], parent_revision=row["parent_revision"], parent_manifest_hash=row["parent_manifest_hash"], certificate=certificate, admission_check_id=row["admission_check_id"], event_id=row["event_id"], command_id=row["command_id"], created_at=row["created_at"], pins=pins)
            return HistoricalRevision(record=record)

    def read_active_consumers(self, mission_id: str, producer_occurrence_id: str) -> tuple[DemandRef, ...]:
        with self._store.read_view() as connection:
            active = connection.execute("SELECT revision FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'", (mission_id,)).fetchall()
            if len(active) != 1:
                _fail("mission must have exactly one ACTIVE revision")
            revision = int(active[0][0])
            history = self.read_revision(mission_id, revision)
            return tuple(row for row in history.record.pins.demand_refs if row.producer_occurrence_id == producer_occurrence_id)


__all__ = ["GraphIntegrityError", "TaskGraphStore"]
