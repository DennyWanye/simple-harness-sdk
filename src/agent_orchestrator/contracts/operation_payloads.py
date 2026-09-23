# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Frozen D2 operation payload contracts.

These documents carry immutable, system-bound materialization inputs.  They are
not worker artifacts and their codecs deliberately have no model ingress.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from simple_harness.contracts import canonical_json

from .models import ContractError
from .operation_completion import CompletionPinV1
from .semantic_base import (
    TypedRef,
    TypedRefKind,
    VersionedRef,
    enum_of,
    fields_of,
    hash_hex,
    identifier,
    index,
    json_object,
    schema_version,
    sequence_of,
)

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PAYLOAD_DEPTH = 16


class PayloadKind(StrEnum):
    PARAMETERS = "PARAMETERS"
    EFFECT_CONTRACT = "EFFECT_CONTRACT"
    ACTION_PROPOSAL = "ACTION_PROPOSAL"


class ConditionalWriteKind(StrEnum):
    NONE = "NONE"
    ETAG = "ETAG"
    TRANSACTION = "TRANSACTION"
    FENCE = "FENCE"


class ReconciliationPolicy(StrEnum):
    RECONCILE_ONLY = "RECONCILE_ONLY"
    EXISTING_APPROVED_POLICY = "EXISTING_APPROVED_POLICY"


class NonapplicationProofKind(StrEnum):
    SERVER_CANCELLED_BEFORE_APPLY = "SERVER_CANCELLED_BEFORE_APPLY"
    SERVER_DEADLINE_FENCED = "SERVER_DEADLINE_FENCED"
    EXECUTOR_NO_SEND_FINAL = "EXECUTOR_NO_SEND_FINAL"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"operation payload has duplicate JSON key {key!r}")
        result[key] = value
    return result


def _bad_constant(value: str) -> Any:
    raise ContractError(f"operation payload rejects non-finite JSON constant {value}")


def _depth(value: object, *, level: int = 0) -> None:
    if level > MAX_PAYLOAD_DEPTH:
        raise ContractError(f"operation payload nests deeper than {MAX_PAYLOAD_DEPTH} levels")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("operation payload keys must be strings")
            _depth(item, level=level + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _depth(item, level=level + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ContractError("operation payload contains non-finite number")


def decode_payload_json(value: object, name: str = "operation_payload") -> dict[str, Any]:
    """Decode only bounded JSON object bytes, rejecting duplicate/non-finite forms."""
    if isinstance(value, bytes):
        if len(value) > MAX_PAYLOAD_BYTES:
            raise ContractError(f"{name} exceeds {MAX_PAYLOAD_BYTES} bytes")
        try:
            decoded = json.loads(
                value.decode("utf-8"),
                object_pairs_hook=_no_duplicates,
                parse_constant=_bad_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ContractError(f"{name} is not strict JSON") from error
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ContractError(f"{name} exceeds {MAX_PAYLOAD_BYTES} bytes")
        try:
            decoded = json.loads(
                value, object_pairs_hook=_no_duplicates, parse_constant=_bad_constant
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise ContractError(f"{name} is not strict JSON") from error
    elif isinstance(value, Mapping):
        decoded = dict(value)
    else:
        raise ContractError(f"{name} must be JSON bytes or an object")
    _depth(decoded)
    if not isinstance(decoded, dict):
        raise ContractError(f"{name} must be an object")
    try:
        encoded = canonical_json(decoded).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ContractError(f"{name} is not canonical JSON") from error
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ContractError(f"{name} exceeds {MAX_PAYLOAD_BYTES} bytes")
    return decoded


def _ref(value: object, kind: TypedRefKind, name: str) -> TypedRef:
    return TypedRef.of_kind(kind, value, name)


def _refs(
    value: object, kind: TypedRefKind, name: str, *, minimum: int = 0
) -> tuple[TypedRef, ...]:
    refs = sequence_of(value, name, lambda item, where: _ref(item, kind, where), minimum=minimum)
    ids = tuple((ref.id, ref.revision, ref.content_hash) for ref in refs)
    if len(set(ids)) != len(ids):
        raise ContractError(f"{name} must not contain duplicate references")
    return refs


def _strings(value: object, name: str, *, minimum: int = 0) -> tuple[str, ...]:
    items = sequence_of(value, name, lambda item, where: identifier(item, where), minimum=minimum)
    if len(set(items)) != len(items):
        raise ContractError(f"{name} must not contain duplicates")
    return items


def _versioned(value: object, name: str) -> VersionedRef:
    return VersionedRef.from_json(value, name)


def _profile_condition(value: object, name: str) -> ConditionalWriteKind | dict[str, str]:
    if value == ConditionalWriteKind.NONE:
        return ConditionalWriteKind.NONE
    data = fields_of(value, name, required=("kind", "protocol_id"))
    kind = enum_of(ConditionalWriteKind, data["kind"], f"{name}.kind")
    if kind is ConditionalWriteKind.NONE:
        raise ContractError(f"{name}.kind NONE must be represented by the string NONE")
    return {
        "kind": str(kind),
        "protocol_id": identifier(data["protocol_id"], f"{name}.protocol_id"),
    }


def _target_condition(value: object, name: str) -> ConditionalWriteKind | dict[str, str]:
    """Actual deployment precondition, distinct from an adapter capability protocol."""
    if value == ConditionalWriteKind.NONE:
        return ConditionalWriteKind.NONE
    data = fields_of(value, name, required=("kind", "value"))
    kind = enum_of(ConditionalWriteKind, data["kind"], f"{name}.kind")
    if kind is ConditionalWriteKind.NONE:
        raise ContractError(f"{name}.kind NONE must be represented by the string NONE")
    return {
        "kind": str(kind),
        "value": identifier(data["value"], f"{name}.value"),
    }


def _namespace(value: object, name: str) -> dict[str, str]:
    data = fields_of(value, name, required=("service_id", "account_scope", "environment"))
    return {key: identifier(data[key], f"{name}.{key}") for key in data}


def _receipt_adapter(value: object, name: str) -> dict[str, str]:
    data = fields_of(value, name, required=("id", "version"))
    return {key: identifier(data[key], f"{name}.{key}") for key in data}


@dataclass(frozen=True, slots=True)
class _Payload:
    schema_version: int
    KIND: ClassVar[PayloadKind]

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        raw = canonical_json(self.to_json()).encode("utf-8")
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ContractError(f"operation payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return raw

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationParametersV1(_Payload):
    intent_id: str
    connector_id: str
    connector_version: str
    operation_name: str
    connector_profile_hash: str
    normalized_target_ref: str
    expected_target_version: str | None
    parameter_schema_ref: VersionedRef
    effective_params: Mapping[str, Any]
    params_hash: str
    candidate_artifact_ref: TypedRef
    accepted_input_refs: tuple[TypedRef, ...]
    KIND: ClassVar[PayloadKind] = PayloadKind.PARAMETERS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(self.schema_version, "parameters.schema_version", expected=1),
        )
        for field in (
            "intent_id",
            "connector_id",
            "connector_version",
            "operation_name",
            "normalized_target_ref",
        ):
            object.__setattr__(self, field, identifier(getattr(self, field), f"parameters.{field}"))
        object.__setattr__(
            self,
            "connector_profile_hash",
            hash_hex(self.connector_profile_hash, "parameters.connector_profile_hash"),
        )
        if self.expected_target_version is not None:
            object.__setattr__(
                self,
                "expected_target_version",
                identifier(self.expected_target_version, "parameters.expected_target_version"),
            )
        if not isinstance(self.parameter_schema_ref, VersionedRef):
            raise ContractError("parameters.parameter_schema_ref must be a VersionedRef")
        values = json_object(self.effective_params, "parameters.effective_params")
        _depth(values)
        object.__setattr__(self, "effective_params", values)
        object.__setattr__(
            self, "params_hash", hash_hex(self.params_hash, "parameters.params_hash")
        )
        if (
            not isinstance(self.candidate_artifact_ref, TypedRef)
            or self.candidate_artifact_ref.kind is not TypedRefKind.ARTIFACT
        ):
            raise ContractError("parameters.candidate_artifact_ref must be an artifact TypedRef")
        if (
            not self.accepted_input_refs
            or any(ref.kind is not TypedRefKind.ACCEPTANCE for ref in self.accepted_input_refs)
            or len({(ref.id, ref.revision, ref.content_hash) for ref in self.accepted_input_refs})
            != len(self.accepted_input_refs)
        ):
            raise ContractError(
                "parameters.accepted_input_refs must be nonempty acceptance TypedRefs"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "intent_id": self.intent_id,
            "connector_id": self.connector_id,
            "connector_version": self.connector_version,
            "operation_name": self.operation_name,
            "connector_profile_hash": self.connector_profile_hash,
            "normalized_target_ref": self.normalized_target_ref,
            "expected_target_version": self.expected_target_version,
            "parameter_schema_ref": self.parameter_schema_ref.to_json(),
            "effective_params": dict(self.effective_params),
            "params_hash": self.params_hash,
            "candidate_artifact_ref": self.candidate_artifact_ref.to_json(),
            "accepted_input_refs": [ref.to_json() for ref in self.accepted_input_refs],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "parameters") -> "OperationParametersV1":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "intent_id",
                "connector_id",
                "connector_version",
                "operation_name",
                "connector_profile_hash",
                "normalized_target_ref",
                "expected_target_version",
                "parameter_schema_ref",
                "effective_params",
                "params_hash",
                "candidate_artifact_ref",
                "accepted_input_refs",
            ),
        )
        return OperationParametersV1(
            data["schema_version"],
            data["intent_id"],
            data["connector_id"],
            data["connector_version"],
            data["operation_name"],
            data["connector_profile_hash"],
            data["normalized_target_ref"],
            data["expected_target_version"],
            _versioned(data["parameter_schema_ref"], f"{name}.parameter_schema_ref"),
            json_object(data["effective_params"], f"{name}.effective_params"),
            data["params_hash"],
            _ref(
                data["candidate_artifact_ref"],
                TypedRefKind.ARTIFACT,
                f"{name}.candidate_artifact_ref",
            ),
            _refs(
                data["accepted_input_refs"],
                TypedRefKind.ACCEPTANCE,
                f"{name}.accepted_input_refs",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class OperationEffectContractV1(_Payload):
    connector_profile_hash: str
    namespace: Mapping[str, str]
    operation_name: str
    required_milestone: str
    milestone_criterion_ids: tuple[str, ...]
    required_target_condition: object
    receipt_adapter: Mapping[str, str]
    retry_policy_ref: VersionedRef
    reconciliation_policy: ReconciliationPolicy
    idempotency_contract: object
    accepted_nonapplication_proofs: tuple[NonapplicationProofKind, ...]
    KIND: ClassVar[PayloadKind] = PayloadKind.EFFECT_CONTRACT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "effect.schema_version",
                expected=2 if type(self) is OperationEffectContractV2 else 1,
            ),
        )
        object.__setattr__(
            self,
            "connector_profile_hash",
            hash_hex(self.connector_profile_hash, "effect.connector_profile_hash"),
        )
        object.__setattr__(self, "namespace", _namespace(self.namespace, "effect.namespace"))
        for field in ("operation_name", "required_milestone"):
            object.__setattr__(self, field, identifier(getattr(self, field), f"effect.{field}"))
        object.__setattr__(
            self,
            "milestone_criterion_ids",
            _strings(self.milestone_criterion_ids, "effect.milestone_criterion_ids", minimum=1),
        )
        object.__setattr__(
            self,
            "required_target_condition",
            _target_condition(self.required_target_condition, "effect.required_target_condition"),
        )
        object.__setattr__(
            self,
            "receipt_adapter",
            _receipt_adapter(self.receipt_adapter, "effect.receipt_adapter"),
        )
        if not isinstance(self.retry_policy_ref, VersionedRef):
            raise ContractError("effect.retry_policy_ref must be a VersionedRef")
        object.__setattr__(
            self,
            "reconciliation_policy",
            enum_of(
                ReconciliationPolicy, self.reconciliation_policy, "effect.reconciliation_policy"
            ),
        )
        object.__setattr__(
            self,
            "idempotency_contract",
            _idempotency(self.idempotency_contract, "effect.idempotency_contract"),
        )
        proofs = tuple(
            enum_of(NonapplicationProofKind, item, "effect.accepted_nonapplication_proofs[]")
            for item in self.accepted_nonapplication_proofs
        )
        if len(set(proofs)) != len(proofs):
            raise ContractError("effect.accepted_nonapplication_proofs must not contain duplicates")
        object.__setattr__(self, "accepted_nonapplication_proofs", proofs)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "connector_profile_hash": self.connector_profile_hash,
            "namespace": dict(self.namespace),
            "operation_name": self.operation_name,
            "required_milestone": self.required_milestone,
            "milestone_criterion_ids": list(self.milestone_criterion_ids),
            "required_target_condition": self.required_target_condition,
            "receipt_adapter": dict(self.receipt_adapter),
            "retry_policy_ref": self.retry_policy_ref.to_json(),
            "reconciliation_policy": str(self.reconciliation_policy),
            "idempotency_contract": self.idempotency_contract,
            "accepted_nonapplication_proofs": [
                str(item) for item in self.accepted_nonapplication_proofs
            ],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "effect_contract") -> "OperationEffectContractV1":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "connector_profile_hash",
                "namespace",
                "operation_name",
                "required_milestone",
                "milestone_criterion_ids",
                "required_target_condition",
                "receipt_adapter",
                "retry_policy_ref",
                "reconciliation_policy",
                "idempotency_contract",
                "accepted_nonapplication_proofs",
            ),
        )
        return OperationEffectContractV1(
            data["schema_version"],
            data["connector_profile_hash"],
            _namespace(data["namespace"], f"{name}.namespace"),
            data["operation_name"],
            data["required_milestone"],
            _strings(data["milestone_criterion_ids"], f"{name}.milestone_criterion_ids", minimum=1),
            _target_condition(
                data["required_target_condition"], f"{name}.required_target_condition"
            ),
            _receipt_adapter(data["receipt_adapter"], f"{name}.receipt_adapter"),
            _versioned(data["retry_policy_ref"], f"{name}.retry_policy_ref"),
            enum_of(
                ReconciliationPolicy, data["reconciliation_policy"], f"{name}.reconciliation_policy"
            ),
            _idempotency(data["idempotency_contract"], f"{name}.idempotency_contract"),
            tuple(
                enum_of(NonapplicationProofKind, item, f"{name}.accepted_nonapplication_proofs[]")
                for item in sequence_of(
                    data["accepted_nonapplication_proofs"],
                    f"{name}.accepted_nonapplication_proofs",
                    lambda item, _: item,
                )
            ),
        )  # type: ignore[call-arg]


@dataclass(frozen=True, slots=True)
class OperationEffectContractV2(OperationEffectContractV1):
    completion_spec_hash: str
    effect_key: str
    milestone_policy_ref: CompletionPinV1

    def __post_init__(self) -> None:
        OperationEffectContractV1.__post_init__(self)
        object.__setattr__(
            self,
            "completion_spec_hash",
            hash_hex(self.completion_spec_hash, "effect.completion_spec_hash"),
        )
        object.__setattr__(self, "effect_key", identifier(self.effect_key, "effect.effect_key"))
        if not isinstance(self.milestone_policy_ref, CompletionPinV1):
            raise ContractError("effect.milestone_policy_ref must be a CompletionPinV1")

    def to_json(self) -> dict[str, Any]:
        return {
            **OperationEffectContractV1.to_json(self),
            "schema_version": 2,
            "completion_spec_hash": self.completion_spec_hash,
            "effect_key": self.effect_key,
            "milestone_policy_ref": self.milestone_policy_ref.to_json(),
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "effect_contract_v2"
    ) -> "OperationEffectContractV2":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "connector_profile_hash",
                "namespace",
                "operation_name",
                "required_milestone",
                "milestone_criterion_ids",
                "required_target_condition",
                "receipt_adapter",
                "retry_policy_ref",
                "reconciliation_policy",
                "idempotency_contract",
                "accepted_nonapplication_proofs",
                "completion_spec_hash",
                "effect_key",
                "milestone_policy_ref",
            ),
        )
        if schema_version(data["schema_version"], f"{name}.schema_version", expected=2) != 2:
            raise ContractError("unreachable")
        return cls(
            2,
            data["connector_profile_hash"],
            _namespace(data["namespace"], f"{name}.namespace"),
            data["operation_name"],
            data["required_milestone"],
            _strings(data["milestone_criterion_ids"], f"{name}.milestone_criterion_ids", minimum=1),
            _target_condition(
                data["required_target_condition"], f"{name}.required_target_condition"
            ),
            _receipt_adapter(data["receipt_adapter"], f"{name}.receipt_adapter"),
            _versioned(data["retry_policy_ref"], f"{name}.retry_policy_ref"),
            enum_of(
                ReconciliationPolicy, data["reconciliation_policy"], f"{name}.reconciliation_policy"
            ),
            _idempotency(data["idempotency_contract"], f"{name}.idempotency_contract"),
            tuple(
                enum_of(NonapplicationProofKind, item, f"{name}.accepted_nonapplication_proofs[]")
                for item in sequence_of(
                    data["accepted_nonapplication_proofs"],
                    f"{name}.accepted_nonapplication_proofs",
                    lambda item, _: item,
                )
            ),
            data["completion_spec_hash"],
            data["effect_key"],
            CompletionPinV1.from_json(data["milestone_policy_ref"], f"{name}.milestone_policy_ref"),
        )


def _idempotency(value: object, name: str) -> object:
    if value == "UNKNOWN":
        return "UNKNOWN"
    data = fields_of(
        value,
        name,
        required=("namespace", "retention_basis_ref", "minimum_retention_ms"),
        optional=("kind", "conflict_protocol_id"),
    )
    if data.get("kind", "ATOMIC_KEY") != "ATOMIC_KEY":
        raise ContractError(f"{name}.kind must be ATOMIC_KEY")
    result: dict[str, Any] = {
        "namespace": identifier(data["namespace"], f"{name}.namespace"),
        "retention_basis_ref": _ref(
            data["retention_basis_ref"], TypedRefKind.SOURCE, f"{name}.retention_basis_ref"
        ).to_json(),
        "minimum_retention_ms": index(
            data["minimum_retention_ms"], f"{name}.minimum_retention_ms", minimum=0
        ),
    }
    if "kind" in data:
        result["kind"] = "ATOMIC_KEY"
    if "conflict_protocol_id" in data:
        result["conflict_protocol_id"] = identifier(
            data["conflict_protocol_id"], f"{name}.conflict_protocol_id"
        )
    return result


@dataclass(frozen=True, slots=True)
class FrozenActionProposalV1(_Payload):
    intent_id: str
    candidate_artifact_ref: TypedRef
    parameters_ref: TypedRef
    effect_contract_ref: TypedRef
    prepared_acceptance_refs: tuple[TypedRef, ...]
    producer_task_ref: TypedRef
    producer_occurrence_id: str
    obligation_id: str
    requirements_ref: TypedRef
    plan_revision: int
    plan_snapshot_hash: str
    request_hash: str
    KIND: ClassVar[PayloadKind] = PayloadKind.ACTION_PROPOSAL

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "proposal.schema_version",
                expected=2 if type(self) is FrozenActionProposalV2 else 1,
            ),
        )
        for field in ("intent_id", "producer_occurrence_id", "obligation_id"):
            object.__setattr__(self, field, identifier(getattr(self, field), f"proposal.{field}"))
        for field in ("candidate_artifact_ref", "parameters_ref", "effect_contract_ref"):
            ref = getattr(self, field)
            if not isinstance(ref, TypedRef) or ref.kind is not TypedRefKind.ARTIFACT:
                raise ContractError(f"proposal.{field} must be an artifact TypedRef")
        if (
            not self.prepared_acceptance_refs
            or any(ref.kind is not TypedRefKind.ACCEPTANCE for ref in self.prepared_acceptance_refs)
            or len(
                {(ref.id, ref.revision, ref.content_hash) for ref in self.prepared_acceptance_refs}
            )
            != len(self.prepared_acceptance_refs)
        ):
            raise ContractError(
                "proposal.prepared_acceptance_refs must be nonempty acceptance TypedRefs"
            )
        if (
            not isinstance(self.producer_task_ref, TypedRef)
            or self.producer_task_ref.kind is not TypedRefKind.TASK
        ):
            raise ContractError("proposal.producer_task_ref must be a task TypedRef")
        if (
            not isinstance(self.requirements_ref, TypedRef)
            or self.requirements_ref.kind is not TypedRefKind.REQUIREMENTS
        ):
            raise ContractError("proposal.requirements_ref must be a requirements TypedRef")
        object.__setattr__(
            self,
            "plan_revision",
            index(self.plan_revision, "proposal.plan_ref.revision", minimum=1),
        )
        object.__setattr__(
            self,
            "plan_snapshot_hash",
            hash_hex(self.plan_snapshot_hash, "proposal.plan_ref.snapshot_hash"),
        )
        object.__setattr__(
            self, "request_hash", hash_hex(self.request_hash, "proposal.request_hash")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "intent_id": self.intent_id,
            "candidate_artifact_ref": self.candidate_artifact_ref.to_json(),
            "parameters_ref": self.parameters_ref.to_json(),
            "effect_contract_ref": self.effect_contract_ref.to_json(),
            "prepared_acceptance_refs": [ref.to_json() for ref in self.prepared_acceptance_refs],
            "producer_task_ref": self.producer_task_ref.to_json(),
            "producer_occurrence_id": self.producer_occurrence_id,
            "obligation_id": self.obligation_id,
            "requirements_ref": self.requirements_ref.to_json(),
            "plan_ref": {"revision": self.plan_revision, "snapshot_hash": self.plan_snapshot_hash},
            "request_hash": self.request_hash,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "action_proposal") -> "FrozenActionProposalV1":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "intent_id",
                "candidate_artifact_ref",
                "parameters_ref",
                "effect_contract_ref",
                "prepared_acceptance_refs",
                "producer_task_ref",
                "producer_occurrence_id",
                "obligation_id",
                "requirements_ref",
                "plan_ref",
                "request_hash",
            ),
        )
        plan = fields_of(
            data["plan_ref"], f"{name}.plan_ref", required=("revision", "snapshot_hash")
        )
        return FrozenActionProposalV1(
            data["schema_version"],
            data["intent_id"],
            _ref(
                data["candidate_artifact_ref"],
                TypedRefKind.ARTIFACT,
                f"{name}.candidate_artifact_ref",
            ),
            _ref(data["parameters_ref"], TypedRefKind.ARTIFACT, f"{name}.parameters_ref"),
            _ref(data["effect_contract_ref"], TypedRefKind.ARTIFACT, f"{name}.effect_contract_ref"),
            _refs(
                data["prepared_acceptance_refs"],
                TypedRefKind.ACCEPTANCE,
                f"{name}.prepared_acceptance_refs",
                minimum=1,
            ),
            _ref(data["producer_task_ref"], TypedRefKind.TASK, f"{name}.producer_task_ref"),
            data["producer_occurrence_id"],
            data["obligation_id"],
            _ref(data["requirements_ref"], TypedRefKind.REQUIREMENTS, f"{name}.requirements_ref"),
            index(plan["revision"], f"{name}.plan_ref.revision", minimum=1),
            hash_hex(plan["snapshot_hash"], f"{name}.plan_ref.snapshot_hash"),
            data["request_hash"],
        )


@dataclass(frozen=True, slots=True)
class FrozenActionProposalV2(FrozenActionProposalV1):
    completion_spec_hash: str
    completion_scope_id: str
    completion_scope_hash: str
    effect_key: str
    completion_owner_occurrence_id: str

    def __post_init__(self) -> None:
        FrozenActionProposalV1.__post_init__(self)
        for field in ("completion_spec_hash", "completion_scope_hash"):
            object.__setattr__(self, field, hash_hex(getattr(self, field), f"proposal.{field}"))
        for field in ("completion_scope_id", "effect_key", "completion_owner_occurrence_id"):
            object.__setattr__(self, field, identifier(getattr(self, field), f"proposal.{field}"))

    def to_json(self) -> dict[str, Any]:
        return {
            **FrozenActionProposalV1.to_json(self),
            "schema_version": 2,
            "completion_spec_hash": self.completion_spec_hash,
            "completion_scope_id": self.completion_scope_id,
            "completion_scope_hash": self.completion_scope_hash,
            "effect_key": self.effect_key,
            "completion_owner_occurrence_id": self.completion_owner_occurrence_id,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "action_proposal_v2") -> "FrozenActionProposalV2":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "intent_id",
                "candidate_artifact_ref",
                "parameters_ref",
                "effect_contract_ref",
                "prepared_acceptance_refs",
                "producer_task_ref",
                "producer_occurrence_id",
                "obligation_id",
                "requirements_ref",
                "plan_ref",
                "request_hash",
                "completion_spec_hash",
                "completion_scope_id",
                "completion_scope_hash",
                "effect_key",
                "completion_owner_occurrence_id",
            ),
        )
        if schema_version(data["schema_version"], f"{name}.schema_version", expected=2) != 2:
            raise ContractError("unreachable")
        plan = fields_of(
            data["plan_ref"], f"{name}.plan_ref", required=("revision", "snapshot_hash")
        )
        return cls(
            2,
            data["intent_id"],
            _ref(
                data["candidate_artifact_ref"],
                TypedRefKind.ARTIFACT,
                f"{name}.candidate_artifact_ref",
            ),
            _ref(data["parameters_ref"], TypedRefKind.ARTIFACT, f"{name}.parameters_ref"),
            _ref(data["effect_contract_ref"], TypedRefKind.ARTIFACT, f"{name}.effect_contract_ref"),
            _refs(
                data["prepared_acceptance_refs"],
                TypedRefKind.ACCEPTANCE,
                f"{name}.prepared_acceptance_refs",
                minimum=1,
            ),
            _ref(data["producer_task_ref"], TypedRefKind.TASK, f"{name}.producer_task_ref"),
            data["producer_occurrence_id"],
            data["obligation_id"],
            _ref(data["requirements_ref"], TypedRefKind.REQUIREMENTS, f"{name}.requirements_ref"),
            index(plan["revision"], f"{name}.plan_ref.revision", minimum=1),
            hash_hex(plan["snapshot_hash"], f"{name}.plan_ref.snapshot_hash"),
            data["request_hash"],
            data["completion_spec_hash"],
            data["completion_scope_id"],
            data["completion_scope_hash"],
            data["effect_key"],
            data["completion_owner_occurrence_id"],
        )


@dataclass(frozen=True, slots=True)
class ConnectorOperationProfileV1(_Payload):
    connector_id: str
    adapter_version: str
    adapter_code_digest: str
    namespace: Mapping[str, str]
    operation_name: str
    parameter_schema_ref: VersionedRef
    outcome_contract_version: str
    supported_milestones: tuple[str, ...]
    receipt_adapter: Mapping[str, str]
    conditional_write: object
    idempotency: object
    nonapplication_proofs: tuple[NonapplicationProofKind, ...]
    authority_source_ref: TypedRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(self.schema_version, "connector_profile.schema_version", expected=1),
        )
        for field in (
            "connector_id",
            "adapter_version",
            "operation_name",
            "outcome_contract_version",
        ):
            object.__setattr__(
                self, field, identifier(getattr(self, field), f"connector_profile.{field}")
            )
        object.__setattr__(
            self,
            "adapter_code_digest",
            hash_hex(self.adapter_code_digest, "connector_profile.adapter_code_digest"),
        )
        object.__setattr__(
            self, "namespace", _namespace(self.namespace, "connector_profile.namespace")
        )
        if not isinstance(self.parameter_schema_ref, VersionedRef):
            raise ContractError("connector_profile.parameter_schema_ref must be a VersionedRef")
        object.__setattr__(
            self,
            "supported_milestones",
            _strings(
                self.supported_milestones, "connector_profile.supported_milestones", minimum=1
            ),
        )
        object.__setattr__(
            self,
            "receipt_adapter",
            _receipt_adapter(self.receipt_adapter, "connector_profile.receipt_adapter"),
        )
        object.__setattr__(
            self,
            "conditional_write",
            _profile_condition(self.conditional_write, "connector_profile.conditional_write"),
        )
        object.__setattr__(
            self, "idempotency", _idempotency(self.idempotency, "connector_profile.idempotency")
        )
        proofs = tuple(
            enum_of(NonapplicationProofKind, item, "connector_profile.nonapplication_proofs[]")
            for item in self.nonapplication_proofs
        )
        if len(set(proofs)) != len(proofs):
            raise ContractError(
                "connector_profile.nonapplication_proofs must not contain duplicates"
            )
        object.__setattr__(self, "nonapplication_proofs", proofs)
        if not isinstance(self.authority_source_ref, TypedRef):
            raise ContractError("connector_profile.authority_source_ref must be a TypedRef")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "connector_id": self.connector_id,
            "adapter_version": self.adapter_version,
            "adapter_code_digest": self.adapter_code_digest,
            "namespace": dict(self.namespace),
            "operation_name": self.operation_name,
            "parameter_schema_ref": self.parameter_schema_ref.to_json(),
            "outcome_contract_version": self.outcome_contract_version,
            "supported_milestones": list(self.supported_milestones),
            "receipt_adapter": dict(self.receipt_adapter),
            "conditional_write": self.conditional_write,
            "idempotency": self.idempotency,
            "nonapplication_proofs": [str(item) for item in self.nonapplication_proofs],
            "authority_source_ref": self.authority_source_ref.to_json(),
        }

    @classmethod
    def from_json(
        cls, value: object, name: str = "connector_profile"
    ) -> "ConnectorOperationProfileV1":
        data = fields_of(
            decode_payload_json(value, name),
            name,
            required=(
                "schema_version",
                "connector_id",
                "adapter_version",
                "adapter_code_digest",
                "namespace",
                "operation_name",
                "parameter_schema_ref",
                "outcome_contract_version",
                "supported_milestones",
                "receipt_adapter",
                "conditional_write",
                "idempotency",
                "nonapplication_proofs",
                "authority_source_ref",
            ),
        )
        return cls(
            data["schema_version"],
            data["connector_id"],
            data["adapter_version"],
            data["adapter_code_digest"],
            _namespace(data["namespace"], f"{name}.namespace"),
            data["operation_name"],
            _versioned(data["parameter_schema_ref"], f"{name}.parameter_schema_ref"),
            data["outcome_contract_version"],
            _strings(data["supported_milestones"], f"{name}.supported_milestones", minimum=1),
            _receipt_adapter(data["receipt_adapter"], f"{name}.receipt_adapter"),
            _profile_condition(data["conditional_write"], f"{name}.conditional_write"),
            _idempotency(data["idempotency"], f"{name}.idempotency"),
            tuple(
                enum_of(NonapplicationProofKind, item, f"{name}.nonapplication_proofs[]")
                for item in sequence_of(
                    data["nonapplication_proofs"],
                    f"{name}.nonapplication_proofs",
                    lambda item, _: item,
                )
            ),
            TypedRef.from_json(data["authority_source_ref"], f"{name}.authority_source_ref"),
        )


def decode_operation_payload(value: object, expected_kind: PayloadKind | str) -> _Payload:
    kind = enum_of(PayloadKind, expected_kind, "expected_kind")
    data = decode_payload_json(value)
    version = index(data.get("schema_version"), "operation_payload.schema_version", minimum=1)
    if kind is PayloadKind.PARAMETERS:
        if version != 1:
            raise ContractError("parameters schema_version must be 1")
        return OperationParametersV1.from_json(data)
    if kind is PayloadKind.EFFECT_CONTRACT:
        if version == 1:
            return OperationEffectContractV1.from_json(data)
        if version == 2:
            return OperationEffectContractV2.from_json(data)
        raise ContractError("effect contract schema_version must be 1 or 2")
    if version == 1:
        return FrozenActionProposalV1.from_json(data)
    if version == 2:
        return FrozenActionProposalV2.from_json(data)
    raise ContractError("action proposal schema_version must be 1 or 2")


# Short names are stable aliases for the D2 specification terminology.
ParametersV1 = OperationParametersV1
EffectV1 = OperationEffectContractV1
EffectV2 = OperationEffectContractV2

__all__ = (
    "ConditionalWriteKind",
    "ConnectorOperationProfileV1",
    "FrozenActionProposalV1",
    "FrozenActionProposalV2",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "NonapplicationProofKind",
    "OperationEffectContractV1",
    "OperationEffectContractV2",
    "OperationParametersV1",
    "ParametersV1",
    "EffectV1",
    "EffectV2",
    "PayloadKind",
    "ReconciliationPolicy",
    "decode_operation_payload",
    "decode_payload_json",
)
