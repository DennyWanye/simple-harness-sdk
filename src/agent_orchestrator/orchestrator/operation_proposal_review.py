# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""D3 coordinator for an independently reviewed frozen operation proposal.

This module owns the review anchor and the judgement over it.  It deliberately
has no submission, operation-intent, payload-object, or AgentBridge write path:
the caller persists the returned frozen documents in its encompassing T0
transaction and dispatches the resulting review request through its own bridge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..contracts import ContractError
from ..contracts.operation_payloads import (
    FrozenActionProposalV2,
)
from ..contracts.resolution import (
    AllExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    RequiredEvidencePolicy,
    RequirementClass,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
    account_for_purpose,
)
from ..contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
    hash_hex,
    identifier,
)
from ..governance.policies import DeploymentPolicy
from ..planning.htn.grounding import derive_id
from ..runtime.operation_payloads import ConnectorProfileRegistry
from ..storage.htn_store import HtnStore
from ..storage.store import Store, StoreError
from ..verification.critics import CriticVerdict, parse_critic_verdict
from .operation_intent_sources import PreparedOperationIntentSources
from .operation_materialization_inputs import (
    DeploymentOperationPolicyInputs,
    FrozenOperationPayloadInputs,
    OperationMaterializationInputError,
    OperationPayloadObjectIds,
    freeze_operation_payloads,
)

ACTION_PROPOSAL_REVIEW_POLICY = "action-proposal-review-v1"
ACTION_PROPOSAL_CRITERIA = (
    "operation-intent-scope",
    "operation-parameters-and-candidate",
    "operation-effect-capability",
    "operation-write-safety",
)


class ActionProposalReviewError(ContractError):
    """A frozen proposal cannot be made into a trustworthy review anchor."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ActionProposalCheckDocument:
    """A deterministic checker result that T0 must persist before reviewer dispatch.

    ``passed`` is not caller supplied: instances are only constructed by
    :func:`build_action_proposal_review` after re-freezing the payloads from the
    authoritative sources and current registry/deployment facts.
    """

    criterion_id: str
    checker: str
    input_hash: str
    result_hash: str
    passed: bool
    facts: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", identifier(self.criterion_id, "check.criterion_id")
        )
        object.__setattr__(self, "checker", identifier(self.checker, "check.checker"))
        object.__setattr__(self, "input_hash", hash_hex(self.input_hash, "check.input_hash"))
        object.__setattr__(self, "result_hash", hash_hex(self.result_hash, "check.result_hash"))
        if not isinstance(self.passed, bool):
            raise ActionProposalReviewError("OP_REVIEW_CHECK_INVALID", "passed must be boolean")
        if not isinstance(self.facts, Mapping):
            raise ActionProposalReviewError("OP_REVIEW_CHECK_INVALID", "facts must be an object")
        object.__setattr__(self, "facts", dict(self.facts))

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "action_proposal_review_check",
            "criterion_id": self.criterion_id,
            "checker": self.checker,
            "input_hash": self.input_hash,
            "result_hash": self.result_hash,
            "passed": self.passed,
            "facts": dict(self.facts),
        }

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())


@dataclass(frozen=True, slots=True)
class ActionProposalReviewDraft:
    """Everything the transaction owner needs to persist and dispatch a review."""

    package: ReviewPackage
    request_content: Mapping[str, Any]
    input_manifest: Mapping[str, Any]
    checks: tuple[ActionProposalCheckDocument, ...]
    check_receipt_refs: tuple[TypedRef, ...]

    @property
    def account(self):
        return account_for_purpose(self.package.purpose)


@dataclass(frozen=True, slots=True)
class ActionProposalReviewReceipt:
    """Typed result of recording the exact Critic reply against a frozen package."""

    record: ReviewRecord
    critic_verdict: CriticVerdict


def _policy_ref() -> TypedRef:
    return TypedRef(
        kind=TypedRefKind.SOURCE,
        id=ACTION_PROPOSAL_REVIEW_POLICY,
        revision=1,
        content_hash=content_hash_of(ACTION_PROPOSAL_REVIEW_POLICY),
    )


def _criteria() -> tuple[Criterion, ...]:
    """The four points the proposal reviewer judges over the frozen facts.

    SEMANTIC (NEXT-TG-1.0, 2026-09-27, independent ruling "方案 C"): ``_checks`` only
    restates facts that the freeze (``freeze_operation_payloads`` +
    ``_assert_frozen_payload_identity``), the materialization inputs
    (``build_operation_materialization_inputs``) and
    ``validate_materialization_review`` already *enforce* in code and refuse on any
    mismatch; they were never run by an independent checker.  Declaring them as
    registered checks made every assured publish unapprovable
    (``CHECK_POLICY_UNRESOLVED: ACTION_PROPOSAL``) and overstated what the record
    proves.  The four check receipts are still persisted and re-verified before
    materialization; only the reviewer's part is a judgement.
    """
    statements = {
        "operation-intent-scope": "The frozen proposal matches the approved operation intent and scope.",
        "operation-parameters-and-candidate": (
            "The frozen parameters were derived from the exact candidate bytes, and the "
            "artifact they will publish is an artifact of the same accepted inputs, with "
            "the same id and content hash.  The candidate file is the operation's "
            "description; the published content is that accepted artifact, so the two "
            "are expected to be different files."
        ),
        "operation-effect-capability": "The current registered adapter profile supports the approved effect milestone.",
        "operation-write-safety": "The registered operation supports the approved conditional-write safety policy.",
    }
    return tuple(
        Criterion(
            criterion_id=criterion_id,
            revision=1,
            origin=CriterionOrigin.POLICY_REQUIRED,
            statement=statements[criterion_id],
            requirement_class=RequirementClass.HARD_CONSTRAINT,
            evaluation_kind=EvaluationKind.SEMANTIC,
            required_evidence_policy=RequiredEvidencePolicy(),
        )
        for criterion_id in ACTION_PROPOSAL_CRITERIA
    )


def _payload_object_ids(payloads: FrozenOperationPayloadInputs) -> OperationPayloadObjectIds:
    return OperationPayloadObjectIds(
        intent_id=payloads.action_proposal.intent_id,
        parameters_object_id=payloads.parameters_ref.id,
        effect_object_id=payloads.effect_contract_ref.id,
        proposal_object_id=payloads.action_proposal_ref.id,
    )


def _assert_frozen_payload_identity(
    payloads: FrozenOperationPayloadInputs, recomputed: FrozenOperationPayloadInputs
) -> None:
    """Refuse if a review would be cut over documents other than D2 rebuilt."""

    pairs = (
        ("parameters", payloads.parameters, recomputed.parameters),
        ("effect_contract", payloads.effect_contract, recomputed.effect_contract),
        ("action_proposal", payloads.action_proposal, recomputed.action_proposal),
        ("parameters_ref", payloads.parameters_ref, recomputed.parameters_ref),
        ("effect_contract_ref", payloads.effect_contract_ref, recomputed.effect_contract_ref),
        ("action_proposal_ref", payloads.action_proposal_ref, recomputed.action_proposal_ref),
    )
    for name, supplied, actual in pairs:
        if supplied != actual:
            raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", name)


def _published_artifact(store: Store, payloads: FrozenOperationPayloadInputs) -> dict[str, Any] | None:
    """The artifact the frozen parameters will publish, verified against the accepted
    inputs (2A upstream run: the reviewer twice judged "params ≠ candidate" because the
    package only named the acceptance, not that the published file belongs to it).

    ``None`` when the operation publishes no artifact.  An ``artifact_id`` that is not
    an accepted artifact of the frozen accepted inputs, or whose hash differs, is a
    refusal here — never a judgement left to the reviewer.
    """
    params = payloads.parameters.effective_params
    artifact_id = params.get("artifact_id")
    if artifact_id is None:
        return None
    from ..storage.htn_store import HtnStore

    htn = HtnStore(store)
    for ref in payloads.parameters.accepted_input_refs:
        acceptance = htn.get_acceptance(str(ref.id))
        for accepted in acceptance.artifact_refs:
            if accepted.id == artifact_id:
                if accepted.content_hash != params.get("content_hash"):
                    raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "published artifact hash differs")
                return {"artifact_id": artifact_id, "artifact_path": params.get("artifact_path"),
                        "content_hash": accepted.content_hash, "accepted_by": str(ref.id),
                        "in_accepted_inputs": True}
    raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "published artifact is not an accepted input")


def _checks(
    sources: PreparedOperationIntentSources,
    payloads: FrozenOperationPayloadInputs,
    *,
    input_hash: str,
    published: Mapping[str, Any] | None = None,
) -> tuple[ActionProposalCheckDocument, ...]:
    proposal = payloads.action_proposal
    checks = (
        (
            "operation-intent-scope",
            "operation-intent-source-v1",
            {
                "intent_id": proposal.intent_id,
                "mission_id": sources.command.mission_id,
                "candidate_artifact_ref": proposal.candidate_artifact_ref.to_json(),
                "requirements_ref": proposal.requirements_ref.to_json(),
                "plan_ref": {
                    "revision": proposal.plan_revision,
                    "snapshot_hash": proposal.plan_snapshot_hash,
                },
                "effect_key": proposal.effect_key,
                "completion_scope_hash": proposal.completion_scope_hash,
            },
        ),
        (
            "operation-parameters-and-candidate",
            "operation-payload-freeze-v2",
            {
                "candidate_file_hash": proposal.candidate_artifact_ref.content_hash,
                "parameters_hash": payloads.parameters.content_hash(),
                "params_hash": payloads.parameters.params_hash,
                "accepted_input_refs": [
                    ref.to_json() for ref in payloads.parameters.accepted_input_refs
                ],
                "raw_candidate_hash": content_hash_of(sources.raw_candidate_bytes.decode("utf-8")),
                # The candidate is the operation's description; this is what it publishes.
                **({} if published is None else {"published_artifact": dict(published)}),
            },
        ),
        (
            "operation-effect-capability",
            "connector-profile-registration-v1",
            {
                "connector_profile_hash": payloads.effect_contract.connector_profile_hash,
                "operation": payloads.effect_contract.operation_name,
                "required_milestone": payloads.effect_contract.required_milestone,
                "effect_key": payloads.effect_contract.effect_key,
            },
        ),
        (
            "operation-write-safety",
            "deployment-conditional-write-v1",
            {
                "required_target_condition": payloads.effect_contract.required_target_condition,
                "reconciliation_policy": str(payloads.effect_contract.reconciliation_policy),
                "idempotency_contract": payloads.effect_contract.idempotency_contract,
            },
        ),
    )
    return tuple(
        ActionProposalCheckDocument(
            criterion_id=criterion_id,
            checker=checker,
            input_hash=input_hash,
            result_hash=content_hash_of(facts),
            passed=True,
            facts=facts,
        )
        for criterion_id, checker, facts in checks
    )


def build_action_proposal_review(
    sources: PreparedOperationIntentSources,
    *,
    payloads: FrozenOperationPayloadInputs,
    package_id: str,
    store: Store,
    connectors: Mapping[str, Any],
    deployment: DeploymentPolicy,
    profile_registry: ConnectorProfileRegistry,
    deployment_policy: DeploymentOperationPolicyInputs,
) -> ActionProposalReviewDraft:
    """Re-run D2 checks and construct the immutable ACTION_PROPOSAL package.

    This is deliberately pure.  The returned manifest and check documents must be
    persisted by the T0 owner with the package and its dispatch intent; this
    function never makes an operation executable.
    """

    if not isinstance(sources, PreparedOperationIntentSources) or not isinstance(
        payloads, FrozenOperationPayloadInputs
    ):
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "typed sources and payloads")
    try:
        recomputed = freeze_operation_payloads(
            sources,
            object_ids=_payload_object_ids(payloads),
            store=store,
            connectors=connectors,
            deployment=deployment,
            profile_registry=profile_registry,
            deployment_policy=deployment_policy,
        )
    except OperationMaterializationInputError as error:
        raise ActionProposalReviewError(error.code, str(error)) from error
    _assert_frozen_payload_identity(payloads, recomputed)

    proposal = payloads.action_proposal
    if not isinstance(proposal, FrozenActionProposalV2):
        raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "ACTION_PROPOSAL v2 is required")
    if proposal.intent_id != payloads.parameters.intent_id:
        raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "intent identity differs")
    if (
        proposal.parameters_ref != payloads.parameters_ref
        or proposal.effect_contract_ref != payloads.effect_contract_ref
    ):
        raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "payload references differ")
    if proposal.candidate_artifact_ref != sources.command.candidate_artifact_ref:
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "candidate artifact differs")
    if proposal.producer_task_ref.id != sources.producer_scope.task_ref.id:
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "producer task differs")
    if proposal.requirements_ref.id != sources.producer_scope.requirements_ref.id:
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "requirements differ")

    manifest: dict[str, Any] = {
        "kind": "action-proposal-review-input-v1",
        "intent_id": proposal.intent_id,
        "package_id": identifier(package_id, "package_id"),
        "candidate_artifact_ref": proposal.candidate_artifact_ref.to_json(),
        "parameters_ref": payloads.parameters_ref.to_json(),
        "effect_contract_ref": payloads.effect_contract_ref.to_json(),
        "action_proposal_ref": payloads.action_proposal_ref.to_json(),
        "prepared_acceptance_refs": [ref.to_json() for ref in proposal.prepared_acceptance_refs],
        "producer_task_ref": proposal.producer_task_ref.to_json(),
        "requirements_ref": proposal.requirements_ref.to_json(),
        "plan_ref": {
            "revision": proposal.plan_revision,
            "snapshot_hash": proposal.plan_snapshot_hash,
        },
        "completion": {
            "spec_hash": proposal.completion_spec_hash,
            "scope_id": proposal.completion_scope_id,
            "scope_hash": proposal.completion_scope_hash,
            "effect_key": proposal.effect_key,
            "owner_occurrence_id": proposal.completion_owner_occurrence_id,
            "milestone": payloads.effect_contract.required_milestone,
        },
        "raw_candidate_hash": content_hash_of(sources.raw_candidate_bytes.decode("utf-8")),
    }
    manifest_hash = content_hash_of(manifest)
    checks = _checks(sources, payloads, input_hash=manifest_hash,
                     published=_published_artifact(store, payloads))
    package = ReviewPackage(
        package_id=ReviewPackageId(package_id),
        purpose=ReviewPurpose.ACTION_PROPOSAL,
        binding=ReviewBinding(
            mission_id=sources.command.mission_id,
            obligation_id=proposal.obligation_id,
            subject_ref=payloads.action_proposal_ref,
            requirements_revision=proposal.requirements_ref.revision,
            input_manifest_hash=manifest_hash,
            policy_ref=_policy_ref(),
        ),
        criteria=_criteria(),
        success_expression=AllExpr(tuple(CriterionExpr(item) for item in ACTION_PROPOSAL_CRITERIA)),
        candidate_refs=(
            proposal.candidate_artifact_ref,
            payloads.parameters_ref,
            payloads.effect_contract_ref,
            payloads.action_proposal_ref,
        ),
        child_acceptance_refs=proposal.prepared_acceptance_refs,
        counter_evidence_refs=(),
        allowed_capabilities=(),
        producer_agent_ids=(str(sources.producer_attempt.agent_id),),
        reviewer_workspace_access=WorkspaceAccess.READ_ONLY,
        requirements_content_hash=sources.requirements.content_hash(),
    )
    request_content: dict[str, Any] = {
        "kind": "action-proposal-review-request-v1",
        "package": package.to_json(),
        "review_account": str(account_for_purpose(ReviewPurpose.ACTION_PROPOSAL)),
        "frozen_payloads": {
            "parameters": payloads.parameters.to_json(),
            "effect_contract": payloads.effect_contract.to_json(),
            "action_proposal": proposal.to_json(),
        },
        "input_manifest": manifest,
        "deterministic_checks": [check.to_json() for check in checks],
        "reviewer_instructions": {
            "independence": "Do not review a proposal you produced and do not modify it.",
            "verdict_format": "Return exactly one strict <critic_verdict> block.",
            "criteria_order": list(ACTION_PROPOSAL_CRITERIA),
            "rule": "A PASS is only a judgement over the frozen facts; it cannot replace a deterministic check.",
        },
    }
    return ActionProposalReviewDraft(package, request_content, manifest, checks, ())


def persist_action_proposal_review_inputs(
    store: Store,
    sources: PreparedOperationIntentSources,
    draft: ActionProposalReviewDraft,
) -> ActionProposalReviewDraft:
    """Persist the review anchor and its four deterministic check receipts.

    The caller must already hold the encompassing T0 transaction.  Each receipt
    is system-produced from a re-executed checker document and is referenced as
    ``TOOL_RECEIPT``; no Worker Artifact or Attempt is invented for a system check.
    """

    htn = HtnStore(store)
    proposal = draft.package.binding.subject_ref
    if proposal.kind is not TypedRefKind.ARTIFACT:
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "proposal subject kind")
    manifest_hash = htn.insert_input_manifest(
        sources.command.mission_id,
        sources.producer_scope.task_ref.id,
        draft.input_manifest,
        request_id=proposal.id,
    )
    if manifest_hash != draft.package.binding.input_manifest_hash:
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "input manifest differs")
    persist_action_proposal_review_package(htn, draft)
    refs: list[TypedRef] = []
    for check in draft.checks:
        receipt_id = derive_id(
            "operation-proposal-check",
            str(draft.package.package_id),
            check.criterion_id,
            check.content_hash(),
        )
        receipt = {
            "kind": "operation_proposal_check",
            "receipt_id": receipt_id,
            # The assured reviewer reads these receipts as material; an Assurance
            # commit_receipt read is scoped to its Mission (NEXT-TG-1.0, 2026-09-27).
            "mission_id": sources.command.mission_id,
            "subject_intent_id": draft.request_content["input_manifest"]["intent_id"],
            "package_id": str(draft.package.package_id),
            "proposal_ref": proposal.to_json(),
            "parameters_ref": draft.request_content["input_manifest"]["parameters_ref"],
            "effect_contract_ref": draft.request_content["input_manifest"]["effect_contract_ref"],
            "checker": check.checker,
            "check": check.to_json(),
        }
        existing = store.get_receipt(receipt_id)
        if existing is None:
            store.insert_receipt(
                commit_id=receipt_id,
                kind="operation_proposal_check",
                subject_id=str(draft.request_content["input_manifest"]["intent_id"]),
                base_version=draft.package.binding.requirements_revision,
                proposal_hash=check.content_hash(),
                receipt=receipt,
            )
        elif existing != receipt:
            raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPT_STALE", check.criterion_id)
        refs.append(
            TypedRef(
                kind=TypedRefKind.TOOL_RECEIPT,
                id=receipt_id,
                revision=1,
                content_hash=content_hash_of(receipt),
                produced_by=Provenance.TOOL,
            )
        )
    return replace(draft, check_receipt_refs=tuple(refs))


def persist_action_proposal_review_package(htn: HtnStore, draft: ActionProposalReviewDraft) -> str:
    """Persist just the package; caller supplies the encompassing transaction boundary."""

    try:
        existing = htn.get_review_package(str(draft.package.package_id))
    except StoreError:
        return htn.insert_review_package(draft.package)
    if existing.content_hash() != draft.package.content_hash():
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_CONFLICT", "package identity differs")
    return existing.content_hash()


def record_action_proposal_critic_verdict(
    store: Store,
    draft: ActionProposalReviewDraft,
    *,
    record_id: str,
    dispatch_intent_id: str,
    reviewer_agent_id: str,
    reviewer_turn_id: str,
    raw_critic_text: str,
) -> ActionProposalReviewReceipt:
    """Persist an official judgement from an independent, strictly parsed Critic.

    Receipt references must name the actual persisted documents returned by this
    coordinator.  Passing Critic output cannot manufacture a successful check.
    """

    dispatch_intent_id = identifier(dispatch_intent_id, "dispatch_intent_id")
    reviewer_agent_id = identifier(reviewer_agent_id, "reviewer_agent_id")
    reviewer_turn_id = identifier(reviewer_turn_id, "reviewer_turn_id")
    package = draft.package
    if package.produced_by(reviewer_agent_id):
        raise ActionProposalReviewError("OP_REVIEWER_NOT_INDEPENDENT", reviewer_agent_id)
    dispatch = store.get_intent(dispatch_intent_id)
    expected_subject = "operation-review:" + str(
        draft.request_content["input_manifest"]["intent_id"]
    )
    if (
        dispatch is None
        or dispatch.mission_id != package.binding.mission_id
        or dispatch.subject_id != expected_subject
        or dispatch.state not in {"SUBMITTED", "SETTLED"}
        or dispatch.agent_id != reviewer_agent_id
        or dispatch.expected_turn_id != reviewer_turn_id
        or str(dispatch.config.get("role", "")) != "operation_proposal_reviewer"
        or str(dispatch.config.get("review_package_id", "")) != str(package.package_id)
        or str(dispatch.config.get("operation_intent_id", ""))
        != str(draft.request_content["input_manifest"]["intent_id"])
    ):
        raise ActionProposalReviewError(
            "OP_REVIEW_DISPATCH_UNAVAILABLE", "dispatch identity differs"
        )
    if len(draft.check_receipt_refs) != len(ACTION_PROPOSAL_CRITERIA):
        raise ActionProposalReviewError(
            "OP_REVIEW_CHECK_RECEIPTS_MISSING", "four exact receipts required"
        )
    check_by_id = {check.criterion_id: check for check in draft.checks}
    evidence_refs: dict[str, TypedRef] = {}
    for criterion_id, ref in zip(ACTION_PROPOSAL_CRITERIA, draft.check_receipt_refs, strict=True):
        check = check_by_id.get(criterion_id)
        if (
            not isinstance(ref, TypedRef)
            or ref.kind is not TypedRefKind.TOOL_RECEIPT
            or check is None
        ):
            raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPTS_MISSING", criterion_id)
        receipt = store.get_receipt(ref.id)
        if (
            receipt is None
            or ref.content_hash != content_hash_of(receipt)
            or receipt.get("kind") != "operation_proposal_check"
            or receipt.get("package_id") != str(package.package_id)
            or receipt.get("subject_intent_id")
            != draft.request_content["input_manifest"]["intent_id"]
            or receipt.get("check") != check.to_json()
        ):
            raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPT_STALE", criterion_id)
        evidence_refs[criterion_id] = ref
    verdict = parse_critic_verdict(raw_critic_text, expected_criteria=ACTION_PROPOSAL_CRITERIA)
    critic_met = {str(item["criterion"]): bool(item["met"]) for item in verdict.mission_criteria}
    outcomes = tuple(
        CriterionOutcome(
            criterion_id=criterion_id,
            verdict=(
                CriterionVerdict.PASS
                if check_by_id[criterion_id].passed and critic_met[criterion_id]
                else CriterionVerdict.FAIL
            ),
            check_execution=CheckExecution.SUCCEEDED,
            evidence_refs=(evidence_refs[criterion_id],),
            limitations=(),
        )
        for criterion_id in ACTION_PROPOSAL_CRITERIA
    )
    accepted = (
        verdict.passed
        and not verdict.needs_human
        and all(item.verdict is CriterionVerdict.PASS for item in outcomes)
    )
    record = ReviewRecord(
        record_id=ReviewRecordId(record_id),
        package_id=package.package_id,
        purpose=ReviewPurpose.ACTION_PROPOSAL,
        binding=package.binding,
        reviewer_agent_id=reviewer_agent_id,
        reviewer_turn_id=reviewer_turn_id,
        evidence_manifest_hash=content_hash_of(
            {
                "package": package.content_hash(),
                "dispatch_intent_id": dispatch_intent_id,
                "critic": verdict.to_json(),
                "checks": {name: ref.to_json() for name, ref in evidence_refs.items()},
            }
        ),
        criteria=outcomes,
        verdict=ReviewVerdict.ACCEPT if accepted else ReviewVerdict.REJECTED,
    )
    htn = HtnStore(store)
    official = htn.official_review_record(str(package.package_id))
    if official is None:
        htn.insert_review_record(record, official=True)
    elif official.to_json() != record.to_json():
        raise ActionProposalReviewError("OP_REVIEW_RECORD_CONFLICT", "official record differs")
    return ActionProposalReviewReceipt(record, verdict)


@dataclass(frozen=True, slots=True)
class ActionProposalReviewCoordinator:
    """Runtime-bound façade used by T0 without owning its transaction or bridge."""

    store: Store
    connectors: Mapping[str, Any]
    deployment: DeploymentPolicy
    profile_registry: ConnectorProfileRegistry
    deployment_policy: DeploymentOperationPolicyInputs

    def prepare_review(
        self,
        sources: PreparedOperationIntentSources,
        payloads: FrozenOperationPayloadInputs,
        *,
        package_id: str,
    ) -> ActionProposalReviewDraft:
        draft = build_action_proposal_review(
            sources,
            payloads=payloads,
            package_id=package_id,
            store=self.store,
            connectors=self.connectors,
            deployment=self.deployment,
            profile_registry=self.profile_registry,
            deployment_policy=self.deployment_policy,
        )
        return persist_action_proposal_review_inputs(self.store, sources, draft)

    def persist_package(self, draft: ActionProposalReviewDraft) -> str:
        return persist_action_proposal_review_package(HtnStore(self.store), draft)

    def record_critic_verdict(
        self,
        draft: ActionProposalReviewDraft,
        *,
        record_id: str,
        dispatch_intent_id: str,
        reviewer_agent_id: str,
        reviewer_turn_id: str,
        raw_critic_text: str,
    ) -> ActionProposalReviewReceipt:
        return record_action_proposal_critic_verdict(
            self.store,
            draft,
            record_id=record_id,
            dispatch_intent_id=dispatch_intent_id,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_turn_id=reviewer_turn_id,
            raw_critic_text=raw_critic_text,
        )


def _assured(store: Store, mission_id: str) -> bool:
    from ..storage.assurance_store import AssuranceStore

    try:
        return AssuranceStore(store).lane(mission_id) == "ASSURANCE_1_1"
    except Exception:  # noqa: BLE001 - a Mission without a lane row is not assured
        return False


def _assured_check_receipts(
    store: Store, package: ReviewPackage, review: ReviewRecord, proposal: Any
) -> dict[str, TypedRef]:
    """An assured official review: grades from its authenticated manifest, and the
    four T0 check receipts read directly (the assured V1 record carries no evidence
    refs and projects a SEMANTIC PASS as UNKNOWN — see ``assurance_review_import``).
    The caller then re-verifies every receipt exactly as on the legacy path.
    """
    from ..assurance.codec import decode
    from ..assurance.refs import AssuranceRef, Pin
    from ..storage.assurance_reads import AssuranceReader

    mission = store.get_mission(package.binding.mission_id)
    if mission is None:
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "mission")
    reader = AssuranceReader(store, tenant_id=mission.tenant_id, mission_id=mission.id)
    pin = Pin(review.evidence_manifest_hash, 0, review.evidence_manifest_hash)
    try:
        manifest = decode(reader.read_exact_metadata(AssuranceRef("input_manifest", pin)).body_json)
    except Exception as error:  # noqa: BLE001 - an unreadable manifest is a refusal
        raise ActionProposalReviewError("OP_REVIEW_RECORD_MISSING", "assured manifest") from error
    grades = {item["criterion_id"]: item["effective_grade"] for item in manifest["criteria"]}
    if (
        manifest.get("effective_verdict") != "ACCEPT"
        or set(grades) != set(ACTION_PROPOSAL_CRITERIA)
    ):
        raise ActionProposalReviewError("OP_REVIEW_REJECTED", "assured manifest verdict")
    for criterion_id in ACTION_PROPOSAL_CRITERIA:
        if grades[criterion_id] != "PASS":
            raise ActionProposalReviewError("OP_REVIEW_REJECTED", criterion_id)
    found: dict[str, TypedRef] = {}
    rows = store.connection.execute(
        "SELECT commit_id, receipt_json FROM commit_receipts "
        "WHERE kind='operation_proposal_check' AND subject_id=? ORDER BY commit_id",
        (proposal.intent_id,),
    ).fetchall()
    for row in rows:
        receipt = json.loads(row[1])
        if receipt.get("package_id") != str(package.package_id):
            continue
        criterion_id = (receipt.get("check") or {}).get("criterion_id")
        if criterion_id in found or criterion_id not in ACTION_PROPOSAL_CRITERIA:
            raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPT_STALE", str(criterion_id))
        found[criterion_id] = TypedRef(
            kind=TypedRefKind.TOOL_RECEIPT, id=str(row[0]), revision=1,
            content_hash=content_hash_of(receipt), produced_by=Provenance.TOOL,
        )
    if set(found) != set(ACTION_PROPOSAL_CRITERIA):
        raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPT_STALE", "check receipts missing")
    return found


def validate_materialization_review(
    store: Store,
    sources: PreparedOperationIntentSources,
    payloads: FrozenOperationPayloadInputs,
    package: ReviewPackage,
    review: ReviewRecord,
) -> None:
    """Refuse materialization unless the stored D3 anchor and judgement still match.

    Materialization verifies the immutable review evidence, not the current adapter.
    Current source and registry checks belong to T0 / operation admission; accepting
    a later model assertion as a substitute for the frozen deterministic receipts is
    deliberately impossible here.
    """

    if not isinstance(store, Store) or not isinstance(sources, PreparedOperationIntentSources):
        raise ActionProposalReviewError("OP_REVIEW_SOURCE_UNRESOLVED", "store and sources")
    if not isinstance(payloads, FrozenOperationPayloadInputs) or not isinstance(
        package, ReviewPackage
    ):
        raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "typed payloads and package")
    if not isinstance(review, ReviewRecord):
        raise ActionProposalReviewError("OP_REVIEW_RECORD_MISSING", "typed review record")
    proposal = payloads.action_proposal
    if not isinstance(proposal, FrozenActionProposalV2):
        raise ActionProposalReviewError("OP_REVIEW_PAYLOAD_STALE", "ACTION_PROPOSAL v2 is required")
    htn = HtnStore(store)
    try:
        stored_package = htn.get_review_package(str(package.package_id))
        stored_manifest = htn.get_input_manifest(package.binding.input_manifest_hash)
        official = htn.official_review_record(str(package.package_id))
    except StoreError as error:
        raise ActionProposalReviewError("OP_REVIEW_RECORD_MISSING", str(error)) from error
    if (
        stored_package.content_hash() != package.content_hash()
        or content_hash_of(stored_manifest) != package.binding.input_manifest_hash
    ):
        raise ActionProposalReviewError(
            "OP_REVIEW_PACKAGE_STALE", "stored package or manifest differs"
        )
    if official is None or official.to_json() != review.to_json():
        raise ActionProposalReviewError("OP_REVIEW_RECORD_MISSING", "official record differs")
    if (
        package.purpose is not ReviewPurpose.ACTION_PROPOSAL
        or review.purpose is not ReviewPurpose.ACTION_PROPOSAL
    ):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "wrong review purpose")
    if package.account != account_for_purpose(ReviewPurpose.ACTION_PROPOSAL):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "wrong review account")
    if (
        package.binding.mission_id != sources.command.mission_id
        or package.binding.obligation_id != proposal.obligation_id
    ):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "mission or obligation differs")
    if package.binding.subject_ref != payloads.action_proposal_ref:
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "proposal subject differs")
    if package.binding.requirements_revision != proposal.requirements_ref.revision:
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "requirements revision differs")
    if (
        package.binding.policy_ref != _policy_ref()
        or package.requirements_content_hash != sources.requirements.content_hash()
    ):
        raise ActionProposalReviewError(
            "OP_REVIEW_PACKAGE_STALE", "policy or requirements pin differs"
        )
    if package.producer_agent_ids != (str(sources.producer_attempt.agent_id),):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "producer identity differs")
    if package.produced_by(review.reviewer_agent_id):
        raise ActionProposalReviewError("OP_REVIEWER_NOT_INDEPENDENT", review.reviewer_agent_id)
    if review.binding != package.binding or review.verdict is not ReviewVerdict.ACCEPT:
        raise ActionProposalReviewError(
            "OP_REVIEW_REJECTED", "review has no accepting exact binding"
        )
    if tuple(item.criterion_id for item in package.criteria) != ACTION_PROPOSAL_CRITERIA:
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "criterion catalogue differs")
    by_id = {item.criterion_id: item for item in review.criteria}
    if set(by_id) != set(ACTION_PROPOSAL_CRITERIA):
        raise ActionProposalReviewError("OP_REVIEW_RECORD_MISSING", "criterion coverage differs")
    if _assured(store, package.binding.mission_id):
        receipts = _assured_check_receipts(store, package, review, proposal)
    else:
        receipts = {}
        for criterion_id in ACTION_PROPOSAL_CRITERIA:
            outcome = by_id[criterion_id]
            if (
                outcome.verdict is not CriterionVerdict.PASS
                or outcome.check_execution is not CheckExecution.SUCCEEDED
                or len(outcome.evidence_refs) != 1
                or outcome.evidence_refs[0].kind is not TypedRefKind.TOOL_RECEIPT
            ):
                raise ActionProposalReviewError("OP_REVIEW_REJECTED", criterion_id)
            receipts[criterion_id] = outcome.evidence_refs[0]
    for criterion_id in ACTION_PROPOSAL_CRITERIA:
        receipt_ref = receipts[criterion_id]
        receipt = store.get_receipt(receipt_ref.id)
        check = None if receipt is None else receipt.get("check")
        if (
            receipt is None
            or receipt_ref.content_hash != content_hash_of(receipt)
            or receipt.get("kind") != "operation_proposal_check"
            or receipt.get("subject_intent_id") != proposal.intent_id
            or receipt.get("package_id") != str(package.package_id)
            or receipt.get("proposal_ref") != payloads.action_proposal_ref.to_json()
            or receipt.get("parameters_ref") != payloads.parameters_ref.to_json()
            or receipt.get("effect_contract_ref") != payloads.effect_contract_ref.to_json()
            or not isinstance(check, Mapping)
            or check.get("criterion_id") != criterion_id
            or check.get("passed") is not True
            or check.get("input_hash") != package.binding.input_manifest_hash
        ):
            raise ActionProposalReviewError("OP_REVIEW_CHECK_RECEIPT_STALE", criterion_id)
    required_candidates = {
        payloads.action_proposal_ref,
        payloads.parameters_ref,
        payloads.effect_contract_ref,
        proposal.candidate_artifact_ref,
    }
    if not required_candidates <= set(package.candidate_refs):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "frozen payload refs absent")
    if tuple(package.child_acceptance_refs) != tuple(proposal.prepared_acceptance_refs):
        raise ActionProposalReviewError("OP_REVIEW_PACKAGE_STALE", "prepared acceptances differ")


__all__ = (
    "ACTION_PROPOSAL_CRITERIA",
    "ACTION_PROPOSAL_REVIEW_POLICY",
    "ActionProposalCheckDocument",
    "ActionProposalReviewDraft",
    "ActionProposalReviewError",
    "ActionProposalReviewReceipt",
    "ActionProposalReviewCoordinator",
    "build_action_proposal_review",
    "validate_materialization_review",
    "persist_action_proposal_review_inputs",
    "persist_action_proposal_review_package",
    "record_action_proposal_critic_verdict",
)
