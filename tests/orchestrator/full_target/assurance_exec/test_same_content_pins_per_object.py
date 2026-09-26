# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 desktop run: two leaves of one Mission produced byte-identical outputs
(the second leaf read the first one's NOTES.md), so one review had to pin two
different objects with the same content.  The live-pin guard keyed on the content
hash rejected the second object with IMMUTABLE_IDENTITY_CONFLICT on every recheck,
the import went to manual resolution and the Mission failed.

A pin keeps bytes alive for one review object; identity is the object, and several
live pins may share one blob.  One object still has at most one live pin.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.assurance.codec import AssuranceError, fingerprint
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.assurance_work import atomic
from agent_orchestrator.storage.store import Store

BLOB = "e" * 64
NOW = 1_700_000_000_000
MISSION = "mission-pins"
REVIEW = "assurance-content:pins"


def _acquire(store: Store, pin_id: str, ref: AssuranceRef) -> bool:
    receipt_id = "prepare:" + pin_id
    body = {
        "schema_version": 1,
        "receipt_role": "BLOB_LIVENESS_ONLY",
        "mission_id": MISSION,
        "review_key": REVIEW,
        "package_id": "package-1",
        "pin_id": pin_id,
        "object_ref": ref.to_json(),
        "blob_hash": ref.pin.content_hash,
        "state": "PREPARING",
        "expected_version": 0,
        "at_ms": NOW,
    }
    if store.get_receipt(receipt_id) is None:  # a replay keeps the original receipt
        with atomic(store):
            store.insert_receipt(commit_id=receipt_id, kind="AssurancePinPrepared", subject_id=pin_id,
                                 base_version=0, proposal_hash=fingerprint(body), receipt=body)
    return AssuranceStore(store).acquire_pin(
        pin_id, mission_id=MISSION, review_key=REVIEW, blob_hash=ref.pin.content_hash, object_ref=ref,
        receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body))), now_ms=NOW)


def _open(tmp_path) -> Store:
    store = Store.open(tmp_path / "orchestrator.db")
    # Storage-level guard only: the Mission/assurance binding rows are out of scope here.
    store.connection.execute("PRAGMA foreign_keys=OFF")
    return store


def _live(store: Store) -> int:
    return store.connection.execute(
        "SELECT COUNT(*) FROM assurance_blob_pins WHERE review_key=? AND state<>'RELEASED'", (REVIEW,)
    ).fetchone()[0]


def test_two_objects_with_identical_bytes_both_pin_in_one_review(tmp_path):
    store = _open(tmp_path)
    try:
        first = AssuranceRef("artifact", Pin("assurance-check-output:first-leaf", 1, BLOB))
        second = AssuranceRef("artifact", Pin("assurance-check-output:second-leaf", 1, BLOB))
        assert _acquire(store, "review-pin:first", first) is True
        assert _acquire(store, "review-pin:second", second) is True
        assert _live(store) == 2
        # a replay of an existing pin stays a read
        assert _acquire(store, "review-pin:first", first) is False
    finally:
        store.close()


def test_one_object_still_has_at_most_one_live_pin(tmp_path):
    store = _open(tmp_path)
    try:
        ref = AssuranceRef("artifact", Pin("assurance-check-output:only", 1, BLOB))
        assert _acquire(store, "review-pin:only", ref) is True
        with pytest.raises(AssuranceError) as raised:
            _acquire(store, "review-pin:only:2", ref)
        assert raised.value.code == "IMMUTABLE_IDENTITY_CONFLICT"
        assert _live(store) == 1
    finally:
        store.close()


def test_the_live_guard_is_keyed_on_the_object_in_the_schema(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    try:
        indexes = {row[0]: row[1] for row in store.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='assurance_blob_pins'")}
        assert "assurance_blob_pin_live_uq" not in indexes
        assert "object_ref_json" in indexes["assurance_blob_pin_object_live_uq"]
        assert schema.SCHEMA_VERSION >= 27  # 28 adds the commit_receipts kind index (2026-09-26)
    finally:
        store.close()
