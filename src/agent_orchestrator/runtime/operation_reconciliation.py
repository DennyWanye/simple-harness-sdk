"""Deployment-owned reconciliation adapters and durable proof verification.

Remote observation is outside the Commit transaction. A registered adapter verifies
its protocol's raw receipts; Commit rechecks the frozen identity and all handoffs.
No built-in best-effort lookup is promoted to a negative proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..contracts.models import ContractError
from ..contracts.operation_reconciliation import ScopedReconciliationObservationV1
from ..contracts.semantic_base import content_hash_of
from ..storage.planning_admission_store import PlanningAdmissionStore
from .operation_payloads import require_connector_profile
from .operation_ref_resolver import OperationReferenceResolver


class ReconciliationProofError(ContractError):
    code = "OP_NONAPPLICATION_PROOF_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    observation: ScopedReconciliationObservationV1
    # Original protocol documents, keyed by tool receipt reference id.
    receipts: Mapping[str, Mapping[str, Any]]


class ReconciliationAdapter(Protocol):
    connector: Any
    profile: Any

    def observe(
        self,
        *,
        action: Mapping[str, Any],
        resolved: Any,
        handoff_events: tuple[Any, ...],
        now_ms: int,
    ) -> ReconciliationEvidence: ...

    def verify(
        self,
        evidence: ReconciliationEvidence,
        *,
        action: Mapping[str, Any],
        resolved: Any,
        handoff_events: tuple[Any, ...],
    ) -> None:
        """Verify raw service protocol semantics; pure, no network or writes."""
        ...


def handoff_events(store: Any, action: Mapping[str, Any]) -> tuple[Any, ...]:
    events = tuple(
        e
        for e in store.iter_events(str(action["mission_id"]))
        if e.type == "ActionHandedOff" and e.payload.get("action_key") == action["action_key"]
    )
    count = action.get("handoffs")
    if type(count) is not int or count < 1 or len(events) != count:
        raise ReconciliationProofError("incomplete handoff ledger")
    if (
        sorted(e.payload.get("handoff", -1) for e in events) != list(range(1, count + 1))
        or len({e.id for e in events}) != count
        or any(e.payload.get("idempotency_key") != action["idempotency_key"] for e in events)
    ):
        raise ReconciliationProofError("handoff ledger identity differs")
    return events


def context(
    store: Any, runtime: Any, action: Mapping[str, Any]
) -> tuple[Any, Any, Any, tuple[Any, ...]]:
    link = PlanningAdmissionStore(store).get_operation_action_link_for_action(
        str(action["action_key"])
    )
    if link is None:
        raise ReconciliationProofError("operation link missing")
    resolved = OperationReferenceResolver(store, runtime.profiles).resolve_historical(
        dict(action), link
    )
    profile = require_connector_profile(
        runtime.profiles,
        connector_id=str(action["connector"]),
        operation_name=str(action["operation"]),
        expected_hash=resolved.parameters.connector_profile_hash,
    )
    registration = runtime.profiles.resolve_connector_operation(
        action["connector"], action["operation"]
    )
    adapter = registration.reconciliation_adapter
    if (
        adapter is None
        or getattr(adapter, "connector", None) is not runtime.connectors.get(action["connector"])
        or getattr(adapter, "profile", None) != profile
        or not callable(getattr(adapter, "observe", None))
        or not callable(getattr(adapter, "verify", None))
    ):
        raise ReconciliationProofError("no registered scoped reconciliation adapter")
    return resolved, profile, adapter, handoff_events(store, action)


def validate_evidence(
    evidence: ReconciliationEvidence,
    *,
    action: Mapping[str, Any],
    resolved: Any,
    profile: Any,
    events: tuple[Any, ...],
) -> dict[str, Any]:
    if not isinstance(evidence, ReconciliationEvidence):
        raise ReconciliationProofError("untyped reconciliation evidence")
    observation = ScopedReconciliationObservationV1.from_json(evidence.observation.to_json())
    data = observation.to_json()
    expected = {
        "action_key": action["action_key"],
        "action_version": action["version"],
        "operation_id": str(resolved.envelope.operation_id),
        "operation_occurrence_id": str(resolved.envelope.operation_occurrence_id),
        "request_hash": str(resolved.envelope.request_hash),
        "params_hash": action["params_hash"],
        "idempotency_key": action["idempotency_key"],
        "connector_profile_hash": profile.content_hash(),
        "namespace": dict(profile.namespace),
        "normalized_target_ref": action["target"],
        "covered_handoff_ids": sorted(e.id for e in events),
    }
    if any(data[k] != v for k, v in expected.items()):
        raise ReconciliationProofError("observation identity or coverage differs")
    if data["query_scope"]["request_identity"] != action["idempotency_key"]:
        raise ReconciliationProofError("query does not bind the idempotency key")
    if data["outcome"] == "NOT_APPLIED_FINAL":
        kind = data["proof"]["kind"]
        if (
            kind not in profile.nonapplication_proofs
            or kind not in resolved.effect_contract.accepted_nonapplication_proofs
        ):
            raise ReconciliationProofError("proof kind not frozen and registered")
    for ref in observation.receipt_refs():
        raw = evidence.receipts.get(ref.id)
        if raw is None or content_hash_of(raw) != ref.content_hash:
            raise ReconciliationProofError("raw protocol receipt missing or altered")
    return data


def stored_negative_proof(
    store: Any, action: Mapping[str, Any], link: Mapping[str, Any] | None = None
) -> bool:
    """Read only the original Commit receipt and history, never caller booleans."""
    try:
        ref = action.get("scoped_reconciliation_ref")
        if not isinstance(ref, Mapping):
            return False
        receipt = store.get_receipt(str(ref["id"]))
        if (
            receipt is None
            or receipt.get("kind") != "scoped_operation_reconciliation"
            or content_hash_of(receipt) != ref["content_hash"]
        ):
            return False
        data = ScopedReconciliationObservationV1.from_json(receipt["observation"]).to_json()
        if data["outcome"] != "NOT_APPLIED_FINAL":
            return False
        bridge = link or PlanningAdmissionStore(store).get_operation_action_link_for_action(
            action["action_key"]
        )
        if bridge is None:
            return False
        if any(
            data[k] != action[v]
            for k, v in (
                ("action_key", "action_key"),
                ("action_version", "version"),
                ("params_hash", "params_hash"),
                ("idempotency_key", "idempotency_key"),
                ("normalized_target_ref", "target"),
            )
        ):
            return False
        if any(
            data[k] != bridge[k]
            for k in ("operation_id", "operation_occurrence_id", "request_hash")
        ):
            return False
        if data["covered_handoff_ids"] != sorted(e.id for e in handoff_events(store, action)):
            return False
        if not any(h.get("scoped_reconciliation_ref") == ref for h in action.get("history", ())):
            return False
        if not any(
            e.type == "ActionScopedReconciled"
            and e.payload.get("action_key") == action["action_key"]
            and e.payload.get("receipt_ref") == ref
            and e.payload.get("observation_hash") == content_hash_of(data)
            for e in store.iter_events(action["mission_id"])
        ):
            return False
        parsed = ScopedReconciliationObservationV1.from_json(data)
        for raw_ref in parsed.receipt_refs():
            if content_hash_of(receipt["raw_receipts"][raw_ref.id]) != raw_ref.content_hash:
                return False
        return True
    except (ContractError, KeyError, TypeError, ValueError):
        return False
