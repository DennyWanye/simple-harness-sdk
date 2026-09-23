# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Read the current persisted completion facts for one adopted occurrence.

This module is a projection only.  It never reviews content, interprets connector
receipts, repairs validity, or manufactures a missing completion fact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..contracts.evidence_state import Validity
from ..contracts.htn import TaskForm
from ..contracts.models import ContractError
from ..contracts.operation_completion import (
    ContributionKind,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
)
from ..contracts.resolution import CriterionVerdict, ReviewPurpose, ReviewVerdict
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ..storage.htn_store import HtnStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.store import Store, StoreConflict, StoreError
from .operation_completion import OperationCompletionReader


@dataclass(frozen=True, slots=True)
class OccurrenceCompletionStatus:
    scope: OccurrenceCompletionScopeV1
    content_ready: bool
    effects_ready: bool
    complete: bool
    preparation_ready: bool
    preparation_acceptance_ids: tuple[str, ...] = ()


def read_current_effect(
    store: Store,
    mission_id: str,
    spec_hash: str,
    effect_key: str,
) -> dict[str, Any]:
    """Explain one approved effect without treating an action success as completion."""
    from ..storage.operation_intent_store import OperationIntentStore

    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        if active is None:
            raise StoreError("effect has no active Plan")
        plan_ref = PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash)
        reader = OperationCompletionReader(store)
        owner = _owner_scope(
            reader,
            htn,
            mission_id=mission_id,
            plan_ref=plan_ref,
            effect_key=effect_key,
            spec_hash=spec_hash,
        )
        if owner is None:
            return {"state": "SCOPE_STALE", "effect_key": effect_key, "complete": False}
        spec = reader.read_requirements(
            mission_id,
            TypedRef(
                TypedRefKind.REQUIREMENTS,
                owner.requirements_ref.id,
                owner.requirements_ref.revision,
                owner.requirements_ref.content_hash,
            ),
        )
        if _effect_ready(
            store,
            reader,
            OperationCompletionStore(store),
            htn,
            mission_id=mission_id,
            plan_ref=plan_ref,
            spec=spec,
            effect_key=effect_key,
        ):
            return {"state": "ACCEPTED", "effect_key": effect_key, "complete": True}
        rows = [
            row
            for row in OperationIntentStore(store).for_mission(mission_id)
            if row["binding"].get("command", {}).get("completion_slot")
            == {"spec_hash": spec_hash, "effect_key": effect_key}
        ]
        replaced = {row["supersedes_intent_id"] for row in rows if row["supersedes_intent_id"]}
        heads = [row for row in rows if row["intent_id"] not in replaced]
        if not heads:
            return {"state": "AWAITING_INTENT", "effect_key": effect_key, "complete": False}
        if len(heads) != 1:
            return {"state": "INTENT_AMBIGUOUS", "effect_key": effect_key, "complete": False}
        intent = heads[0]
        record = htn.official_review_record(intent["review_package_id"])
        state = "AWAITING_PROPOSAL_REVIEW"
        if record is not None:
            state = (
                "AWAITING_MATERIALIZATION"
                if record.verdict is ReviewVerdict.ACCEPT
                else "PROPOSAL_REJECTED"
            )
        materialized = store.get_receipt("materialize:" + intent["intent_id"])
        action = None if materialized is None else store.get_action(materialized["action_key"])
        if materialized is not None:
            state = (
                "SOURCE_UNAVAILABLE"
                if action is None
                else {
                    "SUCCEEDED": "AWAITING_OUTCOME_REVIEW",
                    "UNKNOWN": "RECONCILIATION_REQUIRED",
                    "HANDED_OFF": "EXECUTING",
                    "AWAITING_APPROVAL": "AWAITING_APPROVAL",
                    "APPROVED": "AWAITING_EXECUTION",
                    "PROPOSED": "AWAITING_EXECUTION",
                }.get(str(action["state"]), "EXECUTION_" + str(action["state"]))
            )
        return {
            "state": state,
            "effect_key": effect_key,
            "intent_id": intent["intent_id"],
            "execution_state": None if action is None else action["state"],
            "complete": False,
        }


def _official_accept_review(
    htn: HtnStore,
    *,
    acceptance_id: str,
    mission_id: str,
    task_id: str,
    requirements_revision: int,
    requirements_hash: str,
    contract_revision: int,
    purpose: ReviewPurpose,
) -> bool:
    try:
        acceptance = htn.get_acceptance(acceptance_id)
        stored = htn.get_review_record(str(acceptance.review_record_id))
    except (ContractError, StoreError):
        return False
    record = stored.record
    binding = htn.task_semantics_of(mission_id, task_id)
    if binding is None or any(
        item.subject_id in {acceptance_id, task_id} for item in htn.list_dirty(mission_id)
    ):
        return False
    if (
        not stored.official
        or acceptance.mission_id != mission_id
        or str(acceptance.task_id) != task_id
        or acceptance.requirements_revision != requirements_revision
        or acceptance.contract_revision != contract_revision
        or acceptance.validity is not Validity.CURRENT
        or record.record_id != acceptance.review_record_id
        or record.purpose is not purpose
        or record.verdict is not ReviewVerdict.ACCEPT
    ):
        return False
    official = htn.official_review_record(str(record.package_id))
    if official is None or official.to_json() != record.to_json():
        return False
    try:
        package = htn.get_review_package(str(record.package_id))
    except (ContractError, StoreError):
        return False
    subject_matches = (
        package.binding.subject_ref.kind is TypedRefKind.TASK
        and package.binding.subject_ref.id == task_id
        and package.binding.subject_ref.revision == binding.contract_revision
        and package.binding.subject_ref.content_hash == binding.contract_hash
        if purpose is ReviewPurpose.TASK_CONTENT
        else package.binding.subject_ref.kind in {TypedRefKind.OPERATION, TypedRefKind.TOOL_RECEIPT}
    )
    return (
        package.purpose is purpose
        and package.binding.mission_id == mission_id
        and package.binding.requirements_revision == requirements_revision
        and package.requirements_content_hash == requirements_hash
        and package.binding.input_manifest_hash == acceptance.input_manifest_hash
        and package.binding.obligation_id == acceptance.obligation_id
        and subject_matches
    )


def _primitive_content(
    completion: OperationCompletionStore,
    htn: HtnStore,
    *,
    mission_id: str,
    scope: OccurrenceCompletionScopeV1,
) -> tuple[bool, bool, tuple[str, ...]]:
    required = set(scope.content_criterion_ids)
    preparation = False
    content = False
    accepted_ids: list[str] = []
    for row in completion.retained_content_contributions(scope):
        document = row.get("document")
        if document is None or document.kind not in {
            ContributionKind.CONTENT,
            ContributionKind.PREPARATION,
        }:
            continue
        if not _official_accept_review(
            htn,
            acceptance_id=document.acceptance_id,
            mission_id=mission_id,
            task_id=scope.task_ref.id,
            requirements_revision=scope.requirements_ref.revision,
            requirements_hash=scope.requirements_ref.content_hash,
            contract_revision=scope.task_ref.revision,
            purpose=ReviewPurpose.TASK_CONTENT,
        ):
            continue
        actual = set(document.content_criterion_ids)
        if not actual.issubset(required):
            continue
        preparation = True
        accepted_ids.append(document.acceptance_id)
        if required.issubset(actual):
            content = True
    return content, preparation, tuple(accepted_ids)


def _compound_content(
    htn: HtnStore,
    *,
    mission_id: str,
    scope: OccurrenceCompletionScopeV1,
) -> bool:
    # Adoption is an obligation-level legacy index.  A nested compound may share
    # its parent's obligation, so adopting the root necessarily clears the inner
    # row's adopted bit.  Completion is occurrence/Task scoped and must therefore
    # select the exact immutable resolution identity instead of treating that one
    # obligation-wide pointer as the compound's content verdict.
    candidates = tuple(
        item
        for item in htn.list_goal_resolutions(mission_id, obligation_id=scope.obligation_id)
        if item.mission_id == mission_id
        and item.goal_task_id == scope.task_ref.id
        and item.requirements_version == scope.requirements_ref.revision
        and item.contract_revision == scope.task_ref.revision
        and item.validity is Validity.CURRENT
        and item.verdict is ReviewVerdict.ACCEPT
    )
    if len(candidates) != 1:
        return False
    resolution = candidates[0]
    if any(
        item.subject_id in {str(resolution.resolution_id), scope.task_ref.id}
        for item in htn.list_dirty(mission_id)
    ):
        return False
    active = htn.active_plan_revision(mission_id)
    if active is None or active.revision != scope.plan_ref.revision:
        return False
    instances = htn.list_method_instances(mission_id, state="ADOPTED")
    if not any(
        str(item.instance_id) == str(resolution.method_instance_id)
        and str(item.effective_goal_occurrence_id) == scope.occurrence_id
        for item in instances
    ):
        return False
    try:
        stored = htn.get_review_record(resolution.review_receipt_id)
        package = htn.get_review_package(str(stored.record.package_id))
    except (ContractError, StoreError):
        return False
    record = stored.record
    official = htn.official_review_record(str(record.package_id))
    if (
        not stored.official
        or official is None
        or official.to_json() != record.to_json()
        or record.verdict is not ReviewVerdict.ACCEPT
        or record.purpose not in {ReviewPurpose.COMPOSITION, ReviewPurpose.MISSION_FINAL}
        or package.purpose is not record.purpose
        or package.binding.mission_id != mission_id
        or package.binding.obligation_id != scope.obligation_id
        or package.binding.requirements_revision != scope.requirements_ref.revision
        or package.requirements_content_hash != scope.requirements_ref.content_hash
        or package.binding.subject_ref.kind is not TypedRefKind.TASK
        or package.binding.subject_ref.id != scope.task_ref.id
        or package.binding.subject_ref.revision != scope.task_ref.revision
        or package.binding.subject_ref.content_hash != scope.task_ref.content_hash
    ):
        return False
    required = set(scope.content_criterion_ids)
    if record.purpose is ReviewPurpose.MISSION_FINAL:
        from ..verification.acceptance_rules import evaluate_success_expression, outcomes_by_id

        requirements = htn.get_requirements_revision(mission_id, scope.requirements_ref.revision)
        if (
            package.criteria != requirements.criteria
            or package.success_expression != requirements.success_expression
        ):
            return False
        # The approved root formula may contain ANY. The Scope catalogue is a
        # coverage boundary, not a replacement ALL expression over every ID.
        return evaluate_success_expression(
            requirements.success_expression, outcomes_by_id(record.criteria)
        ).passed
    passed = {
        item.criterion_id for item in resolution.criteria if item.verdict is CriterionVerdict.PASS
    }
    return required.issubset(passed)


def _owner_scope(
    reader: OperationCompletionReader,
    htn: HtnStore,
    *,
    mission_id: str,
    plan_ref: PlanRevisionPinV1,
    effect_key: str,
    spec_hash: str,
) -> OccurrenceCompletionScopeV1 | None:
    owners: list[OccurrenceCompletionScopeV1] = []
    for member in htn.list_plan_memberships(mission_id, plan_ref.revision):
        try:
            candidate = reader.read_scope(mission_id, plan_ref, str(member.occurrence_id))
        except (ContractError, StoreConflict, StoreError):
            return None
        if candidate.spec_hash == spec_hash and effect_key in candidate.owned_effect_keys:
            owners.append(candidate)
    return owners[0] if len(owners) == 1 else None


def _effect_ready(
    store: Store,
    reader: OperationCompletionReader,
    completion: OperationCompletionStore,
    htn: HtnStore,
    *,
    mission_id: str,
    plan_ref: PlanRevisionPinV1,
    spec: OperationCompletionRequirementsV1,
    effect_key: str,
    proofs: list[dict[str, Any]] | None = None,
) -> bool:
    effect = spec.effect(effect_key)
    owner = _owner_scope(
        reader,
        htn,
        mission_id=mission_id,
        plan_ref=plan_ref,
        effect_key=effect_key,
        spec_hash=spec.content_hash(),
    )
    if owner is None or owner.obligation_id != effect.obligation_id:
        return False
    matched: list[dict[str, Any]] = []
    for contribution_row in completion.list_scoped_contributions(mission_id, owner.scope_id):
        contribution = contribution_row.get("document")
        if (
            contribution is None
            or contribution.kind is not ContributionKind.OPERATION_EFFECT
            or contribution.effect_keys != (effect_key,)
            or not contribution.outcome_binding_id
        ):
            continue
        if not _official_accept_review(
            htn,
            acceptance_id=contribution.acceptance_id,
            mission_id=mission_id,
            task_id=owner.task_ref.id,
            requirements_revision=owner.requirements_ref.revision,
            requirements_hash=owner.requirements_ref.content_hash,
            contract_revision=owner.task_ref.revision,
            purpose=ReviewPurpose.OPERATION_OUTCOME,
        ):
            continue
        outcome_row = completion.get_outcome_binding_exact(
            mission_id, contribution.outcome_binding_id
        )
        if outcome_row is None:
            continue
        outcome = outcome_row["document"]
        if (
            outcome.spec_hash != spec.content_hash()
            or outcome.effect_key != effect_key
            or outcome.completion_scope_id != owner.scope_id
            or outcome.milestone_policy_ref != effect.milestone_policy_ref
            or outcome.observed_milestone != effect.required_milestone
        ):
            continue
        try:
            package = htn.get_review_package(str(outcome_row["review_package_id"]))
            official = htn.official_review_record(str(package.package_id))
            receipt = store.get_receipt(str(outcome_row["producer_receipt_id"]))
        except (ContractError, StoreError):
            continue
        policy = package.binding.policy_ref
        if (
            package.purpose is not ReviewPurpose.OPERATION_OUTCOME
            or package.binding.mission_id != mission_id
            or package.binding.requirements_revision != spec.requirements_ref.revision
            or policy.kind is not TypedRefKind.SOURCE
            or policy.id != effect.evidence_policy_ref.id
            or policy.revision != effect.evidence_policy_ref.revision
            or policy.content_hash != effect.evidence_policy_ref.content_hash
            or official is None
            or official.purpose is not ReviewPurpose.OPERATION_OUTCOME
            or official.verdict is not ReviewVerdict.ACCEPT
            or official.package_id != package.package_id
            or receipt is None
            or receipt.get("mission_id") != mission_id
            or receipt.get("subject_id") != contribution.outcome_binding_id
        ):
            continue
        binding_hash = outcome.content_hash()
        if not any(
            ref.id == contribution.outcome_binding_id and ref.content_hash == binding_hash
            for ref in package.candidate_refs
        ):
            continue
        from ..storage.operation_intent_store import OperationIntentStore
        from ..storage.planning_admission_store import PlanningAdmissionStore

        intent = OperationIntentStore(store).get(outcome.intent_id)
        materialized = store.get_receipt("materialize:" + outcome.intent_id)
        action = store.get_action(outcome.action_key)
        link = PlanningAdmissionStore(store).get_operation_action_link(outcome.operation_id)
        if (
            intent is None
            or materialized is None
            or action is None
            or link is None
            or action["state"] != "SUCCEEDED"
            or action["version"] != outcome.action_version
            or action["params_hash"] != outcome.params_hash
            or intent["request_hash"] != outcome.request_hash
            or intent["parameters_content_hash"] != outcome.parameters_content_hash
            or intent["candidate_file_hash"] != outcome.candidate_file_hash
            or intent["effect_content_hash"] != outcome.effect_contract_hash
            or link["link_hash"] != outcome.link_hash
            or link["action_key"] != outcome.action_key
            or materialized["operation_id"] != outcome.operation_id
            or materialized["operation_occurrence_id"] != outcome.operation_occurrence_id
        ):
            continue
        observations = tuple(store.get_receipt(ref.id) for ref in outcome.source_receipt_refs)
        if any(
            observation is None
            or content_hash_of(observation) != ref.content_hash
            or observation.get("receipt") != action.get("receipt")
            or observation.get("intent_id") != outcome.intent_id
            for ref, observation in zip(outcome.source_receipt_refs, observations, strict=True)
        ):
            continue
        if contribution.delivery_receipt_ref is not None:
            delivery = htn.find_delivery_receipt(mission_id, contribution.delivery_receipt_ref.id)
            if (
                delivery is None
                or content_hash_of(delivery.to_json())
                != contribution.delivery_receipt_ref.content_hash
                or str(delivery.acceptance_id) != contribution.acceptance_id
                or delivery.operation_id != outcome.operation_id
            ):
                continue
        acceptance = htn.get_acceptance(contribution.acceptance_id)
        evidence_refs = (
            TypedRef(
                TypedRefKind.SOURCE,
                contribution.acceptance_id,
                1,
                content_hash_of(acceptance.to_json()),
                produced_by=Provenance.TOOL,
            ),
            TypedRef(
                TypedRefKind.REVIEW,
                str(official.record_id),
                1,
                content_hash_of(official.to_json()),
                produced_by=Provenance.TOOL,
            ),
            *(
                TypedRef(
                    TypedRefKind.TOOL_RECEIPT,
                    ref.id,
                    ref.revision,
                    ref.content_hash,
                    produced_by=Provenance.TOOL,
                )
                for ref in outcome.source_receipt_refs
            ),
        )
        matched.append(
            {
                "acceptance_id": contribution.acceptance_id,
                "occurrence_id": owner.occurrence_id,
                "effect_key": effect_key,
                "criterion_ids": effect.criterion_ids,
                "outcome_binding": outcome.to_json(),
                "observations": observations,
                "evidence_refs": evidence_refs,
            }
        )
    if len(matched) != 1:
        return False
    if proofs is not None:
        proofs.extend(matched)
    return True


def current_effect_proofs(store: Store, mission_id: str) -> tuple[dict[str, Any], ...]:
    """Read current effect evidence for the root reviewer and final commit alike."""
    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        requirements = htn.latest_requirements_revision(mission_id)
        if active is None or requirements is None:
            return ()
        reader = OperationCompletionReader(store)
        spec = reader.read_requirements(
            mission_id,
            TypedRef(
                TypedRefKind.REQUIREMENTS,
                str(requirements.revision_id),
                requirements.revision,
                requirements.content_hash(),
            ),
        )
        proofs: list[dict[str, Any]] = []
        for effect in spec.effects:
            _effect_ready(
                store,
                reader,
                OperationCompletionStore(store),
                htn,
                mission_id=mission_id,
                plan_ref=PlanRevisionPinV1(
                    revision=active.revision, snapshot_hash=active.snapshot_hash
                ),
                spec=spec,
                effect_key=effect.effect_key,
                proofs=proofs,
            )
        return tuple(proofs)


def read_occurrence_completion(
    store: Store, mission_id: str, occurrence_id: str
) -> OccurrenceCompletionStatus:
    """Project current completion state from exact immutable producer records."""

    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        if active is None:
            raise StoreError(f"mission {mission_id} has no active plan revision")
        plan_ref = PlanRevisionPinV1(
            revision=active.revision,
            snapshot_hash=active.snapshot_hash,
        )
        reader = OperationCompletionReader(store)
        scope = reader.read_scope(mission_id, plan_ref, occurrence_id)
        spec = reader.read_requirements(
            mission_id,
            TypedRef(
                kind=TypedRefKind.REQUIREMENTS,
                id=scope.requirements_ref.id,
                revision=scope.requirements_ref.revision,
                content_hash=scope.requirements_ref.content_hash,
            ),
        )
        binding = htn.task_semantics_of(mission_id, scope.task_ref.id)
        if binding is None:
            raise StoreError("completion scope Task binding is unavailable")
        completion = OperationCompletionStore(store)
        try:
            if binding.form is TaskForm.PRIMITIVE:
                content_ready, preparation_ready, preparation_ids = _primitive_content(
                    completion, htn, mission_id=mission_id, scope=scope
                )
            else:
                content_ready = _compound_content(htn, mission_id=mission_id, scope=scope)
                preparation_ready = False
                preparation_ids = ()
            effects_ready = all(
                _effect_ready(
                    store,
                    reader,
                    completion,
                    htn,
                    mission_id=mission_id,
                    plan_ref=plan_ref,
                    spec=spec,
                    effect_key=effect_key,
                )
                for effect_key in scope.required_effect_keys
            )
        except (
            ContractError,
            KeyError,
            sqlite3.Error,
            StoreConflict,
            StoreError,
            TypeError,
            ValueError,
        ):
            preparation_ids = ()
            content_ready = False
            preparation_ready = False
            effects_ready = False
        return OccurrenceCompletionStatus(
            scope=scope,
            content_ready=content_ready,
            effects_ready=effects_ready,
            complete=content_ready and effects_ready,
            preparation_ready=preparation_ready,
            preparation_acceptance_ids=preparation_ids,
        )


__all__ = ("OccurrenceCompletionStatus", "read_occurrence_completion")
