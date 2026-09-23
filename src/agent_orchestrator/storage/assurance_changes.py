# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Original writer UoW context for global source invalidation.

The context is not permission to mutate a source. Only the original Commit
writer supplies its actual command receipt id. The migration's deferred FK
requires that receipt to exist by commit; this module creates no receipts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from ..assurance.codec import AssuranceError, canonical, decode, fingerprint, text

if TYPE_CHECKING:
    from .store import Store


def install_change_context(store: Store) -> None:
    """Install a connection-local receipt accessor, including on reopened Stores."""
    store._assurance_change_context = None

    def current_receipt() -> str | None:
        context = store._assurance_change_context
        if context is None or not store.connection.in_transaction:
            return None
        receipt_id, holder = context
        if holder is not store._current_task():
            return None
        return receipt_id

    store.connection.create_function("assurance_change_receipt", 0, current_receipt)

    def offline_relocation() -> int:
        context = store._assurance_offline_relocation
        return int(
            context is not None
            and context[1] is store._current_task()
            and store.connection.in_transaction
        )

    store.connection.create_function("assurance_offline_relocation", 0, offline_relocation)

    def same_artifact_content(old: str, new: str) -> int:
        try:
            old_body, new_body = decode(old), decode(new)
            if not isinstance(old_body, dict) or not isinstance(new_body, dict):
                return 0
            old_body.pop("storage_uri", None)
            new_body.pop("storage_uri", None)
            return int(canonical(old_body) == canonical(new_body))
        except AssuranceError:
            return 0

    store.connection.create_function(
        "assurance_same_artifact_content",
        2,
        same_artifact_content,
        deterministic=True,
    )


@contextmanager
def relocating_restored_artifacts(store: Store, *, root_incarnation_id: str) -> Iterator[None]:
    """Managed offline restore only: relocate paths without rewriting formal history.

    The restored root is already quarantined before this path can be used. The
    SQL exception permits only storage_uri changes; it cannot suppress changes
    to content identity or verification. Old certificates remain tied to the old
    root, and the normal startup gate must refuse disclosure/dispatch.
    """
    import hashlib

    from .assurance_work import atomic

    root = store.path.parent
    marker_path = root / "restore-quarantine.json"
    manifest_path = root / "restore-manifest.json"
    if marker_path.is_symlink() or manifest_path.is_symlink():
        raise AssuranceError("RESTORE_QUARANTINE_MISMATCH")
    marker = decode(marker_path.read_bytes())
    if (
        marker.get("root_incarnation_id") != root_incarnation_id
        or marker.get("state") != "QUARANTINED"
        or marker.get("restore_manifest_hash")
        != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    ):
        raise AssuranceError("RESTORE_QUARANTINE_MISMATCH")
    if store._assurance_offline_relocation is not None:
        raise AssuranceError("RESTORE_RELOCATION_ALREADY_ACTIVE")
    with atomic(store):
        store._assurance_offline_relocation = (root_incarnation_id, store._current_task())
        try:
            yield
        finally:
            store._assurance_offline_relocation = None


@contextmanager
def source_change_receipt(store: Store, receipt_id: str) -> Iterator[None]:
    """Tie original source writes to this original business/import receipt.

    Supports a not-yet-inserted id because the FK is deferred. The calling
    Commit UoW must insert its real receipt before its own commit. A caught
    exception rolls source writes and epochs back together using a savepoint.
    """
    from .assurance_work import atomic

    text(receipt_id)
    if not store.connection.in_transaction:
        raise AssuranceError("SOURCE_CHANGE_UOW_REQUIRED")
    previous = store._assurance_change_context
    if previous is not None and previous != (receipt_id, store._current_task()):
        raise AssuranceError("SOURCE_CHANGE_RECEIPT_CONFLICT")
    with atomic(store):
        store._assurance_change_context = (receipt_id, store._current_task())
        try:
            yield
        finally:
            store._assurance_change_context = previous


@contextmanager
def original_source_mutation(store: Store, *, writer: str) -> Iterator[None]:
    """Receipt for an original typed writer that previously had no commit receipt.

    Called explicitly at policy/method command boundaries, not at every Store
    transaction or row trigger. It records only a real epoch transition and its
    wakeup ids. These are invalidation audit records, NOT source attestations.
    It carries no approval, truth, or permission verdict.
    When an enclosing original command already supplies a receipt, reuse it.
    """
    from .assurance_work import atomic

    text(writer)
    with atomic(store) as connection:
        enabled = connection.execute("SELECT 1 FROM assurance_mission_bindings LIMIT 1").fetchone()
        if enabled is None or store._assurance_change_context is not None:
            yield
            return
        state = connection.execute(
            "SELECT epoch FROM assurance_environment_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise AssuranceError("ASSURANCE_ENVIRONMENT_UNINITIALIZED")
        before = state[0]
        first_seq = connection.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
        receipt_id = "assurance-source-command:" + uuid4().hex
        with source_change_receipt(store, receipt_id):
            yield
            after = connection.execute(
                "SELECT epoch FROM assurance_environment_state WHERE singleton=1"
            ).fetchone()[0]
            if before == after:
                return  # exact replay/no relevant mutation creates no receipt
            # Link the actual persisted invalidation wakeups, without copying policy,
            # method content, ACL material, or arbitrary provider output.
            wake_event_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT event_id FROM events WHERE seq>? AND type='AssuranceEvidenceChanged' "
                    "AND json_extract(payload_json,'$.source_receipt_id')=? ORDER BY seq",
                    (first_seq, receipt_id),
                )
            ]
            if not wake_event_ids:
                raise AssuranceError("SOURCE_MUTATION_EVENT_MISSING")
            body = {
                "schema_version": 1,
                "receipt_role": "INVALIDATION_AUDIT_ONLY",
                "writer": writer,
                "environment_epoch_before": before,
                "environment_epoch_after": after,
                "wake_event_ids": wake_event_ids,
            }
            store.insert_receipt(
                commit_id=receipt_id,
                kind="AssuranceSourceMutation",
                subject_id=writer,
                base_version=before,
                proposal_hash=fingerprint(body),
                receipt=body,
            )
