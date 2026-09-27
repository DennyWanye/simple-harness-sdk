# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""T3: persisted execution facts -> independent review -> atomic effect acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import ContractError
from ..contracts.operation_completion import (
    CompletionPinV1,
    OperationOutcomeReviewBindingV1,
    PlanRevisionPinV1,
    derive_outcome_binding_id,
)
from ..contracts.resolution import (
    AllExpr,
    CheckExecution,
    CriterionExpr,
    CriterionOutcome,
    CriterionVerdict,
    DeliveryReceipt,
    DeliveryStage,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
)
from ..contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from ..runtime.connectors import Receipt, params_hash
from ..runtime.operation_outcomes import resolve_receipt_adapter
from ..runtime.operation_payloads import require_connector_profile
from ..runtime.operation_ref_resolver import OperationReferenceResolver
from ..storage.htn_store import HtnStore
from ..storage.operation_completion_store import OperationCompletionStore
from ..storage.operation_intent_store import OperationIntentStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.store import Store, StoreError
from ..verification.critics import parse_critic_verdict
from .completion_status import _owner_scope
from .operation_completion import OperationCompletionReader


class OperationOutcomeError(ContractError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _effect_owner(store: Store, mission_id: str, effect_key: str):
    htn = HtnStore(store)
    active = htn.active_plan_revision(mission_id)
    requirements = htn.latest_requirements_revision(mission_id)
    if active is None or requirements is None:
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "no current Plan/Requirements")
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
    owner = _owner_scope(
        reader,
        htn,
        mission_id=mission_id,
        plan_ref=PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash),
        effect_key=effect_key,
        spec_hash=spec.content_hash(),
    )
    if owner is None:
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "no unique current owner")
    return owner, spec, requirements


def _execution_sources(store: Store, intent_id: str, profiles: Any):
    row = OperationIntentStore(store).get(intent_id)
    materialized = store.get_receipt("materialize:" + intent_id)
    if row is None or materialized is None:
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "no T0/T1 source")
    action = store.get_action(materialized["action_key"])
    link = PlanningAdmissionStore(store).get_operation_action_link(materialized["operation_id"])
    if (
        action is None
        or link is None
        or action["state"] != "SUCCEEDED"
        or not action.get("receipt")
    ):
        raise OperationOutcomeError("OP_OUTCOME_PENDING")
    if any(
        link.get(key) != value
        for key, value in {
            "mission_id": row["mission_id"],
            "action_key": action["action_key"],
            "action_version": action["version"],
            "link_hash": materialized["link_hash"],
            "operation_occurrence_id": materialized["operation_occurrence_id"],
            "envelope_hash": materialized["envelope_hash"],
            "params_hash": row["params_hash"],
            "idempotency_key": action["idempotency_key"],
        }.items()
    ):
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "T1/action link differs")
    resolved = OperationReferenceResolver(store, profiles).resolve_historical(action, link)
    parameters = resolved.parameters
    if (
        action["connector"],
        action["operation"],
        action["target"],
        action["params"],
        params_hash(action["params"]),
    ) != (
        parameters.connector_id,
        parameters.operation_name,
        parameters.normalized_target_ref,
        dict(parameters.effective_params),
        parameters.params_hash,
    ):
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "action parameters differ")
    return row, materialized, action, resolved


@dataclass(frozen=True, slots=True)
class OutcomeReviewPreparation:
    binding_id: str
    binding: OperationOutcomeReviewBindingV1
    package: ReviewPackage
    manifest: dict[str, Any]
    observation: dict[str, Any]
    request_content: dict[str, Any]
    action_snapshot_hash: str


def prepare_operation_outcome_review(
    store: Store, *, intent_id: str, connectors: Any, profiles: Any
) -> OutcomeReviewPreparation:
    """Readback outside any write transaction; no connector execution or invented receipts."""
    if store._depth:
        raise OperationOutcomeError("OP_OUTCOME_PREPARE_IN_TRANSACTION")
    with store.read_view():
        row, materialized, action, resolved = _execution_sources(store, intent_id, profiles)
        owner, spec, requirements = _effect_owner(
            store, row["mission_id"], resolved.effect_contract.effect_key
        )
        original = OperationCompletionStore(store).get_scope_exact(
            row["mission_id"],
            resolved.proposal.plan_revision,
            resolved.proposal.completion_owner_occurrence_id,
        )
        if (
            original is None
            or original["document"].task_ref != owner.task_ref
            or owner.obligation_id != resolved.envelope.obligation_id
        ):
            raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "executed owner differs")
        slot = spec.effect(resolved.effect_contract.effect_key)
        policy_registry = (profiles.policy_registry(action["connector"], action["operation"])
                           if callable(getattr(profiles, "policy_registry", None)) else profiles)
        if (slot.milestone_policy_ref != policy_registry.milestone_policy_ref
            or slot.evidence_policy_ref != policy_registry.evidence_policy_ref):
            raise OperationOutcomeError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "policy differs")
        profile = require_connector_profile(
            profiles,
            connector_id=action["connector"],
            operation_name=action["operation"],
            expected_hash=resolved.parameters.connector_profile_hash,
        )
        events = tuple(store.iter_events(row["mission_id"]))
        handoffs = tuple(
            e
            for e in events
            if e.type == "ActionHandedOff" and e.payload.get("action_key") == action["action_key"]
        )
        receipt = Receipt.from_json(action["receipt"])
        outcomes = tuple(
            e
            for e in events
            if e.type in {"ActionSucceeded", "ActionReconciled"}
            and e.payload.get("action_key") == action["action_key"]
            and e.payload.get("state") == "SUCCEEDED"
            and e.payload.get("receipt_hash") == receipt.receipt_hash
        )
        if len(outcomes) != 1:
            raise OperationOutcomeError(
                "OP_OUTCOME_SOURCE_UNAVAILABLE", "no unique runtime receipt event"
            )
        attempt = store.get_attempt(row["source_attempt_id"])
        if attempt is None or not attempt.agent_id:
            raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "producer missing")
    observed = resolve_receipt_adapter(profile, connectors.get(action["connector"]), profiles=profiles).interpret(
        receipt,
        action=action,
        parameters=resolved.parameters,
        handoff_events=handoffs,
        required_milestone=slot.required_milestone,
    )
    if (observed.milestone != slot.required_milestone
            or len(set(observed.covered_handoff_ids)) != len(handoffs)
            or set(observed.covered_handoff_ids) != {str(event.id) for event in handoffs}):
        raise OperationOutcomeError("OP_OUTCOME_MILESTONE_UNVERIFIABLE", "adapter milestone or coverage differs")
    observation = {
        "kind": "operation_outcome_observation",
        "mission_id": row["mission_id"],
        "intent_id": intent_id,
        "action_key": action["action_key"],
        "receipt": action["receipt"],
        "runtime_event": outcomes[0].to_json(),
        "handoffs": [e.to_json() for e in handoffs],
        "adapter": dict(profile.receipt_adapter),
        "profile_hash": profile.content_hash(),
        "milestone": observed.milestone,
        "facts": dict(observed.source_facts),
    }
    source_hash = content_hash_of(observation)
    source_ref = CompletionPinV1(id="op-observation-" + source_hash[:32], revision=1, content_hash=source_hash)
    manifest = {
        "source_receipt_refs": [source_ref.to_json()],
        "covered_handoff_ids": sorted(observed.covered_handoff_ids),
        "receipt_adapter": dict(profile.receipt_adapter),
        "milestone_policy_ref": slot.milestone_policy_ref.to_json(),
    }
    manifest_hash = content_hash_of(manifest)
    binding_id = derive_outcome_binding_id(
        intent_id=intent_id,
        spec_hash=spec.content_hash(),
        effect_key=slot.effect_key,
        completion_scope_hash=owner.content_hash(),
        source_manifest_hash=manifest_hash,
    )
    binding = OperationOutcomeReviewBindingV1(
        schema_version=1,
        mission_id=row["mission_id"],
        spec_hash=spec.content_hash(),
        effect_key=slot.effect_key,
        completion_scope_id=owner.scope_id,
        intent_id=intent_id,
        operation_id=materialized["operation_id"],
        operation_occurrence_id=materialized["operation_occurrence_id"],
        action_key=action["action_key"],
        action_version=action["version"],
        request_hash=row["request_hash"],
        candidate_file_hash=row["candidate_file_hash"],
        parameters_content_hash=row["parameters_content_hash"],
        params_hash=row["params_hash"],
        effect_contract_hash=row["effect_content_hash"],
        link_hash=materialized["link_hash"],
        connector_profile_hash=profile.content_hash(),
        namespace_hash=content_hash_of(dict(profile.namespace)),
        target_identity_hash=content_hash_of(
            {
                "normalized_target_ref": action["target"],
                "expected_target_version": resolved.parameters.expected_target_version,
            }
        ),
        milestone_policy_ref=slot.milestone_policy_ref,
        observed_milestone=observed.milestone,
        covered_handoff_ids=tuple(sorted(observed.covered_handoff_ids)),
        source_receipt_refs=(source_ref,),
    )
    criteria = tuple(c for c in requirements.criteria if c.criterion_id in slot.criterion_ids)
    if len(criteria) != len(slot.criterion_ids):
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "criterion catalogue differs")
    checked = set(observed.source_facts["executed_check_ids"])
    if any(
        not set(c.required_evidence_policy.required_check_ids).issubset(checked) for c in criteria
    ):
        raise OperationOutcomeError(
            "OP_OUTCOME_MILESTONE_UNVERIFIABLE", "required checker is not registered"
        )
    package = ReviewPackage(
        package_id=ReviewPackageId("pkg-" + binding_id),
        purpose=ReviewPurpose.OPERATION_OUTCOME,
        binding=ReviewBinding(
            mission_id=row["mission_id"],
            obligation_id=owner.obligation_id,
            subject_ref=TypedRef(
                TypedRefKind.OPERATION, binding.operation_id, 1, resolved.envelope.content_hash()
            ),
            requirements_revision=requirements.revision,
            input_manifest_hash=manifest_hash,
            policy_ref=TypedRef(
                TypedRefKind.SOURCE,
                slot.evidence_policy_ref.id,
                slot.evidence_policy_ref.revision,
                slot.evidence_policy_ref.content_hash,
            ),
        ),
        criteria=criteria,
        success_expression=AllExpr(tuple(CriterionExpr(c.criterion_id) for c in criteria)),
        candidate_refs=(
            TypedRef(
                TypedRefKind.TOOL_RECEIPT,
                binding_id,
                1,
                binding.content_hash(),
                produced_by=Provenance.TOOL,
            ),
        ),
        producer_agent_ids=(attempt.agent_id,),
        reviewer_workspace_access=WorkspaceAccess.READ_ONLY,
        requirements_content_hash=requirements.content_hash(),
    )
    request = {
        "package": package.to_json(),
        "outcome_binding": binding.to_json(),
        "observation": observation,
        "parameters": resolved.parameters.to_json(),
        "effect_contract": resolved.effect_contract.to_json(),
    }
    return OutcomeReviewPreparation(
        binding_id, binding, package, manifest, observation, request, content_hash_of(action)
    )


def persist_operation_outcome_review(
    commit: Any, prepared: OutcomeReviewPreparation, *, runtime: Any
) -> None:
    """Caller includes this in the same Store transaction as dispatch/budget reservation."""
    store = commit.store
    if store._reading or not store._depth:
        raise OperationOutcomeError("OP_OUTCOME_TRANSACTION_REQUIRED")
    binding = prepared.binding
    row, _, action, _ = _execution_sources(store, binding.intent_id, runtime.profiles)
    owner, spec, _ = _effect_owner(store, row["mission_id"], binding.effect_key)
    if (
        content_hash_of(action) != prepared.action_snapshot_hash
        or owner.scope_id != binding.completion_scope_id
        or spec.content_hash() != binding.spec_hash
    ):
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "sources changed during readback")
    producer = {
        "kind": "operation_outcome_review_prepared",
        "mission_id": binding.mission_id,
        "subject_id": prepared.binding_id,
        "binding_hash": binding.content_hash(),
        "source_manifest_hash": content_hash_of(prepared.manifest),
        "review_package_id": str(prepared.package.package_id),
    }
    for receipt_id, body in (
        (binding.source_receipt_refs[0].id, prepared.observation),
        ("prepare:" + prepared.binding_id, producer),
    ):
        existing = store.get_receipt(receipt_id)
        if existing is None:
            store.insert_receipt(
                commit_id=receipt_id,
                kind=body["kind"],
                subject_id=prepared.binding_id,
                base_version=owner.requirements_ref.revision,
                proposal_hash=content_hash_of(body),
                receipt=body,
            )
        elif existing != body:
            raise OperationOutcomeError(
                "OP_OUTCOME_SOURCE_UNAVAILABLE", "immutable receipt conflict"
            )
    htn = HtnStore(store)
    htn.insert_input_manifest(
        binding.mission_id, owner.task_ref.id, prepared.manifest, request_id=binding.intent_id
    )
    try:
        existing_package = htn.get_review_package(str(prepared.package.package_id))
    except StoreError:
        htn.insert_review_package(prepared.package)
    else:
        if existing_package != prepared.package:
            raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH")
    OperationCompletionStore(store).insert_outcome_binding(
        prepared.binding_id,
        binding,
        source_manifest_hash=content_hash_of(prepared.manifest),
        review_package_id=str(prepared.package.package_id),
        producer_receipt_id="prepare:" + prepared.binding_id,
    )
    commit._emit(
        "OperationOutcomeReviewPrepared",
        binding.mission_id,
        key=prepared.binding_id,
        payload=producer,
    )


def record_operation_outcome_review(
    store: Store, *, mission_id: str, binding_id: str, dispatch: Any, turn_id: str, text: str
) -> ReviewRecord:
    row = OperationCompletionStore(store).get_outcome_binding_exact(mission_id, binding_id)
    if row is None:
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE")
    htn = HtnStore(store)
    package = htn.get_review_package(row["review_package_id"])
    actual = store.get_intent(dispatch.intent_id)
    existing = htn.official_review_record(str(package.package_id))
    if (
        actual is None
        or (actual.state != "SUBMITTED" and existing is None)
        or actual.agent_id != dispatch.agent_id
        or actual.expected_turn_id != turn_id
        or actual.config.get("role") != "operation_outcome_reviewer"
        or actual.config.get("outcome_binding_id") != binding_id
        or actual.config.get("review_package_id") != str(package.package_id)
        or actual.mission_id != mission_id
    ):
        raise OperationOutcomeError("OP_REVIEW_NOT_OFFICIAL", "runtime review identity differs")
    if not actual.agent_id or package.produced_by(actual.agent_id):
        raise OperationOutcomeError("OP_REVIEWER_NOT_INDEPENDENT")
    verdict = parse_critic_verdict(
        text, expected_criteria=[c.criterion_id for c in package.criteria]
    )
    refs = tuple(
        TypedRef(
            TypedRefKind.TOOL_RECEIPT,
            ref.id,
            ref.revision,
            ref.content_hash,
            produced_by=Provenance.TOOL,
        )
        for ref in row["document"].source_receipt_refs
    )
    for ref in refs:
        receipt = store.get_receipt(ref.id)
        if receipt is None or content_hash_of(receipt) != ref.content_hash:
            raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE")
    outcomes = tuple(
        CriterionOutcome(
            criterion_id=item["criterion"],
            verdict=CriterionVerdict.PASS if item["met"] else CriterionVerdict.FAIL,
            check_execution=CheckExecution.SUCCEEDED,
            evidence_refs=refs,
        )
        for item in verdict.mission_criteria
    )
    record = ReviewRecord(
        ReviewRecordId("review:" + binding_id),
        package.package_id,
        ReviewPurpose.OPERATION_OUTCOME,
        package.binding,
        actual.agent_id,
        turn_id,
        content_hash_of({"critic": verdict.to_json(), "dispatch": dispatch.intent_id}),
        outcomes,
        ReviewVerdict.ACCEPT
        if verdict.passed
        and not verdict.needs_human
        and all(c.verdict is CriterionVerdict.PASS for c in outcomes)
        else ReviewVerdict.REJECTED,
    )
    existing = htn.official_review_record(str(package.package_id))
    if existing is None:
        htn.insert_review_record(record, official=True)
    elif existing != record:
        raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH", "official review conflict")
    return record


@dataclass(frozen=True, slots=True)
class ScopedOutcomeProjection:
    scope: Any
    spec: Any
    criteria: tuple[Any, ...]
    expression: Any
    binding_id: str
    effect_key: str
    delivery_receipt: DeliveryReceipt
    artifacts: tuple[Any, ...] = ()


def validate_scoped_outcome_command(
    store: Store, command: Any, *, runtime: Any
) -> ScopedOutcomeProjection:
    if runtime is None:
        raise OperationOutcomeError("OP_SERVICE_CALLER_REQUIRED")
    binding_id = str(command.source.get("outcome_binding_id", ""))
    row = OperationCompletionStore(store).get_outcome_binding_exact(command.mission_id, binding_id)
    if row is None or row["review_package_id"] != str(command.package.package_id):
        raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH")
    binding = row["document"]
    source, materialized, action, resolved = _execution_sources(store, binding.intent_id, runtime.profiles)
    profile = require_connector_profile(runtime.profiles, connector_id=action["connector"],
        operation_name=action["operation"], expected_hash=resolved.parameters.connector_profile_hash)
    frozen_identity = {
        "operation_id": materialized["operation_id"],
        "operation_occurrence_id": materialized["operation_occurrence_id"],
        "action_key": action["action_key"], "action_version": action["version"],
        "request_hash": source["request_hash"], "candidate_file_hash": source["candidate_file_hash"],
        "parameters_content_hash": source["parameters_content_hash"], "params_hash": source["params_hash"],
        "effect_contract_hash": source["effect_content_hash"], "link_hash": materialized["link_hash"],
        "connector_profile_hash": profile.content_hash(), "namespace_hash": content_hash_of(dict(profile.namespace)),
        "target_identity_hash": content_hash_of({"normalized_target_ref": action["target"],
            "expected_target_version": resolved.parameters.expected_target_version}),
    }
    if any(getattr(binding, key) != value for key, value in frozen_identity.items()):
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "frozen execution chain differs")
    owner, spec, requirements = _effect_owner(store, command.mission_id, binding.effect_key)
    slot = spec.effect(binding.effect_key)
    if (
        (owner.scope_id, owner.task_ref.id, spec.content_hash(), requirements.content_hash())
        != (
            binding.completion_scope_id,
            command.task_id,
            binding.spec_hash,
            command.requirements.content_hash(),
        )
        or binding.observed_milestone != slot.required_milestone
        or binding.milestone_policy_ref != slot.milestone_policy_ref
    ):
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE")
    criteria = tuple(c for c in requirements.criteria if c.criterion_id in slot.criterion_ids)
    expression = AllExpr(tuple(CriterionExpr(c.criterion_id) for c in criteria))
    if (
        command.package.criteria != criteria
        or command.package.success_expression != expression
        or command.artifact_refs
        or command.outputs
    ):
        raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH", "effect scope differs")
    policy = command.package.binding.policy_ref
    if (policy.id, policy.revision, policy.content_hash) != (
        slot.evidence_policy_ref.id,
        slot.evidence_policy_ref.revision,
        slot.evidence_policy_ref.content_hash,
    ):
        raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH", "evidence policy differs")
    for ref in binding.source_receipt_refs:
        receipt = store.get_receipt(ref.id)
        if (
            receipt is None
            or content_hash_of(receipt) != ref.content_hash
            or receipt.get("intent_id") != binding.intent_id
        ):
            raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE")
        events = {event.id: event for event in store.iter_events(command.mission_id)}
        original = receipt.get("runtime_event", {})
        event = events.get(original.get("id"))
        handoffs = tuple(event for event in events.values() if event.type == "ActionHandedOff"
            and event.payload.get("action_key") == action["action_key"])
        facts = receipt.get("facts", {})
        if (receipt.get("kind") != "operation_outcome_observation"
            or receipt.get("mission_id") != command.mission_id
            or receipt.get("action_key") != action["action_key"]
            or receipt.get("receipt") != action["receipt"]
            or receipt.get("profile_hash") != profile.content_hash()
            or receipt.get("adapter") != dict(profile.receipt_adapter)
            or receipt.get("milestone") != binding.observed_milestone
            or event is None or event.to_json() != original
            or event.type not in {"ActionSucceeded", "ActionReconciled"}
            or event.payload.get("state") != "SUCCEEDED"
            or event.payload.get("receipt_hash") != Receipt.from_json(action["receipt"]).receipt_hash
            or receipt.get("handoffs") != [event.to_json() for event in handoffs]
            or tuple(sorted(event.id for event in handoffs)) != binding.covered_handoff_ids
            or len(handoffs) != action["handoffs"]
            or facts.get("receipt") != action["receipt"]
            or facts.get("content_hash") != resolved.parameters.effective_params.get("content_hash")
            or facts.get("bytes") != resolved.parameters.effective_params.get("size")
            or any(not set(c.required_evidence_policy.required_check_ids).issubset(
                facts.get("executed_check_ids", ())) for c in criteria)):
            raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE", "original runtime evidence differs")
    manifest = {"source_receipt_refs": [ref.to_json() for ref in binding.source_receipt_refs],
        "covered_handoff_ids": list(binding.covered_handoff_ids),
        "receipt_adapter": dict(profile.receipt_adapter), "milestone_policy_ref": slot.milestone_policy_ref.to_json()}
    producer = store.get_receipt(str(row["producer_receipt_id"]))
    if (content_hash_of(manifest) != row["source_manifest_hash"]
        or command.package.binding.input_manifest_hash != row["source_manifest_hash"]
        or command.package.binding.subject_ref != TypedRef(TypedRefKind.OPERATION,
            binding.operation_id, 1, resolved.envelope.content_hash())
        or command.package.candidate_refs != (TypedRef(TypedRefKind.TOOL_RECEIPT,
            binding_id, 1, binding.content_hash(), produced_by=Provenance.TOOL),)
        or producer is None or producer.get("kind") != "operation_outcome_review_prepared"
        or producer.get("mission_id") != command.mission_id
        or producer.get("subject_id") != binding_id
        or producer.get("binding_hash") != binding.content_hash()):
        raise OperationOutcomeError("OP_REVIEW_BINDING_MISMATCH", "outcome producer differs")
    if any(
        item.subject_id in {command.task_id, binding.operation_id, binding_id}
        for item in HtnStore(store).list_dirty(command.mission_id)
    ):
        raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "effect source is dirty")
    action = store.get_action(binding.action_key)
    if (
        action is None
        or action["state"] != "SUCCEEDED"
        or action["params_hash"] != binding.params_hash
    ):
        raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE")
    if binding.observed_milestone not in {"FILE_PUBLISHED", "CONTENT_HASH_VERIFIED"}:
        raise OperationOutcomeError(
            "OP_OUTCOME_MILESTONE_UNVERIFIABLE", "delivery mapping is unregistered"
        )
    delivery = DeliveryReceipt(
        "delivery:" + binding_id,
        command.mission_id,
        command.acceptance_id,
        DeliveryStage.PERSISTED,
        command.accepted_at_ms,
        binding.operation_id,
        tuple(
            TypedRef(
                TypedRefKind.TOOL_RECEIPT,
                ref.id,
                ref.revision,
                ref.content_hash,
                produced_by=Provenance.TOOL,
            )
            for ref in binding.source_receipt_refs
        ),
    )
    return ScopedOutcomeProjection(
        owner, spec, criteria, expression, binding_id, binding.effect_key, delivery
    )


def accept_operation_outcome(
    commit: Any, *, mission_id: str, binding_id: str, service_authority: object
) -> Any:
    """Consume an official outcome review and finish eligible owner work atomically."""
    runtime = getattr(commit, "_operation_materialization_runtime", None)
    if runtime is None or service_authority is not runtime.service_authority:
        raise OperationOutcomeError("OP_SERVICE_CALLER_REQUIRED")
    candidate = _assured_outcome_use(commit, mission_id, binding_id)
    try:
        return _accept_operation_outcome(commit, mission_id, binding_id, candidate)
    finally:
        if candidate is not None:
            commit._assurance_validity.forget(mission_id, str(candidate.record.record_id))


def _assured_outcome_use(commit: Any, mission_id: str, binding_id: str) -> Any:
    """An assured Mission's outcome acceptance is licensed by a current UseCertificate
    prepared here, outside the write lock, and committed by ``accept_review`` beside
    the Acceptance — never by the legacy self-issued witness."""
    from ..storage.assurance_store import AssuranceStore

    store = commit.store
    try:
        if AssuranceStore(store).lane(mission_id) != "ASSURANCE_1_1":
            return None
    except Exception:  # noqa: BLE001 - a Mission without a lane row is not assured
        return None
    row = OperationCompletionStore(store).get_outcome_binding_exact(mission_id, binding_id)
    record = (
        None if row is None else HtnStore(store).official_review_record(row["review_package_id"])
    )
    validity = getattr(commit, "_assurance_validity", None)
    if record is None or record.verdict is not ReviewVerdict.ACCEPT or validity is None:
        raise OperationOutcomeError("OP_REVIEW_NOT_OFFICIAL", "no licensable outcome review")
    return validity.prepare_outcome_use(record, acceptance_id="acc-" + binding_id)


def _accept_operation_outcome(commit: Any, mission_id: str, binding_id: str, candidate: Any) -> Any:
    from ..contracts import TaskStatus
    from ..contracts.htn import TaskForm
    from ..memory.summaries import refresh_summaries
    from ..verification.acceptance_rules import ExecutionPosture, IndependenceFacts
    from .completion_status import read_occurrence_completion
    from .leaf_acceptance import LeafAcceptanceAssembly
    from .resolution_commits import AcceptReviewCommand, ResolutionPrincipal
    from .state_machine import next_task

    store = commit.store
    with store.transaction():
        htn = HtnStore(store)
        row = OperationCompletionStore(store).get_outcome_binding_exact(mission_id, binding_id)
        if row is None:
            raise OperationOutcomeError("OP_OUTCOME_SOURCE_UNAVAILABLE")
        binding = row["document"]
        owner, _, requirements = _effect_owner(store, mission_id, binding.effect_key)
        package = htn.get_review_package(row["review_package_id"])
        record = htn.official_review_record(str(package.package_id))
        if record is None or record.verdict is not ReviewVerdict.ACCEPT:
            raise OperationOutcomeError("OP_REVIEW_NOT_OFFICIAL", "no accepted outcome review")
        semantics = htn.task_semantics_of(mission_id, owner.task_ref.id)
        task = store.get_task(owner.task_ref.id)
        if (
            semantics is None
            or task is None
            or task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}
        ):
            raise OperationOutcomeError("OP_EFFECT_SCOPE_STALE", "owner unavailable")
        acceptance_id = "acc-" + binding_id
        try:
            previous = htn.get_acceptance(acceptance_id)
        except StoreError:
            now_ms = int(store.now * 1000)
        else:
            now_ms = previous.accepted_at_ms
        issuer = "operation-outcome-acceptor"
        anchors = LeafAcceptanceAssembly(store, commit, reviewer_agent_id=issuer)
        if candidate is not None and candidate.record != record:
            raise OperationOutcomeError(
                "OP_REVIEW_NOT_OFFICIAL", "use certificate names another record"
            )
        witness_id = (
            candidate.certificate_id
            if candidate is not None
            else anchors._witness(
                mission_id, task.id, acceptance_id=acceptance_id, now_ms=now_ms
            ).witness_id
        )
        command = AcceptReviewCommand(
            command_id="accept:" + binding_id,
            mission_id=mission_id,
            task_id=task.id,
            obligation_id=owner.obligation_id,
            acceptance_id=acceptance_id,
            package=package,
            record=record,
            requirements=requirements,
            witness_id=witness_id,
            independence=IndependenceFacts(
                producer_agent_ids=package.producer_agent_ids, reviewer_can_write_candidate=False
            ),
            posture=ExecutionPosture(cancellation_requested=task.status is TaskStatus.CANCELLED),
            read_set=anchors._read_set(mission_id, semantics, requirements),
            accepted_at_ms=now_ms,
            purpose=ReviewPurpose.OPERATION_OUTCOME,
            policy_ref=package.binding.policy_ref.id,
            issued_by=issuer,
            source={"outcome_binding_id": binding_id},
        )
        receipt = commit.accept_review(
            command, ResolutionPrincipal(issuer, manager_epoch=htn.epoch(mission_id, "mission"))
        )
        completion = read_occurrence_completion(store, mission_id, owner.occurrence_id)
        if (
            semantics.form is TaskForm.PRIMITIVE
            and completion.complete
            and task.status is TaskStatus.VERIFYING
        ):
            completed = next_task(task, TaskStatus.COMPLETED)
            store.update_task(completed, expected_version=task.version)
            unblocked = commit._unblock(mission_id, unblocked_by=task.id)
            refresh_summaries(store, mission_id)
            commit._emit(
                "TaskCompleted",
                mission_id,
                key=task.id,
                task_id=task.id,
                payload={
                    "result_id": task.accepted_result_id,
                    "artifacts": list(task.accepted_artifacts),
                    "outcome_binding_id": binding_id,
                    "superseded": [],
                    "unblocked": [item.id for item in unblocked],
                },
            )
        commit._emit(
            "OperationOutcomeAccepted",
            mission_id,
            key=binding_id,
            task_id=task.id,
            payload={
                "outcome_binding_id": binding_id,
                "acceptance_id": acceptance_id,
                "effect_key": binding.effect_key,
            },
        )
        return receipt
