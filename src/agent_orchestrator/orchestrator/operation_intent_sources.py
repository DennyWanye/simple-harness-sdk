# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Authoritative, read-only source preparation for completion-bound operation T0."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifacts.store import ArtifactStore, ArtifactStoreError
from ..contracts.evidence_state import Validity
from ..contracts.models import ContractError
from ..contracts.operation_completion import (
    AcceptanceContributionScopeV1,
    ContributionKind,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
    RequiredEffectSlotV1,
)
from ..contracts.operation_intents import OperationIntentSourceKind, SubmitOperationIntentV2
from ..contracts.resolution import Acceptance, RequirementsRevision
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from ..governance.permissions import Principal
from ..storage.htn_store import HtnStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.store import Store, StoredResult, StoreError
from .completion_status import read_occurrence_completion
from .operation_completion import OperationCompletionReader


class OperationIntentSourceError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedOperationIntentSources:
    command: SubmitOperationIntentV2
    raw_candidate_bytes: bytes
    candidate_artifact: Any
    producer_result: StoredResult
    producer_attempt: Any
    producer_acceptance: Acceptance
    prepared_acceptances: tuple[Acceptance, ...]
    producer_contribution: AcceptanceContributionScopeV1
    spec: OperationCompletionRequirementsV1
    effect: RequiredEffectSlotV1
    producer_scope: OccurrenceCompletionScopeV1
    owner_scope: OccurrenceCompletionScopeV1
    requirements: RequirementsRevision
    plan_ref: PlanRevisionPinV1
    tenant_id: str
    principal: Principal


def _fail(code: str, detail: str) -> OperationIntentSourceError:
    return OperationIntentSourceError(code, detail)


def prepare_operation_intent_sources(
    store: Store,
    artifact_store: ArtifactStore,
    command: SubmitOperationIntentV2,
    *,
    tenant_id: str,
    principal: Principal,
) -> PreparedOperationIntentSources:
    """Resolve every T0 source in one consistent read view; write nothing."""

    if not isinstance(command, SubmitOperationIntentV2):
        raise _fail("OP_INTENT_COMMAND_INVALID", "SubmitOperationIntentV2 is required")
    if not isinstance(principal, Principal) or not str(tenant_id).strip():
        raise _fail("OP_INTENT_CALLER_UNAUTHENTICATED", "authenticated caller is required")
    system = command.intent_source.kind is OperationIntentSourceKind.AUTHORIZED_SLOT

    with store.read_view():
        mission = store.get_mission(command.mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "Mission is unavailable to this tenant")
        if mission.status != "ACTIVE":
            raise _fail("OP_EFFECT_SCOPE_STALE", "operation intent requires an active Mission")
        htn = HtnStore(store)
        active = htn.active_plan_revision(command.mission_id)
        if active is None:
            raise _fail("OP_EFFECT_SCOPE_STALE", "Mission has no active Plan")
        plan_ref = PlanRevisionPinV1(
            revision=int(active.revision), snapshot_hash=active.snapshot_hash
        )
        completion = OperationCompletionStore(store)
        reader = OperationCompletionReader(store)
        scopes = tuple(
            reader.read_scope(command.mission_id, plan_ref, str(member.occurrence_id))
            for member in htn.list_plan_memberships(command.mission_id, active.revision)
        )
        owners = tuple(
            scope
            for scope in scopes
            if scope.spec_hash == command.completion_slot.spec_hash
            and command.completion_slot.effect_key in scope.owned_effect_keys
        )
        if len(owners) != 1:
            raise _fail("OP_COMPLETION_SCOPE_UNRESOLVED", "completion effect has no unique owner")
        owner = owners[0]
        spec = reader.read_requirements(
            command.mission_id,
            TypedRef(
                kind=TypedRefKind.REQUIREMENTS,
                id=owner.requirements_ref.id,
                revision=owner.requirements_ref.revision,
                content_hash=owner.requirements_ref.content_hash,
            ),
        )
        if spec.content_hash() != command.completion_slot.spec_hash:
            raise _fail("OP_EFFECT_SCOPE_STALE", "completion Spec hash differs")
        try:
            effect = spec.effect(command.completion_slot.effect_key)
        except (ContractError, KeyError, ValueError) as error:
            raise _fail(
                "OP_COMPLETION_SLOT_REQUIRED", "approved Spec has no such effect"
            ) from error
        requirements = htn.get_requirements_revision(
            command.mission_id, owner.requirements_ref.revision
        )
        operation = None
        if system:
            # 2026-09-29：系统按已批准效果准备的申请单（system_operations）。候选引用指向那份
            # 审过的真实文件本身；申请单 JSON 由系统从效果生成，不来自任何模型产出。
            from .system_operations import authorized_slot_operation

            try:
                operation = authorized_slot_operation(
                    store, command, principal, effect, requirements
                )
            except ContractError as error:
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", str(error)) from error

        artifact = store.get_artifact(command.candidate_artifact_ref.id)
        if (
            artifact is None
            or artifact.mission_id != command.mission_id
            or artifact.version != command.candidate_artifact_ref.revision
            or artifact.content_hash != command.candidate_artifact_ref.content_hash
            or artifact.verification_status != "VERIFIED"
            or (not system and artifact.size_bytes > 262144)
            or Path(artifact.storage_uri) != artifact_store.path_for(artifact.content_hash)
        ):
            raise _fail("OP_INTENT_CANDIDATE_UNAVAILABLE", "candidate Artifact differs")
        if operation is not None:
            from .system_operations import source_matches, system_candidate_bytes

            if not source_matches(artifact.path, operation[2]):
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "source file does not match the target")
            raw = system_candidate_bytes(operation, artifact.path)
        else:
            try:
                raw = artifact_store.read(artifact.content_hash)
            except (ArtifactStoreError, OSError) as error:
                raise _fail("OP_INTENT_CANDIDATE_UNAVAILABLE", str(error)) from error

        acceptances: list[Acceptance] = []
        producer_rows: list[tuple[Acceptance, AcceptanceContributionScopeV1]] = []
        for ref in command.prepared_acceptance_refs:
            try:
                acceptance = htn.get_acceptance(ref.id)
            except StoreError as error:
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "Acceptance is unavailable") from error
            if (
                acceptance.mission_id != command.mission_id
                or acceptance.validity is not Validity.CURRENT
                or ref.revision != 1
                or ref.content_hash != content_hash_of(acceptance.to_json())
            ):
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "Acceptance identity differs")
            row = completion.get_acceptance_scope_exact(command.mission_id, ref.id)
            document = None if row is None else row.get("document")
            if not isinstance(document, AcceptanceContributionScopeV1) or document.kind not in {
                ContributionKind.CONTENT,
                ContributionKind.PREPARATION,
            }:
                raise _fail(
                    "OP_INTENT_SOURCE_UNRESOLVED",
                    "Acceptance has no content contribution",
                )
            contribution_scopes = [
                item for item in scopes if item.scope_id == document.completion_scope_id
            ]
            if (
                len(contribution_scopes) != 1
                or contribution_scopes[0].spec_hash != spec.content_hash()
                or document.spec_hash != spec.content_hash()
                or contribution_scopes[0].obligation_id != owner.obligation_id
            ):
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "prepared Scope differs")
            status = read_occurrence_completion(
                store, command.mission_id, contribution_scopes[0].occurrence_id
            )
            if acceptance.acceptance_id not in status.preparation_acceptance_ids:
                raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "preparation is not currently usable")
            acceptances.append(acceptance)
            if any(
                pin.id == artifact.id
                and pin.revision == artifact.version
                and pin.content_hash == artifact.content_hash
                for pin in document.output_artifact_refs
            ) and any(
                ref.id == artifact.id
                and ref.revision == artifact.version
                and ref.content_hash == artifact.content_hash
                for ref in acceptance.artifact_refs
            ):
                producer_rows.append((acceptance, document))
        if len(producer_rows) != 1:
            raise _fail(
                "OP_INTENT_SOURCE_UNRESOLVED",
                "candidate has no unique producer Acceptance",
            )
        producer_acceptance, producer_contribution = producer_rows[0]
        producer_scopes = tuple(
            scope for scope in scopes if scope.scope_id == producer_contribution.completion_scope_id
        )
        if len(producer_scopes) != 1:
            raise _fail("OP_COMPLETION_SCOPE_UNRESOLVED", "producer Scope is unavailable")
        producer_scope = producer_scopes[0]
        if (
            producer_scope.spec_hash != spec.content_hash()
            or producer_scope.obligation_id != owner.obligation_id
            or producer_acceptance.task_id != producer_scope.task_ref.id
        ):
            raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "producer and owner authority differ")

        # Result identity comes from the Artifact's real Attempt, never from the command.
        attempt = store.get_attempt(artifact.attempt_id)
        if (
            attempt is None
            or attempt.mission_id != command.mission_id
            or attempt.task_id != artifact.task_id
        ):
            raise _fail("OP_INTENT_SOURCE_UNRESOLVED", "candidate Attempt differs")
        result = None if attempt.result_id is None else store.get_result(attempt.result_id)
        task = store.get_task(artifact.task_id)
        if (
            result is None
            or result.envelope.mission_id != command.mission_id
            or result.envelope.task_id != artifact.task_id
            or result.envelope.attempt_id != attempt.id
            or artifact.id not in result.artifacts
            or result.verification_state != "DONE"
            or result.verdict != "PASS"
            or attempt.status != "COMPLETED"
            or task is None
            or task.accepted_result_id != result.envelope.id
            or producer_scope.task_ref.id != task.id
        ):
            raise _fail(
                "OP_INTENT_SOURCE_UNRESOLVED",
                "candidate Result is not accepted preparation",
            )

        return PreparedOperationIntentSources(
            command,
            raw,
            artifact,
            result,
            attempt,
            producer_acceptance,
            tuple(acceptances),
            producer_contribution,
            spec,
            effect,
            producer_scope,
            owner,
            requirements,
            plan_ref,
            tenant_id,
            principal,
        )


__all__ = (
    "OperationIntentSourceError",
    "PreparedOperationIntentSources",
    "prepare_operation_intent_sources",
)
