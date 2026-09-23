"""D2 scoped reconciliation documents; structural validity alone proves no effect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ContractError
from .semantic_base import (
    TypedRef,
    TypedRefKind,
    content_hash_of,
    hash_hex,
    identifier,
    index,
    json_object,
)


def _exact(value: object, fields: str, name: str) -> dict[str, Any]:
    data = json_object(value, name)
    if set(data) != set(fields.split()):
        raise ContractError(f"{name}: unexpected or missing fields")
    return data


def _namespace(value: object) -> dict[str, Any]:
    data = _exact(value, "service_id account_scope environment", "namespace")
    for k, v in data.items():
        identifier(v, k)
    return data


def _receipt(value: object) -> TypedRef:
    ref = TypedRef.from_json(value)
    if ref.kind is not TypedRefKind.TOOL_RECEIPT:
        raise ContractError("reconciliation needs tool receipt references")
    return ref


@dataclass(frozen=True, slots=True)
class ScopedReconciliationObservationV1:
    """Canonical immutable bytes, including the discriminated proof union."""

    canonical: str

    @classmethod
    def from_json(cls, value: object) -> ScopedReconciliationObservationV1:
        from simple_harness.contracts import canonical_json

        data = _exact(
            value,
            """schema_version action_key action_version operation_id
            operation_occurrence_id request_hash params_hash idempotency_key
            connector_profile_hash namespace normalized_target_ref covered_handoff_ids
            queried_at_ms observation_receipt_ref observation_origin query_scope outcome proof""",
            "reconciliation",
        )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ContractError("unsupported reconciliation schema")
        for name in (
            "action_key",
            "operation_id",
            "operation_occurrence_id",
            "idempotency_key",
            "normalized_target_ref",
        ):
            identifier(data[name], name)
        for name in ("request_hash", "params_hash", "connector_profile_hash"):
            hash_hex(data[name], name)
        index(data["action_version"], "action_version", minimum=1)
        index(data["queried_at_ms"], "queried_at_ms")
        _namespace(data["namespace"])
        ids = data["covered_handoff_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 128:
            raise ContractError("missing/bounded handoff coverage")
        for item in ids:
            identifier(item, "handoff_id")
        if ids != sorted(set(ids)):
            raise ContractError("handoff ids must be unique and sorted")
        _receipt(data["observation_receipt_ref"])
        if data["observation_origin"] not in ("REMOTE_QUERY", "LOCAL_EXECUTOR"):
            raise ContractError("unknown observation origin")
        if data["outcome"] not in ("APPLIED", "NOT_APPLIED_FINAL", "PENDING", "PARTIAL", "UNKNOWN"):
            raise ContractError("unknown observation outcome")
        scope = _exact(
            data["query_scope"],
            "namespace request_identity consistency_kind high_watermark",
            "query_scope",
        )
        if _namespace(scope["namespace"]) != data["namespace"]:
            raise ContractError("query namespace mismatch")
        identifier(scope["request_identity"], "request_identity")
        if scope["consistency_kind"] not in ("AUTHORITATIVE", "BEST_EFFORT", "UNKNOWN"):
            raise ContractError("unknown query consistency")
        if scope["high_watermark"] is not None:
            identifier(scope["high_watermark"], "high_watermark")
        proof = json_object(data["proof"], "proof")
        kind = proof.get("kind")
        variants = {
            "SERVER_CANCELLED_BEFORE_APPLY": "cancellation_receipt_ref server_final_status",
            "SERVER_DEADLINE_FENCED": (
                "fence_protocol_version server_deadline_ms "
                "fence_receipt_ref nonapplication_query_ref"
            ),
            "EXECUTOR_NO_SEND_FINAL": (
                "execution_epoch send_boundary_receipt_ref dispatch_closed_receipt_ref"
            ),
        }
        if kind == "NONE":
            _exact(proof, "kind reason_code", "proof")
            identifier(proof["reason_code"], "reason_code")
            if data["outcome"] == "NOT_APPLIED_FINAL":
                raise ContractError("negative outcome requires a proof")
        elif kind in variants:
            _exact(proof, "kind proof_id basis_receipt_refs " + variants[kind], "proof")
            identifier(proof["proof_id"], "proof_id")
            refs = proof["basis_receipt_refs"]
            if not isinstance(refs, list) or not 1 <= len(refs) <= 128:
                raise ContractError("missing/bounded proof basis")
            for ref in refs:
                _receipt(ref)
            for name in variants[kind].split():
                if name.endswith("_ref"):
                    _receipt(proof[name])
                elif name in ("execution_epoch", "server_deadline_ms"):
                    index(proof[name], name)
                else:
                    identifier(proof[name], name)
            origin = "LOCAL_EXECUTOR" if kind == "EXECUTOR_NO_SEND_FINAL" else "REMOTE_QUERY"
            if data["observation_origin"] != origin:
                raise ContractError("proof origin mismatch")
            if (
                data["outcome"] != "NOT_APPLIED_FINAL"
                or scope["consistency_kind"] != "AUTHORITATIVE"
            ):
                raise ContractError("negative proof needs authoritative final observation")
        else:
            raise ContractError("unknown nonapplication proof")
        return cls(canonical_json(data))

    def to_json(self) -> dict[str, Any]:
        import json

        return json.loads(self.canonical)

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())

    def receipt_refs(self) -> tuple[TypedRef, ...]:
        data = self.to_json()
        proof = data["proof"]
        refs = [data["observation_receipt_ref"], *proof.get("basis_receipt_refs", [])]
        refs.extend(v for k, v in proof.items() if k.endswith("_ref"))
        return tuple(_receipt(ref) for ref in refs)
