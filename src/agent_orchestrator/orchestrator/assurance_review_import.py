# SPDX-License-Identifier: Apache-2.0
"""Original official review writer, fed only by authenticated runtime imports.

A review is a historical judgement, never an acceptance/use licence. Current
acceptance still requires its own complete validity certificate and OCC checks.
All preparation runs outside the write lock; the original writer repeats the
bounded identity/authority/epoch checks in the effect transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..assurance.certificates import UseIdentity
from ..assurance.checks import (
    CheckResult,
    CriterionPolicy,
    Formula,
    Grade,
    ReviewReply,
    decide_review,
    evaluate_check_gate,
)
from ..assurance.codec import AssuranceError, canonical, decode, fingerprint
from ..assurance.disclosure import DisclosureBatch, disclosed_to_turn
from ..assurance.evidence import CatalogueEntry, ReadItem, resolve_evidence_ids
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.reviews import (
    REVIEW_CODEC_VERSION,
    AssuranceReviewBinding,
    ReviewInvocation,
    ReviewRecordBinding,
)
from ..assurance.root_gate import CurrentReadPermission
from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK
from ..contracts.resolution import (
    CheckExecution,
    CriterionOutcome,
    CriterionVerdict,
    ReviewRecord,
    ReviewVerdict,
)
from ..contracts.state_machines import TaskStatus
from ..storage.assurance_blobs import PreparedBlob, read_pinned_blob
from ..storage.assurance_reads import (
    AssuranceReader,
    CompleteRead,
    EpochSnapshot,
    ExactMetadata,
    read_complete_evidence_snapshot,
    read_epochs_locked,
    require_epochs_locked,
)
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic
from ..storage.htn_store import HtnStore
from .assurance_check_use import CurrentAuthority, PreparedCheckUse, _merge_reads, _permission
from .assurance_review_transport import read_review_invocation_locked


@dataclass(frozen=True, slots=True)
class ImportedReview:
    invocation: ReviewInvocation
    binding: AssuranceReviewBinding
    classification: ExactMetadata
    turn: ExactMetadata
    provider_manifest: ExactMetadata
    disclosures: tuple[DisclosureBatch, ...]
    disclosure_metadata: tuple[ExactMetadata, ...]
    catalogue: tuple[CatalogueEntry, ...]
    exposed: frozenset[str]


PRE_SCOPE_ID = "mission"


def review_scope_id(body: Mapping[str, Any]) -> str:
    """The use-identity scope of a review binding.

    Only METHOD_PLAN may be bound before a completion Scope exists (subject
    shape rule); its use scope is the Mission itself, exactly as the purpose
    builder froze it (``PurposeSubject.scope_id == "mission"``). Every other
    purpose without a Scope is refused rather than given a guessed scope.
    """
    subject = body["subject"]
    scope = subject["completion_scope_ref"]
    if scope is not None:
        return str(scope["id"])
    if subject["purpose"] != "METHOD_PLAN":
        raise AssuranceError("REVIEW_SCOPE_UNAVAILABLE")
    return PRE_SCOPE_ID


def review_subject_stopped(store: Any, binding: AssuranceReviewBinding) -> bool:
    """Whether the reviewed subject can no longer use an answer; purpose-aware.

    A stopped Mission stops every review. A TASK_CONTENT review dies with its
    Attempt or Task. A MISSION_FINAL review is asked *after* the root Task's own
    work is accepted, so the root being DONE is its normal state; it stops when
    the root duty is already resolved or the cut package was superseded. The
    other purposes are bound to a live owner Task.
    """
    body = binding.to_json()
    mission = store.get_mission(body["mission_id"])
    task = store.get_task(body["subject"]["owner_task_ref"]["id"])
    if (
        mission is None
        or mission.status in TERMINAL_MISSION
        or task is None
        or task.mission_id != mission.id
    ):
        return True
    purpose = body["subject"]["purpose"]
    if purpose == "MISSION_FINAL":
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return True
        from ..storage.htn_store import HtnStore
        from ..storage.store import StoreError
        from .hierarchical_dispatch import ROOT_REVIEW_SUPERSEDED

        package_id = body["package_ref"]["id"]
        try:
            package = HtnStore(store).get_review_package(package_id)
        except StoreError:
            return True
        if HtnStore(store).adopted_goal_resolution(mission.id, str(package.binding.obligation_id)):
            return True
        return any(
            event.type == ROOT_REVIEW_SUPERSEDED
            and str((event.payload or {}).get("package_id", "")) == str(package_id)
            for event in store.list_events(mission.id)
        )
    if task.status in TERMINAL_TASK:
        return True
    if purpose == "TASK_CONTENT":
        result = store.get_result(body["subject"]["target"]["pin"]["id"])
        attempt = None if result is None else store.get_attempt(result.envelope.attempt_id)
        return attempt is None or attempt.status in TERMINAL_ATTEMPT
    return False


def read_imported_review_locked(
    commit: Any, reader: AssuranceReader, classification_ref: AssuranceRef
) -> ImportedReview:
    """Authenticate the source receipt and exact final request's exposure chain."""
    if not reader.store.connection.in_transaction:
        raise AssuranceError("READ_TRANSACTION_REQUIRED")
    if classification_ref.kind != "commit_receipt":
        raise AssuranceError("REVIEW_CLASSIFICATION_SOURCE_INVALID")
    classified = reader.read_exact_metadata(classification_ref)
    source, meta = decode(classified.body_json), decode(classified.lifecycle_json)
    intent_id = source.get("intent_id")
    invocation, binding = read_review_invocation_locked(commit, reader, intent_id)
    value, bound = invocation.to_json(), binding.to_json()
    if (
        meta["kind"] != "AssuranceReviewClassified"
        or meta["subject_id"] != intent_id
        or meta["base_version"] != 0
        or meta["proposal_hash"] != fingerprint(source)
        or source.get("classification") != "READY_FOR_CURRENT_REVIEW"
        or source.get("error_code") is not None
        or source.get("mission_id") != reader.mission_id
        or source.get("review_key") != bound["review_key"]
        or source.get("invocation_ordinal") != value["ordinal"]
    ):
        raise AssuranceError("REVIEW_CLASSIFICATION_SOURCE_INVALID")
    turn = reader.read_exact_metadata(
        AssuranceRef.from_json(source.get("turn_ref"), kinds={"agent_turn_receipt"})
    )
    payload = decode(turn.body_json)["payload"]
    intent = reader.store.get_intent(intent_id)
    if (
        payload.get("intent_id") != intent_id
        or payload.get("state") != "COMMITTED"
        or payload.get("review_key") != bound["review_key"]
        or payload.get("invocation_ordinal") != value["ordinal"]
        or payload.get("turn_id") != intent.expected_turn_id
        or payload.get("agent_id") != intent.agent_id
        or payload.get("raw_output_hash") != source.get("raw_output_hash")
        or payload.get("exposure_error") is not None
    ):
        raise AssuranceError("REVIEW_TURN_SOURCE_MISMATCH")
    # A classification cannot transform an untrusted source row into a runtime
    # receipt. Authenticate the original importer receipt as well as the Event.
    imported_id = "assurance-review-turn:" + fingerprint(
        {"intent": intent_id, "turn": intent.expected_turn_id}
    )
    imported = reader.store.connection.execute(
        "SELECT * FROM commit_receipts WHERE commit_id=?", (imported_id,)
    ).fetchone()
    expected = {
        "mission_id": reader.mission_id,
        "intent_id": intent_id,
        "source_hash": fingerprint(payload),
        "event_ref": turn.ref.to_json(),
    }
    if (
        imported is None
        or imported["kind"] != "AssuranceReviewTurnImported"
        or imported["subject_id"] != intent_id
        or imported["base_version"] != 0
        or imported["proposal_hash"] != fingerprint(payload)
        or decode(imported["receipt_json"]) != expected
    ):
        raise AssuranceError("REVIEW_TURN_SOURCE_MISMATCH")
    if intent.agent_id in bound["producer_agent_ids"]:
        raise AssuranceError("REVIEW_INDEPENDENCE_REQUIRED")
    manifest = reader.read_exact_metadata(
        AssuranceRef.from_json(payload.get("provider_manifest_ref"), kinds={"input_manifest"})
    )
    provider = decode(manifest.body_json)
    batches = AssuranceStore(reader.store).disclosure_chain(reader.mission_id, bound["review_key"])
    exposed = disclosed_to_turn(
        batches,
        mission_id=reader.mission_id,
        review_key=bound["review_key"],
        agent_id=intent.agent_id,
        turn_receipt_ref=turn.ref,
        provider_input_hash=provider["provider_input_hash"],
    )
    catalogue = {
        item["label"]: CatalogueEntry(item["label"], AssuranceRef.from_json(item["ref"]))
        for item in bound["evidence_catalogue"]
    }
    disclosure_metadata = []
    selected = []
    for batch in batches:
        event = reader.read_exact_metadata(batch.delivery_receipt_ref)
        disclosure_metadata.append(event)
        expected_event = {
            "review_key": batch.review_key,
            "batch_no": batch.batch_no,
            "previous_batch_hash": batch.previous_batch_hash,
            "delta_hash": fingerprint([item.to_json() for item in batch.entries]),
            "reviewer_agent_id": batch.reviewer_agent_id,
            "turn_receipt_ref": batch.turn_receipt_ref.to_json(),
            "provider_input_hash": batch.provider_input_hash,
            "visible_message_ids": list(batch.visible_message_ids),
        }
        if decode(event.body_json)["payload"] != expected_event:
            raise AssuranceError("DISCLOSURE_INPUT_BINDING")
        if (
            batch.turn_receipt_ref == turn.ref
            and batch.reviewer_agent_id == intent.agent_id
            and batch.provider_input_hash == provider["provider_input_hash"]
        ):
            if not set(batch.visible_message_ids) <= set(provider["selected_message_ids"]):
                raise AssuranceError("UNEXPOSED_EVIDENCE")
            selected.append(batch)
            for item in batch.entries:
                if catalogue.setdefault(item.label, item) != item:
                    raise AssuranceError("LABEL_COLLISION")
    return ImportedReview(
        invocation,
        binding,
        classified,
        turn,
        manifest,
        tuple(selected),
        tuple(disclosure_metadata),
        tuple(catalogue[k] for k in sorted(catalogue)),
        exposed,
    )


def _interpret(
    imported: ImportedReview, raw: bytes, checks: Mapping[AssuranceRef, CheckResult]
) -> tuple[ReviewRecord, dict]:
    bound = imported.binding.to_json()
    turn = decode(imported.turn.body_json)["payload"]
    reply = ReviewReply.from_json(decode(raw))
    policies = {
        row["criterion_id"]: CriterionPolicy.from_json(row) for row in bound["check_requirements"]
    }
    decision = decide_review(
        reply,
        Formula.from_json(bound["formula"], frozenset(policies)),
        tuple(bound["mandatory_ids"]),
        policies,
        checks,
    )
    outcomes, details = [], []
    for assessment in sorted(reply.assessments, key=lambda item: item.criterion_id):
        name = assessment.criterion_id
        refs = resolve_evidence_ids(
            bound["review_key"], assessment.evidence_ids, imported.catalogue, imported.exposed
        )
        gate = evaluate_check_gate(policies[name], checks)
        effective = decision.effective_grades[name]
        execution = CheckExecution.NOT_RUN
        if gate.grade in {Grade.PASS, Grade.FAIL}:
            execution = CheckExecution.SUCCEEDED
        elif gate.grade is not None and gate.consumed:
            states = {
                row.execution_state for row in checks.values() if row.receipt in gate.consumed
            }
            execution = next(
                (
                    state
                    for state in (
                        CheckExecution.ERROR,
                        CheckExecution.CANCELLED,
                        CheckExecution.RUNNING,
                    )
                    if state.value in states
                ),
                CheckExecution.NOT_RUN,
            )
        # Public CriterionOutcome V1 cannot represent SEMANTIC PASS/NOT_RUN.
        # Keep its bytes/codec unchanged and project that combination as UNKNOWN;
        # the exact model/effective grades live in the authenticated manifest.
        # Assured acceptance must consume that manifest, not the legacy projection.
        projected = effective
        limitations = list(assessment.limitations)
        if effective is Grade.PASS and execution is not CheckExecution.SUCCEEDED:
            projected = Grade.UNKNOWN
            limitations.append("ASSURANCE_SEMANTIC_GRADE_IN_BOUND_MANIFEST")
        outcomes.append(
            CriterionOutcome(
                name,
                CriterionVerdict(projected.value),
                execution,
                evidence_refs=(),
                limitations=tuple(limitations),
            )
        )
        details.append(
            {
                "criterion_id": name,
                "model_grade": assessment.verdict.value,
                "effective_grade": effective.value,
                "check_gate": None if gate.grade is None else gate.grade.value,
                "check_reason": gate.reason,
                "evidence_ids": list(assessment.evidence_ids),
                "evidence_refs": [ref.to_json() for ref in refs],
                "reason": assessment.reason,
                "limitations": list(assessment.limitations),
            }
        )
    verdict = reply.verdict
    if verdict == "ACCEPT" and not decision.acceptable:
        verdict = "REWORK" if Grade.FAIL in decision.effective_grades.values() else "INCONCLUSIVE"
    manifest = {
        "schema_version": 1,
        "codec_version": REVIEW_CODEC_VERSION,
        "review_key": bound["review_key"],
        "binding_hash": imported.binding.content_hash,
        "turn_ref": imported.turn.ref.to_json(),
        "raw_output_hash": turn["raw_output_hash"],
        "model_verdict": reply.verdict,
        "effective_verdict": verdict,
        "criteria": details,
        "findings": [
            {"criterion_id": item.criterion_id, "severity": item.severity, "reason": item.reason}
            for item in reply.findings
        ],
        "success_witness": sorted(decision.success_witness),
        "consumed_receipts": [ref.to_json() for ref in decision.consumed_receipts],
        "exposed_evidence_refs": [
            item.to_json() for item in imported.catalogue if item.label in imported.exposed
        ],
        "provider_manifest_ref": imported.provider_manifest.ref.to_json(),
        "disclosure_refs": [batch.delivery_receipt_ref.to_json() for batch in imported.disclosures],
    }
    # Package's original binding remains authoritative; no expanded TypedRef V1.
    from ..contracts.resolution import ReviewPackage

    package = ReviewPackage.from_json(decode(raw_package(imported)))
    record = ReviewRecord(
        "assurance-review:"
        + fingerprint({"review_key": bound["review_key"], "turn_ref": imported.turn.ref.to_json()}),
        package.package_id,
        package.purpose,
        package.binding,
        turn["agent_id"],
        turn["turn_id"],
        fingerprint(manifest),
        tuple(outcomes),
        ReviewVerdict(verdict),
    )
    return record, manifest


def raw_package(imported: ImportedReview) -> str:
    # The frozen initial message contains the exact package; its hash was
    # checked by the original invocation writer. No latest reconstruction.
    from ..assurance.review_input import read_initial_materials

    provider = decode(imported.provider_manifest.body_json)
    matches = []
    for row in provider["messages"]:
        if row["kind"] != "user_input":
            continue
        try:
            read_initial_materials(row["message"], imported.binding)
        except AssuranceError:
            continue
        matches.append(decode(row["message"]["content"])["package"])
    if len(matches) != 1:
        raise AssuranceError("REVIEW_PACKAGE_SOURCE_UNAVAILABLE")
    return canonical(matches[0])


@dataclass(frozen=True, slots=True)
class PreparedOfficialReview:
    commit: Any
    tenant_id: str
    identity: UseIdentity
    authority: CurrentAuthority
    imported: ImportedReview
    epochs: EpochSnapshot
    metadata: tuple[ExactMetadata, ...]
    permissions: tuple[tuple[AssuranceRef, CurrentReadPermission], ...]
    blobs: tuple[PreparedBlob, ...]
    check_uses: tuple[PreparedCheckUse, ...]
    check_adapter: Any
    complete: tuple[CompleteRead, ...]
    read_set: tuple[ReadItem, ...]
    prepared_at_ms: int
    not_after_ms: int
    record: ReviewRecord
    manifest_json: str

    def require_locked(self, store: Any, record: ReviewRecord) -> None:
        if (
            store is not self.commit.store
            or not store.connection.in_transaction
            or record != self.record
        ):
            raise AssuranceError("REVIEW_IMPORT_TRANSACTION_REQUIRED")
        now_ms = int(store.now * 1000)
        if not self.prepared_at_ms <= now_ms < self.not_after_ms:
            raise AssuranceError("REVIEW_IMPORT_EXPIRED")
        gate = self.commit._assurance_root_gate
        if (
            gate is None
            or gate.require_execution().root_incarnation_id != self.identity.root_incarnation_id
        ):
            raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
        require_epochs_locked(
            store.connection, self.identity.mission_id, self.epochs, now_ms=now_ms
        )
        reader = AssuranceReader(
            store, tenant_id=self.tenant_id, mission_id=self.identity.mission_id
        )
        current = read_imported_review_locked(self.commit, reader, self.imported.classification.ref)
        if current != self.imported:
            raise AssuranceError("RECHECK_REQUIRED")
        if review_subject_stopped(store, current.binding):
            raise AssuranceError("REVIEW_SUBJECT_STOPPED")
        for item in self.metadata:
            if reader.read_exact_metadata(item.ref) != item:
                raise AssuranceError("RECHECK_REQUIRED")
        for ref, permission in self.permissions:
            if _permission(self.authority, self.identity, ref, now_ms) != permission:
                raise AssuranceError("RECHECK_REQUIRED")
        for blob in self.blobs:
            blob.require_current_locked(
                reader,
                now_ms=now_ms,
                authorize=lambda ref: (
                    _permission(self.authority, self.identity, ref, now_ms).access
                ),
            )
        checks = _consume_checks(
            self.check_uses, self.check_adapter, self.identity, self.authority, now_ms
        )
        raw = next(blob.data for blob in self.blobs if blob.metadata.ref == _raw_ref(self.imported))
        computed, manifest = _interpret(current, raw, checks)
        if computed != record or canonical(manifest) != self.manifest_json:
            raise AssuranceError("RECHECK_REQUIRED")

    def import_locked(self) -> AssuranceRef:
        store = self.commit.store
        with atomic(store):
            semantic = HtnStore(store)
            existing = semantic.official_review_record(self.record.package_id)
            if existing is not None:
                if existing != self.record:
                    raise AssuranceError("REVIEW_ALREADY_OFFICIAL")
                gate = self.commit._assurance_root_gate
                if (
                    gate is None
                    or gate.require_execution().root_incarnation_id
                    != self.identity.root_incarnation_id
                ):
                    raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
                _permission(
                    self.authority,
                    self.identity,
                    self.imported.classification.ref,
                    int(store.now * 1000),
                )
                # Replays are verified through the immutable import sidecar.
                return read_official_review_binding_locked(
                    self.commit, self.tenant_id, self.record
                ).ref
            self.require_locked(store, self.record)
            semantic.insert_review_record(self.record, official=True, assurance_import=self)
            owner = self.imported.binding.to_json()["subject"]["owner_task_ref"]["id"]
            manifest_hash = semantic.insert_input_manifest(
                self.identity.mission_id, owner, decode(self.manifest_json)
            )
            if manifest_hash != self.record.evidence_manifest_hash:
                raise AssuranceError("REVIEW_EVIDENCE_MANIFEST_MISMATCH")
            consumed = {
                AssuranceRef.from_json(ref)
                for ref in decode(self.manifest_json)["consumed_receipts"]
            }
            bound = self.imported.binding.to_json()
            side = ReviewRecordBinding.from_json(
                {
                    "schema_version": 2,
                    "mission_id": self.identity.mission_id,
                    "review_key": bound["review_key"],
                    "package_ref": bound["package_ref"],
                    "record_ref": Pin(
                        str(self.record.record_id), 0, fingerprint(self.record.to_json())
                    ).to_json(),
                    "reviewer_agent_id": self.record.reviewer_agent_id,
                    "reviewer_turn_ref": self.imported.turn.ref.to_json(),
                    "raw_output_ref": _raw_ref(self.imported).to_json(),
                    "raw_output_hash": _raw_ref(self.imported).pin.content_hash,
                    "codec_version": REVIEW_CODEC_VERSION,
                    "consumed_check_refs": [
                        use.binding_ref.to_json()
                        for use in self.check_uses
                        if use.binding.execution_ref in consumed
                    ],
                    "exposed_evidence_refs": decode(self.manifest_json)["exposed_evidence_refs"],
                    "evidence_manifest_hash": manifest_hash,
                    "binding_read_set": [item.to_json() for item in self.read_set],
                    "disclosure_refs": decode(self.manifest_json)["disclosure_refs"],
                    "invocation_ordinal": self.imported.invocation.to_json()["ordinal"],
                }
            )
            receipt_id = "assurance-review-import:" + str(self.record.record_id)
            receipt = {
                "mission_id": self.identity.mission_id,
                "record_id": str(self.record.record_id),
                "binding_hash": side.content_hash,
                "classification_ref": self.imported.classification.ref.to_json(),
            }
            self.commit.store.insert_receipt(
                commit_id=receipt_id,
                kind="AssuranceReviewImported",
                subject_id=str(self.record.record_id),
                base_version=0,
                proposal_hash=side.content_hash,
                receipt=receipt,
            )
            AssuranceStore(store)._insert(
                store.connection,
                "assurance_review_record_bindings",
                {
                    "record_id": str(self.record.record_id),
                    "mission_id": self.identity.mission_id,
                    "review_key": bound["review_key"],
                    "source_turn_ref_hash": self.imported.turn.ref.key,
                    "raw_output_hash": _raw_ref(self.imported).pin.content_hash,
                    "evidence_manifest_hash": manifest_hash,
                    "binding_hash": side.content_hash,
                    "binding_json": side.body_json,
                    "import_receipt_id": receipt_id,
                    "created_at_ms": int(store.now * 1000),
                },
                identities=(("record_id",), ("mission_id", "review_key", "source_turn_ref_hash")),
            )
            self.commit._emit(
                "AssuranceReviewImported",
                self.identity.mission_id,
                key=str(self.record.record_id),
                payload=receipt,
            )
            return AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(receipt)))


def _raw_ref(imported: ImportedReview) -> AssuranceRef:
    return AssuranceRef.from_json(
        decode(imported.turn.body_json)["payload"]["raw_output_ref"], kinds={"source"}
    )


def _consume_checks(
    uses: tuple[PreparedCheckUse, ...],
    adapter: Any,
    identity: UseIdentity,
    authority: CurrentAuthority,
    now_ms: int,
) -> dict[AssuranceRef, CheckResult]:
    results = {}
    for use in uses:
        if use.binding.check_spec_ref in results:
            raise AssuranceError("AMBIGUOUS_REVIEW_CHECK")
        results[use.binding.check_spec_ref] = use.consume_locked(
            adapter, identity=identity, authority=authority, now_ms=now_ms
        )
    return results


def prepare_official_review(
    commit: Any,
    *,
    tenant_id: str,
    classification_ref: AssuranceRef,
    identity: UseIdentity,
    authority: CurrentAuthority,
    cas: Any,
    pins: Mapping[AssuranceRef, str],
    check_uses: tuple[PreparedCheckUse, ...] = (),
    check_adapter: Any = None,
    maximum_blob_bytes: int = 32 * 1024 * 1024,
) -> PreparedOfficialReview:
    store = commit.store
    if store.connection.in_transaction:
        raise AssuranceError("REVIEW_PREPARATION_INSIDE_TRANSACTION")
    reader = AssuranceReader(store, tenant_id=tenant_id, mission_id=identity.mission_id)
    now_ms = int(store.now * 1000)
    with store.read_view():
        imported = read_imported_review_locked(commit, reader, classification_ref)
        body = imported.binding.to_json()
        scope = body["subject"]["completion_scope_ref"]
        if (
            identity.consumer_kind != "REVIEW"
            or identity.consumer_id != body["review_key"]
            or identity.purpose != "ACCEPT"
            or identity.scope_id != review_scope_id(body)
        ):
            raise AssuranceError("REVIEW_IMPORT_IDENTITY")
        if AssuranceStore(store).lane(identity.mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        epochs = read_epochs_locked(store.connection, identity.mission_id)
        refs = {
            classification_ref,
            imported.turn.ref,
            imported.provider_manifest.ref,
            _raw_ref(imported),
            AssuranceRef("review_package", Pin.from_json(body["package_ref"])),
            AssuranceRef("requirements", Pin.from_json(body["requirements_ref"])),
            AssuranceRef.from_json(body["criterion_policy_ref"]),
            AssuranceRef.from_json(body["subject"]["target"]),
            AssuranceRef("task", Pin.from_json(body["subject"]["owner_task_ref"])),
        }
        if scope is not None:
            refs.add(AssuranceRef("completion_scope", Pin.from_json(scope)))
        refs.update(item.ref for item in imported.catalogue)
        refs.update(item.ref for item in imported.disclosure_metadata)
        metadata = tuple(
            reader.read_exact_metadata(ref) for ref in sorted(refs, key=lambda ref: ref.key)
        )
        permissions = tuple(
            (ref, _permission(authority, identity, ref, now_ms))
            for ref in sorted(refs, key=lambda ref: ref.key)
        )
        complete = read_complete_evidence_snapshot(reader, scope_id=identity.scope_id)
        checks = _consume_checks(check_uses, check_adapter, identity, authority, now_ms)
        allowed = {
            AssuranceRef.from_json(ref)
            for policy in body["check_requirements"]
            for group in policy["any_check_sets"]
            for ref in group
        }
        if not set(checks) <= allowed:
            raise AssuranceError("REVIEW_CHECK_POLICY_MISMATCH")
    blobs, remaining = [], maximum_blob_bytes
    for ref in sorted(
        (ref for ref in refs if ref.kind in {"source", "artifact"}), key=lambda ref: ref.key
    ):
        if ref not in pins:
            raise AssuranceError("LIVE_BLOB_PIN_REQUIRED", ref.pin.id)
        blob = read_pinned_blob(
            reader,
            ref,
            pin_id=pins[ref],
            review_key=body["review_key"],
            cas=cas,
            maximum_bytes=remaining,
            authorize=lambda r: _permission(authority, identity, r, int(store.now * 1000)).access,
            now_ms=lambda: int(store.now * 1000),
        )
        blobs.append(blob)
        remaining -= len(blob.data)
    raw = next(blob.data for blob in blobs if blob.metadata.ref == _raw_ref(imported))
    record, manifest = _interpret(imported, raw, checks)
    reads = [item.read_item for item in metadata] + [item.read_item for item in complete]
    for _, permission in permissions:
        reads.extend((permission.access, permission.policy))
    for use in check_uses:
        reads.extend(use.read_set)
    deadlines = [permission.not_after_ms for _, permission in permissions] + [
        use.not_after_ms for use in check_uses
    ]
    prepared = PreparedOfficialReview(
        commit,
        tenant_id,
        identity,
        authority,
        imported,
        epochs,
        metadata,
        permissions,
        tuple(blobs),
        check_uses,
        check_adapter,
        complete,
        _merge_reads(reads),
        now_ms,
        min(deadlines),
        record,
        canonical(manifest),
    )
    with store.read_view():
        prepared.require_locked(store, record)
    return prepared


def read_official_review_binding_locked(
    commit: Any, tenant_id: str, record: ReviewRecord
) -> ExactMetadata:
    """Authenticate historical transport and sidecar; this grants no current use."""
    reader = AssuranceReader(
        commit.store, tenant_id=tenant_id, mission_id=record.binding.mission_id
    )
    original = reader.read_exact_metadata(
        AssuranceRef("review", Pin(str(record.record_id), 0, fingerprint(record.to_json())))
    )
    if not decode(original.lifecycle_json)["official"]:
        raise AssuranceError("REVIEW_NOT_OFFICIAL")
    row = commit.store.connection.execute(
        "SELECT * FROM assurance_review_record_bindings WHERE record_id=?", (str(record.record_id),)
    ).fetchone()
    if row is None:
        raise AssuranceError("REVIEW_RECORD_BINDING_REQUIRED")
    binding = ReviewRecordBinding(row["binding_json"])
    side = binding.to_json()
    receipt = commit.store.get_receipt(row["import_receipt_id"])
    if receipt is None:
        raise AssuranceError("REVIEW_IMPORT_RECEIPT_MISSING")
    receipt_ref = AssuranceRef(
        "commit_receipt", Pin(row["import_receipt_id"], 0, fingerprint(dict(receipt)))
    )
    metadata = reader.read_exact_metadata(receipt_ref)
    lifecycle = decode(metadata.lifecycle_json)
    imported = read_imported_review_locked(
        commit,
        reader,
        AssuranceRef.from_json(receipt.get("classification_ref"), kinds={"commit_receipt"}),
    )
    if (
        row["binding_hash"] != binding.content_hash
        or lifecycle["kind"] != "AssuranceReviewImported"
        or lifecycle["subject_id"] != str(record.record_id)
        or lifecycle["base_version"] != 0
        or lifecycle["proposal_hash"] != binding.content_hash
        or receipt.get("binding_hash") != binding.content_hash
        or receipt.get("record_id") != str(record.record_id)
        or receipt.get("mission_id") != reader.mission_id
        or side["record_ref"] != original.ref.pin.to_json()
        or side["mission_id"] != reader.mission_id
        or side["package_ref"] != imported.binding.to_json()["package_ref"]
        or side["review_key"] != imported.binding.to_json()["review_key"]
        or side["reviewer_turn_ref"] != imported.turn.ref.to_json()
        or side["reviewer_agent_id"] != record.reviewer_agent_id
        or side["raw_output_ref"] != _raw_ref(imported).to_json()
        or side["evidence_manifest_hash"] != record.evidence_manifest_hash
        or side["invocation_ordinal"] != imported.invocation.to_json()["ordinal"]
        or side["codec_version"] != REVIEW_CODEC_VERSION
        or side["raw_output_hash"] != _raw_ref(imported).pin.content_hash
        or row["mission_id"] != reader.mission_id
        or row["review_key"] != side["review_key"]
        or row["source_turn_ref_hash"] != imported.turn.ref.key
        or row["raw_output_hash"] != side["raw_output_hash"]
        or row["evidence_manifest_hash"] != side["evidence_manifest_hash"]
    ):
        raise AssuranceError("REVIEW_RECORD_BINDING_MISMATCH")
    evidence = reader.read_exact_metadata(
        AssuranceRef(
            "input_manifest", Pin(record.evidence_manifest_hash, 0, record.evidence_manifest_hash)
        )
    )
    manifest = decode(evidence.body_json)
    if (
        manifest.get("codec_version") != REVIEW_CODEC_VERSION
        or manifest.get("binding_hash") != imported.binding.content_hash
        or manifest.get("turn_ref") != imported.turn.ref.to_json()
        or manifest.get("raw_output_hash") != side["raw_output_hash"]
        or manifest.get("effective_verdict") != str(record.verdict)
        or sorted(
            manifest.get("disclosure_refs", []), key=lambda ref: AssuranceRef.from_json(ref).key
        )
        != side["disclosure_refs"]
        or manifest.get("exposed_evidence_refs") != side["exposed_evidence_refs"]
    ):
        raise AssuranceError("REVIEW_EVIDENCE_MANIFEST_MISMATCH")
    return metadata
