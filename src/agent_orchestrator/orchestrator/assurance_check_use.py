# SPDX-License-Identifier: Apache-2.0
"""Prepare an exact local assertion for a current use, without issuing a licence.

The review/validity assembler supplies the actual consumer, current authority and
already committed preparation pins. Reads cannot register a checker or import a
binding. Every local candidate branch is captured; the full validity evaluator
must still admit rules and compute conflict-free support before issuing a use
certificate. This component proves only the registered layer assertion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..assurance.certificates import PURPOSES, UseIdentity
from ..assurance.check_bindings import CheckBinding
from ..assurance.checks import CheckResult
from ..assurance.codec import AssuranceError, decode, fingerprint, integer, one_of, text
from ..assurance.evidence import ReadItem, canonical_read_set
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.root_gate import CurrentReadPermission
from ..contracts import Artifact
from ..contracts.operation_completion import OccurrenceCompletionScopeV1
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
from ..storage.htn_store import HtnStore
from .assurance_check_import import read_local_check_binding_locked

if TYPE_CHECKING:
    from .assurance_local_checks import AssuranceLocalChecks

CurrentAuthority = Callable[[UseIdentity, AssuranceRef], CurrentReadPermission]


def _permission(
    authority: CurrentAuthority, identity: UseIdentity, ref: AssuranceRef, now_ms: int
) -> CurrentReadPermission:
    permission = authority(identity, ref)
    if not isinstance(permission, CurrentReadPermission):
        raise AssuranceError("ACCESS_POLICY_WITNESS_REQUIRED")
    if now_ms >= permission.not_after_ms:
        raise AssuranceError("CHECK_USE_EXPIRED")
    return permission


def _require_same_permission(
    authority: CurrentAuthority,
    identity: UseIdentity,
    ref: AssuranceRef,
    captured: CurrentReadPermission,
    now_ms: int,
) -> None:
    """Final barrier: the captured grant is still in force and CURRENT authority
    still grants the same access under the same policy.

    ``not_after_ms`` is a lease on the captured grant, not part of its identity: a
    production authority re-issues it relative to *now*, so comparing it would
    fail every re-check that is not in the same millisecond (Host real model run
    10, 2026-09-23). The captured lease is enforced; the fresh one is checked by
    ``_permission``.
    """
    if now_ms >= captured.not_after_ms:
        raise AssuranceError("CHECK_USE_EXPIRED")
    current = _permission(authority, identity, ref, now_ms)
    if (current.access, current.policy) != (captured.access, captured.policy):
        raise AssuranceError("RECHECK_REQUIRED")


def _merge_reads(rows: list[ReadItem]) -> tuple[ReadItem, ...]:
    # A source used twice is one read. Conflicting observations never collapse.
    # The wire contract still rejects duplicate keys rather than normalizing it.
    unique: dict[tuple[str, str], ReadItem] = {}
    for row in rows:
        key = row.channel, row.key
        if unique.setdefault(key, row) != row:
            raise AssuranceError("RECHECK_REQUIRED")
    return canonical_read_set(unique.values())


@dataclass(frozen=True, slots=True)
class PreparedCheckUse:
    identity: UseIdentity
    binding: CheckBinding
    binding_ref: AssuranceRef
    epochs: EpochSnapshot
    metadata: tuple[ExactMetadata, ...]
    permissions: tuple[tuple[AssuranceRef, CurrentReadPermission], ...]
    blobs: tuple[PreparedBlob, ...]
    complete_reads: tuple[CompleteRead, ...]
    read_set: tuple[ReadItem, ...]
    prepared_at_ms: int
    not_after_ms: int

    def consume_locked(
        self,
        adapter: AssuranceLocalChecks,
        *,
        identity: UseIdentity,
        authority: CurrentAuthority,
        now_ms: int,
    ) -> CheckResult:
        """Short original use transaction: no CAS reads or full-set rescan here.

        Returning this normalized assertion does not authorize the consumer or
        accept a review. The original consumer also needs a full use certificate.
        """
        store = adapter.commit.store
        if not store.connection.in_transaction:
            raise AssuranceError("CHECK_USE_TRANSACTION_REQUIRED")
        if identity != self.identity:
            raise AssuranceError("CHECK_USE_IDENTITY")
        integer(now_ms)
        if now_ms < self.prepared_at_ms or now_ms >= self.not_after_ms:
            raise AssuranceError("CHECK_USE_EXPIRED")
        adapter._require_deployment()
        if (
            self.binding.adapter_ref
            != Pin("assurance-local-check-adapter", 1, adapter.recorder_hash)
            or self.binding.environment_hash != adapter.environment_hash
        ):
            raise AssuranceError("RECHECK_REQUIRED")
        gate = adapter.commit._assurance_root_gate
        if (
            gate is None
            or gate.require_execution().root_incarnation_id != identity.root_incarnation_id
        ):
            raise AssuranceError("CHECK_USE_IDENTITY")
        reader = AssuranceReader(store, tenant_id=adapter.tenant_id, mission_id=identity.mission_id)
        require_epochs_locked(store.connection, identity.mission_id, self.epochs, now_ms=now_ms)
        # Exact objects are bounded by the preparation byte/object limits. Other
        # candidate branches are protected by the shared source-writer epochs.
        for metadata in self.metadata:
            if reader.read_exact_metadata(metadata.ref) != metadata:
                raise AssuranceError("RECHECK_REQUIRED")
        for ref, captured in self.permissions:
            # Same grant, not the same lease (the fifth barrier missed by run 10's fix;
            # Host native run arp.11, 2026-09-24: every checked review re-checked 32x).
            _require_same_permission(authority, identity, ref, captured, now_ms)
        for blob in self.blobs:
            blob.require_current_locked(
                reader,
                now_ms=now_ms,
                authorize=lambda ref: _permission(authority, identity, ref, now_ms).access,
            )
        return CheckResult(
            self.binding.check_spec_ref,
            self.binding.execution_ref,
            self.binding.execution_state,
            self.binding.verdict,
            source_valid=True,
        )


def prepare_local_check_use(
    adapter: AssuranceLocalChecks,
    *,
    binding_ref: AssuranceRef,
    result_ref: AssuranceRef,
    completion_scope: AssuranceRef,
    identity: UseIdentity,
    review_key: str,
    pins: Mapping[AssuranceRef, str],
    authority: CurrentAuthority,
    maximum_blob_bytes: int,
) -> PreparedCheckUse:
    """Authenticate persisted sources, then read pinned CAS outside the Store lock.

    The caller's pin map names existing original preparation receipts, never CAS
    locations. A missing/unreadable dependency rejects preparation; callers must
    represent it as UNKNOWN, never substitute the historical PASS.
    """
    store = adapter.commit.store
    if store.connection.in_transaction:
        raise AssuranceError("CHECK_PREPARATION_INSIDE_TRANSACTION")
    if (
        binding_ref.kind != "check_binding"
        or result_ref.kind != "result"
        or completion_scope.kind != "completion_scope"
        or not isinstance(identity, UseIdentity)
    ):
        raise AssuranceError("CHECK_USE_IDENTITY")
    for value in (
        identity.mission_id,
        identity.scope_id,
        identity.consumer_kind,
        identity.consumer_id,
        identity.principal_id,
        identity.root_incarnation_id,
        review_key,
    ):
        text(value)
    one_of(identity.purpose, PURPOSES)
    # A local check records a point in time, never continuous monitoring.
    if identity.purpose == "MAINTAIN":
        raise AssuranceError("CHECK_CONTINUOUS_COVERAGE_UNAVAILABLE")
    integer(maximum_blob_bytes, minimum=1)
    adapter._require_deployment()
    gate = adapter.commit._assurance_root_gate
    if gate is None or gate.require_execution().root_incarnation_id != identity.root_incarnation_id:
        raise AssuranceError("CHECK_USE_IDENTITY")
    reader = AssuranceReader(store, tenant_id=adapter.tenant_id, mission_id=identity.mission_id)
    now_ms = integer(int(store.now * 1000))
    with store.read_view() as connection:
        epochs = read_epochs_locked(connection, identity.mission_id)
        require_epochs_locked(connection, identity.mission_id, epochs, now_ms=now_ms)
        binding_metadata = reader.read_exact_metadata(binding_ref)
        binding = CheckBinding.from_json(decode(binding_metadata.body_json))
        scope_metadata = reader.read_exact_metadata(completion_scope)
        scope = OccurrenceCompletionScopeV1.from_json(decode(scope_metadata.body_json))
        if (
            scope.scope_id != identity.scope_id
            or binding.mission_id != identity.mission_id
            or binding.scope_hash != completion_scope.pin.content_hash
            or binding.subject_hash != result_ref.pin.content_hash
        ):
            raise AssuranceError("CHECK_USE_IDENTITY")
        if binding.observed_at_ms > now_ms or (
            binding.not_after_ms is not None and now_ms >= binding.not_after_ms
        ):
            raise AssuranceError("CHECK_USE_EXPIRED")
        actual = read_local_check_binding_locked(
            adapter,
            mission_id=identity.mission_id,
            execution_ref=binding.execution_ref,
            completion_scope=completion_scope,
        )
        if actual != binding:
            raise AssuranceError("CHECK_BINDING_SOURCE_MISMATCH")
        event_metadata = reader.read_exact_metadata(binding.execution_ref)
        payload = decode(event_metadata.body_json)["payload"]
        if AssuranceRef.from_json(payload["result_ref"]) != result_ref:
            raise AssuranceError("CHECK_USE_IDENTITY")
        manifest_ref = AssuranceRef.from_json(
            payload["input_manifest_ref"], kinds={"input_manifest"}
        )
        manifest_metadata = reader.read_exact_metadata(manifest_ref)
        manifest = decode(manifest_metadata.body_json)
        source_row = decode(binding_metadata.lifecycle_json)
        receipt_id = source_row["import_receipt_id"]
        receipt_row = connection.execute(
            "SELECT * FROM commit_receipts WHERE commit_id=?", (receipt_id,)
        ).fetchone()
        expected = {
            "mission_id": identity.mission_id,
            "check_binding_id": binding_ref.pin.id,
            "binding_hash": binding_ref.pin.content_hash,
            "execution_ref": binding.execution_ref.to_json(),
        }
        if (
            receipt_row is None
            or receipt_row["kind"] != "AssuranceCheckBound"
            or receipt_row["subject_id"] != binding_ref.pin.id
            or receipt_row["base_version"] != 0
            or receipt_row["proposal_hash"] != binding_ref.pin.content_hash
            or decode(receipt_row["receipt_json"]) != expected
        ):
            raise AssuranceError("CHECK_IMPORT_RECEIPT_MISMATCH")
        receipt_ref = AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(expected)))
        semantics = HtnStore(store).get_task_semantics(scope.task_ref.id, scope.task_ref.revision)
        # OCC pins the contract_hash field; Assurance task metadata pins the
        # complete original semantic binding. They are distinct hash domains.
        task_ref = AssuranceRef(
            "task", Pin(scope.task_ref.id, scope.task_ref.revision, semantics.content_hash())
        )
        required = {
            binding_ref,
            result_ref,
            completion_scope,
            binding.check_spec_ref,
            binding.execution_ref,
            manifest_ref,
            receipt_ref,
            AssuranceRef("requirements", Pin.from_json(scope.requirements_ref.to_json())),
            task_ref,
        }
        blob_refs = set(binding.evidence_refs)
        for document in manifest["artifacts"]:
            artifact = Artifact.from_json(document)
            ref = AssuranceRef(
                "artifact", Pin(artifact.id, artifact.version, artifact.content_hash)
            )
            if decode(reader.read_exact_metadata(ref).body_json) != document:
                raise AssuranceError("CHECK_INPUT_MANIFEST_MISMATCH")
            blob_refs.add(ref)
        if not blob_refs <= pins.keys():
            raise AssuranceError("LIVE_BLOB_PIN_REQUIRED")
        required.update(blob_refs)
        if len(required) > 512:
            raise AssuranceError("CHECK_USE_DEPENDENCY_LIMIT")
        metadata = tuple(
            reader.read_exact_metadata(ref) for ref in sorted(required, key=lambda r: r.key)
        )
        permissions = tuple(
            (row.ref, _permission(authority, identity, row.ref, now_ms)) for row in metadata
        )
        complete = read_complete_evidence_snapshot(reader, scope_id=identity.scope_id)
    # No writer calls above: complete snapshot and sources refer to one read view.
    remaining = maximum_blob_bytes
    blobs = []
    for ref in sorted(blob_refs, key=lambda r: r.key):
        if remaining <= 0:
            raise AssuranceError("EVIDENCE_BYTE_LIMIT")
        blob = read_pinned_blob(
            reader,
            ref,
            pin_id=pins[ref],
            review_key=review_key,
            cas=adapter.cas,
            maximum_bytes=remaining,
            authorize=lambda r: _permission(authority, identity, r, int(store.now * 1000)).access,
            now_ms=lambda: int(store.now * 1000),
        )
        remaining -= len(blob.data)
        blobs.append(blob)
    deadlines = [permission.not_after_ms for _, permission in permissions]
    if binding.not_after_ms is not None:
        deadlines.append(binding.not_after_ms)
    reads = [row.read_item for row in metadata] + [row.read_item for row in complete]
    for _, permission in permissions:
        reads.extend((permission.access, permission.policy))
    prepared = PreparedCheckUse(
        identity,
        binding,
        binding_ref,
        epochs,
        metadata,
        permissions,
        tuple(blobs),
        complete,
        _merge_reads(reads),
        now_ms,
        min(deadlines),
    )
    # Check races during CAS I/O before handing even the normalized assertion to
    # a coordinator. The consuming original write transaction checks again.
    with store.read_view():
        prepared.consume_locked(
            adapter, identity=identity, authority=authority, now_ms=int(store.now * 1000)
        )
    return prepared
