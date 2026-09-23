# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Assurance side records on the orchestrator Store, not an authority service.

Only the original Commit Service may call writers after authenticating callers
and verifying source receipts. No method here is a Host/model action.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ..assurance.certificates import UseCertificate
from ..assurance.check_bindings import CheckBinding
from ..assurance.checks import CriterionPolicy
from ..assurance.clock import ClockState
from ..assurance.codec import (
    AssuranceError,
    canonical,
    decode,
    digest,
    fingerprint,
    integer,
    one_of,
    text,
)
from ..assurance.disclosure import DisclosureBatch
from ..assurance.policy import AssurancePolicy
from ..assurance.policy_domain import policy_domain_hash
from ..assurance.refs import AssuranceRef, Pin
from .assurance_reads import MISSION_EPOCH_SCOPE
from .assurance_work import WorkTarget, atomic
from .store import Store, StoreConflict


class AssuranceStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def initialize_environment(
        self,
        *,
        receipt: AssuranceRef,
        root_incarnation_id: str,
        now_ms: int,
    ) -> None:
        """Called by the real startup installation UoW, never by a reader.

        Reopening an existing environment preserves counters and clock history.
        A new root still needs the separate restore authorization gate.
        """
        text(root_incarnation_id)
        integer(now_ms)
        with atomic(self.store) as connection:
            body = self._receipt(receipt)
            original = connection.execute(
                "SELECT kind,subject_id FROM commit_receipts WHERE commit_id=?", (receipt.pin.id,)
            ).fetchone()
            if (
                original["kind"] != "AssuranceEnvironmentInstalled"
                or original["subject_id"] != root_incarnation_id
                or body.get("root_incarnation_id") != root_incarnation_id
            ):
                raise AssuranceError("ENVIRONMENT_INSTALL_RECEIPT_MISMATCH")
            existing = connection.execute(
                "SELECT 1 FROM assurance_environment_state WHERE singleton=1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO assurance_environment_state VALUES(1,0,0,?,'STABLE',1,?)",
                    (now_ms, receipt.pin.id),
                )

    def bind_profile_locked(
        self,
        mission_id: str,
        *,
        policy: AssurancePolicy,
        activation_receipt: AssuranceRef,
        activation_event_id: str,
        now_ms: int,
        reconciliation: Mapping[str, Sequence[WorkTarget]],
    ) -> None:
        """Finish the original Mission factory's atomic creation protocol.

        Requirements and the original activation event/receipt already exist in
        this same UoW. No existing Mission may be silently upgraded here.
        """
        if not self.store.connection.in_transaction:
            raise AssuranceError("FACTORY_TRANSACTION_REQUIRED")
        integer(now_ms)
        if not isinstance(policy, AssurancePolicy):
            raise AssuranceError("ASSURANCE_POLICY_UNREGISTERED")
        from .assurance_work import CONSUMERS, AssuranceWorkStore

        if set(reconciliation) != CONSUMERS:
            raise AssuranceError("ACTIVATION_RECONCILIATION_INCOMPLETE")
        manifest = {
            consumer: [
                {"work_key": target.work_key, "fingerprint": target.fingerprint}
                for target in sorted(reconciliation[consumer], key=lambda value: value.work_key)
            ]
            for consumer in sorted(CONSUMERS)
        }
        if any(len(rows) != len({row["work_key"] for row in rows}) for rows in manifest.values()):
            raise AssuranceError("ACTIVATION_RECONCILIATION_DUPLICATE")
        policy_json = canonical(policy.to_json())
        policy_hash = fingerprint(policy.to_json())
        with atomic(self.store) as connection:
            receipt = self._receipt(activation_receipt)
            original = connection.execute(
                "SELECT kind,subject_id FROM commit_receipts WHERE commit_id=?",
                (activation_receipt.pin.id,),
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM events WHERE event_id=? AND mission_id=?",
                (activation_event_id, mission_id),
            ).fetchone()
            if (
                original["kind"] != "AssuranceProfileActivated"
                or original["subject_id"] != mission_id
                or receipt.get("mission_id") != mission_id
                or receipt.get("policy_hash") != policy_hash
                or receipt.get("activation_event_id") != activation_event_id
                or receipt.get("reconciliation") != manifest
                or receipt.get("reconciliation_hash") != fingerprint(manifest)
                or event is None
                or event["type"] != "AssuranceProfileActivated"
                or event["actor_type"] != "system"
                or decode(event["payload_json"]).get("policy_hash") != policy_hash
                or decode(event["payload_json"]).get("reconciliation_hash") != fingerprint(manifest)
            ):
                raise AssuranceError("PROFILE_ACTIVATION_RECEIPT_MISMATCH")
            creation = connection.execute(
                "SELECT lane,receipt_id FROM assurance_creation_contracts WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if (
                creation is None
                or creation["lane"] != "ASSURANCE_1_1"
                or creation["receipt_id"] != activation_receipt.pin.id
            ):
                raise AssuranceError("CREATION_CONTRACT_UNRESOLVED")
            if (
                connection.execute(
                    "SELECT 1 FROM requirements_revisions WHERE mission_id=? LIMIT 1", (mission_id,)
                ).fetchone()
                is None
            ):
                raise AssuranceError("FACTORY_REQUIREMENTS_MISSING")
            if (
                connection.execute(
                    "SELECT 1 FROM assurance_environment_state WHERE singleton=1"
                ).fetchone()
                is None
            ):
                raise AssuranceError("ASSURANCE_ENVIRONMENT_UNINITIALIZED")
            known = connection.execute(
                "SELECT * FROM assurance_mission_bindings WHERE mission_id=?", (mission_id,)
            ).fetchone()
            if known is not None:
                if (
                    known["policy_json"] != policy_json
                    or known["activation_receipt_id"] != activation_receipt.pin.id
                ):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", mission_id)
                # Replay must not repair missing state or reset cursors/epochs.
                from .assurance_reads import read_epochs_locked

                read_epochs_locked(connection, mission_id)
                consumers = {
                    r[0]
                    for r in connection.execute(
                        "SELECT consumer FROM assurance_event_cursors WHERE mission_id=?",
                        (mission_id,),
                    )
                }
                if consumers != {"REVIEW", "VALIDITY", "CLOSEOUT", "NOTIFY"}:
                    raise AssuranceError("CURSOR_UNINITIALIZED")
                return
            connection.execute(
                "INSERT INTO validity_epochs VALUES(?,?,0,?,?)",
                (mission_id, MISSION_EPOCH_SCOPE, activation_receipt.pin.id, self.store.now),
            )
            connection.execute(
                "INSERT INTO assurance_mission_bindings VALUES(?,'assurance-exec-v1.1',?,?,?,?)",
                (mission_id, policy_hash, policy_json, activation_receipt.pin.id, now_ms),
            )
            work = AssuranceWorkStore(self.store)
            events = self.store.list_events(mission_id, after_seq=event["seq"] - 1, limit=1)
            if len(events) != 1 or events[0].id != activation_event_id:
                raise AssuranceError("ACTIVATION_EVENT_MISSING")
            activation = events[0]
            for consumer in sorted(CONSUMERS):
                work.seed(mission_id, consumer, activation, reconciliation[consumer], now_ms=now_ms)
                work.initialize_cursor(
                    mission_id, consumer, activation_seq=event["seq"], now_ms=now_ms
                )

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        table: str,
        row: Mapping[str, Any],
        *,
        identities: Sequence[tuple[str, ...]],
    ) -> bool:
        """Only called with fixed table/column literals below, never wire identifiers.

        Full identity replay is a read; any alternate unique collision is a
        conflict. Created-at metadata from a retry does not overwrite history.
        SQL guards remain the second line against raw REPLACE.
        """
        clauses = ["(" + " AND ".join(f"{key}=?" for key in group) + ")" for group in identities]
        params = tuple(row[key] for group in identities for key in group)
        found = connection.execute(
            f"SELECT * FROM {table} WHERE " + " OR ".join(clauses), params
        ).fetchall()
        if found:
            if len(found) != 1 or any(
                found[0][key] != value for key, value in row.items() if key != "created_at_ms"
            ):
                raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", table)
            return False
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        connection.execute(
            f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(row.values())
        )
        return True

    def _receipt(self, ref: AssuranceRef) -> dict[str, Any]:
        if ref.kind != "commit_receipt" or ref.pin.revision != 0:
            raise AssuranceError("COMMIT_RECEIPT_REF_REQUIRED")
        body = self.store.get_receipt(ref.pin.id)
        if body is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", ref.pin.id)
        frozen = dict(body)
        if fingerprint(frozen) != ref.pin.content_hash:
            raise AssuranceError("REF_BODY_CONFLICT", ref.pin.id)
        return frozen

    def lane(self, mission_id: str) -> str:
        row = self.store.connection.execute(
            "SELECT lane FROM assurance_creation_contracts WHERE mission_id=?", (mission_id,)
        ).fetchone()
        if row is None:
            raise AssuranceError("CREATION_CONTRACT_UNRESOLVED")
        if (
            row["lane"] == "ASSURANCE_1_1"
            and self.store.connection.execute(
                "SELECT 1 FROM assurance_mission_bindings WHERE mission_id=?", (mission_id,)
            ).fetchone()
            is None
        ):
            raise AssuranceError("ASSURANCE_PROFILE_UNBOUND")
        return row["lane"]

    def record_creation_contract(
        self,
        mission_id: str,
        *,
        lane: str,
        origin: str,
        source_hash: str,
        receipt: AssuranceRef,
        now_ms: int,
    ) -> bool:
        row = {
            "mission_id": text(mission_id),
            "lane": one_of(lane, {"LEGACY", "COMPLETION_V1", "ASSURANCE_1_1"}),
            "origin": one_of(origin, {"FACTORY", "MIGRATION_CLASSIFICATION", "EXPLICIT_SUCCESSOR"}),
            "source_hash": digest(source_hash),
            "receipt_id": receipt.pin.id,
            "created_at_ms": integer(now_ms),
        }
        with atomic(self.store) as connection:
            self._receipt(receipt)
            return self._insert(
                connection, "assurance_creation_contracts", row, identities=(("mission_id",),)
            )

    def record_criterion_policy(
        self,
        policy_id: str,
        *,
        mission_id: str,
        requirements: Pin,
        scope_hash: str,
        criteria: tuple[CriterionPolicy, ...],
        approval_receipt: AssuranceRef,
        adapter_version: str,
    ) -> AssuranceRef:
        if not 1 <= len(criteria) <= 256 or len({c.criterion_id for c in criteria}) != len(
            criteria
        ):
            raise AssuranceError("POLICY_CRITERIA_INVALID")
        document = {
            "schema_version": 1,
            "mission_id": text(mission_id),
            "requirements_ref": requirements.to_json(),
            "scope_hash": digest(scope_hash),
            "criteria": [c.to_json() for c in sorted(criteria, key=lambda c: c.criterion_id)],
            "approval_receipt_ref": approval_receipt.to_json(),
            "adapter_version": text(adapter_version),
        }
        content_hash = fingerprint(document)
        row = {
            "policy_id": text(policy_id),
            "mission_id": mission_id,
            "requirements_revision": requirements.revision,
            "requirements_hash": requirements.content_hash,
            "scope_hash": scope_hash,
            "policy_hash": content_hash,
            "policy_json": canonical(document),
            "approval_receipt_id": approval_receipt.pin.id,
        }
        with atomic(self.store) as connection:
            approval = self._receipt(approval_receipt)
            original = connection.execute(
                "SELECT kind,subject_id,base_version,proposal_hash FROM commit_receipts "
                "WHERE commit_id=?",
                (approval_receipt.pin.id,),
            ).fetchone()
            scope = approval.get("completion_scope")
            subject = approval.get("planning_subject")
            if (scope is None) == (subject is None):
                raise AssuranceError("CHECK_POLICY_APPROVAL_RECEIPT_MISMATCH")
            scope_ref = (
                None if scope is None else AssuranceRef.from_json(scope, kinds={"completion_scope"})
            )
            subject_ref = (
                None if subject is None else AssuranceRef.from_json(subject, kinds={"task"})
            )
            purpose = str(approval.get("purpose", "CONTENT"))
            expected_domain = policy_domain_hash(
                "TASK_CONTENT" if purpose == "CONTENT" else purpose,
                scope_hash=None if scope_ref is None else scope_ref.pin.content_hash,
                task_hash=None if subject_ref is None else subject_ref.pin.content_hash,
            )
            if (
                original["kind"] != "AssuranceCheckPolicyApproved"
                or original["subject_id"] != policy_id
                or original["base_version"] != requirements.revision
                or original["proposal_hash"] != fingerprint(approval)
                or approval.get("mission_id") != mission_id
                or approval.get("policy_id") != policy_id
                or approval.get("requirements_ref")
                != AssuranceRef("requirements", requirements).to_json()
                or expected_domain != scope_hash
                or approval.get("criteria") != document["criteria"]
                or approval.get("adapter_version") != adapter_version
            ):
                raise AssuranceError("CHECK_POLICY_APPROVAL_RECEIPT_MISMATCH")
            source = connection.execute(
                "SELECT revision_id,content_hash,revision_json FROM requirements_revisions "
                "WHERE mission_id=? AND revision=?",
                (mission_id, requirements.revision),
            ).fetchone()
            if source is None:
                raise AssuranceError("SOURCE_UNAVAILABLE", requirements.id)
            if (
                source["revision_id"] != requirements.id
                or source["content_hash"] != requirements.content_hash
                or fingerprint(decode(source["revision_json"])) != requirements.content_hash
            ):
                raise AssuranceError("REF_BODY_CONFLICT", requirements.id)
            self._insert(
                connection,
                "assurance_criterion_policies",
                row,
                identities=(("policy_id",), ("mission_id", "requirements_revision", "scope_hash")),
            )
        return AssuranceRef("check_policy", Pin(policy_id, 0, content_hash))

    def record_check_binding(
        self,
        binding_id: str,
        binding: CheckBinding,
        *,
        receipt: AssuranceRef,
    ) -> AssuranceRef:
        if not isinstance(binding, CheckBinding):
            raise AssuranceError("CHECK_BINDING_REQUIRED")
        body = binding.to_json()
        binding_hash = fingerprint(body)
        row = {
            "check_binding_id": text(binding_id),
            "mission_id": binding.mission_id,
            "execution_ref_hash": binding.execution_ref.key,
            "check_spec_hash": binding.check_spec_ref.key,
            "subject_hash": binding.subject_hash,
            "assertion_key": binding.assertion_key,
            "binding_hash": binding_hash,
            "binding_json": canonical(body),
            "import_receipt_id": receipt.pin.id,
            "created_at_ms": binding.observed_at_ms,
        }
        with atomic(self.store) as connection:
            imported = self._receipt(receipt)
            original = connection.execute(
                "SELECT kind,subject_id,base_version,proposal_hash FROM commit_receipts "
                "WHERE commit_id=?",
                (receipt.pin.id,),
            ).fetchone()
            if (
                original["kind"] != "AssuranceCheckBound"
                or original["subject_id"] != binding_id
                or original["base_version"] != 0
                or original["proposal_hash"] != binding_hash
                or imported
                != {
                    "mission_id": binding.mission_id,
                    "check_binding_id": binding_id,
                    "binding_hash": binding_hash,
                    "execution_ref": binding.execution_ref.to_json(),
                }
            ):
                raise AssuranceError("CHECK_IMPORT_RECEIPT_MISMATCH")
            self._insert(
                connection,
                "assurance_check_bindings",
                row,
                identities=(
                    ("check_binding_id",),
                    (
                        "mission_id",
                        "execution_ref_hash",
                        "check_spec_hash",
                        "subject_hash",
                        "assertion_key",
                    ),
                ),
            )
        return AssuranceRef("check_binding", Pin(binding_id, 0, binding_hash))

    def record_disclosure(self, batch: DisclosureBatch) -> bool:
        """Trusted runtime importer already verified the exact Provider input.

        Verify the immutable Event binding again here. The event payload contains
        the delta hash, never this batch's future hash (no circular hash).
        """
        row = {
            "review_key": batch.review_key,
            "batch_no": batch.batch_no,
            "mission_id": batch.mission_id,
            "previous_hash": batch.previous_batch_hash,
            "batch_hash": batch.content_hash,
            "batch_json": canonical(batch.to_json()),
            "disclosure_event_id": batch.delivery_receipt_ref.pin.id,
        }
        with atomic(self.store) as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (batch.delivery_receipt_ref.pin.id,)
            ).fetchone()
            if (
                event is None
                or event["type"] != "AssuranceEvidenceDisclosed"
                or (
                    event["mission_id"] != batch.mission_id
                    or batch.delivery_receipt_ref.pin.revision != 0
                )
            ):
                raise AssuranceError("DISCLOSURE_EVENT_INVALID")
            # Reuse Event.to_json, including original seq, rather than hashing a
            # projection with fields removed from the canonical event contract.
            from .store import _event_from_row

            actual = _event_from_row(event)
            if fingerprint(actual.to_json()) != batch.delivery_receipt_ref.pin.content_hash:
                raise AssuranceError("REF_BODY_CONFLICT")
            expected = {
                "review_key": batch.review_key,
                "batch_no": batch.batch_no,
                "previous_batch_hash": batch.previous_batch_hash,
                "delta_hash": fingerprint([e.to_json() for e in batch.entries]),
                "reviewer_agent_id": batch.reviewer_agent_id,
                "turn_receipt_ref": batch.turn_receipt_ref.to_json(),
                "provider_input_hash": batch.provider_input_hash,
                "visible_message_ids": list(batch.visible_message_ids),
            }
            if any(actual.payload.get(key) != value for key, value in expected.items()):
                raise AssuranceError("DISCLOSURE_INPUT_BINDING")
            return self._insert(
                connection,
                "assurance_disclosure_batches",
                row,
                identities=(("review_key", "batch_no"),),
            )

    def disclosure_chain(self, mission_id: str, review_key: str) -> tuple[DisclosureBatch, ...]:
        rows = self.store.connection.execute(
            "SELECT * FROM assurance_disclosure_batches WHERE mission_id=? AND review_key=? "
            "ORDER BY batch_no",
            (mission_id, review_key),
        ).fetchall()
        batches = []
        previous = None
        for ordinal, row in enumerate(rows):
            batch = DisclosureBatch.from_json(decode(row["batch_json"]))
            if (
                batch.content_hash != row["batch_hash"]
                or batch.batch_no != ordinal
                or batch.mission_id != mission_id
                or batch.review_key != review_key
                or batch.previous_batch_hash != previous
            ):
                raise AssuranceError("DISCLOSURE_CHAIN_INVALID")
            batches.append(batch)
            previous = batch.content_hash
        return tuple(batches)

    def record_certificate(self, certificate_id: str, certificate: UseCertificate) -> bool:
        """Called by the use writer after final-lock revalidation, within its UoW."""
        body = certificate.to_json()
        row = {
            "certificate_id": text(certificate_id),
            "mission_id": certificate.mission_id,
            "consumer_kind": certificate.consumer_kind,
            "consumer_id": certificate.consumer_id,
            "purpose": certificate.purpose,
            "scope_id": certificate.scope_id,
            "read_set_hash": fingerprint(body["read_set"]),
            "certificate_hash": fingerprint(body),
            "certificate_json": canonical(body),
            "issued_at_ms": certificate.issued_at_ms,
            "not_after_ms": certificate.not_after_ms,
        }
        with atomic(self.store) as connection:
            inserted = self._insert(
                connection, "assurance_use_certificates", row, identities=(("certificate_id",),)
            )
            if inserted:
                connection.executemany(
                    "INSERT INTO assurance_dependency_index VALUES(?,?,?,?,?)",
                    [
                        (
                            certificate.mission_id,
                            certificate_id,
                            item.channel,
                            item.key,
                            item.fingerprint,
                        )
                        for item in certificate.read_set
                    ],
                )
            return inserted

    def acquire_pin(
        self,
        pin_id: str,
        *,
        mission_id: str,
        review_key: str,
        blob_hash: str,
        object_ref: AssuranceRef,
        receipt: AssuranceRef,
        now_ms: int,
    ) -> bool:
        """PREPARING must commit before a subsequent CAS read; failed reads release it."""
        row = {
            "pin_id": text(pin_id),
            "mission_id": text(mission_id),
            "review_key": text(review_key),
            "blob_hash": digest(blob_hash),
            "object_ref_json": canonical(object_ref.to_json()),
            "state": "PREPARING",
            "row_version": 1,
            "created_at_ms": integer(now_ms),
            "released_at_ms": None,
            "source_receipt_id": receipt.pin.id,
            "last_receipt_id": receipt.pin.id,
        }
        with atomic(self.store) as connection:
            # Acquisition replay may encounter an already BOUND/RELEASED pin.
            from .assurance_pins import require_pin_receipt

            require_pin_receipt(
                connection,
                receipt_id=receipt.pin.id,
                pin=row,
                state="PREPARING",
                expected_version=0,
                at_ms=now_ms,
                receipt_ref=receipt,
            )
            old = connection.execute(
                "SELECT * FROM assurance_blob_pins WHERE pin_id=?", (pin_id,)
            ).fetchone()
            if old is not None:
                stable = (
                    "pin_id",
                    "mission_id",
                    "review_key",
                    "blob_hash",
                    "object_ref_json",
                    "source_receipt_id",
                )
                if any(old[key] != row[key] for key in stable):
                    raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT", pin_id)
                return False
            return self._insert(
                connection,
                "assurance_blob_pins",
                row,
                identities=(("pin_id",), ("mission_id", "review_key", "blob_hash")),
            )

    def transition_pin(
        self,
        pin_id: str,
        *,
        mission_id: str,
        expected_version: int,
        state: str,
        receipt: AssuranceRef,
        now_ms: int,
    ) -> bool:
        one_of(state, {"BOUND", "RELEASED"})
        integer(expected_version, minimum=1)
        integer(now_ms)
        with atomic(self.store) as connection:
            row = connection.execute(
                "SELECT * FROM assurance_blob_pins WHERE pin_id=? AND mission_id=?",
                (pin_id, mission_id),
            ).fetchone()
            if row is None:
                raise AssuranceError("SOURCE_UNAVAILABLE", pin_id)
            from .assurance_pins import require_pin_receipt

            require_pin_receipt(
                connection,
                receipt_id=receipt.pin.id,
                pin=row,
                state=state,
                expected_version=expected_version,
                at_ms=now_ms,
                receipt_ref=receipt,
            )
            if row["last_receipt_id"] == receipt.pin.id and row["state"] == state:
                return False
            if row["row_version"] != expected_version:
                raise StoreConflict("assurance pin changed")
            connection.execute(
                "UPDATE assurance_blob_pins SET state=?,row_version=row_version+1,"
                "released_at_ms=?,last_receipt_id=? WHERE pin_id=? AND row_version=?",
                (
                    state,
                    now_ms if state == "RELEASED" else None,
                    receipt.pin.id,
                    pin_id,
                    expected_version,
                ),
            )
            return True

    def has_live_pin(self, blob_hash: str) -> bool:
        digest(blob_hash)
        return (
            self.store.connection.execute(
                "SELECT 1 FROM assurance_blob_pins WHERE blob_hash=? "
                "AND state IN ('PREPARING','BOUND') LIMIT 1",
                (blob_hash,),
            ).fetchone()
            is not None
        )

    def observe_clock(self, now_ms: int, *, receipt: AssuranceRef) -> ClockState:
        """Original tick owns the receipt and TimeDiscontinuity event in the same UoW."""
        integer(now_ms)
        with atomic(self.store) as connection:
            body = self._receipt(receipt)
            row = connection.execute(
                "SELECT * FROM assurance_environment_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise AssuranceError("ASSURANCE_ENVIRONMENT_UNINITIALIZED")
            old = ClockState(row["clock_generation"], row["wall_high_ms"], row["clock_state"])
            new = old.observe(now_ms)
            original = connection.execute(
                "SELECT kind,subject_id,base_version FROM commit_receipts WHERE commit_id=?",
                (receipt.pin.id,),
            ).fetchone()
            if (
                original["kind"] != "AssuranceClockObserved"
                or original["subject_id"] != "assurance-environment"
                or original["base_version"] != row["row_version"]
                or body
                != {
                    "schema_version": 1,
                    "receipt_role": "CLOCK_OBSERVATION_ONLY",
                    "observed_at_ms": now_ms,
                    "previous_row_version": row["row_version"],
                    "clock_generation": new.generation,
                    "wall_high_ms": new.wall_high_ms,
                    "clock_state": new.state,
                }
            ):
                raise AssuranceError("CLOCK_OBSERVATION_RECEIPT_MISMATCH")
            if new != old:
                connection.execute(
                    "UPDATE assurance_environment_state SET clock_generation=?,wall_high_ms=?,"
                    "clock_state=?,row_version=row_version+1,change_receipt_id=? "
                    "WHERE singleton=1 AND row_version=?",
                    (
                        new.generation,
                        new.wall_high_ms,
                        new.state,
                        receipt.pin.id,
                        row["row_version"],
                    ),
                )
            return new
