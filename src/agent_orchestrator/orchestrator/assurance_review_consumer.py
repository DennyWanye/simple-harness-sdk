# SPDX-License-Identifier: Apache-2.0
"""Durable REVIEW import on the original tick, without creating an executor."""

from __future__ import annotations

from typing import Any

from ..assurance.certificates import UseIdentity
from ..assurance.check_bindings import CheckBinding
from ..assurance.codec import AssuranceError, decode, fingerprint, text
from ..assurance.refs import AssuranceRef, Pin
from ..contracts import Artifact, Event
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_work import WorkClaim, WorkTarget
from ..storage.htn_store import HtnStore
from ..storage.store import _event_from_row
from .assurance_check_use import CurrentAuthority, prepare_local_check_use
from .assurance_review_import import (
    prepare_official_review,
    read_imported_review_locked,
    read_official_review_binding_locked,
    review_scope_id,
    review_subject_stopped,
)
from .assurance_review_pins import ensure_review_blob_pins
from .assurance_tick import AssuranceWait, PreparedAssuranceWork


class AssuranceReviewConsumer:
    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        principal_id: str,
        authority: CurrentAuthority,
        cas: Any,
        check_adapter: Any,
    ) -> None:
        self.commit = commit
        self.store = commit.store
        self.tenant_id = text(tenant_id)
        self.principal_id = text(principal_id)
        self.authority = authority
        self.cas = cas
        self.check_adapter = check_adapter

    def classify(self, event: Event) -> tuple[WorkTarget, ...]:
        if event.type == "AssuranceEvidenceChanged":
            # Wake the same unfinished logical work, including a capped wait.
            # Its first-start time/tries remain owned by WorkStore. Completed
            # historical imports are not reinterpreted when a source changes.
            rows = self.store.connection.execute(
                "SELECT w.work_key,c.commit_id,c.receipt_json FROM assurance_pending_work w "
                "JOIN assurance_review_invocations i ON i.mission_id=w.mission_id "
                "AND w.work_key='review-import:'||i.review_key||':'||i.ordinal "
                "JOIN commit_receipts c ON c.subject_id=i.dispatch_intent_id "
                "AND c.kind IN ('AssuranceReviewClassified','AssuranceReviewFormatRejected') "
                "JOIN events e ON e.mission_id=i.mission_id AND e.type=c.kind AND json_extract(e.payload_json,'$.classification_receipt_ref.pin.id')=c.commit_id "
                "WHERE w.mission_id=? AND w.consumer='REVIEW' AND w.state IN ('PENDING','RUNNING','WAITING') "
                "AND e.seq<? ORDER BY w.work_key LIMIT 257",
                (event.mission_id, event.seq),
            ).fetchall()
            if len(rows) > 256 or len({row["work_key"] for row in rows}) != len(rows):
                raise AssuranceError("REVIEW_WAKEUP_INVENTORY_INCOMPLETE")
            return tuple(
                WorkTarget(
                    row["work_key"],
                    fingerprint(
                        {
                            "classification_ref": AssuranceRef(
                                "commit_receipt",
                                Pin(row["commit_id"], 0, fingerprint(decode(row["receipt_json"]))),
                            ).to_json(),
                            "change_event": fingerprint(event.to_json()),
                        }
                    ),
                )
                for row in rows
            )
        expected = {
            "AssuranceReviewClassified": {"READY_FOR_CURRENT_REVIEW", "TURN_FAILED"},
            "AssuranceReviewFormatRejected": {"FORMAT_INVALID"},
        }
        if (
            event.type not in expected
            or event.payload.get("classification") not in expected[event.type]
        ):
            return ()
        ref = AssuranceRef.from_json(
            event.payload.get("classification_receipt_ref"), kinds={"commit_receipt"}
        )
        key = (
            "review-import:"
            + text(event.payload.get("review_key"))
            + ":"
            + str(event.payload.get("invocation_ordinal"))
        )
        return (WorkTarget(key, ref.key),)

    async def prepare(self, claim: WorkClaim) -> PreparedAssuranceWork | AssuranceWait:
        """No Provider call: source, pins, checks, interpretation, final commit."""
        if claim.consumer != "REVIEW":
            raise AssuranceError("REVIEW_WORK_IDENTITY")
        reader = AssuranceReader(self.store, tenant_id=self.tenant_id, mission_id=claim.mission_id)
        with self.store.read_view():
            row = self.store.connection.execute(
                "SELECT * FROM events WHERE event_id=? AND mission_id=?",
                (claim.trigger_event_id, claim.mission_id),
            ).fetchone()
            if row is None:
                raise AssuranceError("REVIEW_WORK_SOURCE_MISSING")
            event = _event_from_row(row)
            if (
                WorkTarget(claim.work_key, claim.target_fingerprint) not in self.classify(event)
                or event.seq != claim.target_epoch
            ):
                raise AssuranceError("REVIEW_WORK_IDENTITY")
            if event.type == "AssuranceEvidenceChanged":
                sources = self.store.connection.execute(
                    "SELECT e.* FROM assurance_review_invocations i "
                    "JOIN commit_receipts c ON c.subject_id=i.dispatch_intent_id "
                    "AND c.kind IN ('AssuranceReviewClassified','AssuranceReviewFormatRejected') "
                    "JOIN events e ON e.mission_id=i.mission_id AND e.type=c.kind AND json_extract(e.payload_json,'$.classification_receipt_ref.pin.id')=c.commit_id "
                    "WHERE i.mission_id=? AND 'review-import:'||i.review_key||':'||i.ordinal=?",
                    (claim.mission_id, claim.work_key),
                ).fetchall()
                if len(sources) != 1:
                    raise AssuranceError("REVIEW_WORK_SOURCE_MISSING")
                event = _event_from_row(sources[0])
            ref = AssuranceRef.from_json(event.payload["classification_receipt_ref"])
            if (event.type == "AssuranceReviewFormatRejected"
                    or event.payload.get("classification") == "TURN_FAILED"):
                return self._prepare_format_repair(reader, ref)
            imported = read_imported_review_locked(self.commit, reader, ref)
            body = imported.binding.to_json()
            existing = HtnStore(self.store).official_review_record(body["package_ref"]["id"])
            if existing is not None:
                # Recovery after effect commit/before ACK reads the original
                # immutable receipt. Do not recompute a historical judgement.
                source = read_official_review_binding_locked(self.commit, self.tenant_id, existing)
                if decode(source.body_json)["classification_ref"] != ref.to_json():
                    return self._prepare_late(
                        reader, imported, "OTHER_TURN_ALREADY_OFFICIAL", source
                    )

                def replay() -> AssuranceRef:
                    self.commit._assurance_root_gate.require_execution()
                    actual = read_official_review_binding_locked(
                        self.commit, self.tenant_id, existing
                    )
                    if actual != source:
                        raise AssuranceError("RECHECK_REQUIRED")
                    return actual.ref

                return PreparedAssuranceWork(replay)
            if review_subject_stopped(self.store, imported.binding):
                return self._prepare_late(reader, imported, "REVIEW_SUBJECT_STOPPED")
            scope = body["subject"]["completion_scope_ref"]
            identity = UseIdentity(
                claim.mission_id,
                "REVIEW",
                body["review_key"],
                review_scope_id(body),
                self.principal_id,
                "ACCEPT",
                self.commit._assurance_root_gate.require_execution().root_incarnation_id,
            )
            required = {
                AssuranceRef.from_json(ref)
                for policy in body["check_requirements"]
                for group in policy["any_check_sets"]
                for ref in group
            }
            # A pre-Scope METHOD_PLAN review has no Scope-bound check bindings:
            # its policy domain is the planning subject and only SEMANTIC checks
            # apply, so no executor/local check is selected for it.
            candidates = (
                []
                if scope is None
                else self.store.connection.execute(
                    "SELECT * FROM assurance_check_bindings WHERE mission_id=? AND subject_hash=? "
                    "AND json_extract(binding_json,'$.scope_hash')=? "
                    "ORDER BY created_at_ms,check_binding_id LIMIT 257",
                    (
                        claim.mission_id,
                        body["subject"]["target"]["pin"]["content_hash"],
                        scope["content_hash"],
                    ),
                ).fetchall()
            )
            if len(candidates) > 256:
                return AssuranceWait("RECHECK_REQUIRED", int(self.store.now * 1000))
            selected = {}
            for row in candidates:
                check = CheckBinding.from_json(decode(row["binding_json"]))
                if check.check_spec_ref in required:
                    if row["binding_hash"] != fingerprint(check.to_json()):
                        raise AssuranceError("CHECK_BINDING_SOURCE_MISMATCH")
                    # Deterministic latest actual run for this exact Spec/subject;
                    # a newer ERROR never falls back to an older PASS.
                    selected[check.check_spec_ref] = (
                        AssuranceRef(
                            "check_binding", Pin(row["check_binding_id"], 0, row["binding_hash"])
                        ),
                        check,
                    )
            raw_ref = AssuranceRef.from_json(
                decode(imported.turn.body_json)["payload"]["raw_output_ref"]
            )
            blobs = {raw_ref} | {
                item.ref for item in imported.catalogue if item.ref.kind in {"artifact", "source"}
            }
            for _, check in selected.values():
                blobs.update(check.evidence_refs)
                if check.execution_ref.kind not in {"local_check_receipt", "execution_receipt"}:
                    continue
                payload = decode(reader.read_exact_metadata(check.execution_ref).body_json)[
                    "payload"
                ]
                manifest = decode(
                    reader.read_exact_metadata(
                        AssuranceRef.from_json(payload["input_manifest_ref"])
                    ).body_json
                )
                for document in manifest["artifacts"]:
                    artifact = Artifact.from_json(document)
                    blobs.add(
                        AssuranceRef(
                            "artifact", Pin(artifact.id, artifact.version, artifact.content_hash)
                        )
                    )
        pins = ensure_review_blob_pins(
            self.commit, tenant_id=self.tenant_id, binding=imported.binding, refs=blobs
        )
        checks = []
        try:
            for ref, check in selected.values():
                if (
                    check.execution_ref.kind not in {"local_check_receipt", "execution_receipt"}
                    or self.check_adapter is None
                ):
                    # Unsupported sources remain UNKNOWN; no synthesized successful
                    # check or retry of a potentially effectful tool.  An executor
                    # check (code_test) is used from its recorded receipt, never re-run.
                    continue
                try:
                    prepared = prepare_local_check_use(
                        self.check_adapter,
                        binding_ref=ref,
                        result_ref=AssuranceRef.from_json(body["subject"]["target"]),
                        completion_scope=AssuranceRef("completion_scope", Pin.from_json(scope)),
                        identity=identity,
                        review_key=body["review_key"],
                        pins=pins,
                        authority=self.authority,
                        maximum_blob_bytes=32 * 1024 * 1024,
                    )
                except AssuranceError as error:
                    if error.code == "CHECK_USE_EXPIRED":
                        continue
                    raise
                checks.append(prepared)
            prepared = prepare_official_review(
                self.commit,
                tenant_id=self.tenant_id,
                classification_ref=ref_from_event(event),
                identity=identity,
                authority=self.authority,
                cas=self.cas,
                pins=pins,
                check_uses=tuple(checks),
                check_adapter=self.check_adapter,
            )
            return PreparedAssuranceWork(prepared.import_locked)
        except AssuranceError as error:
            if error.code not in {
                "UNEXPOSED_EVIDENCE",
                "DUPLICATE_CRITERION",
                "FINDING_SCOPE",
                "POLICY_CATALOGUE_MISMATCH",
                "MANDATORY_CRITERIA_INVALID",
            }:
                # Missing/temporarily denied evidence is not an immutable model
                # error. The tick retains it under the same persistent retry cap.
                raise
            # Immutable interpretation failure is an explicit original receipt,
            # not a dropped inbox item or a fabricated ReviewRecord.
            error_code = error.code
            if (
                error_code in REPAIRABLE_INTERPRETATION_ERRORS
                and imported.invocation.to_json()["ordinal"] == 1
                and _format_retries(self.store, claim.mission_id) == 1
            ):
                # 2026-09-26 Host run: the root's final review answered ACCEPT but
                # cited one label it had only seen in a listing.  Rejecting that as
                # final left a Mission whose six steps were all accepted with no
                # review and nothing to do.  It is the reviewer's mistake in the
                # reply, so it gets the one second invocation a malformed reply gets,
                # told exactly what was wrong; the second reply is judged as strictly.
                return self._prepare_interpretation_repair(reader, imported, event, error_code)

            def rejected() -> AssuranceRef:
                self.commit._assurance_root_gate.require_execution()
                if (
                    read_imported_review_locked(self.commit, reader, ref_from_event(event))
                    != imported
                ):
                    raise AssuranceError("RECHECK_REQUIRED")
                receipt = {
                    "mission_id": claim.mission_id,
                    "review_key": body["review_key"],
                    "classification_ref": ref_from_event(event).to_json(),
                    "reason": error_code,
                }
                receipt_id = "assurance-review-rejected:" + fingerprint(receipt)
                self.store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssuranceReviewImportRejected",
                    subject_id=body["review_key"],
                    base_version=0,
                    proposal_hash=fingerprint(receipt),
                    receipt=receipt,
                )
                self.commit._emit(
                    "AssuranceReviewImportRejected",
                    claim.mission_id,
                    key=receipt_id,
                    payload=receipt,
                )
                return AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(receipt)))

            return PreparedAssuranceWork(rejected, rejected=True)

    def _prepare_late(self, reader, imported, reason, official=None) -> PreparedAssuranceWork:
        def late() -> AssuranceRef:
            self.commit._assurance_root_gate.require_execution()
            if (
                read_imported_review_locked(self.commit, reader, imported.classification.ref)
                != imported
            ):
                raise AssuranceError("RECHECK_REQUIRED")
            body = imported.binding.to_json()
            if official is not None:
                record = HtnStore(self.store).official_review_record(body["package_ref"]["id"])
                if (
                    record is None
                    or read_official_review_binding_locked(self.commit, self.tenant_id, record)
                    != official
                ):
                    raise AssuranceError("RECHECK_REQUIRED")
            elif not review_subject_stopped(self.store, imported.binding):
                raise AssuranceError("RECHECK_REQUIRED")
            receipt = {
                "mission_id": reader.mission_id,
                "review_key": body["review_key"],
                "classification_ref": imported.classification.ref.to_json(),
                "turn_ref": imported.turn.ref.to_json(),
                "reason": reason,
                "existing_official_receipt_ref": None
                if official is None
                else official.ref.to_json(),
            }
            digest = fingerprint(receipt)
            receipt_id = "assurance-review-late:" + digest
            if self.store.get_receipt(receipt_id) is None:
                self.store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssuranceReviewLateTurn",
                    subject_id=body["review_key"],
                    base_version=0,
                    proposal_hash=digest,
                    receipt=receipt,
                )
                self.commit._emit(
                    "AssuranceReviewLateTurn", reader.mission_id, key=receipt_id, payload=receipt
                )
            return AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest))

        return PreparedAssuranceWork(late)

    def _prepare_interpretation_repair(
        self, reader: AssuranceReader, imported: Any, event: Event, error_code: str
    ) -> PreparedAssuranceWork:
        invocation = imported.invocation.to_json()
        body = {
            "mission_id": reader.mission_id,
            "review_key": invocation["review_key"],
            "intent_id": invocation["dispatch_intent_id"],
            "invocation_ordinal": 1,
            "classification": "INTERPRETATION_INVALID",
            "error_code": error_code,
            "classification_ref": ref_from_event(event).to_json(),
            "turn_ref": imported.turn.ref.to_json(),
        }
        digest = fingerprint(body)
        receipt_id = "assurance-review-interpretation:" + digest

        def repair() -> AssuranceRef:
            self.commit._assurance_root_gate.require_execution()
            if read_imported_review_locked(self.commit, reader, ref_from_event(event)) != imported:
                raise AssuranceError("RECHECK_REQUIRED")
            old = self.store.get_receipt(receipt_id)
            if old is not None and dict(old) != body:
                raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
            if old is None:
                self.store.insert_receipt(
                    commit_id=receipt_id,
                    kind=INTERPRETATION_REJECTED,
                    subject_id=body["intent_id"],
                    base_version=0,
                    proposal_hash=digest,
                    receipt=body,
                )
                self.commit._emit(INTERPRETATION_REJECTED, reader.mission_id, key=receipt_id,
                                  payload=body)
            repaired = self.commit.ensure_assurance_format_repair(
                tenant_id=self.tenant_id,
                prior_failure=AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest)),
            )
            return AssuranceRef.from_json(
                repaired.to_json()["source_receipt_ref"], kinds={"commit_receipt"}
            )

        return PreparedAssuranceWork(repair)

    def _prepare_format_repair(
        self, reader: AssuranceReader, ref: AssuranceRef
    ) -> PreparedAssuranceWork:
        from .assurance_review_transport import read_review_invocation_locked

        metadata = reader.read_exact_metadata(ref)
        source, row = decode(metadata.body_json), decode(metadata.lifecycle_json)
        invocation, binding = read_review_invocation_locked(
            self.commit, reader, source.get("intent_id")
        )
        value = invocation.to_json()
        second_sources = {"FORMAT_INVALID": "AssuranceReviewFormatRejected",
                          "TURN_FAILED": "AssuranceReviewClassified"}
        if (
            second_sources.get(str(source.get("classification"))) != row["kind"]
            or row["subject_id"] != source.get("intent_id")
            or row["base_version"] != 0
            or row["proposal_hash"] != fingerprint(source)
            or source.get("mission_id") != reader.mission_id
            or source.get("review_key") != value["review_key"]
            or source.get("invocation_ordinal") != value["ordinal"]
        ):
            raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
        if value["ordinal"] == 1:

            def repair() -> AssuranceRef:
                repaired = self.commit.ensure_assurance_format_repair(
                    tenant_id=self.tenant_id, prior_failure=ref
                )
                return AssuranceRef.from_json(
                    repaired.to_json()["source_receipt_ref"], kinds={"commit_receipt"}
                )

            return PreparedAssuranceWork(repair)

        def exhausted() -> AssuranceRef:
            self.commit._assurance_root_gate.require_execution()
            if reader.read_exact_metadata(ref) != metadata:
                raise AssuranceError("RECHECK_REQUIRED")
            body = {
                "mission_id": reader.mission_id,
                "review_key": value["review_key"],
                "classification_ref": ref.to_json(),
                "reason": ("REVIEW_FORMAT_REPAIR_EXHAUSTED"
                           if source.get("classification") == "FORMAT_INVALID"
                           else "REVIEW_TURN_RETRY_EXHAUSTED"),
            }
            receipt_id = "assurance-review-format-exhausted:" + binding.to_json()["review_key"]
            old = self.store.get_receipt(receipt_id)
            if old is not None and dict(old) != body:
                raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
            if old is None:
                self.store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssuranceReviewFormatExhausted",
                    subject_id=value["review_key"],
                    base_version=0,
                    proposal_hash=fingerprint(body),
                    receipt=body,
                )
                self.commit._emit(
                    "AssuranceReviewFormatExhausted",
                    reader.mission_id,
                    key=receipt_id,
                    payload=body,
                )
            return AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body)))

        return PreparedAssuranceWork(exhausted, rejected=True)


#: A reply the reviewer committed that decodes but cannot be imported as given —
#: its own mistake, not a missing source.  POLICY_CATALOGUE_MISMATCH is the
#: package's, not the reply's, and stays final.
REPAIRABLE_INTERPRETATION_ERRORS = frozenset(
    {"UNEXPOSED_EVIDENCE", "DUPLICATE_CRITERION", "FINDING_SCOPE", "MANDATORY_CRITERIA_INVALID"}
)
INTERPRETATION_REJECTED = "AssuranceReviewInterpretationRejected"


def _format_retries(store: Any, mission_id: str) -> int | None:
    from ..assurance.policy import AssurancePolicy

    row = store.connection.execute(
        "SELECT policy_json FROM assurance_mission_bindings WHERE mission_id=?", (mission_id,)
    ).fetchone()
    return None if row is None else AssurancePolicy.from_json(decode(row[0])).format_retries


def ref_from_event(event: Event) -> AssuranceRef:
    return AssuranceRef.from_json(
        event.payload["classification_receipt_ref"], kinds={"commit_receipt"}
    )
