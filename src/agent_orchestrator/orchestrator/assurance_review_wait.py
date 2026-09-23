# SPDX-License-Identifier: Apache-2.0
"""Record an unanswered original handoff without reissuing or releasing it."""

from __future__ import annotations

from typing import Any

from ..assurance.codec import AssuranceError, fingerprint
from ..assurance.refs import AssuranceRef, Pin
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic


def record_provider_wait(commit: Any, intent: Any, liveness: Any) -> AssuranceRef:
    """Observation only. Original accounting recovery remains the sole resolver.

    The receipt deliberately omits arbitrary provider error text: it may contain
    authentication material and cannot establish a known zero charge. Cold
    collection of this same executor yields the same immutable observation.
    """
    with atomic(commit.store):
        current = commit.store.get_intent(intent.intent_id)
        if (
            current is None
            or current != intent
            or current.state != "SUBMITTED"
            or not current.agent_id
            or not current.expected_turn_id
            or AssuranceStore(commit.store).lane(current.mission_id) != "ASSURANCE_1_1"
            or not liveness.exists
            or liveness.settled
        ):
            raise AssuranceError("REVIEW_WAIT_SOURCE_MISMATCH")
        body = {
            "mission_id": current.mission_id,
            "intent_id": current.intent_id,
            "subject_id": current.subject_id,
            "agent_id": current.agent_id,
            "turn_id": current.expected_turn_id,
            "input_id": current.input_id,
            "input_hash": current.input_hash,
            "reason": "ORIGINAL_PROVIDER_RECONCILIATION_REQUIRED",
        }
        digest = fingerprint(body)
        receipt_id = "assurance-provider-wait:" + digest
        old = commit.store.get_receipt(receipt_id)
        if old is not None and dict(old) != body:
            raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
        if old is None:
            commit.store.insert_receipt(
                commit_id=receipt_id,
                kind="AssuranceProviderReconciliationRequired",
                subject_id=current.intent_id,
                base_version=0,
                proposal_hash=digest,
                receipt=body,
            )
            commit._emit(
                "AssuranceProviderReconciliationRequired",
                current.mission_id,
                key=receipt_id,
                payload=body,
            )
        return AssuranceRef("commit_receipt", Pin(receipt_id, 0, digest))
