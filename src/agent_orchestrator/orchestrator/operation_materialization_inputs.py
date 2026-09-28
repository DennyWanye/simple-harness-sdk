# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure T0 payload freezing from already-authoritative operation sources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import ContractError
from ..contracts.operation_completion import CompletionPinV1
from ..contracts.operation_payloads import (
    ConditionalWriteKind,
    ConnectorOperationProfileV1,
    FrozenActionProposalV2,
    OperationEffectContractV2,
    OperationParametersV1,
    ReconciliationPolicy,
)
from ..contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef, identifier
from ..governance.policies import DeploymentPolicy
from ..runtime.connectors import params_hash
from ..runtime.operation_payloads import (
    ConnectorProfileRegistry,
    OperationPayloadUnavailable,
    payload_request_hash,
    require_connector_profile,
)
from ..storage.store import Store
from .action_commits import CandidateRejected, bind_artifact_params, check_candidate
from .operation_intent_sources import PreparedOperationIntentSources


class OperationMaterializationInputError(ContractError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class OperationPayloadObjectIds:
    """System-assigned T0 identities; this builder never derives business Operations."""

    intent_id: str
    parameters_object_id: str
    effect_object_id: str
    proposal_object_id: str

    def __post_init__(self) -> None:
        for field in (
            "intent_id",
            "parameters_object_id",
            "effect_object_id",
            "proposal_object_id",
        ):
            object.__setattr__(self, field, identifier(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class DeploymentOperationPolicyInputs:
    """Explicit versioned policy/current-precondition facts supplied by deployment."""

    retry_policy_ref: VersionedRef
    required_target_condition: ConditionalWriteKind | Mapping[str, str]
    reconciliation_policy: ReconciliationPolicy
    idempotency_contract: object
    evidence_policy_ref: CompletionPinV1

    def __post_init__(self) -> None:
        if not isinstance(self.retry_policy_ref, VersionedRef) or not isinstance(
            self.evidence_policy_ref, CompletionPinV1
        ):
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "deployment policy references must be versioned"
            )
        if not isinstance(self.reconciliation_policy, ReconciliationPolicy):
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "reconciliation policy is not typed"
            )
        if not isinstance(self.required_target_condition, (ConditionalWriteKind, Mapping)):
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "target condition is not typed"
            )


@dataclass(frozen=True, slots=True)
class FrozenOperationPayloadInputs:
    parameters: OperationParametersV1
    effect_contract: OperationEffectContractV2
    action_proposal: FrozenActionProposalV2
    parameters_ref: TypedRef
    effect_contract_ref: TypedRef
    action_proposal_ref: TypedRef
    profile: ConnectorOperationProfileV1
    raw_candidate: Mapping[str, Any]
    effective_params: Mapping[str, Any]

    @property
    def effect(self) -> OperationEffectContractV2:
        return self.effect_contract

    @property
    def proposal(self) -> FrozenActionProposalV2:
        return self.action_proposal


def _candidate(raw: bytes) -> Mapping[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise OperationMaterializationInputError(
                    "OP_INTENT_CANDIDATE_UNAVAILABLE", "duplicate candidate key"
                )
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                OperationMaterializationInputError(
                    "OP_INTENT_CANDIDATE_UNAVAILABLE", f"non-finite constant {constant}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise OperationMaterializationInputError(
            "OP_INTENT_CANDIDATE_UNAVAILABLE", "candidate JSON is unavailable"
        ) from error
    if not isinstance(value, Mapping):
        raise OperationMaterializationInputError("OP_INTENT_CANDIDATE_UNAVAILABLE", "not object")
    return value


def _accepted_artifacts(
    sources: PreparedOperationIntentSources, values: Mapping[str, Any]
) -> dict[str, Any]:
    output_ids = set(sources.producer_result.artifacts)
    result: dict[str, Any] = {}
    for path, artifact in values.items():
        if (
            not isinstance(path, str)
            or getattr(artifact, "path", None) != path
            or getattr(artifact, "id", None) not in output_ids
            or getattr(artifact, "mission_id", None) != sources.command.mission_id
            or getattr(artifact, "attempt_id", None) != sources.producer_attempt.id
            or getattr(artifact, "task_id", None) != sources.producer_scope.task_ref.id
            or getattr(artifact, "verification_status", None) != "VERIFIED"
        ):
            raise OperationMaterializationInputError(
                "OP_INTENT_SOURCE_UNRESOLVED", "artifact is not an accepted producer output"
            )
        result[path] = artifact
    return result


def _actual_accepted_artifacts(
    store: Store, sources: PreparedOperationIntentSources
) -> dict[str, Any]:
    """Re-read only the producer Result's registered artifacts from the real Store."""
    values: dict[str, Any] = {}
    accepted = {
        (pin.id, pin.revision, pin.content_hash)
        for pin in sources.producer_acceptance.artifact_refs
    }
    contribution_outputs = {
        (pin.id, pin.revision, pin.content_hash)
        for pin in sources.producer_contribution.output_artifact_refs
    }
    for artifact_id in sources.producer_result.artifacts:
        artifact = store.get_artifact(artifact_id)
        path = getattr(artifact, "path", None)
        identity = (
            getattr(artifact, "id", None),
            getattr(artifact, "version", None),
            getattr(artifact, "content_hash", None),
        )
        if identity not in accepted or identity not in contribution_outputs:
            continue
        if not isinstance(path, str) or path in values:
            raise OperationMaterializationInputError(
                "OP_INTENT_SOURCE_UNRESOLVED", "accepted output Artifact is unavailable"
            )
        values[path] = artifact
    return _accepted_artifacts(sources, values)


def _typed_ref(kind: TypedRefKind, pin: CompletionPinV1) -> TypedRef:
    return TypedRef(kind, pin.id, pin.revision, pin.content_hash)


def _condition_supported(
    capability: object, required: ConditionalWriteKind | Mapping[str, str]
) -> bool:
    """Check an adapter's declared protocol, never a caller's claimed capability."""
    if capability is ConditionalWriteKind.NONE:
        return required is ConditionalWriteKind.NONE
    if not isinstance(capability, Mapping) or not isinstance(required, Mapping):
        return False
    # A profile names the protocol it implements; a deployment supplies the live
    # token/value for that protocol.  The token must never be guessed or compared
    # with a profile's protocol identifier.
    return capability.get("kind") == required.get("kind")


def build_operation_materialization_inputs(
    sources: PreparedOperationIntentSources,
    *,
    store: Store,
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
    profiles: ConnectorProfileRegistry,
    intent_id: str,
    parameters_object_id: str,
    effect_object_id: str,
    proposal_object_id: str,
    policy: DeploymentOperationPolicyInputs,
) -> FrozenOperationPayloadInputs:
    """Freeze T0 payload bytes only; no Store write, receipt, operation, or connector call."""
    object_ids = OperationPayloadObjectIds(
        intent_id, parameters_object_id, effect_object_id, proposal_object_id
    )
    if not isinstance(sources, PreparedOperationIntentSources) or not isinstance(store, Store):
        raise OperationMaterializationInputError(
            "OP_INTENT_SOURCE_UNRESOLVED", "typed sources/store"
        )
    if not isinstance(deployment, DeploymentPolicy):
        raise OperationMaterializationInputError("OP_CAPABILITY_UNSUPPORTED", "deployment missing")
    if not isinstance(policy, DeploymentOperationPolicyInputs):
        raise OperationMaterializationInputError(
            "OP_CAPABILITY_UNSUPPORTED", "deployment policy missing"
        )
    if policy.evidence_policy_ref != sources.effect.evidence_policy_ref:
        raise OperationMaterializationInputError(
            "OP_CAPABILITY_UNSUPPORTED", "evidence policy differs from approved Spec"
        )
    mission = store.get_mission(sources.command.mission_id)
    artifact = store.get_artifact(sources.command.candidate_artifact_ref.id)
    if (
        mission is None
        or mission.tenant_id != sources.tenant_id
        or mission.status != "ACTIVE"
        or artifact is None
        or artifact.mission_id != sources.command.mission_id
        or artifact.version != sources.command.candidate_artifact_ref.revision
        or artifact.content_hash != sources.command.candidate_artifact_ref.content_hash
        or artifact.verification_status != "VERIFIED"
    ):
        raise OperationMaterializationInputError(
            "OP_INTENT_SOURCE_UNRESOLVED", "Mission or accepted candidate differs"
        )

    raw = _candidate(sources.raw_candidate_bytes)
    try:
        candidate, decision = check_candidate(
            raw,
            criteria=mission.success_criteria,
            connectors=connectors,
            deployment=deployment,
        )
        from ..contracts.operation_intents import OperationIntentSourceKind

        if (sources.command.intent_source.kind is OperationIntentSourceKind.AUTHORIZED_SLOT
                and decision.required_approvals < 1):
            # 2026-09-29：系统只代办需要人批准的操作；不需要批准的会被自动执行，不能由系统
            # 代为提交（审阅：AppWorld 注册了 L1 操作）。
            raise OperationMaterializationInputError(
                "OP_CAPABILITY_UNSUPPORTED", "a system-prepared operation must require human approval")
        effective = bind_artifact_params(
            candidate["params"], _actual_accepted_artifacts(store, sources)
        )
    except CandidateRejected as error:
        raise OperationMaterializationInputError(
            "OP_INTENT_CANDIDATE_REJECTED", error.reason
        ) from error
    try:
        profile = require_connector_profile(
            profiles,
            connector_id=str(candidate["connector"]),
            operation_name=str(candidate["operation"]),
        )
    except OperationPayloadUnavailable as error:
        raise OperationMaterializationInputError(error.code, str(error)) from error
    if sources.effect.required_milestone not in profile.supported_milestones:
        raise OperationMaterializationInputError("OP_CAPABILITY_UNSUPPORTED", "required milestone")
    if not _condition_supported(profile.conditional_write, policy.required_target_condition):
        raise OperationMaterializationInputError(
            "OP_CAPABILITY_UNSUPPORTED", "current precondition is not adapter-supported"
        )
    if profile.idempotency != policy.idempotency_contract:
        raise OperationMaterializationInputError(
            "OP_CAPABILITY_UNSUPPORTED", "deployment idempotency exceeds adapter profile"
        )

    condition = policy.required_target_condition
    expected_target_version = (
        condition.get("value") if isinstance(condition, Mapping) and condition.get("kind") == "ETAG"
        else None
    )
    candidate_ref = sources.command.candidate_artifact_ref
    parameters = OperationParametersV1(
        1,
        object_ids.intent_id,
        profile.connector_id,
        profile.adapter_version,
        profile.operation_name,
        profile.content_hash(),
        str(candidate["target"]),
        expected_target_version,
        profile.parameter_schema_ref,
        effective,
        params_hash(effective),
        candidate_ref,
        sources.command.prepared_acceptance_refs,
    )
    effect = OperationEffectContractV2(
        2,
        profile.content_hash(),
        profile.namespace,
        profile.operation_name,
        sources.effect.required_milestone,
        sources.effect.criterion_ids,
        policy.required_target_condition,
        profile.receipt_adapter,
        policy.retry_policy_ref,
        policy.reconciliation_policy,
        policy.idempotency_contract,
        profile.nonapplication_proofs,
        sources.spec.content_hash(),
        sources.effect.effect_key,
        sources.effect.milestone_policy_ref,
    )
    parameters_ref = TypedRef(
        TypedRefKind.ARTIFACT, object_ids.parameters_object_id, 1, parameters.content_hash()
    )
    effect_ref = TypedRef(
        TypedRefKind.ARTIFACT, object_ids.effect_object_id, 1, effect.content_hash()
    )
    task_ref = _typed_ref(TypedRefKind.TASK, sources.producer_scope.task_ref)
    requirements_ref = _typed_ref(
        TypedRefKind.REQUIREMENTS, sources.producer_scope.requirements_ref
    )
    request_hash = payload_request_hash(
        version=2,
        intent_id=object_ids.intent_id,
        connector_profile_hash=profile.content_hash(),
        normalized_target_ref=str(candidate["target"]),
        expected_target_version=expected_target_version,
        parameters_content_hash=parameters.content_hash(),
        params_hash=parameters.params_hash,
        candidate_file_hash=candidate_ref.content_hash,
        effect_contract_hash=effect.content_hash(),
        requirements_ref=requirements_ref,
        producer_task_ref=task_ref,
        producer_occurrence_id=sources.producer_scope.occurrence_id,
        completion_spec_hash=sources.spec.content_hash(),
        completion_scope_hash=sources.owner_scope.content_hash(),
        effect_key=sources.effect.effect_key,
        completion_owner_occurrence_id=sources.owner_scope.occurrence_id,
    )
    proposal = FrozenActionProposalV2(
        2,
        object_ids.intent_id,
        candidate_ref,
        parameters_ref,
        effect_ref,
        sources.command.prepared_acceptance_refs,
        task_ref,
        sources.producer_scope.occurrence_id,
        sources.owner_scope.obligation_id,
        requirements_ref,
        sources.plan_ref.revision,
        sources.plan_ref.snapshot_hash,
        request_hash,
        sources.spec.content_hash(),
        sources.owner_scope.scope_id,
        sources.owner_scope.content_hash(),
        sources.effect.effect_key,
        sources.owner_scope.occurrence_id,
    )
    proposal_ref = TypedRef(
        TypedRefKind.ARTIFACT, object_ids.proposal_object_id, 1, proposal.content_hash()
    )
    return FrozenOperationPayloadInputs(
        parameters,
        effect,
        proposal,
        parameters_ref,
        effect_ref,
        proposal_ref,
        profile,
        raw,
        effective,
    )


def freeze_operation_payloads(
    sources: PreparedOperationIntentSources,
    *,
    store: Store,
    object_ids: OperationPayloadObjectIds,
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
    profile_registry: ConnectorProfileRegistry,
    deployment_policy: DeploymentOperationPolicyInputs,
) -> FrozenOperationPayloadInputs:
    """Compatibility spelling which preserves the required real-Store boundary."""
    return build_operation_materialization_inputs(
        sources,
        store=store,
        connectors=connectors,
        deployment=deployment,
        profiles=profile_registry,
        intent_id=object_ids.intent_id,
        parameters_object_id=object_ids.parameters_object_id,
        effect_object_id=object_ids.effect_object_id,
        proposal_object_id=object_ids.proposal_object_id,
        policy=deployment_policy,
    )


__all__ = (
    "DeploymentOperationPolicyInputs",
    "FrozenOperationPayloadInputs",
    "OperationMaterializationInputError",
    "OperationPayloadObjectIds",
    "build_operation_materialization_inputs",
    "freeze_operation_payloads",
)
