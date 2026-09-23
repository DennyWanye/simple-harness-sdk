"""Scoped reconciliation writes on the original action/Commit receipt ledger."""

from __future__ import annotations

from typing import Any

from ..contracts.semantic_base import content_hash_of
from ..runtime.operation_reconciliation import (
    ReconciliationEvidence,
    ReconciliationProofError,
    context,
    validate_evidence,
)


class OperationReconciliationCommitsMixin:
    def record_scoped_reconciliation(
        self,
        action_key: str,
        *,
        evidence: ReconciliationEvidence,
        adapter: object,
        service_authority: object,
    ) -> dict[str, Any]:
        runtime = self._operation_runtime()
        if service_authority is not runtime.service_authority:
            raise ReconciliationProofError("unbound reconciliation service")
        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None:
                raise ReconciliationProofError("action missing")
            resolved, profile, registered, events = context(self._store, runtime, action)
            if adapter is not registered:
                raise ReconciliationProofError("unregistered receipt verifier")
            data = validate_evidence(
                evidence, action=action, resolved=resolved, profile=profile, events=events
            )
            if data["queried_at_ms"] > int(self._store.now * 1000):
                raise ReconciliationProofError("observation is from the future")
            # Protocol interpretation is trusted adapter code, never caller proof fields.
            registered.verify(evidence, action=action, resolved=resolved, handoff_events=events)
            receipt = {
                "kind": "scoped_operation_reconciliation",
                "mission_id": action["mission_id"],
                "observation": data,
                "raw_receipts": dict(evidence.receipts),
                "adapter_implementation_ref": runtime.profiles.resolve_connector_operation(
                    action["connector"], action["operation"]
                ).implementation_ref.to_json(),
            }
            digest = content_hash_of(receipt)
            receipt_id = "scoped-reconciliation:" + digest
            previous = self._store.get_receipt(receipt_id)
            if previous is not None:
                if previous != receipt:
                    raise ReconciliationProofError("receipt replay conflict")
                return action
            lapsed = (
                action["state"] == "HANDED_OFF"
                and float(action.get("lease_expires_at") or 0) <= self._store.now
            )
            if action["state"] not in ("UNKNOWN", "FAILED") and not lapsed:
                raise ReconciliationProofError("action is not available for reconciliation")
            if lapsed:
                action = self._resolve_action(
                    action, "UNKNOWN", error="lease_lapsed_without_outcome"
                )
            self._store.insert_receipt(
                commit_id=receipt_id,
                kind=receipt["kind"],
                subject_id=action_key,
                base_version=action["version"],
                proposal_hash=digest,
                receipt=receipt,
            )
            ref = {"id": receipt_id, "content_hash": digest}
            negative = data["outcome"] == "NOT_APPLIED_FINAL"
            action = self._update_action(
                action_key,
                scoped_reconciliation_ref=ref,
                history=[
                    *action.get("history", []),
                    {
                        "at": self._store.now,
                        "state": action["state"],
                        "scoped_reconciliation_ref": ref,
                    },
                ],
                reconcile="CONFIRMED_NOT_STARTED" if negative else "STILL_UNKNOWN",
                needs_human=not negative,
                reconcile_note=None if negative else ReconciliationProofError.code,
            )
            self._emit(
                "ActionScopedReconciled",
                str(action["mission_id"]),
                key=receipt_id,
                task_id=action.get("task_id"),
                payload={
                    "action_key": action_key,
                    "observation_hash": content_hash_of(data),
                    "receipt_ref": ref,
                    "outcome": data["outcome"],
                },
            )
            return action

    def finish_proven_nonapplication(
        self, action_key: str, *, reason: str, service_authority: object
    ) -> dict[str, Any]:
        """Close a proven old handoff when a fresh send failed the current gates.

        Recheck under the write lock: a concurrently admitted new handoff invalidates
        the old proof and must retain its own reservation and effect uncertainty.
        """
        from ..runtime.operation_reconciliation import stored_negative_proof

        runtime = self._operation_runtime()
        if service_authority is not runtime.service_authority:
            raise ReconciliationProofError("unbound reconciliation service")
        with self._store.transaction():
            action = self._store.get_action(action_key)
            if action is None:
                raise ReconciliationProofError("action missing")
            if action["state"] != "UNKNOWN" or not stored_negative_proof(self._store, action):
                return action
            return self._resolve_action(
                action,
                "FAILED",
                error="proven_not_applied:rehandoff_refused:" + reason,
                event="ActionNonapplicationFinalized",
                extra={"scoped_reconciliation_ref": action["scoped_reconciliation_ref"]},
            )
