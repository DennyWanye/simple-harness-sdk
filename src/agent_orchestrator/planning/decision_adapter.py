# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Adapt admitted planning decisions to the existing HTN proposal chain (V2 §44).

The adapter is deliberately a value-only boundary.  It does not read a store,
emit an event, ground a method, compile a proposal or commit a plan revision.
The caller supplies the request-bound system fields, while the admitted command
supplies only the already checked planning intent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.htn import (
    EvidenceRef,
    PlanProposal,
    PlanRevision,
    MissionRef,
    ReadItem,
    ReadItemKind,
    RefineOperation,
    RebindInputOperation,
    CancelBranchOperation,
    ProposeSuccessorOperation,
    BindSharedGoalOperation,
    RetireMethodOperation,
    RunningWorkPolicy,
)
from ..contracts.models import ContractError
from ..contracts.planning_decisions import (
    BindExistingGoalDecision,
    BlockedItemV1,
    DeclareBlockedDecision,
    NoChangeDecision,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionType,
    PlanningRefKind,
    PlanningRefV1,
    RefineDecision,
    RepairRefineDeeperDecision,
    RepairRebindInputDecision,
    RepairCancelBranchDecision,
    RepairProposeSuccessorDecision,
    RepairReplaceMethodDecision,
    ResumableIf,
    WaitDecision,
)
from ..contracts.semantic_base import VersionedRef, hash_hex, identifier, index
from .decision_admission import (
    AdmissionContext,
    AdmittedPlanningDecision,
    NoMutationDecision,
    PreAdmittedPlanningDecision,
)


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Request-bound values required to rebuild a legacy ``PlanProposal``.

    ``base_plan_revision``, ``mission_id`` and ``proposal_id`` are system-owned.
    ``read_set`` and ``trigger_refs`` are the caller's durable request context;
    neither is taken from a model decision.
    """

    mission_id: str
    base_plan_revision: int
    proposal_id: str
    read_set: tuple[ReadItem, ...]
    trigger_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "adapter.mission_id"))
        object.__setattr__(
            self,
            "base_plan_revision",
            index(self.base_plan_revision, "adapter.base_plan_revision", minimum=0),
        )
        object.__setattr__(self, "proposal_id", identifier(self.proposal_id, "adapter.proposal_id"))
        if not isinstance(self.read_set, Sequence) or isinstance(self.read_set, (str, bytes)):
            raise ContractError("adapter.read_set must be a sequence")
        for item in self.read_set:
            if not isinstance(item, ReadItem):
                raise ContractError("adapter.read_set entries must be ReadItem values")
        if not isinstance(self.trigger_refs, Sequence) or isinstance(
            self.trigger_refs, (str, bytes)
        ):
            raise ContractError("adapter.trigger_refs must be a sequence")
        for trigger in self.trigger_refs:
            if not isinstance(trigger, EvidenceRef):
                raise ContractError("adapter.trigger_refs entries must be EvidenceRef values")
        object.__setattr__(self, "read_set", tuple(self.read_set))
        object.__setattr__(self, "trigger_refs", tuple(self.trigger_refs))

    @classmethod
    def from_admission_context(cls, context: AdmissionContext) -> AdapterContext:
        """Build adapter context from the request record used by admission.

        ``PlanningRefKind.OBSERVATION`` is the V2 spelling of the legacy
        ``ReadItemKind.FACT``.  ``resolution`` and ``method_instance`` have no
        legacy read-set kind, so they are not fabricated into a different kind;
        callers needing those reads provide an explicit ``AdapterContext``.
        """

        if not isinstance(context, AdmissionContext):
            raise ContractError("adapter context must be an AdapterContext or AdmissionContext")
        read_items: list[ReadItem] = []
        kinds = {
            PlanningRefKind.REQUIREMENTS: ReadItemKind.REQUIREMENTS,
            PlanningRefKind.OBLIGATION: ReadItemKind.OBLIGATION,
            PlanningRefKind.TASK: ReadItemKind.TASK,
            PlanningRefKind.METHOD: ReadItemKind.METHOD,
            PlanningRefKind.OBSERVATION: ReadItemKind.FACT,
            PlanningRefKind.ACCEPTANCE: ReadItemKind.ACCEPTANCE,
            PlanningRefKind.OPERATION: ReadItemKind.OPERATION,
            PlanningRefKind.AUTHORITY: ReadItemKind.AUTHORITY,
            PlanningRefKind.CAPABILITY: ReadItemKind.CAPABILITY,
        }
        for ref in context.visible_refs:
            kind = kinds.get(ref.kind)
            if kind is not None:
                read_items.append(
                    ReadItem(kind, ref.id, ref.semantic_revision, ref.content_hash)
                )
        return cls(
            mission_id=context.binding.mission_id,
            base_plan_revision=context.binding.base_plan_revision,
            proposal_id=context.decision_id,
            read_set=tuple(read_items),
        )


@dataclass(frozen=True, slots=True)
class DurableOnly:
    """The minimal durable signal for a decision that does not revise the plan."""

    decision_type: PlanningDecisionType
    reason: str
    canonical_hash: str
    wait_for: tuple[PlanningRefV1, ...] = ()
    blockers: tuple[BlockedItemV1, ...] = ()
    resumable_if: tuple[ResumableIf, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_hash",
            hash_hex(self.canonical_hash, "durable.canonical_hash"),
        )


@dataclass(frozen=True, slots=True)
class AdaptedPlanningOutcome:
    """Exactly one of an existing proposal or a durable-only signal."""

    proposal: PlanProposal | None = None
    durable_only: DurableOnly | None = None

    def __post_init__(self) -> None:
        if (self.proposal is None) == (self.durable_only is None):
            raise ContractError("adapted outcome must contain exactly one result")


def _context(value: AdapterContext | AdmissionContext) -> AdapterContext:
    if isinstance(value, AdapterContext):
        return value
    if isinstance(value, AdmissionContext):
        return AdapterContext.from_admission_context(value)
    raise ContractError("adapter context must be an AdapterContext or AdmissionContext")


def _subject_id(subject: Mapping[str, Any], name: str) -> str:
    value = subject.get(name)
    if value is None:
        raise ContractError(f"admitted.subject is missing {name!r}")
    return identifier(value, f"admitted.subject.{name}")


def _versioned_ref(ref: Any) -> VersionedRef:
    return VersionedRef(id=ref.method_id, version=ref.version, content_hash=ref.content_hash)


def _method_ref(admitted: AdmittedPlanningDecision) -> VersionedRef:
    """Use admission's resolved method identity, never a model-supplied revision."""

    if not admitted.method_refs:
        raise ContractError("admitted executable decision has no resolved method")
    return _versioned_ref(admitted.method_refs[0])


def _instance_id(admitted: AdmittedPlanningDecision, payload_ref: PlanningRefV1) -> str:
    resolved = next(
        (
            ref.id
            for ref in admitted.method_instances
            if ref.kind is PlanningRefKind.METHOD_INSTANCE
        ),
        None,
    )
    return identifier(resolved if resolved is not None else payload_ref.id, "method_instance_id")


def _refine_operation(
    admitted: AdmittedPlanningDecision,
    payload: RefineDecision | RepairRefineDeeperDecision | RepairReplaceMethodDecision,
) -> RefineOperation:
    return RefineOperation(
        goal_id=_subject_id(admitted.subject, "task_id"),
        obligation_id=_subject_id(admitted.subject, "obligation_id"),
        method_ref=_method_ref(admitted),
        bindings=dict(payload.bindings),
    )


def _proposal(
    admitted: AdmittedPlanningDecision,
    context: AdapterContext,
    operations: tuple[Any, ...],
    *,
    running_work_policy: RunningWorkPolicy = RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED,
) -> AdaptedPlanningOutcome:
    decision = admitted.decision
    return AdaptedPlanningOutcome(
        proposal=PlanProposal(
            proposal_id=context.proposal_id,
            mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs,
            read_set=context.read_set,
            operations=operations,
            rationale=decision.rationale,
            running_work_policy=running_work_policy,
        )
    )


def _durable(decision: PlanningDecisionEnvelopeV1, canonical_hash: str) -> AdaptedPlanningOutcome:
    payload = decision.payload
    if isinstance(payload, DeclareBlockedDecision):
        return AdaptedPlanningOutcome(
            durable_only=DurableOnly(
                decision_type=decision.decision_type,
                reason=decision.rationale,
                canonical_hash=canonical_hash,
                blockers=payload.blockers,
                resumable_if=payload.resumable_if,
            )
        )
    if isinstance(payload, WaitDecision):
        return AdaptedPlanningOutcome(
            durable_only=DurableOnly(
                decision_type=decision.decision_type,
                reason=payload.reason,
                canonical_hash=canonical_hash,
                wait_for=payload.wait_for,
            )
        )
    if isinstance(payload, NoChangeDecision):
        return AdaptedPlanningOutcome(
            durable_only=DurableOnly(
                decision_type=decision.decision_type,
                reason=payload.reason,
                canonical_hash=canonical_hash,
            )
        )
    raise ContractError("decision is not a durable-only type")


def adapt_admitted_decision(
    admitted: AdmittedPlanningDecision,
    *,
    context: AdapterContext | AdmissionContext,
) -> AdaptedPlanningOutcome:
    """Translate one admitted decision without performing any downstream action."""

    if not isinstance(admitted, AdmittedPlanningDecision):
        raise ContractError("adapter input must be an AdmittedPlanningDecision")
    adapter_context = _context(context)
    decision = admitted.decision
    payload = decision.payload

    if isinstance(payload, RepairProposeSuccessorDecision):
        return _proposal(admitted, adapter_context, (_successor_operation(payload),),
                         running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if isinstance(payload, RepairCancelBranchDecision):
        return _proposal(admitted, adapter_context,
            (CancelBranchOperation(payload.method_instance_ref.id, payload.step),),
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if isinstance(payload, RepairRebindInputDecision):
        return _proposal(admitted, adapter_context, (_rebind_operation(payload),),
                         running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)

    if isinstance(payload, BindExistingGoalDecision):
        return _proposal(admitted, adapter_context, (_bind_operation(payload),),
                         running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)

    if decision.decision_type is PlanningDecisionType.REFINE or isinstance(payload, RepairRefineDeeperDecision):
        if not isinstance(payload, (RefineDecision, RepairRefineDeeperDecision)):
            raise ContractError("REFINE payload is not a RefineDecision")
        return _proposal(
            admitted,
            adapter_context,
            (_refine_operation(admitted, payload),),
        )

    if decision.decision_type is PlanningDecisionType.REPAIR:
        if isinstance(payload, RepairReplaceMethodDecision):
            retire = RetireMethodOperation(
                method_instance_id=_instance_id(admitted, payload.rejected_method_instance),
                reason=decision.rationale,
            )
            refine = _refine_operation(admitted, payload)
            return _proposal(
                admitted,
                adapter_context,
                (retire, refine),
                running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE,
            )
        raise ContractError("REPAIR payload has no enabled repair kind")

    if decision.decision_type in {
        PlanningDecisionType.DECLARE_BLOCKED,
        PlanningDecisionType.WAIT,
        PlanningDecisionType.NO_CHANGE,
    }:
        return _durable(decision, admitted.canonical_hash)

    raise ContractError(
        f"decision type {decision.decision_type!s} is not enabled for the adapter"
    )


def adapt_for_preview(
    pre_admitted: PreAdmittedPlanningDecision | NoMutationDecision,
    *,
    context: AdapterContext,
) -> PlanProposal | NoMutationDecision:
    """Build a typed candidate without manufacturing final admission."""

    if isinstance(pre_admitted, NoMutationDecision):
        return pre_admitted
    if not isinstance(pre_admitted, PreAdmittedPlanningDecision):
        raise ContractError("preview adapter requires a pre-admitted decision")
    if not isinstance(context, AdapterContext):
        raise ContractError("preview adapter context must be an AdapterContext")
    decision = pre_admitted.decision
    payload = decision.payload
    if isinstance(payload, RepairProposeSuccessorDecision):
        return PlanProposal(proposal_id=context.proposal_id, mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs, read_set=context.read_set,
            operations=(_successor_operation(payload),), rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if isinstance(payload, RepairCancelBranchDecision):
        return PlanProposal(proposal_id=context.proposal_id, mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs, read_set=context.read_set,
            operations=(CancelBranchOperation(payload.method_instance_ref.id, payload.step),), rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if isinstance(payload, RepairRebindInputDecision):
        return PlanProposal(proposal_id=context.proposal_id, mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs, read_set=context.read_set,
            operations=(_rebind_operation(payload),), rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if isinstance(payload, BindExistingGoalDecision):
        return PlanProposal(proposal_id=context.proposal_id, mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs, read_set=context.read_set,
            operations=(_bind_operation(payload),), rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE)
    if decision.decision_type is PlanningDecisionType.REFINE or isinstance(payload, RepairRefineDeeperDecision):
        if not isinstance(payload, (RefineDecision, RepairRefineDeeperDecision)) or not pre_admitted.method_refs:
            raise ContractError("REFINE preview lacks a resolved method")
        operation = RefineOperation(
            goal_id=_subject_id(pre_admitted.subject, "task_id"),
            obligation_id=_subject_id(pre_admitted.subject, "obligation_id"),
            method_ref=_versioned_ref(pre_admitted.method_refs[0]),
            bindings=dict(payload.bindings),
        )
        return PlanProposal(
            proposal_id=context.proposal_id,
            mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs,
            read_set=context.read_set,
            operations=(operation,),
            rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED,
        )
    if decision.decision_type is PlanningDecisionType.REPAIR:
        if not isinstance(payload, RepairReplaceMethodDecision) or not pre_admitted.method_refs:
            raise ContractError("REPAIR preview has no enabled replacement")
        instance = next(
            (item for item in pre_admitted.context.admission.active_method_instances
             if item.ref_key == (
                 str(payload.rejected_method_instance.kind),
                 payload.rejected_method_instance.id,
                 payload.rejected_method_instance.semantic_revision,
                 payload.rejected_method_instance.content_hash,
             )),
            None,
        )
        retire = RetireMethodOperation(
            method_instance_id=(instance.instance_id if instance is not None
                                else payload.rejected_method_instance.id),
            reason=decision.rationale,
        )
        refine = RefineOperation(
            goal_id=_subject_id(pre_admitted.subject, "task_id"),
            obligation_id=_subject_id(pre_admitted.subject, "obligation_id"),
            method_ref=_versioned_ref(pre_admitted.method_refs[0]),
            bindings=dict(payload.bindings),
        )
        return PlanProposal(
            proposal_id=context.proposal_id,
            mission_id=MissionRef(context.mission_id),
            expected_plan_revision=PlanRevision(context.base_plan_revision),
            trigger_refs=context.trigger_refs,
            read_set=context.read_set,
            operations=(retire, refine),
            rationale=decision.rationale,
            running_work_policy=RunningWorkPolicy.REQUEST_STOP_THEN_RECONCILE,
        )
    raise ContractError(f"decision type {decision.decision_type!s} is not enabled for preview")


__all__ = (
    "AdapterContext",
    "AdaptedPlanningOutcome",
    "DurableOnly",
    "adapt_for_preview",
    "adapt_admitted_decision",
)


def _rebind_operation(payload: RepairRebindInputDecision) -> RebindInputOperation:
    return RebindInputOperation(payload.consumer_task_ref.id, payload.requirement_id,
        payload.expected_requirement_hash, payload.producer_task_ref.id, payload.output_port)


def _successor_operation(payload: RepairProposeSuccessorDecision) -> ProposeSuccessorOperation:
    return ProposeSuccessorOperation(payload.old_task_ref.id, payload.obligation_ref.id,
        VersionedRef(payload.goal_type_ref.id, payload.goal_type_ref.version, payload.goal_type_ref.content_hash),
        dict(payload.bindings))


def _bind_operation(payload: BindExistingGoalDecision) -> BindSharedGoalOperation:
    return BindSharedGoalOperation(payload.consumer_method_instance_ref.id, payload.step,
        payload.goal_ref.id, None if payload.resolution_ref is None else payload.resolution_ref.id)
