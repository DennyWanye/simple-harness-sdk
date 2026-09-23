# SPDX-License-Identifier: Apache-2.0
"""Original Commit receipts for review-owned CAS liveness (never access grants)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..assurance.codec import AssuranceError, canonical, decode, fingerprint
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.reviews import AssuranceReviewBinding
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic


def ensure_review_blob_pins(
    commit: Any, *, tenant_id: str, binding: AssuranceReviewBinding, refs: Iterable[AssuranceRef]
) -> dict[AssuranceRef, str]:
    """Stable owner/ref identity; retries retain the first receipt and timestamp.

    Internal builder/collector only. Actual reads still require current caller
    authority and verify source lifecycle, exact bytes and final-use epochs.
    """
    body = binding.to_json()
    ordered = tuple(sorted(set(refs), key=lambda ref: ref.key))
    if len(ordered) > 1024 or any(ref.kind not in {"artifact", "source"} for ref in ordered):
        raise AssuranceError("REVIEW_PIN_SCOPE_INVALID")
    store = commit.store
    gate = commit._assurance_root_gate
    if gate is None:
        raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
    gate.require_execution()
    reader = AssuranceReader(store, tenant_id=tenant_id, mission_id=body["mission_id"])
    result = {}
    with atomic(store):
        if AssuranceStore(store).lane(body["mission_id"]) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        side = AssuranceStore(store)
        for ref in ordered:
            reader.read_exact_metadata(ref)
            base_id = "review-pin:" + fingerprint(
                {
                    "mission": body["mission_id"],
                    "review_key": body["review_key"],
                    "ref": ref.to_json(),
                }
            )
            history = store.connection.execute(
                "SELECT * FROM assurance_blob_pins WHERE mission_id=? AND review_key=? AND object_ref_json=? ORDER BY created_at_ms,pin_id LIMIT 34",
                (body["mission_id"], body["review_key"], canonical(ref.to_json())),
            ).fetchall()
            if len(history) > 32:
                raise AssuranceError("REVIEW_PIN_PREPARATION_EXHAUSTED")
            active = [row for row in history if row["state"] in {"PREPARING", "BOUND"}]
            if len(active) > 1:
                raise AssuranceError("REVIEW_PIN_AMBIGUOUS")
            if not active and len(history) >= 32:
                raise AssuranceError("REVIEW_PIN_PREPARATION_EXHAUSTED")
            pin_id = (
                active[0]["pin_id"]
                if active
                else base_id
                if not history
                else base_id + ":" + str(len(history) + 1)
            )
            receipt_id = "prepare:" + pin_id
            old = store.get_receipt(receipt_id)
            at_ms = int(store.now * 1000) if old is None else old.get("at_ms")
            receipt = {
                "schema_version": 1,
                "receipt_role": "BLOB_LIVENESS_ONLY",
                "mission_id": body["mission_id"],
                "review_key": body["review_key"],
                "package_id": body["package_ref"]["id"],
                "pin_id": pin_id,
                "object_ref": ref.to_json(),
                "blob_hash": ref.pin.content_hash,
                "state": "PREPARING",
                "expected_version": 0,
                "at_ms": at_ms,
            }
            if old is not None and dict(old) != receipt:
                raise AssuranceError("PIN_RECEIPT_MISMATCH")
            if old is None:
                store.insert_receipt(
                    commit_id=receipt_id,
                    kind="AssurancePinPrepared",
                    subject_id=pin_id,
                    base_version=0,
                    proposal_hash=fingerprint(receipt),
                    receipt=receipt,
                )
            side.acquire_pin(
                pin_id,
                mission_id=body["mission_id"],
                review_key=body["review_key"],
                blob_hash=ref.pin.content_hash,
                object_ref=ref,
                receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(receipt))),
                now_ms=at_ms,
            )
            row = store.connection.execute(
                "SELECT state FROM assurance_blob_pins WHERE pin_id=?", (pin_id,)
            ).fetchone()
            if row["state"] == "RELEASED":
                raise AssuranceError("REVIEW_PIN_ALREADY_RELEASED")
            result[ref] = pin_id
        if (
            store.connection.execute(
                "SELECT 1 FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
                (body["mission_id"], body["review_key"]),
            ).fetchone()
            is not None
        ):
            bind_review_blob_pins(commit, binding)
    return result


def _transition(commit: Any, row: Any, *, package_id: str, state: str) -> None:
    body = {
        "schema_version": 1,
        "receipt_role": "BLOB_LIVENESS_ONLY",
        "mission_id": row["mission_id"],
        "review_key": row["review_key"],
        "package_id": package_id,
        "pin_id": row["pin_id"],
        "object_ref": decode(row["object_ref_json"]),
        "blob_hash": row["blob_hash"],
        "state": state,
        "expected_version": row["row_version"],
        "at_ms": int(commit.store.now * 1000),
    }
    receipt_id = "review-pin-transition:" + fingerprint(body)
    kind = {"BOUND": "AssurancePinBound", "RELEASED": "AssurancePinReleased"}[state]
    commit.store.insert_receipt(
        commit_id=receipt_id,
        kind=kind,
        subject_id=row["pin_id"],
        base_version=row["row_version"],
        proposal_hash=fingerprint(body),
        receipt=body,
    )
    AssuranceStore(commit.store).transition_pin(
        row["pin_id"],
        mission_id=row["mission_id"],
        expected_version=row["row_version"],
        state=state,
        receipt=AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body))),
        now_ms=body["at_ms"],
    )


def bind_review_blob_pins(commit: Any, binding: AssuranceReviewBinding) -> None:
    """Bind after the original package/binding exist, in that same transaction."""
    body = binding.to_json()
    with atomic(commit.store):
        row = commit.store.connection.execute(
            "SELECT binding_hash FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
            (body["mission_id"], body["review_key"]),
        ).fetchone()
        if row is None or row[0] != binding.content_hash:
            raise AssuranceError("REVIEW_BINDING_SOURCE_MISMATCH")
        pins = commit.store.connection.execute(
            "SELECT * FROM assurance_blob_pins WHERE mission_id=? AND review_key=? AND state='PREPARING' LIMIT 1025",
            (body["mission_id"], body["review_key"]),
        ).fetchall()
        if len(pins) > 1024:
            raise AssuranceError("REVIEW_PIN_SCOPE_INVALID")
        for pin in pins:
            _transition(commit, pin, package_id=body["package_ref"]["id"], state="BOUND")


def release_failed_preparation(commit: Any, binding: AssuranceReviewBinding) -> None:
    """Release only an unbound, abandoned preparation; never delete CAS bytes.

    Released rows stay immutable history. A later attempt acquires a new pin
    identity; a stale preparation holding the released identity remains invalid.
    Bound review material is retained, including after business completion.
    """
    body = binding.to_json()
    with atomic(commit.store):
        if (
            commit.store.connection.execute(
                "SELECT 1 FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
                (body["mission_id"], body["review_key"]),
            ).fetchone()
            is not None
        ):
            return
        rows = commit.store.connection.execute(
            "SELECT * FROM assurance_blob_pins WHERE mission_id=? AND review_key=? AND state='PREPARING' LIMIT 1025",
            (body["mission_id"], body["review_key"]),
        ).fetchall()
        if len(rows) > 1024:
            raise AssuranceError("REVIEW_PIN_SCOPE_INVALID")
        for row in rows:
            _transition(commit, row, package_id=body["package_ref"]["id"], state="RELEASED")


def release_orphan_preparations(
    commit: Any, *, mission_id: str, now_ms: int
) -> dict[str, list[str]]:
    """Startup reconciliation (handoff item 8): release abandoned PREPARING pins.

    A pin is PREPARING between the original builder's acquisition and the
    package/binding transaction that binds it. At startup nothing is mid
    preparation, so a PREPARING pin whose ``review_key`` has no
    ``assurance_review_bindings`` row is a preparation the crashed process
    abandoned: it moves to RELEASED through the same receipted transition the
    in-process failure path uses. CAS bytes are never deleted (no GC exists),
    BOUND pins are retained, and a PREPARING pin that *does* have a binding is
    reported, not repaired — deciding it here would be a blind write.

    A rolled-back clock never backdates a release: a pin created after ``now_ms``
    is deferred (reported) until the wall clock is past its creation again. At
    most 1024 rows are handled per startup; the rest wait for the next one.
    """
    store = commit.store
    released: list[str] = []
    anomalies: list[str] = []
    deferred: list[str] = []
    with atomic(store):
        rows = store.connection.execute(
            "SELECT * FROM assurance_blob_pins WHERE mission_id=? AND state='PREPARING' "
            "ORDER BY created_at_ms,pin_id LIMIT 1024",
            (mission_id,),
        ).fetchall()
        for row in rows:
            if int(row["created_at_ms"]) > int(now_ms):
                deferred.append(row["pin_id"])
                continue
            bound = store.connection.execute(
                "SELECT 1 FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
                (mission_id, row["review_key"]),
            ).fetchone()
            if bound is not None:
                anomalies.append(row["pin_id"])
                continue
            prepared = store.get_receipt("prepare:" + row["pin_id"])
            if prepared is None or prepared.get("pin_id") != row["pin_id"]:
                raise AssuranceError("PIN_RECEIPT_MISMATCH", row["pin_id"])
            _transition(commit, row, package_id=prepared["package_id"], state="RELEASED")
            released.append(row["pin_id"])
    return {"released": released, "preparing_with_binding": anomalies, "deferred": deferred}
