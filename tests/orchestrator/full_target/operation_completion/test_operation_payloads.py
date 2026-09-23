"""D2 payload codecs and CAS metadata use only real Store receipts and bytes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import ContractError, TypedRef, VersionedRef
from agent_orchestrator.contracts.operation_completion import CompletionPinV1
from agent_orchestrator.contracts.operation_payloads import (
    ConnectorOperationProfileV1,
    FrozenActionProposalV1,
    FrozenActionProposalV2,
    NonapplicationProofKind,
    OperationEffectContractV1,
    OperationEffectContractV2,
    OperationParametersV1,
    PayloadKind,
    ReconciliationPolicy,
    decode_operation_payload,
)
from agent_orchestrator.contracts.semantic_base import TypedRefKind
from agent_orchestrator.runtime.operation_payloads import (
    ConnectorProfileRegistration,
    OperationPayloadUnavailable,
    require_connector_profile,
)
from agent_orchestrator.storage.operation_payload_store import OperationPayloadStore
from agent_orchestrator.storage.store import StoreConflict

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_spec_approval import _approval_world  # noqa: E402

_H = "a" * 64
_H2 = "b" * 64


def _ref(kind: TypedRefKind, name: str, digest: str = _H) -> TypedRef:
    return TypedRef(kind, name, 1, digest)


def _parameters() -> OperationParametersV1:
    return OperationParametersV1(
        1,
        "intent-1",
        "registered-connector",
        "adapter-v1",
        "send",
        _H,
        "target-1",
        None,
        VersionedRef("params-schema", 1, _H),
        {"body": "bound"},
        _H2,
        _ref(TypedRefKind.ARTIFACT, "candidate"),
        (_ref(TypedRefKind.ACCEPTANCE, "acceptance"),),
    )


def _effect(version: int = 1):
    kwargs = dict(
        connector_profile_hash=_H,
        namespace={"service_id": "svc", "account_scope": "acct", "environment": "dev"},
        operation_name="send",
        required_milestone="DELIVERED",
        milestone_criterion_ids=("criterion-1",),
        required_target_condition="NONE",
        receipt_adapter={"id": "receipt-adapter", "version": "1"},
        retry_policy_ref=VersionedRef("retry-policy", 1, _H),
        reconciliation_policy=ReconciliationPolicy.RECONCILE_ONLY,
        idempotency_contract="UNKNOWN",
        accepted_nonapplication_proofs=(NonapplicationProofKind.EXECUTOR_NO_SEND_FINAL,),
    )
    if version == 1:
        return OperationEffectContractV1(1, **kwargs)
    return OperationEffectContractV2(
        2,
        **kwargs,
        completion_spec_hash=_H2,
        effect_key="effect-1",
        milestone_policy_ref=CompletionPinV1(id="milestone-policy", revision=1, content_hash=_H),
    )


def _proposal(version: int = 1):
    kwargs = dict(
        intent_id="intent-1",
        candidate_artifact_ref=_ref(TypedRefKind.ARTIFACT, "candidate"),
        parameters_ref=_ref(TypedRefKind.ARTIFACT, "parameters"),
        effect_contract_ref=_ref(TypedRefKind.ARTIFACT, "effect"),
        prepared_acceptance_refs=(_ref(TypedRefKind.ACCEPTANCE, "acceptance"),),
        producer_task_ref=_ref(TypedRefKind.TASK, "task-1"),
        producer_occurrence_id="occurrence-1",
        obligation_id="obligation-1",
        requirements_ref=_ref(TypedRefKind.REQUIREMENTS, "requirements"),
        plan_revision=1,
        plan_snapshot_hash=_H,
        request_hash=_H2,
    )
    if version == 1:
        return FrozenActionProposalV1(1, **kwargs)
    return FrozenActionProposalV2(
        2,
        **kwargs,
        completion_spec_hash=_H,
        completion_scope_id="scope-1",
        completion_scope_hash=_H2,
        effect_key="effect-1",
        completion_owner_occurrence_id="owner-1",
    )


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (_parameters(), PayloadKind.PARAMETERS),
        (_effect(), PayloadKind.EFFECT_CONTRACT),
        (_effect(2), PayloadKind.EFFECT_CONTRACT),
        (_proposal(), PayloadKind.ACTION_PROPOSAL),
        (_proposal(2), PayloadKind.ACTION_PROPOSAL),
    ],
)
def test_d2_payload_roundtrips_exact_canonical_bytes(payload, kind) -> None:
    decoded = decode_operation_payload(payload.canonical_bytes(), kind)
    assert decoded.to_json() == payload.to_json()
    assert decoded.content_hash() == payload.content_hash()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b'{"schema_version":true}',
        b"{" + b'"x":{' * 17 + b"0" + b"}" * 17 + b"}",
        b"{" + b'"x":"' + b"z" * (256 * 1024) + b'"}',
    ],
)
def test_d2_payload_decoder_rejects_noncanonical_or_unbounded_json(raw: bytes) -> None:
    with pytest.raises(ContractError):
        decode_operation_payload(raw, PayloadKind.PARAMETERS)


def test_d2_payload_rejects_wrong_typed_ref_and_duplicate_semantic_array() -> None:
    raw = _parameters().to_json()
    raw["candidate_artifact_ref"]["kind"] = "acceptance"
    with pytest.raises(ContractError):
        OperationParametersV1.from_json(raw)
    raw = _parameters().to_json()
    raw["accepted_input_refs"] *= 2
    with pytest.raises(ContractError):
        OperationParametersV1.from_json(raw)


def test_d2_payload_store_requires_same_mission_receipt_and_rechecks_cas(tmp_path) -> None:
    world, _ = _approval_world(tmp_path)
    payload = _parameters()
    cas = ArtifactStore(tmp_path / "payload-cas")
    writer = OperationPayloadStore(world.store)
    with pytest.raises(StoreConflict, match="transaction"):
        writer.put_payload(
            mission_id=world.mission.id,
            object_id="parameters-1",
            kind=PayloadKind.PARAMETERS,
            payload=payload,
            source_receipt_id="not-a-receipt",
            cas=cas,
        )
    with world.store.transaction():
        world.store.insert_receipt(
            commit_id="payload-source",
            kind="operation_intent_submitted",
            subject_id="intent-1",
            base_version=None,
            proposal_hash=_H,
            receipt={"mission_id": world.mission.id, "kind": "operation_intent_submitted"},
        )
        ref = writer.put_payload(
            mission_id=world.mission.id,
            object_id="parameters-1",
            kind=PayloadKind.PARAMETERS,
            payload=payload,
            source_receipt_id="payload-source",
            cas=cas,
        )
    assert ref.kind is TypedRefKind.ARTIFACT and ref.revision == 1
    assert (
        writer.get_payload(
            mission_id=world.mission.id, ref=ref, expected_kind=PayloadKind.PARAMETERS, cas=cas
        ).payload
        == payload
    )
    cas.path_for(ref.content_hash).chmod(0o600)
    cas.path_for(ref.content_hash).write_bytes(b"corrupt")
    with pytest.raises(StoreConflict, match="bytes"):
        writer.get_payload(
            mission_id=world.mission.id, ref=ref, expected_kind=PayloadKind.PARAMETERS, cas=cas
        )


class _Adapter:
    def __init__(self, profile: ConnectorOperationProfileV1) -> None:
        self._profile = profile

    def operation_profile(self) -> ConnectorOperationProfileV1:
        return self._profile


class _Registry:
    def __init__(self, registration: ConnectorProfileRegistration | None) -> None:
        self.registration = registration

    def resolve_connector_operation(self, connector_id: str, operation_name: str):
        return self.registration


def test_profile_requires_actual_adapter_registration_not_strings() -> None:
    profile = ConnectorOperationProfileV1(
        1,
        "registered-connector",
        "adapter-v1",
        _H,
        {"service_id": "svc", "account_scope": "acct", "environment": "dev"},
        "send",
        VersionedRef("params-schema", 1, _H),
        "outcome-v1",
        ("DELIVERED",),
        {"id": "receipt-adapter", "version": "1"},
        "NONE",
        "UNKNOWN",
        (),
        _ref(TypedRefKind.SOURCE, "deployment-contract"),
    )
    registry = _Registry(
        ConnectorProfileRegistration(
            _Adapter(profile), _ref(TypedRefKind.SOURCE, "adapter-build", _H2), profile
        )
    )
    with pytest.raises(OperationPayloadUnavailable, match="identity"):
        require_connector_profile(
            registry, connector_id="registered-connector", operation_name="send"
        )
    profile = ConnectorOperationProfileV1.from_json(
        {**profile.to_json(), "adapter_code_digest": _H2}
    )
    registry = _Registry(
        ConnectorProfileRegistration(
            _Adapter(profile), _ref(TypedRefKind.SOURCE, "adapter-build", _H2), profile
        )
    )
    assert (
        require_connector_profile(
            registry, connector_id="registered-connector", operation_name="send"
        )
        == profile
    )
