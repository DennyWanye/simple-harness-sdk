# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Runtime-only connector profile authority for frozen operation payloads."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..contracts.operation_payloads import ConnectorOperationProfileV1, PayloadKind
from ..contracts.semantic_base import TypedRef, hash_hex, identifier


class OperationPayloadUnavailable(ContractError):
    """A payload/profile source was absent or did not prove its frozen identity."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ConnectorProfileRegistration:
    """Actual runtime adapter binding, never an API supplied version string.

    ``implementation_ref`` names the built adapter artifact that the registry loaded;
    its content hash must equal the profile's code digest.  ``adapter`` must produce
    the same typed profile at lookup time, proving this is not a stale caller DTO.
    """

    adapter: object
    implementation_ref: TypedRef
    profile: ConnectorOperationProfileV1
    outcome_adapter: object | None = None
    reconciliation_adapter: object | None = None


class ConnectorProfileRegistry(Protocol):
    def resolve_connector_operation(
        self, connector_id: str, operation_name: str
    ) -> ConnectorProfileRegistration | None: ...


def require_connector_profile(
    registry: ConnectorProfileRegistry,
    *,
    connector_id: str,
    operation_name: str,
    expected_hash: str | None = None,
) -> ConnectorOperationProfileV1:
    """Read a typed profile from the actual adapter registration or fail closed.

    There is intentionally no fallback to ``OperationSpec``, connector class names,
    package versions, TestConfig, or an untrusted caller-provided profile.
    """
    try:
        registration = registry.resolve_connector_operation(connector_id, operation_name)
    except Exception as error:  # registry availability is an authority boundary
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", type(error).__name__) from error
    if not isinstance(registration, ConnectorProfileRegistration):
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE")
    profile = registration.profile
    if not isinstance(profile, ConnectorOperationProfileV1):
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", "untyped_profile")
    if (
        profile.connector_id != identifier(connector_id, "connector_id")
        or profile.operation_name != identifier(operation_name, "operation_name")
        or registration.implementation_ref.content_hash != profile.adapter_code_digest
    ):
        raise OperationPayloadUnavailable(
            "OP_PROFILE_UNAVAILABLE", "registration_identity_mismatch"
        )
    operation_profile = getattr(registration.adapter, "operation_profile", None)
    if not callable(operation_profile):
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", "adapter_has_no_profile")
    try:
        observed = operation_profile()
    except Exception as error:
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", type(error).__name__) from error
    if (
        not isinstance(observed, ConnectorOperationProfileV1)
        or observed.content_hash() != profile.content_hash()
    ):
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", "adapter_profile_mismatch")
    if expected_hash is not None and profile.content_hash() != hash_hex(
        expected_hash, "expected_profile_hash"
    ):
        raise OperationPayloadUnavailable("OP_PROFILE_UNAVAILABLE", "frozen_profile_mismatch")
    return profile


def payload_request_hash(
    *,
    version: int,
    intent_id: str,
    connector_profile_hash: str,
    normalized_target_ref: str,
    expected_target_version: str | None,
    parameters_content_hash: str,
    params_hash: str,
    candidate_file_hash: str,
    effect_contract_hash: str,
    requirements_ref: TypedRef,
    producer_task_ref: TypedRef,
    producer_occurrence_id: str,
    completion_spec_hash: str | None = None,
    completion_scope_hash: str | None = None,
    effect_key: str | None = None,
    completion_owner_occurrence_id: str | None = None,
) -> str:
    """Exact D2 request-hash projection; it never creates an operation identity."""
    if type(version) is not int or version not in (1, 2):
        raise ContractError("operation request version must be 1 or 2")
    body: dict[str, Any] = {
        "kind": f"operation-request-v{version}",
        "intent_id": identifier(intent_id, "intent_id"),
        "connector_profile_hash": hash_hex(connector_profile_hash, "connector_profile_hash"),
        "normalized_target_ref": identifier(normalized_target_ref, "normalized_target_ref"),
        "expected_target_version": None
        if expected_target_version is None
        else identifier(expected_target_version, "expected_target_version"),
        "parameters_content_hash": hash_hex(parameters_content_hash, "parameters_content_hash"),
        "params_hash": hash_hex(params_hash, "params_hash"),
        "candidate_file_hash": hash_hex(candidate_file_hash, "candidate_file_hash"),
        "effect_contract_hash": hash_hex(effect_contract_hash, "effect_contract_hash"),
        "requirements_ref": requirements_ref.to_json(),
        "producer_task_ref": producer_task_ref.to_json(),
        "producer_occurrence_id": identifier(producer_occurrence_id, "producer_occurrence_id"),
    }
    v2 = (completion_spec_hash, completion_scope_hash, effect_key, completion_owner_occurrence_id)
    if version == 1 and any(item is not None for item in v2):
        raise ContractError("v1 request hash must not carry completion fields")
    if version == 2:
        if any(item is None for item in v2):
            raise ContractError("v2 request hash needs all completion fields")
        body.update(
            completion_spec_hash=hash_hex(completion_spec_hash, "completion_spec_hash"),
            completion_scope_hash=hash_hex(completion_scope_hash, "completion_scope_hash"),
            effect_key=identifier(effect_key, "effect_key"),
            completion_owner_occurrence_id=identifier(
                completion_owner_occurrence_id, "completion_owner_occurrence_id"
            ),
        )
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


__all__ = (
    "ConnectorProfileRegistration",
    "ConnectorProfileRegistry",
    "OperationPayloadUnavailable",
    "PayloadKind",
    "payload_request_hash",
    "require_connector_profile",
)
