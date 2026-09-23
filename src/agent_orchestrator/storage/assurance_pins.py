# SPDX-License-Identifier: Apache-2.0
"""Authenticate pin lifecycle receipts without mistaking GC liveness for access.

PREPARING deliberately precedes the ReviewPackage transaction. Its receipt binds
the allocated package identity; BOUND additionally checks the finished binding.
Only the original preparation writer may issue these receipts. They are not
grants and do not allow the reader to skip current source/root/ACL checks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from ..assurance.codec import AssuranceError, decode, fingerprint, integer, text
from ..assurance.refs import AssuranceRef


def require_pin_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_id: str,
    pin: Mapping[str, Any],
    state: str,
    expected_version: int,
    at_ms: int,
    receipt_ref: AssuranceRef | None = None,
) -> dict[str, Any]:
    if not connection.in_transaction:
        raise AssuranceError("READ_TRANSACTION_REQUIRED")
    original = connection.execute(
        "SELECT * FROM commit_receipts WHERE commit_id=?", (receipt_id,)
    ).fetchone()
    if original is None:
        raise AssuranceError("PIN_RECEIPT_MISSING", receipt_id)
    body = decode(original["receipt_json"])
    kind = {
        "PREPARING": "AssurancePinPrepared",
        "BOUND": "AssurancePinBound",
        "RELEASED": "AssurancePinReleased",
    }[state]
    ref = AssuranceRef.from_json(decode(pin["object_ref_json"]), kinds={"artifact", "source"})
    if ref.pin.content_hash != pin["blob_hash"]:
        raise AssuranceError("PIN_OBJECT_HASH_MISMATCH", pin["pin_id"])
    expected = {
        "schema_version": 1,
        "receipt_role": "BLOB_LIVENESS_ONLY",
        "mission_id": pin["mission_id"],
        "review_key": pin["review_key"],
        "pin_id": pin["pin_id"],
        "object_ref": ref.to_json(),
        "blob_hash": pin["blob_hash"],
        "state": state,
        "expected_version": expected_version,
        "at_ms": at_ms,
    }
    if (
        not isinstance(body, dict)
        or set(body) != set(expected) | {"package_id"}
        or any(body.get(key) != value for key, value in expected.items())
        or original["kind"] != kind
        or original["subject_id"] != pin["pin_id"]
        or original["base_version"] != expected_version
        or original["proposal_hash"] != fingerprint(body)
    ):
        raise AssuranceError("PIN_RECEIPT_MISMATCH", receipt_id)
    # Reject bools that compare equal to integers and other JSON type aliases.
    for key in ("schema_version", "expected_version", "at_ms"):
        integer(body[key])
    text(body["package_id"])
    if receipt_ref is not None and (
        receipt_ref.kind != "commit_receipt"
        or receipt_ref.pin.id != receipt_id
        or receipt_ref.pin.revision != 0
        or receipt_ref.pin.content_hash != fingerprint(body)
    ):
        raise AssuranceError("PIN_RECEIPT_MISMATCH", receipt_id)
    if state != "PREPARING":
        source = require_pin_receipt(
            connection,
            receipt_id=pin["source_receipt_id"],
            pin=pin,
            state="PREPARING",
            expected_version=0,
            at_ms=pin["created_at_ms"],
        )
        if source["package_id"] != body["package_id"]:
            raise AssuranceError("PIN_PACKAGE_MISMATCH", pin["pin_id"])
    if state == "BOUND":
        binding = connection.execute(
            "SELECT package_id FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
            (pin["mission_id"], pin["review_key"]),
        ).fetchone()
        if binding is None or binding["package_id"] != body["package_id"]:
            raise AssuranceError("PIN_PACKAGE_MISMATCH", pin["pin_id"])
    return body


def require_live_pin_locked(
    connection: sqlite3.Connection,
    *,
    mission_id: str,
    ref: AssuranceRef,
    pin_id: str,
    review_key: str,
) -> None:
    from ..assurance.codec import canonical

    if not connection.in_transaction:
        raise AssuranceError("READ_TRANSACTION_REQUIRED")
    row = connection.execute(
        "SELECT * FROM assurance_blob_pins WHERE pin_id=? AND mission_id=?", (pin_id, mission_id)
    ).fetchone()
    if (
        row is None
        or row["review_key"] != review_key
        or row["state"] not in {"PREPARING", "BOUND"}
        or row["blob_hash"] != ref.pin.content_hash
        or row["object_ref_json"] != canonical(ref.to_json())
    ):
        raise AssuranceError("LIVE_BLOB_PIN_REQUIRED", pin_id)
    require_pin_receipt(
        connection,
        receipt_id=row["source_receipt_id"],
        pin=row,
        state="PREPARING",
        expected_version=0,
        at_ms=row["created_at_ms"],
    )
    if row["state"] == "BOUND":
        receipt = connection.execute(
            "SELECT receipt_json FROM commit_receipts WHERE commit_id=?", (row["last_receipt_id"],)
        ).fetchone()
        if receipt is None:
            raise AssuranceError("PIN_RECEIPT_MISSING", row["last_receipt_id"])
        body = decode(receipt[0])
        if not isinstance(body, dict) or "at_ms" not in body:
            raise AssuranceError("PIN_RECEIPT_MISMATCH", row["last_receipt_id"])
        require_pin_receipt(
            connection,
            receipt_id=row["last_receipt_id"],
            pin=row,
            state="BOUND",
            expected_version=row["row_version"] - 1,
            at_ms=body["at_ms"],
        )
