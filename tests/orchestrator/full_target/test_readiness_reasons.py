# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.1c: ``evaluate_readiness`` and the controlled ``EligiblePrimitiveTask`` gate.

Every test here is a pure counterexample over contract values.  Nothing opens a
database, touches a scheduler or calls a model: a snapshot is built from
contracts, a readiness report is computed from it, and the report is inspected.

The four facts this suite exists to pin down are

* each refusal keeps its own reason — a missing input, an unreachable observation
  service and an external operation whose effect is UNKNOWN are three different
  answers and never collapse into "the task failed" (TG §8.2);
* a ``form=compound`` task is intercepted by its *form*, whatever the legacy
  ``TaskStatus`` string says, so ``READY`` can never walk a compound into the
  Worker path (plan §18.5 hard constraint 4);
* ``EligiblePrimitiveTask`` can only be built by :func:`admit_for_dispatch` from
  a ``READY_CANDIDATE`` report — and not by replacing a field on one, copying one
  or round-tripping one through pickle either; and
* a cached verdict dies when any read-set item moves, including the four *control*
  channels (dispatch generation, input binding revision, approval, obligation
  lifecycle) the gates actually read.

The last section mutates the production gates one at a time and asserts the
verdict changes, so a green suite cannot be green because the gates do nothing.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.artifacts.input_bindings import (
    InputManifest,
    ManifestNotFrozen,
    ResolutionProblem,
    ResolutionProblemKind,
    ResolutionResult,
    ResolvedInputBinding,
    ResourceIdentity,
    SymbolicBinding,
)
from agent_orchestrator.contracts.evidence_state import (
    Availability,
    PreconditionPhase,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    BoundInput,
    ChildBinding,
    ContractRevision,
    DataRequirement,
    DispatchGeneration,
    GoalSignature,
    InputBindingRevision,
    MethodInstanceDraft,
    MethodInstanceId,
    MethodOccurrenceBinding,
    MethodRef,
    MissionRef,
    ObligationId,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanRevision,
    PortSpec,
    PreconditionRef,
    ReadItem,
    ReadItemKind,
    ReleaseCondition,
    ScopeEpochRead,
    SemanticReadSet,
    SourceRevisionPolicy,
    SupportSetRead,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.obligations import ObligationAccountView, ObligationLifecycle
from agent_orchestrator.contracts.resolution import (
    ApprovalDecision,
    ApprovalState,
    EffectOutcome,
    OperationControl,
    OperationCurrentState,
    OperationEnvelope,
    OperationKind,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, VersionedRef
from agent_orchestrator.contracts.state_machines import TaskStatus
from agent_orchestrator.graph import eligibility as eligibility_module
from agent_orchestrator.graph.eligibility import (
    ADMITTED_DISPATCH_REASONS,
    PLANNING_REASONS,
    READINESS_PRECEDENCE,
    ActivePlanView,
    AdmittedDispatch,
    DispatchCandidacy,
    EligibilityGateBypassed,
    EligiblePrimitiveTask,
    EvidenceView,
    ExecutionFrontier,
    NotEligible,
    OccurrenceOutcome,
    PendingOperation,
    PlanningFrontier,
    ReadinessReason,
    ReadinessReport,
    TaskView,
    admit_for_dispatch,
    evaluate_readiness,
    gate_precedence,
    legacy_ready_is_not_eligibility,
    order_released,
    stale_after,
)
from agent_orchestrator.graph.projection_validation import GraphIntegrityError
from agent_orchestrator.graph.task_network import TaskNetworkSnapshot

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64

MISSION = MissionRef("m-1")
OCC_ROOT = OccurrenceId("occ-root")
OCC_A = OccurrenceId("occ-a")
OCC_B = OccurrenceId("occ-b")
T_ROOT = TaskRef("t-root")
T_A = TaskRef("t-a")
T_B = TaskRef("t-b")
O_ROOT = ObligationId("o-root")
O_A = ObligationId("o-a")
O_B = ObligationId("o-b")
MI_1 = MethodInstanceId("mi-1")
DIGEST_START = "1" * 64
DIGEST_MAINTAIN = "2" * 64
SCOPE = "mission"
NOW_MS = 1_000_000

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def vref(name: str, *, version: int = 1, content_hash: str = HASH_A) -> VersionedRef:
    return VersionedRef(id=name, version=version, content_hash=content_hash)


def signature(name: str = "produce-report") -> GoalSignature:
    return GoalSignature(
        signature_id=name,
        version=1,
        parameter_schema_ref=vref("params"),
        output_schema_ref=vref("output"),
        statement="produce a report",
    )


def binding_for(
    task_id: TaskRef,
    obligation: ObligationId,
    form: TaskForm,
    **overrides: object,
) -> TaskSemanticBindingV1:
    fields: dict[str, object] = {
        "task_id": task_id,
        "obligation_id": obligation,
        "contract_revision": ContractRevision(3),
        "contract_hash": HASH_A,
        "form": form,
        "goal_signature": signature(),
        "semantic_scope": SCOPE,
    }
    if form is TaskForm.PRIMITIVE:
        fields["operator_ref"] = vref("operator")
    fields.update(overrides)
    return TaskSemanticBindingV1(**fields)  # type: ignore[arg-type]


def consumer_binding(**overrides: object) -> TaskSemanticBindingV1:
    """``t-b``: one required input port, one START precondition, one method slot."""

    fields: dict[str, object] = {
        "input_ports": (PortSpec(port_key="in", schema_ref=vref("schema-in")),),
        "requirement_refs": ("req-1",),
        "precondition_refs": (
            PreconditionRef(condition_digest=DIGEST_START, phase=PreconditionPhase.SELECT),
        ),
        "occurrence_binding": MethodOccurrenceBinding(
            method_instance_id=MI_1, occurrence_id=OCC_B, slot_key="consume"
        ),
        "dispatch_generation": DispatchGeneration(7),
        "input_binding_revision": InputBindingRevision(4),
    }
    fields.update(overrides)
    return binding_for(T_B, O_B, TaskForm.PRIMITIVE, **fields)


def method_draft(
    instance_id: MethodInstanceId = MI_1,
    *,
    children: Sequence[ChildBinding] | None = None,
) -> MethodInstanceDraft:
    bound = (
        tuple(children)
        if children is not None
        else (
            ChildBinding(
                instance_id=instance_id,
                slot_key="produce",
                occurrence_id=OCC_A,
                obligation_id=O_A,
            ),
            ChildBinding(
                instance_id=instance_id,
                slot_key="consume",
                occurrence_id=OCC_B,
                obligation_id=O_B,
            ),
        )
    )
    return MethodInstanceDraft(
        instance_id=instance_id,
        goal_id=T_ROOT,
        obligation_id=O_ROOT,
        method_ref=MethodRef(method_id="method-x", version=1, content_hash=HASH_B),
        child_bindings=bound,
        goal_occurrence_id=OCC_ROOT,
        plan_revision=PlanRevision(5),
    )


def snapshot_of(
    *,
    consumer: TaskSemanticBindingV1 | None = None,
    release_condition: ReleaseCondition = ReleaseCondition.ACCEPTED,
    extra_instances: Sequence[MethodInstanceDraft] = (),
    adopted: Sequence[MethodInstanceId] = (MI_1,),
) -> TaskNetworkSnapshot:
    consumer_record = consumer if consumer is not None else consumer_binding()
    root = binding_for(
        T_ROOT,
        O_ROOT,
        TaskForm.COMPOUND,
        adopted_method_instance_id=MI_1 if MI_1 in tuple(adopted) else None,
    )
    producer = binding_for(
        T_A,
        O_A,
        TaskForm.PRIMITIVE,
        output_ports=(PortSpec(port_key="out", schema_ref=vref("schema-in")),),
    )
    return TaskNetworkSnapshot(
        mission_id=MISSION,
        plan_revision=PlanRevision(5),
        occurrences=(
            OccurrenceSpec(
                occurrence_id=OCC_ROOT,
                task_id=T_ROOT,
                obligation_id=O_ROOT,
                form=TaskForm.COMPOUND,
            ),
            OccurrenceSpec(
                occurrence_id=OCC_A, task_id=T_A, obligation_id=O_A, form=TaskForm.PRIMITIVE
            ),
            OccurrenceSpec(
                occurrence_id=OCC_B, task_id=T_B, obligation_id=O_B, form=TaskForm.PRIMITIVE
            ),
        ),
        task_bindings=(root, producer, consumer_record),
        method_instances=(method_draft(), *extra_instances),
        adopted_instance_ids=tuple(adopted),
        root_occurrence_ids=(OCC_ROOT,),
        order_constraints=(
            OrderConstraint(before=OCC_A, after=OCC_B, release_condition=release_condition),
        ),
        data_requirements=(
            DataRequirement(
                requirement_id="req-1",
                producer_occurrence=OCC_A,
                output_port="out",
                consumer_occurrence=OCC_B,
                input_port="in",
                schema_ref=vref("schema-in"),
                assurance_policy_ref="assurance-standard",
                freshness_policy_ref="freshness-standard",
            ),
        ),
    )


def account(
    obligation: ObligationId,
    lifecycle: ObligationLifecycle = ObligationLifecycle.UNSATISFIED,
    *,
    has_admitted_demand: bool = True,
) -> ObligationAccountView:
    return ObligationAccountView(
        obligation_id=obligation,
        failure_count=0,
        consumed_cost_micros=0,
        consumed_attempts=0,
        has_admitted_demand=has_admitted_demand,
        fuel_limit=3,
        fuel_used=0,
        expansions=0,
        shape_changes=0,
        lifecycle=lifecycle,
    )


def all_accounts(
    *changed: ObligationAccountView,
) -> dict[ObligationId, ObligationAccountView]:
    """The three duties of the fixture world, with any of them replaced."""

    accounts = {O_B: account(O_B), O_A: account(O_A), O_ROOT: account(O_ROOT)}
    for item in changed:
        accounts[item.obligation_id] = item
    return accounts


def plan_of(snapshot: TaskNetworkSnapshot | None = None, **overrides: object) -> ActivePlanView:
    fields: dict[str, object] = {
        "snapshot": snapshot if snapshot is not None else snapshot_of(),
        "requirements_revision": 11,
        "manager_epoch": 2,
        "budget_grant_revision": 3,
        "scope_epochs": {SCOPE: 9},
        "dispatch_generations": {OCC_B: 7, OCC_A: 0, OCC_ROOT: 0},
        "input_binding_revisions": {T_B: 4, T_A: 0, T_ROOT: 0},
        "obligation_accounts": all_accounts(),
    }
    fields.update(overrides)
    return ActivePlanView(**fields)  # type: ignore[arg-type]


def witness(
    *,
    purpose: WitnessPurpose = WitnessPurpose.START,
    truth: TruthValue = TruthValue.TRUE,
    decision: WitnessDecision = WitnessDecision.USABLE,
    freshness: Validity = Validity.CURRENT,
    availability: Availability = Availability.READABLE,
    scope_epoch: int = 9,
    not_after_ms: int | None = NOW_MS + 5_000,
    scope_id: str = SCOPE,
    consumer_ref: TypedRef | None = None,
) -> ValidityWitness:
    return ValidityWitness(
        witness_id="w-start",
        consumer_ref=(
            consumer_ref
            if consumer_ref is not None
            else TypedRef(kind=TypedRefKind.TASK, id=str(T_B), revision=3, content_hash=HASH_A)
        ),
        purpose=purpose,
        truth=truth,
        freshness=freshness,
        availability=availability,
        decision=decision,
        scope_id=scope_id,
        scope_epoch=scope_epoch,
        support_revision=17,
        as_of_ms=NOW_MS - 1_000,
        not_after_ms=not_after_ms,
    )


def evidence_of(**overrides: object) -> EvidenceView:
    fields: dict[str, object] = {
        "witnesses": {DIGEST_START: witness()},
        "observer_available": True,
        "support_sets": (SupportSetRead(support_set_id="ss-1", revision=4, member_digest=HASH_C),),
        "operation_range_revision": 6,
    }
    fields.update(overrides)
    return EvidenceView(**fields)  # type: ignore[arg-type]


def resolved_binding(*, provisional: bool = False) -> ResolvedInputBinding:
    return ResolvedInputBinding(
        binding_id="b-1",
        bound=BoundInput(
            requirement_id="req-1",
            producer_result_id="r-1",
            acceptance_id="acc-1",
            artifact_id="art-1",
            content_hash=HASH_D,
            schema_ref=vref("schema-in"),
            source_revision="rev-1",
        ),
        producer_task_ref=T_A,
        producer_occurrence=OCC_A,
        output_port="out",
        support_revision=21,
        consumer_task_ref=T_B,
        input_port="in",
        port_ordinal=0,
        source_identity=ResourceIdentity(namespace="ws/attempt-a", path="report.md"),
        produced_schema_ref=vref("schema-in"),
        read_policy="read-standard",
        freshness_policy="freshness-standard",
        disclosure_scope=SCOPE,
        source_revision_policy=SourceRevisionPolicy.PINNED,
        provisional=provisional,
        witness_id="w-start",
    )


def manifest_of(*, provisional: bool = False, pending: bool = False) -> InputManifest:
    return InputManifest(
        consumer_task_ref=T_B,
        bindings=(resolved_binding(provisional=provisional),),
        pending=(
            (
                SymbolicBinding(
                    requirement_id="req-2",
                    producer_occurrence=OCC_A,
                    output_port="out",
                    consumer_task_ref=T_B,
                    input_port="in",
                ),
            )
            if pending
            else ()
        ),
    )


def input_ok() -> ResolutionResult:
    return ResolutionResult(manifest=manifest_of())


def input_problem(kind: ResolutionProblemKind) -> ResolutionResult:
    return ResolutionResult(
        manifest=None,
        problems=(ResolutionProblem(kind=kind, detail=str(kind), input_port="in"),),
    )


def envelope(operation_id: str = "op-1") -> OperationEnvelope:
    ref = TypedRef(kind=TypedRefKind.ARTIFACT, id="params", revision=1, content_hash=HASH_A)
    return OperationEnvelope(
        operation_id=operation_id,  # type: ignore[arg-type]
        operation_occurrence_id="op-occ-1",  # type: ignore[arg-type]
        mission_id=str(MISSION),
        obligation_id=str(O_B),
        scope_id=SCOPE,
        connector_id="connector-x",
        connector_version="v1",
        operation_name="send-report",
        operation_kind=OperationKind.STATE_WRITE,
        target_ref="target-1",
        expected_target_version=None,
        parameters_artifact_ref=ref,
        request_hash=HASH_B,
        requirements_revision=11,
        review_ref=TypedRef(kind=TypedRefKind.REVIEW, id="rev-1", revision=1, content_hash=HASH_A),
        accepted_input_refs=(),
        effect_contract_ref=TypedRef(
            kind=TypedRefKind.OPERATION, id="eff-1", revision=1, content_hash=HASH_A
        ),
    )


def operation_state(outcome: EffectOutcome, operation_id: str = "op-1") -> OperationCurrentState:
    """An operation that has been authorised, unless it was never handed off."""

    control = (
        OperationControl.PROPOSED
        if outcome is EffectOutcome.NOT_HANDED_OFF
        else OperationControl.DISPATCHING
    )
    return OperationCurrentState(
        operation_id=operation_id,  # type: ignore[arg-type]
        authorization_state=control,
        authorization_epoch=1,
        dispatch_generation=1,
        effect_outcome=outcome,
    )


def operations(outcome: EffectOutcome, *, conflicts: bool = True) -> tuple[PendingOperation, ...]:
    return (
        PendingOperation(
            envelope=envelope(),
            state=operation_state(outcome),
            conflicts_with=frozenset({OCC_B}) if conflicts else frozenset({OCC_A}),
        ),
    )


def granted_approval(*, expires_at_ms: int | None = None) -> ApprovalState:
    return ApprovalState(
        decision=ApprovalDecision.GRANTED,
        granted_by="reviewer-1",
        granted_at_ms=NOW_MS - 10_000,
        expires_at_ms=expires_at_ms,
    )


def consumer_view(**overrides: object) -> TaskView:
    fields: dict[str, object] = {
        "occurrence_id": OCC_B,
        "task_id": T_B,
        "binding": consumer_binding(),
        "legacy_status": str(TaskStatus.READY),
    }
    fields.update(overrides)
    return TaskView(**fields)  # type: ignore[arg-type]


def root_view(**overrides: object) -> TaskView:
    fields: dict[str, object] = {
        "occurrence_id": OCC_ROOT,
        "task_id": T_ROOT,
        "binding": binding_for(T_ROOT, O_ROOT, TaskForm.COMPOUND, adopted_method_instance_id=MI_1),
        "legacy_status": str(TaskStatus.READY),
    }
    fields.update(overrides)
    return TaskView(**fields)  # type: ignore[arg-type]


_UNSET: Any = object()

RELEASED: Mapping[OccurrenceId, OccurrenceOutcome] = {
    OCC_A: OccurrenceOutcome.ACCEPTED,
    OCC_ROOT: OccurrenceOutcome.RUNNING,
}


def report_for(
    view: TaskView | None = None,
    *,
    plan: ActivePlanView | None = None,
    resolutions: Mapping[OccurrenceId, OccurrenceOutcome] | None = None,
    evidence: EvidenceView | None = None,
    input_result: ResolutionResult | None = _UNSET,
    now_ms: int = NOW_MS,
) -> ReadinessReport:
    return evaluate_readiness(
        view if view is not None else consumer_view(),
        plan if plan is not None else plan_of(),
        dict(RELEASED) if resolutions is None else resolutions,
        evidence if evidence is not None else evidence_of(),
        input_ok() if input_result is _UNSET else input_result,
        now_ms=now_ms,
    )


def ready_report() -> ReadinessReport:
    return report_for()


def admitted_task() -> EligiblePrimitiveTask:
    return admit_for_dispatch(
        ready_report(), consumer_view(), plan_of(), manifest_of(), now_ms=NOW_MS
    )


# --------------------------------------------------------------------------------------
# 1. GRAPH_INTEGRITY: corruption is corruption, not a legacy fallback (§18.5)
# --------------------------------------------------------------------------------------


def test_a_missing_semantic_binding_is_graph_integrity_not_a_fallback() -> None:
    report = report_for(consumer_view(binding=None))
    assert report.reason is ReadinessReason.GRAPH_INTEGRITY
    assert "semantic_binding_missing" in report.detail_codes


def test_an_occurrence_the_snapshot_does_not_contain_is_graph_integrity() -> None:
    report = report_for(consumer_view(occurrence_id=OccurrenceId("occ-ghost")))
    assert report.reason is ReadinessReason.GRAPH_INTEGRITY
    assert "unknown_occurrence" in report.detail_codes


def test_an_unorderable_projection_is_graph_integrity_for_every_task() -> None:
    plan = plan_of(integrity_error=GraphIntegrityError(remaining=("n1", "n2"), cycle=("n1", "n2")))
    assert report_for(plan=plan).reason is ReadinessReason.GRAPH_INTEGRITY
    assert report_for(root_view(), plan=plan).reason is ReadinessReason.GRAPH_INTEGRITY


def test_a_binding_that_names_another_task_is_graph_integrity() -> None:
    report = report_for(consumer_view(task_id=T_A))
    assert report.reason is ReadinessReason.GRAPH_INTEGRITY


def test_a_healthy_network_is_not_reported_as_graph_integrity() -> None:
    assert ready_report().reason is not ReadinessReason.GRAPH_INTEGRITY


# --------------------------------------------------------------------------------------
# 2. NEEDS_REFINEMENT: the gate is form=compound, not a status string (§18.5 constraint 4)
# --------------------------------------------------------------------------------------


def test_a_compound_task_needs_refinement() -> None:
    assert report_for(root_view()).reason is ReadinessReason.NEEDS_REFINEMENT


@pytest.mark.parametrize("status", [str(item) for item in TaskStatus])
def test_a_compound_task_needs_refinement_whatever_the_legacy_status_says(status: str) -> None:
    report = report_for(root_view(legacy_status=status))
    assert report.reason is ReadinessReason.NEEDS_REFINEMENT
    assert "form_compound" in report.detail_codes


def test_a_compound_task_needs_refinement_even_when_every_other_gate_is_satisfied() -> None:
    report = report_for(
        root_view(),
        resolutions={OCC_A: OccurrenceOutcome.ACCEPTED, OCC_ROOT: OccurrenceOutcome.ACCEPTED},
    )
    assert report.reason is ReadinessReason.NEEDS_REFINEMENT


def test_a_primitive_task_is_never_reported_as_needing_refinement() -> None:
    assert ready_report().reason is not ReadinessReason.NEEDS_REFINEMENT


def test_a_compound_report_can_never_be_admitted_for_dispatch() -> None:
    report = report_for(root_view())
    with pytest.raises(NotEligible):
        admit_for_dispatch(report, root_view(), plan_of(), manifest_of(), now_ms=NOW_MS)


# --------------------------------------------------------------------------------------
# 3. NOT_SELECTED
# --------------------------------------------------------------------------------------


def test_an_occurrence_outside_the_adopted_plan_is_not_selected() -> None:
    lonely = OccurrenceSpec(
        occurrence_id=OccurrenceId("occ-c"),
        task_id=TaskRef("t-c"),
        obligation_id=ObligationId("o-c"),
        form=TaskForm.PRIMITIVE,
    )
    base = snapshot_of()
    snapshot = replace(
        base,
        occurrences=(*base.occurrences, lonely),
        task_bindings=(
            *base.task_bindings,
            binding_for(TaskRef("t-c"), ObligationId("o-c"), TaskForm.PRIMITIVE),
        ),
    )
    view = TaskView(
        occurrence_id=OccurrenceId("occ-c"),
        task_id=TaskRef("t-c"),
        binding=binding_for(TaskRef("t-c"), ObligationId("o-c"), TaskForm.PRIMITIVE),
    )
    report = report_for(view, plan=plan_of(snapshot))
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert "not_in_adopted_plan" in report.detail_codes


def test_a_mission_that_does_not_admit_work_makes_every_task_not_selected() -> None:
    report = report_for(plan=plan_of(mission_admits_work=False))
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert "mission_does_not_admit_work" in report.detail_codes


@pytest.mark.parametrize(
    "lifecycle",
    [
        ObligationLifecycle.SATISFIED,
        ObligationLifecycle.CANCELLED,
        ObligationLifecycle.SUPERSEDED,
    ],
)
def test_a_duty_that_is_no_longer_outstanding_is_not_selected(
    lifecycle: ObligationLifecycle,
) -> None:
    plan = plan_of(obligation_accounts=all_accounts(account(O_B, lifecycle)))
    report = report_for(plan=plan)
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert f"obligation_{lifecycle!s}" in report.detail_codes


def test_a_duty_with_no_admitted_demand_is_not_selected() -> None:
    plan = plan_of(obligation_accounts=all_accounts(account(O_B, has_admitted_demand=False)))
    report = report_for(plan=plan)
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert "obligation_demand_not_admitted" in report.detail_codes


def test_a_duty_with_no_account_record_at_all_is_not_selected() -> None:
    """No account means no admitted demand; a missing ledger entry is not a permission."""

    report = report_for(plan=plan_of(obligation_accounts={}))
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert "obligation_account_missing" in report.detail_codes


@pytest.mark.parametrize(
    "candidacy",
    [
        DispatchCandidacy.RUNNING_WORK_EXISTS,
        DispatchCandidacy.ATTEMPTS_EXHAUSTED,
        DispatchCandidacy.WITHDRAWN,
    ],
)
def test_a_candidacy_that_refuses_a_new_dispatch_is_not_selected(
    candidacy: DispatchCandidacy,
) -> None:
    report = report_for(consumer_view(dispatch_candidacy=candidacy))
    assert report.reason is ReadinessReason.NOT_SELECTED
    assert f"dispatch_candidacy_{candidacy!s}" in report.detail_codes


def test_an_adopted_live_duty_with_an_allowing_candidacy_is_not_reported_as_not_selected() -> None:
    assert ready_report().reason is not ReadinessReason.NOT_SELECTED


# --------------------------------------------------------------------------------------
# 4. WAITING_ORDER: release on acceptance, and UNKNOWN never settles (TG decision 1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "condition", "released"),
    [
        (OccurrenceOutcome.ACCEPTED, ReleaseCondition.ACCEPTED, True),
        (OccurrenceOutcome.ACCEPTED, ReleaseCondition.SETTLED_TERMINAL, True),
        (OccurrenceOutcome.RUNNING, ReleaseCondition.ACCEPTED, False),
        (OccurrenceOutcome.RUNNING, ReleaseCondition.SETTLED_TERMINAL, False),
        (OccurrenceOutcome.FAILED, ReleaseCondition.ACCEPTED, False),
        (OccurrenceOutcome.FAILED, ReleaseCondition.SETTLED_TERMINAL, True),
        (OccurrenceOutcome.CANCELLED, ReleaseCondition.ACCEPTED, False),
        (OccurrenceOutcome.CANCELLED, ReleaseCondition.SETTLED_TERMINAL, True),
        (OccurrenceOutcome.SETTLED_OTHER, ReleaseCondition.ACCEPTED, False),
        (OccurrenceOutcome.SETTLED_OTHER, ReleaseCondition.SETTLED_TERMINAL, True),
        (OccurrenceOutcome.UNKNOWN, ReleaseCondition.ACCEPTED, False),
        (OccurrenceOutcome.UNKNOWN, ReleaseCondition.SETTLED_TERMINAL, False),
    ],
)
def test_order_release_truth_table(
    outcome: OccurrenceOutcome, condition: ReleaseCondition, released: bool
) -> None:
    assert order_released(outcome, condition) is released


def test_a_running_predecessor_leaves_the_consumer_waiting_on_order() -> None:
    report = report_for(resolutions={OCC_A: OccurrenceOutcome.RUNNING})
    assert report.reason is ReadinessReason.WAITING_ORDER
    assert "order_not_released" in report.detail_codes


def test_a_failed_predecessor_does_not_satisfy_an_accepted_release_condition() -> None:
    report = report_for(resolutions={OCC_A: OccurrenceOutcome.FAILED})
    assert report.reason is ReadinessReason.WAITING_ORDER


def test_a_cancelled_predecessor_does_not_satisfy_an_accepted_release_condition() -> None:
    report = report_for(resolutions={OCC_A: OccurrenceOutcome.CANCELLED})
    assert report.reason is ReadinessReason.WAITING_ORDER


def test_a_failed_predecessor_satisfies_settled_terminal_when_the_contract_allows_it() -> None:
    plan = plan_of(snapshot_of(release_condition=ReleaseCondition.SETTLED_TERMINAL))
    report = report_for(plan=plan, resolutions={OCC_A: OccurrenceOutcome.FAILED})
    assert report.reason is not ReadinessReason.WAITING_ORDER


def test_an_unknown_predecessor_never_settles_even_under_settled_terminal() -> None:
    plan = plan_of(snapshot_of(release_condition=ReleaseCondition.SETTLED_TERMINAL))
    report = report_for(plan=plan, resolutions={OCC_A: OccurrenceOutcome.UNKNOWN})
    assert report.reason is ReadinessReason.WAITING_ORDER
    assert "order_outcome_unknown" in report.detail_codes


def test_a_predecessor_with_no_observed_outcome_is_waiting_not_released() -> None:
    report = report_for(resolutions={})
    assert report.reason is ReadinessReason.WAITING_ORDER
    assert "order_outcome_unobserved" in report.detail_codes


def test_an_accepted_predecessor_clears_the_order_gate() -> None:
    assert ready_report().reason is not ReadinessReason.WAITING_ORDER


def test_the_order_gate_names_the_predecessor_it_is_waiting_for() -> None:
    report = report_for(resolutions={OCC_A: OccurrenceOutcome.RUNNING})
    assert any(detail.subject == str(OCC_A) for detail in report.details)


# --------------------------------------------------------------------------------------
# 5. WAITING_DATA: each input_bindings problem kind stays visible (TG §4.3)
# --------------------------------------------------------------------------------------


def test_a_missing_input_resolution_is_waiting_data_not_a_failure() -> None:
    report = report_for(input_result=None)
    assert report.reason is ReadinessReason.WAITING_DATA
    assert "input_resolution_missing" in report.detail_codes


def test_a_pending_producer_is_waiting_data_and_keeps_its_problem_kind() -> None:
    report = report_for(input_result=input_problem(ResolutionProblemKind.PENDING_PRODUCER))
    assert report.reason is ReadinessReason.WAITING_DATA
    assert ResolutionProblemKind.PENDING_PRODUCER in report.data_problem_kinds


def test_a_not_disclosable_input_is_waiting_data_and_keeps_its_problem_kind() -> None:
    report = report_for(input_result=input_problem(ResolutionProblemKind.NOT_DISCLOSABLE))
    assert report.reason is ReadinessReason.WAITING_DATA
    assert ResolutionProblemKind.NOT_DISCLOSABLE in report.data_problem_kinds


def test_pending_producer_and_not_disclosable_are_different_details_of_one_reason() -> None:
    pending = report_for(input_result=input_problem(ResolutionProblemKind.PENDING_PRODUCER))
    revoked = report_for(input_result=input_problem(ResolutionProblemKind.NOT_DISCLOSABLE))
    assert pending.reason is revoked.reason is ReadinessReason.WAITING_DATA
    assert pending.data_problem_kinds != revoked.data_problem_kinds


def test_an_unfrozen_manifest_is_waiting_data_even_with_no_problems() -> None:
    report = report_for(input_result=ResolutionResult(manifest=manifest_of(pending=True)))
    assert report.reason is ReadinessReason.WAITING_DATA
    assert "manifest_not_frozen" in report.detail_codes


def test_a_provisional_binding_does_not_satisfy_a_required_port() -> None:
    report = report_for(input_result=ResolutionResult(manifest=manifest_of(provisional=True)))
    assert report.reason is ReadinessReason.WAITING_DATA
    assert "required_port_not_firmly_bound" in report.detail_codes


def test_a_resolution_result_with_problems_and_a_manifest_still_waits() -> None:
    result = ResolutionResult(
        manifest=manifest_of(),
        problems=(
            ResolutionProblem(
                kind=ResolutionProblemKind.PENDING_PRODUCER, detail="producer running"
            ),
        ),
    )
    assert report_for(input_result=result).reason is ReadinessReason.WAITING_DATA


def test_a_frozen_complete_manifest_clears_the_data_gate() -> None:
    assert ready_report().reason is not ReadinessReason.WAITING_DATA


# --------------------------------------------------------------------------------------
# 6. Evidence: START witnesses, the consumer they were issued to, and the epoch barrier
# --------------------------------------------------------------------------------------


def test_a_missing_start_witness_is_waiting_evidence() -> None:
    report = report_for(evidence=evidence_of(witnesses={}))
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert "witness_missing" in report.detail_codes


@pytest.mark.parametrize(
    "purpose",
    [
        WitnessPurpose.PLAN,
        WitnessPurpose.MAINTAIN,
        WitnessPurpose.ACCEPT,
        WitnessPurpose.CONTEXT,
        WitnessPurpose.DISCLOSE,
        WitnessPurpose.RECOVERY,
    ],
)
def test_only_a_start_purpose_witness_licenses_a_dispatch(purpose: WitnessPurpose) -> None:
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: witness(purpose=purpose)}))
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert "witness_purpose_not_start" in report.detail_codes


def test_a_witness_issued_to_another_task_is_that_task_s_permission_not_this_one_s() -> None:
    """Same rule as ``input_bindings`` WITNESS_CONSUMER_MISMATCH (§11.5 / AER §8.1)."""

    foreign = witness(
        consumer_ref=TypedRef(kind=TypedRefKind.TASK, id=str(T_A), revision=3, content_hash=HASH_A)
    )
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: foreign}))
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert "witness_consumer_mismatch" in report.detail_codes


def test_a_witness_whose_consumer_ref_is_not_a_task_is_refused() -> None:
    not_a_task = witness(
        consumer_ref=TypedRef(
            kind=TypedRefKind.METHOD, id=str(T_B), revision=1, content_hash=HASH_A
        )
    )
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: not_a_task}))
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert "witness_consumer_mismatch" in report.detail_codes


def test_a_witness_issued_to_this_very_task_clears_the_consumer_check() -> None:
    assert ready_report().reason is ReadinessReason.READY_CANDIDATE
    assert "witness_consumer_mismatch" not in ready_report().detail_codes


def test_a_start_purpose_usable_witness_clears_the_evidence_gate() -> None:
    assert ready_report().reason is ReadinessReason.READY_CANDIDATE


@pytest.mark.parametrize("truth", [TruthValue.UNKNOWN, TruthValue.CONFLICT, TruthValue.FALSE])
def test_a_witness_that_is_not_true_is_waiting_evidence(truth: TruthValue) -> None:
    report = report_for(
        evidence=evidence_of(
            witnesses={DIGEST_START: witness(truth=truth, decision=WitnessDecision.NEEDS_REVIEW)}
        )
    )
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert f"witness_truth_{truth!s}" in report.detail_codes


def test_a_blocked_witness_is_waiting_evidence() -> None:
    report = report_for(
        evidence=evidence_of(witnesses={DIGEST_START: witness(decision=WitnessDecision.BLOCKED)})
    )
    assert report.reason is ReadinessReason.WAITING_EVIDENCE
    assert "witness_decision_BLOCKED" in report.detail_codes


def test_an_unavailable_witness_is_observer_unavailable_not_waiting_evidence() -> None:
    report = report_for(
        evidence=evidence_of(
            witnesses={
                DIGEST_START: witness(
                    decision=WitnessDecision.UNAVAILABLE,
                    truth=TruthValue.UNKNOWN,
                    availability=Availability.UNAVAILABLE,
                )
            }
        )
    )
    assert report.reason is ReadinessReason.OBSERVER_UNAVAILABLE


def test_an_unreachable_observation_service_is_its_own_reason() -> None:
    report = report_for(evidence=evidence_of(observer_available=False))
    assert report.reason is ReadinessReason.OBSERVER_UNAVAILABLE
    assert "observation_service_unavailable" in report.detail_codes


def test_an_available_observer_is_not_reported_as_unavailable() -> None:
    assert ready_report().reason is not ReadinessReason.OBSERVER_UNAVAILABLE


def test_a_bumped_scope_epoch_returns_validity_recheck_pending() -> None:
    report = report_for(plan=plan_of(scope_epochs={SCOPE: 10}))
    assert report.reason is ReadinessReason.VALIDITY_RECHECK_PENDING
    assert "witness_epoch_stale" in report.detail_codes


def test_an_expired_witness_deadline_returns_validity_recheck_pending() -> None:
    report = report_for(
        evidence=evidence_of(witnesses={DIGEST_START: witness(not_after_ms=NOW_MS)})
    )
    assert report.reason is ReadinessReason.VALIDITY_RECHECK_PENDING
    assert "witness_deadline_passed" in report.detail_codes


@pytest.mark.parametrize("freshness", [Validity.STALE, Validity.REVOKED])
def test_a_witness_whose_support_is_no_longer_current_returns_validity_recheck_pending(
    freshness: Validity,
) -> None:
    """§11.5: STALE / REVOKED support is recomputed, not read as a weaker yes."""

    stale = witness(freshness=freshness, decision=WitnessDecision.NEEDS_REVIEW)
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: stale}))
    assert report.reason is ReadinessReason.VALIDITY_RECHECK_PENDING
    assert f"witness_freshness_{freshness!s}" in report.detail_codes


def test_a_scope_the_plan_cannot_confirm_returns_validity_recheck_pending() -> None:
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: witness(scope_id="other")}))
    assert report.reason is ReadinessReason.VALIDITY_RECHECK_PENDING
    assert "witness_epoch_unknown" in report.detail_codes


def test_a_current_epoch_and_live_deadline_do_not_trigger_a_recheck() -> None:
    assert ready_report().reason is not ReadinessReason.VALIDITY_RECHECK_PENDING


def test_a_witness_without_a_deadline_is_accepted_when_its_epoch_matches() -> None:
    report = report_for(evidence=evidence_of(witnesses={DIGEST_START: witness(not_after_ms=None)}))
    assert report.reason is ReadinessReason.READY_CANDIDATE


def test_a_maintain_phase_precondition_is_not_checked_at_start() -> None:
    binding = consumer_binding(
        precondition_refs=(
            PreconditionRef(condition_digest=DIGEST_MAINTAIN, phase=PreconditionPhase.MAINTAIN),
        )
    )
    plan = plan_of(snapshot_of(consumer=binding))
    report = report_for(
        consumer_view(binding=binding), plan=plan, evidence=evidence_of(witnesses={})
    )
    assert report.reason is ReadinessReason.READY_CANDIDATE


def test_an_undeclared_phase_defaults_to_start_and_is_checked() -> None:
    binding = consumer_binding(precondition_refs=(PreconditionRef(condition_digest=DIGEST_START),))
    plan = plan_of(snapshot_of(consumer=binding))
    report = report_for(
        consumer_view(binding=binding), plan=plan, evidence=evidence_of(witnesses={})
    )
    assert report.reason is ReadinessReason.WAITING_EVIDENCE


def test_an_unavailable_observer_outranks_a_merely_unusable_witness() -> None:
    binding = consumer_binding(
        precondition_refs=(
            PreconditionRef(condition_digest=DIGEST_START, phase=PreconditionPhase.SELECT),
            PreconditionRef(condition_digest=DIGEST_MAINTAIN, phase=PreconditionPhase.SELECT),
        )
    )
    plan = plan_of(snapshot_of(consumer=binding))
    unavailable = witness(
        decision=WitnessDecision.UNAVAILABLE,
        truth=TruthValue.UNKNOWN,
        availability=Availability.UNAVAILABLE,
    )
    blocked = witness(decision=WitnessDecision.BLOCKED)
    report = report_for(
        consumer_view(binding=binding),
        plan=plan,
        evidence=evidence_of(witnesses={DIGEST_START: blocked, DIGEST_MAINTAIN: unavailable}),
    )
    assert report.reason is ReadinessReason.OBSERVER_UNAVAILABLE


# --------------------------------------------------------------------------------------
# 7. WAITING_APPROVAL (contracts.resolution.ApprovalState)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.PENDING, ApprovalDecision.DENIED, ApprovalDecision.EXPIRED],
)
def test_an_approval_that_is_not_in_hand_is_waiting_approval(
    decision: ApprovalDecision,
) -> None:
    report = report_for(consumer_view(approval=ApprovalState(decision=decision)))
    assert report.reason is ReadinessReason.WAITING_APPROVAL
    assert f"approval_{decision!s}" in report.detail_codes


def test_a_grant_that_has_expired_is_not_a_standing_permission() -> None:
    """Invariant I09: "it was approved once" and "it is approved now" differ."""

    report = report_for(consumer_view(approval=granted_approval(expires_at_ms=NOW_MS)))
    assert report.reason is ReadinessReason.WAITING_APPROVAL
    assert "approval_expired_grant" in report.detail_codes


def test_a_live_grant_clears_the_approval_gate() -> None:
    report = report_for(consumer_view(approval=granted_approval(expires_at_ms=NOW_MS + 60_000)))
    assert report.reason is ReadinessReason.READY_CANDIDATE


def test_a_grant_without_an_expiry_clears_the_approval_gate() -> None:
    report = report_for(consumer_view(approval=granted_approval()))
    assert report.reason is ReadinessReason.READY_CANDIDATE


def test_a_task_that_needs_no_approval_clears_the_approval_gate() -> None:
    assert ready_report().reason is ReadinessReason.READY_CANDIDATE


# --------------------------------------------------------------------------------------
# 8. STALE_BINDING: two independent sources
# --------------------------------------------------------------------------------------


def test_a_moved_dispatch_generation_is_a_stale_binding() -> None:
    plan = plan_of(dispatch_generations={OCC_B: 8, OCC_A: 0, OCC_ROOT: 0})
    report = report_for(plan=plan)
    assert report.reason is ReadinessReason.STALE_BINDING
    assert "dispatch_generation_moved" in report.detail_codes


def test_a_moved_input_binding_revision_is_a_stale_binding() -> None:
    plan = plan_of(input_binding_revisions={T_B: 5, T_A: 0, T_ROOT: 0})
    report = report_for(plan=plan)
    assert report.reason is ReadinessReason.STALE_BINDING
    assert "input_binding_revision_moved" in report.detail_codes


def test_the_two_stale_binding_sources_are_reported_separately() -> None:
    plan = plan_of(
        dispatch_generations={OCC_B: 8, OCC_A: 0, OCC_ROOT: 0},
        input_binding_revisions={T_B: 5, T_A: 0, T_ROOT: 0},
    )
    codes = report_for(plan=plan).detail_codes
    assert "dispatch_generation_moved" in codes
    assert "input_binding_revision_moved" in codes


def test_matching_generation_and_revision_clear_the_stale_gate() -> None:
    assert ready_report().reason is not ReadinessReason.STALE_BINDING


def test_a_plan_that_records_no_generation_for_the_occurrence_does_not_invent_staleness() -> None:
    report = report_for(plan=plan_of(dispatch_generations={}, input_binding_revisions={}))
    assert report.reason is ReadinessReason.READY_CANDIDATE


# --------------------------------------------------------------------------------------
# 9. WAITING_OPERATION_UNKNOWN: an unsettled external effect is not a failure (TG §11.5)
# --------------------------------------------------------------------------------------


def test_a_conflicting_unknown_operation_blocks_the_successor() -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(EffectOutcome.UNKNOWN)))
    assert report.reason is ReadinessReason.WAITING_OPERATION_UNKNOWN
    assert "operation_UNKNOWN" in report.detail_codes


def test_a_conflicting_pending_operation_blocks_the_successor() -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(EffectOutcome.PENDING)))
    assert report.reason is ReadinessReason.WAITING_OPERATION_UNKNOWN
    assert "operation_PENDING" in report.detail_codes


def test_a_conflicting_partial_operation_blocks_the_successor() -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(EffectOutcome.PARTIAL)))
    assert report.reason is ReadinessReason.WAITING_OPERATION_UNKNOWN


def test_an_unknown_operation_that_does_not_conflict_does_not_block() -> None:
    report = report_for(
        evidence=evidence_of(pending_operations=operations(EffectOutcome.UNKNOWN, conflicts=False))
    )
    assert report.reason is ReadinessReason.READY_CANDIDATE


@pytest.mark.parametrize(
    "outcome",
    [EffectOutcome.APPLIED, EffectOutcome.NOT_APPLIED, EffectOutcome.NOT_HANDED_OFF],
)
def test_a_settled_operation_does_not_block_its_successor(outcome: EffectOutcome) -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(outcome)))
    assert report.reason is ReadinessReason.READY_CANDIDATE


def test_an_unknown_operation_is_not_reported_as_a_missing_input() -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(EffectOutcome.UNKNOWN)))
    assert report.reason is not ReadinessReason.WAITING_DATA
    assert report.reason is not ReadinessReason.OBSERVER_UNAVAILABLE


def test_the_blocking_operation_is_named_in_the_details() -> None:
    report = report_for(evidence=evidence_of(pending_operations=operations(EffectOutcome.UNKNOWN)))
    assert any(detail.subject == "op-1" for detail in report.details)


def test_a_control_record_for_another_operation_is_refused_by_the_contract() -> None:
    with pytest.raises(ContractError):
        PendingOperation(
            envelope=envelope("op-1"),
            state=operation_state(EffectOutcome.UNKNOWN, "op-2"),
        )


# --------------------------------------------------------------------------------------
# 10. READY_CANDIDATE, reason independence and precedence
# --------------------------------------------------------------------------------------


def test_the_happy_path_is_a_ready_candidate_with_no_details() -> None:
    report = ready_report()
    assert report.reason is ReadinessReason.READY_CANDIDATE
    assert report.details == ()
    assert report.ready is True


def test_every_reason_is_its_own_value_and_none_are_merged() -> None:
    assert len({str(reason) for reason in ReadinessReason}) == len(list(ReadinessReason))
    assert len(list(ReadinessReason)) == 12


def test_the_precedence_list_covers_every_reason_exactly_once() -> None:
    assert set(READINESS_PRECEDENCE) == set(ReadinessReason)
    assert len(READINESS_PRECEDENCE) == len(set(READINESS_PRECEDENCE))


def test_the_documented_precedence_is_the_order_the_gates_actually_run_in() -> None:
    assert gate_precedence() == READINESS_PRECEDENCE


def test_the_report_carries_the_plan_it_judged_against() -> None:
    report = ready_report()
    assert report.occurrence_id == OCC_B
    assert report.task_id == T_B
    assert report.mission_id == MISSION
    assert report.plan_revision == PlanRevision(5)
    assert report.evaluated_at_ms == NOW_MS


def test_the_report_read_set_records_what_was_actually_read() -> None:
    read_set = ready_report().read_set
    assert read_set.requirements_revision == 11
    assert read_set.manager_epoch == 2
    assert read_set.budget_grant_revision == 3
    assert any(item.id == str(T_B) for item in read_set.goal_revisions)
    assert any(item.id == str(MI_1) for item in read_set.method_revisions)
    assert any(item.id == "w-start" for item in read_set.observation_revisions)
    assert any(item.id == "acc-1" for item in read_set.acceptance_revisions)
    assert any(item.scope_id == SCOPE for item in read_set.scope_epochs)
    assert any(item.predicate for item in read_set.absences)


def test_the_two_dispatch_control_values_ride_the_task_lane() -> None:
    """A ReadItem carries one revision, so the task's three revisions need three ids."""

    ids = {item.id for item in ready_report().read_set.goal_revisions}
    assert str(T_B) in ids
    assert f"{T_B}#dispatch_generation" in ids
    assert f"{T_B}#input_binding_revision" in ids


def test_the_duty_is_recorded_in_the_obligation_lane() -> None:
    read_set = ready_report().read_set
    assert [item.id for item in read_set.obligation_revisions] == [str(O_B)]
    assert all(item.kind is ReadItemKind.OBLIGATION for item in read_set.obligation_revisions)


def test_the_approval_is_recorded_in_the_authority_lane() -> None:
    read_set = ready_report().read_set
    assert [item.id for item in read_set.authority_revisions] == [str(T_B)]
    assert all(item.kind is ReadItemKind.AUTHORITY for item in read_set.authority_revisions)


def test_the_authority_lane_carries_the_approval_record_version() -> None:
    report = report_for(consumer_view(approval=granted_approval(), approval_revision=3))
    assert report.read_set.authority_revisions[0].semantic_revision == 3


def test_a_duty_with_no_account_hashes_differently_from_a_live_one() -> None:
    absent = report_for(plan=plan_of(obligation_accounts={})).read_set
    live = ready_report().read_set
    assert absent.obligation_revisions[0].content_hash != live.obligation_revisions[0].content_hash


def test_a_refused_report_still_carries_a_read_set() -> None:
    report = report_for(resolutions={OCC_A: OccurrenceOutcome.RUNNING})
    assert isinstance(report.read_set, SemanticReadSet)
    assert report.read_set.requirements_revision == 11


# --------------------------------------------------------------------------------------
# 11. EligiblePrimitiveTask: only the gate builds it, and it is not derivable from one
# --------------------------------------------------------------------------------------


def eligible_kwargs() -> dict[str, Any]:
    return {
        "mission_id": MISSION,
        "plan_revision": PlanRevision(5),
        "occurrence_id": OCC_B,
        "task_id": T_B,
        "obligation_id": O_B,
        "contract_revision": ContractRevision(3),
        "contract_hash": HASH_A,
        "dispatch_generation": DispatchGeneration(7),
        "input_binding_revision": InputBindingRevision(4),
        "input_manifest_hash": HASH_D,
        "method_instance_id": MI_1,
        "requirement_refs": ("req-1",),
        "acceptance_ids": ("acc-1",),
        "read_set": SemanticReadSet(requirements_revision=11),
        "admitted_at_ms": NOW_MS,
    }


def test_constructing_an_eligible_primitive_task_directly_is_refused() -> None:
    with pytest.raises(EligibilityGateBypassed):
        EligiblePrimitiveTask(**eligible_kwargs())


def test_the_admission_mark_is_not_a_constructor_argument() -> None:
    assert "_token" not in inspect.signature(EligiblePrimitiveTask).parameters
    with pytest.raises(TypeError):
        EligiblePrimitiveTask(**eligible_kwargs(), _token="admitted")


def test_replacing_a_field_on_an_admitted_record_is_refused() -> None:
    """``replace`` would carry the admission across to a record no gate judged."""

    admitted = admitted_task()
    with pytest.raises(EligibilityGateBypassed):
        dataclasses.replace(admitted, task_id=T_A, occurrence_id=OCC_A)


def test_copying_an_admitted_record_is_refused() -> None:
    admitted = admitted_task()
    with pytest.raises(EligibilityGateBypassed):
        copy.copy(admitted)
    with pytest.raises(EligibilityGateBypassed):
        copy.deepcopy(admitted)


def test_pickling_an_admitted_record_is_refused() -> None:
    admitted = admitted_task()
    with pytest.raises(EligibilityGateBypassed):
        pickle.dumps(admitted)


def test_admit_for_dispatch_builds_one_from_a_ready_report() -> None:
    admitted = admitted_task()
    assert isinstance(admitted, EligiblePrimitiveTask)
    assert admitted.task_id == T_B
    assert admitted.gate_passed is True


@pytest.mark.parametrize("reason_case", ["order", "data", "evidence", "approval", "stale"])
def test_admit_for_dispatch_refuses_every_non_ready_report(reason_case: str) -> None:
    cases: dict[str, dict[str, Any]] = {
        "order": {"resolutions": {OCC_A: OccurrenceOutcome.RUNNING}},
        "data": {"input_result": input_problem(ResolutionProblemKind.PENDING_PRODUCER)},
        "evidence": {"evidence": evidence_of(witnesses={})},
        "approval": {"view": consumer_view(approval=ApprovalState(ApprovalDecision.PENDING))},
        "stale": {"plan": plan_of(dispatch_generations={OCC_B: 99})},
    }
    report = report_for(**cases[reason_case])
    assert report.reason is not ReadinessReason.READY_CANDIDATE
    with pytest.raises(NotEligible):
        admit_for_dispatch(report, consumer_view(), plan_of(), manifest_of(), now_ms=NOW_MS)


def test_the_admitted_record_binds_every_version_the_dispatch_depends_on() -> None:
    admitted = admitted_task()
    assert admitted.contract_revision == ContractRevision(3)
    assert admitted.contract_hash == HASH_A
    assert admitted.dispatch_generation == DispatchGeneration(7)
    assert admitted.input_binding_revision == InputBindingRevision(4)
    assert admitted.input_manifest_hash == manifest_of().manifest_hash()
    assert admitted.method_instance_id == MI_1
    assert admitted.requirement_refs == ("req-1",)
    assert admitted.acceptance_ids == ("acc-1",)
    assert admitted.read_set == ready_report().read_set
    assert admitted.plan_revision == PlanRevision(5)
    assert admitted.mission_id == MISSION


def test_the_admitted_record_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        admitted_task().task_id = T_A  # type: ignore[misc]


def test_an_unfrozen_manifest_cannot_be_admitted() -> None:
    with pytest.raises(ManifestNotFrozen):
        admit_for_dispatch(
            ready_report(), consumer_view(), plan_of(), manifest_of(pending=True), now_ms=NOW_MS
        )


def test_a_manifest_belonging_to_another_consumer_cannot_be_admitted() -> None:
    foreign = InputManifest(consumer_task_ref=T_A, bindings=())
    with pytest.raises(NotEligible):
        admit_for_dispatch(ready_report(), consumer_view(), plan_of(), foreign, now_ms=NOW_MS)


def test_a_report_for_another_occurrence_cannot_admit_this_view() -> None:
    other = consumer_view(occurrence_id=OCC_A, task_id=T_A)
    with pytest.raises(NotEligible):
        admit_for_dispatch(ready_report(), other, plan_of(), manifest_of(), now_ms=NOW_MS)


def test_a_report_from_another_plan_revision_cannot_admit_a_dispatch() -> None:
    later = replace(snapshot_of(), plan_revision=PlanRevision(6))
    with pytest.raises(NotEligible):
        admit_for_dispatch(
            ready_report(), consumer_view(), plan_of(later), manifest_of(), now_ms=NOW_MS
        )


def test_a_report_from_another_requirements_revision_cannot_admit_a_dispatch() -> None:
    with pytest.raises(NotEligible):
        admit_for_dispatch(
            ready_report(),
            consumer_view(),
            plan_of(requirements_revision=12),
            manifest_of(),
            now_ms=NOW_MS,
        )


def test_a_report_taken_at_another_scope_epoch_cannot_admit_a_dispatch() -> None:
    with pytest.raises(NotEligible):
        admit_for_dispatch(
            ready_report(),
            consumer_view(),
            plan_of(scope_epochs={SCOPE: 10}),
            manifest_of(),
            now_ms=NOW_MS,
        )


def test_the_admitted_record_says_it_is_not_a_security_token() -> None:
    doc = EligiblePrimitiveTask.__doc__ or ""
    assert "not a security token" in doc
    assert "handoff" in doc


# --------------------------------------------------------------------------------------
# 12. stale_after: every read-set lane invalidates a cached readiness
# --------------------------------------------------------------------------------------


def observed_now() -> SemanticReadSet:
    return ready_report().read_set


def test_an_unchanged_read_set_is_not_stale() -> None:
    assert stale_after(ready_report(), observed_now()) is False


def test_a_changed_requirements_revision_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), requirements_revision=12)) is True


def test_a_changed_manager_epoch_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), manager_epoch=3)) is True


def test_a_changed_budget_grant_revision_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), budget_grant_revision=4)) is True


def test_a_changed_task_contract_revision_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        goal_revisions=tuple(
            replace(item, semantic_revision=4) if item.id == str(T_B) else item
            for item in before.goal_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_method_instance_revision_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        method_revisions=tuple(
            replace(item, content_hash=HASH_C) for item in before.method_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_fact_revision_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        observation_revisions=tuple(
            replace(item, semantic_revision=18) for item in before.observation_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_acceptance_revision_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        acceptance_revisions=tuple(
            replace(item, content_hash=HASH_B) for item in before.acceptance_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_support_set_digest_invalidates_a_cached_readiness() -> None:
    current = replace(
        observed_now(),
        support_sets=(SupportSetRead(support_set_id="ss-1", revision=4, member_digest=HASH_D),),
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_support_set_revision_alone_invalidates_a_cached_readiness() -> None:
    """The members can be identical and the set still have been re-derived."""

    current = replace(
        observed_now(),
        support_sets=(SupportSetRead(support_set_id="ss-1", revision=5, member_digest=HASH_C),),
    )
    assert stale_after(ready_report(), current) is True


def test_a_bumped_scope_epoch_invalidates_a_cached_readiness() -> None:
    current = replace(
        observed_now(), scope_epochs=(ScopeEpochRead(scope_id=SCOPE, validity_epoch=10),)
    )
    assert stale_after(ready_report(), current) is True


def test_a_changed_absence_range_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before, absences=tuple(replace(item, range_revision=7) for item in before.absences)
    )
    assert stale_after(ready_report(), current) is True


def test_a_moved_dispatch_generation_invalidates_a_cached_readiness() -> None:
    current = report_for(plan=plan_of(dispatch_generations={OCC_B: 8})).read_set
    assert stale_after(ready_report(), current) is True


def test_a_moved_input_binding_revision_invalidates_a_cached_readiness() -> None:
    current = report_for(plan=plan_of(input_binding_revisions={T_B: 5})).read_set
    assert stale_after(ready_report(), current) is True


def test_a_changed_approval_invalidates_a_cached_readiness() -> None:
    current = report_for(consumer_view(approval=granted_approval(), approval_revision=2)).read_set
    assert stale_after(ready_report(), current) is True


def test_a_changed_authority_lane_entry_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        authority_revisions=tuple(
            replace(item, content_hash=HASH_B) for item in before.authority_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_an_authority_lane_entry_that_disappeared_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), authority_revisions=())) is True


def test_a_changed_obligation_lane_entry_invalidates_a_cached_readiness() -> None:
    before = observed_now()
    current = replace(
        before,
        obligation_revisions=tuple(
            replace(item, content_hash=HASH_C) for item in before.obligation_revisions
        ),
    )
    assert stale_after(ready_report(), current) is True


def test_an_obligation_lane_entry_that_disappeared_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), obligation_revisions=())) is True


def test_a_changed_obligation_lifecycle_invalidates_a_cached_readiness() -> None:
    plan = plan_of(obligation_accounts=all_accounts(account(O_B, ObligationLifecycle.CANCELLED)))
    assert stale_after(ready_report(), report_for(plan=plan).read_set) is True


def test_a_withdrawn_demand_invalidates_a_cached_readiness() -> None:
    plan = plan_of(obligation_accounts=all_accounts(account(O_B, has_admitted_demand=False)))
    assert stale_after(ready_report(), report_for(plan=plan).read_set) is True


def test_a_read_item_that_disappeared_invalidates_a_cached_readiness() -> None:
    assert stale_after(ready_report(), replace(observed_now(), goal_revisions=())) is True


def test_a_wider_current_observation_does_not_by_itself_invalidate() -> None:
    before = observed_now()
    current = replace(
        before,
        goal_revisions=(
            *before.goal_revisions,
            ReadItem(kind=ReadItemKind.TASK, id=str(T_A), semantic_revision=1, content_hash=HASH_B),
        ),
    )
    assert stale_after(ready_report(), current) is False


# --------------------------------------------------------------------------------------
# 13. The three frontiers (TG §8.1 / plan §24.1 decision 6)
# --------------------------------------------------------------------------------------


def all_reports() -> dict[OccurrenceId, ReadinessReport]:
    plan = plan_of()
    producer_view = TaskView(
        occurrence_id=OCC_A,
        task_id=T_A,
        binding=binding_for(
            T_A,
            O_A,
            TaskForm.PRIMITIVE,
            output_ports=(PortSpec(port_key="out", schema_ref=vref("schema-in")),),
        ),
    )
    return {
        OCC_ROOT: report_for(root_view(), plan=plan),
        OCC_A: report_for(
            producer_view,
            plan=plan,
            input_result=ResolutionResult(manifest=InputManifest(consumer_task_ref=T_A)),
            evidence=evidence_of(witnesses={}),
        ),
        OCC_B: report_for(plan=plan),
    }


def test_a_compound_task_belongs_to_the_planning_frontier() -> None:
    assert OCC_ROOT in PlanningFrontier.compute(plan_of(), all_reports()).occurrences


def test_a_compound_task_never_reaches_the_execution_frontier() -> None:
    assert OCC_ROOT not in ExecutionFrontier.compute(plan_of(), all_reports()).occurrences


def test_a_ready_primitive_belongs_to_the_execution_frontier() -> None:
    assert OCC_B in ExecutionFrontier.compute(plan_of(), all_reports()).occurrences
    assert OCC_B not in PlanningFrontier.compute(plan_of(), all_reports()).occurrences


def test_a_primitive_waiting_on_order_is_in_neither_frontier() -> None:
    reports = dict(all_reports())
    reports[OCC_B] = report_for(resolutions={OCC_A: OccurrenceOutcome.RUNNING})
    assert OCC_B not in ExecutionFrontier.compute(plan_of(), reports).occurrences
    assert OCC_B not in PlanningFrontier.compute(plan_of(), reports).occurrences


def test_an_unadopted_occurrence_is_in_neither_frontier() -> None:
    """An occurrence no adopted method reaches is outside both frontiers.

    The reason code alone is not the membership test: the compound interception
    deliberately runs before the selection gate, so adoption is applied here.
    """

    lonely_occurrence = OccurrenceId("occ-c")
    lonely_task = TaskRef("t-c")
    lonely_binding = binding_for(lonely_task, ObligationId("o-c"), TaskForm.COMPOUND)
    base = snapshot_of()
    snapshot = replace(
        base,
        occurrences=(
            *base.occurrences,
            OccurrenceSpec(
                occurrence_id=lonely_occurrence,
                task_id=lonely_task,
                obligation_id=ObligationId("o-c"),
                form=TaskForm.COMPOUND,
            ),
        ),
        task_bindings=(*base.task_bindings, lonely_binding),
    )
    plan = plan_of(snapshot)
    view = TaskView(occurrence_id=lonely_occurrence, task_id=lonely_task, binding=lonely_binding)
    report = report_for(view, plan=plan)
    assert report.reason is ReadinessReason.NEEDS_REFINEMENT
    reports = {lonely_occurrence: report}
    assert PlanningFrontier.compute(plan, reports).occurrences == ()
    assert ExecutionFrontier.compute(plan, reports).occurrences == ()


def test_a_damaged_graph_puts_a_task_in_neither_frontier() -> None:
    plan = plan_of(integrity_error=GraphIntegrityError(remaining=("n1",), cycle=()))
    reports = {OCC_B: report_for(plan=plan), OCC_ROOT: report_for(root_view(), plan=plan)}
    assert PlanningFrontier.compute(plan, reports).occurrences == ()
    assert ExecutionFrontier.compute(plan, reports).occurrences == ()


def test_the_planning_frontier_groups_its_members_by_reason() -> None:
    frontier = PlanningFrontier.compute(plan_of(), all_reports())
    assert frontier.by_reason[ReadinessReason.NEEDS_REFINEMENT] == (OCC_ROOT,)


def test_admitted_dispatch_is_not_decided_in_this_module() -> None:
    assert AdmittedDispatch.not_decided_here().admitted == ()


def test_no_readiness_reason_by_itself_admits_a_dispatch() -> None:
    assert ADMITTED_DISPATCH_REASONS == frozenset()


def test_the_planning_reasons_and_the_ready_candidate_reason_do_not_overlap() -> None:
    assert ReadinessReason.READY_CANDIDATE not in PLANNING_REASONS


# --------------------------------------------------------------------------------------
# 14. Purity, isolation and the legacy READY helper
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("banned", ["storage", "scheduling", "sqlite", "orchestration_store"])
def test_the_module_imports_no_persistence_or_scheduling(banned: str) -> None:
    source = Path(eligibility_module.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert banned not in stripped


def test_evaluate_readiness_is_deterministic() -> None:
    assert report_for() == report_for()


def test_evaluate_readiness_does_not_mutate_its_inputs() -> None:
    view = consumer_view()
    plan = plan_of()
    evidence = evidence_of()
    resolutions = dict(RELEASED)
    before = (view, plan, evidence, dict(resolutions))
    evaluate_readiness(view, plan, resolutions, evidence, input_ok(), now_ms=NOW_MS)
    assert (view, plan, evidence, resolutions) == before


@pytest.mark.parametrize("status", [str(item) for item in TaskStatus])
def test_a_legacy_status_never_admits_a_dispatch(status: str) -> None:
    assert legacy_ready_is_not_eligibility(status, form=TaskForm.PRIMITIVE).admits_dispatch is False


def test_the_legacy_helper_intercepts_a_compound_with_needs_refinement() -> None:
    verdict = legacy_ready_is_not_eligibility(str(TaskStatus.READY), form=TaskForm.COMPOUND)
    assert verdict.gate_reason is ReadinessReason.NEEDS_REFINEMENT
    assert verdict.admits_dispatch is False


def test_the_legacy_helper_sends_a_primitive_back_through_evaluate_readiness() -> None:
    verdict = legacy_ready_is_not_eligibility(str(TaskStatus.READY), form=TaskForm.PRIMITIVE)
    assert verdict.gate_reason is None
    assert "evaluate_readiness" in verdict.explanation


def test_evaluate_readiness_takes_now_ms_as_a_keyword_only_argument() -> None:
    parameter = inspect.signature(evaluate_readiness).parameters["now_ms"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_a_readiness_report_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ready_report().reason = ReadinessReason.NOT_SELECTED  # type: ignore[misc]


def test_a_task_view_rejects_a_blank_occurrence_id() -> None:
    with pytest.raises(ContractError):
        TaskView(occurrence_id=OccurrenceId(""), task_id=T_B)


def test_a_task_view_rejects_an_approval_that_is_not_the_contract_type() -> None:
    with pytest.raises(ContractError):
        TaskView(occurrence_id=OCC_B, task_id=T_B, approval="GRANTED")  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# 15. Mutation self-proofs: each gate is load-bearing
# --------------------------------------------------------------------------------------


def _blind(_context: object) -> None:
    """A gate that never refuses anything."""

    return None


def test_mutant_refinement_gate_lets_a_compound_through(monkeypatch: pytest.MonkeyPatch) -> None:
    assert report_for(root_view()).reason is ReadinessReason.NEEDS_REFINEMENT
    monkeypatch.setattr(eligibility_module, "_refinement_gate", _blind)
    assert report_for(root_view()).reason is not ReadinessReason.NEEDS_REFINEMENT


def test_mutant_selection_gate_lets_a_halted_mission_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_of(mission_admits_work=False)
    assert report_for(plan=plan).reason is ReadinessReason.NOT_SELECTED
    monkeypatch.setattr(eligibility_module, "_selection_gate", _blind)
    assert report_for(plan=plan).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_order_gate_lets_a_running_predecessor_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = {OCC_A: OccurrenceOutcome.RUNNING}
    assert report_for(resolutions=resolutions).reason is ReadinessReason.WAITING_ORDER
    monkeypatch.setattr(eligibility_module, "_order_gate", _blind)
    assert report_for(resolutions=resolutions).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_data_gate_lets_an_unresolved_input_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = input_problem(ResolutionProblemKind.PENDING_PRODUCER)
    assert report_for(input_result=result).reason is ReadinessReason.WAITING_DATA
    monkeypatch.setattr(eligibility_module, "_data_gate", _blind)
    assert report_for(input_result=result).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_evidence_gate_lets_a_missing_witness_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = evidence_of(witnesses={})
    assert report_for(evidence=evidence).reason is ReadinessReason.WAITING_EVIDENCE
    monkeypatch.setattr(eligibility_module, "_evidence_gate", _blind)
    assert report_for(evidence=evidence).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_approval_gate_lets_an_unapproved_task_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = consumer_view(approval=ApprovalState(ApprovalDecision.PENDING))
    assert report_for(view).reason is ReadinessReason.WAITING_APPROVAL
    monkeypatch.setattr(eligibility_module, "_approval_gate", _blind)
    assert report_for(view).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_stale_gate_lets_an_expired_generation_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_of(dispatch_generations={OCC_B: 99})
    assert report_for(plan=plan).reason is ReadinessReason.STALE_BINDING
    monkeypatch.setattr(eligibility_module, "_stale_gate", _blind)
    assert report_for(plan=plan).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_operation_gate_lets_an_unknown_effect_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = evidence_of(pending_operations=operations(EffectOutcome.UNKNOWN))
    assert report_for(evidence=evidence).reason is ReadinessReason.WAITING_OPERATION_UNKNOWN
    monkeypatch.setattr(eligibility_module, "_operation_gate", _blind)
    assert report_for(evidence=evidence).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_integrity_gate_lets_a_missing_binding_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = consumer_view(binding=None)
    assert report_for(view).reason is ReadinessReason.GRAPH_INTEGRITY
    monkeypatch.setattr(eligibility_module, "_integrity_gate", _blind)
    assert report_for(view).reason is not ReadinessReason.GRAPH_INTEGRITY


def test_mutant_witness_consumer_check_lets_a_foreign_witness_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer check specifically, not just the evidence gate as a whole."""

    foreign = witness(
        consumer_ref=TypedRef(kind=TypedRefKind.TASK, id=str(T_A), revision=3, content_hash=HASH_A)
    )
    evidence = evidence_of(witnesses={DIGEST_START: foreign})
    assert report_for(evidence=evidence).reason is ReadinessReason.WAITING_EVIDENCE

    real = eligibility_module._witness_verdict

    def blind_consumer(
        witness_value: Any, digest: str, *, consumer_task: Any, **kwargs: Any
    ) -> Any:
        return real(
            witness_value,
            digest,
            consumer_task=(
                witness_value.consumer_ref.id if witness_value is not None else consumer_task
            ),
            **kwargs,
        )

    monkeypatch.setattr(eligibility_module, "_witness_verdict", blind_consumer)
    assert report_for(evidence=evidence).reason is ReadinessReason.READY_CANDIDATE


def test_mutant_same_origin_check_admits_a_report_from_another_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = plan_of(replace(snapshot_of(), plan_revision=PlanRevision(6)))
    with pytest.raises(NotEligible):
        admit_for_dispatch(ready_report(), consumer_view(), later, manifest_of(), now_ms=NOW_MS)
    monkeypatch.setattr(eligibility_module, "_same_origin", lambda report, plan: None)
    admitted = admit_for_dispatch(
        ready_report(), consumer_view(), later, manifest_of(), now_ms=NOW_MS
    )
    assert admitted.plan_revision == PlanRevision(6)
