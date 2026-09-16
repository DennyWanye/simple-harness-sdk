# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c: ``accept_review`` / ``commit_goal_resolution`` (AER §6–§7, §25.1 decision 4).

Four properties, ordered by what they would cost to get wrong:

1. **"Wrongly declared complete" = 0.**  A Mission root ``GoalResolution`` is formed
   only by ``commit_goal_resolution``, only from the success formula plus the final
   acceptance, and — when the requirements declare a delivery contract — only once a
   ``DeliveryReceipt`` says the output actually travelled that far.  A compound whose
   required child has no valid ``Acceptance`` is refused; a root requirement the
   resolution does not carry is refused **and writes nothing**.
2. **Two actions, not one.**  ``ACCEPT`` writes an ``Acceptance`` and leaves the duty
   open; only the resolution moves it to ``SATISFIED`` with its ``resolution_ref``.
3. **A commit re-checks current applicability.**  The scope epoch, the support-set
   *revision* and the ``purpose=ACCEPT`` witness are re-read inside the transaction;
   a stale one refuses with nothing written.  Independence and in-flight facts have
   to be *stated* — the command cannot be built without them.
4. **Nothing is half-written.**  A failure anywhere in the write half leaves no
   acceptance, no resolution, no receipt and an untouched duty lifecycle.

The mixin is composed onto ``CommitService`` here rather than added to its base
list: the base-class line is the second half of P2.3c, so this slice proves the
mixin works without changing the class every other suite constructs.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import Any

import pytest

from agent_orchestrator.contracts import Budget, ContractError
from agent_orchestrator.contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    AbsenceRead,
    ChildBinding,
    MethodInstanceDraft,
    MethodInstanceId,
    MethodRef,
    ObligationId,
    OccurrenceId,
    ReadItem,
    ReadItemKind,
    Requiredness,
    ScopeEpochRead,
    SemanticReadSet,
    SupportSetRead,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.obligations import (
    Obligation,
    ObligationLifecycle,
    ShapeChange,
)
from agent_orchestrator.contracts.resolution import (
    Acceptance,
    AcceptanceId,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    DeliveryReceipt,
    DeliveryStage,
    EvaluationKind,
    GoalResolution,
    GoalResolutionId,
    RequiredEvidencePolicy,
    RequirementClass,
    RequirementsRevision,
    RequirementsRevisionId,
    ResolutionCriterion,
    ReviewBinding,
    ReviewPackage,
    ReviewPackageId,
    ReviewPurpose,
    ReviewRecord,
    ReviewRecordId,
    ReviewVerdict,
    WorkspaceAccess,
)
from agent_orchestrator.contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    VersionedRef,
    content_hash_of,
)
from agent_orchestrator.knowledge.validity import NO_SUBJECT
from agent_orchestrator.orchestrator._read_set import SemanticReadSetChecker
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.orchestrator.plan_commits import HIERARCHICAL_SEMANTICS, LEGACY_SEMANTICS
from agent_orchestrator.orchestrator.resolution_commits import (
    ACCEPTANCE_COMMITTED,
    ACCEPTANCE_KIND,
    DELIVERY_STAGE_ORDER,
    GOAL_RESOLUTION_COMMITTED,
    GOAL_RESOLUTION_KIND,
    AcceptReviewCommand,
    CommitGoalResolutionCommand,
    ResolutionCommitRejected,
    ResolutionCommitsMixin,
    ResolutionPrincipal,
    command_idempotency_key,
    delivery_reached,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.obligation_store import ObligationStore
from agent_orchestrator.storage.store import Store, StoreError
from agent_orchestrator.verification.acceptance_rules import (
    CompoundFacts,
    ExecutionPosture,
    IndependenceFacts,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

LEAF_TASK = "task-leaf"
LEAF_DUTY = "obl-leaf"
ROOT_TASK = "task-root"
ROOT_DUTY = "obl-root"
INSTANCE = "mi-1"
LEAF_OCCURRENCE = "occ-leaf"
ROOT_OCCURRENCE = "occ-root"
SCOPE = "mission"
NOW_MS = 1_000_000
PRINCIPAL = "reviewer-commit-1"
CRITERION = "c-done"
ROOT_CRITERION = "c-delivered"


# ======================================================================================
# The service under test: ``CommitService`` itself (P2.3c part 2 wired the mixin in)
# ======================================================================================

#: P2.3c part 1 composed ``ResolutionCommitsMixin`` onto ``CommitService`` here, in a
#: local subclass, because adding it to the real base list was part 2's job and the
#: P2.3b review was still in flight.  Part 2 did add it, so the alias now *is* the
#: service — and the assertion below is what stops the alias from quietly becoming a
#: second, divergent composition again.
ResolutionService = CommitService


def test_the_accept_side_is_wired_into_the_commit_service() -> None:
    """The two entry points are on the deployment's one writing authority (§18.2)."""

    assert issubclass(CommitService, ResolutionCommitsMixin)
    assert CommitService.accept_review is ResolutionCommitsMixin.accept_review
    assert CommitService.commit_goal_resolution is ResolutionCommitsMixin.commit_goal_resolution
    assert CommitService.record_delivery_receipt is ResolutionCommitsMixin.record_delivery_receipt


def test_the_accept_half_shares_no_private_name_with_anything_before_it_in_the_mro() -> None:
    """No base on ``CommitService`` may silently take (or shadow) an accept-side helper.

    P2.3c part 2c, review F10: this used to compare ``ResolutionCommitsMixin`` against
    ``PlanCommitsMixin`` alone.  ``CommitService`` mixes in eleven other bases and
    ``ResolutionCommitsMixin`` sits **last** in the MRO, so a name collision with any
    one of them shadows the accept-side method just as completely — and the pair-wise
    check would not see it.  The whole MRO is walked instead, which also means a base
    added later is covered without anyone remembering to extend a list.
    """

    from agent_orchestrator.orchestrator.commit_service import CommitService

    def own(kind: type) -> set[str]:
        return {name for name in vars(kind) if not name.startswith("__")}

    accept_names = own(ResolutionCommitsMixin)
    assert accept_names, "the accept half defines nothing; the guard would be vacuous"
    bases = [
        kind
        for kind in CommitService.__mro__
        if kind not in (object, CommitService, ResolutionCommitsMixin)
    ]
    assert len(bases) >= 10, f"the MRO shrank unexpectedly: {[k.__name__ for k in bases]}"
    collisions = {
        kind.__name__: sorted(own(kind) & accept_names)
        for kind in bases
        if own(kind) & accept_names
    }
    assert collisions == {}, collisions


# ======================================================================================
# Builders
# ======================================================================================


def tref(kind: TypedRefKind, ident: str, *, digest: str = HASH_A) -> TypedRef:
    return TypedRef(kind=kind, id=ident, revision=1, content_hash=digest)


def receipt_ref(ident: str) -> TypedRef:
    """A receipt the *system* attributed to a dispatched tool (AER §5.4)."""

    return TypedRef(
        kind=TypedRefKind.TOOL_RECEIPT,
        id=ident,
        revision=1,
        content_hash=HASH_B,
        produced_by=Provenance.TOOL,
    )


def vref(ident: str) -> VersionedRef:
    return VersionedRef(id=ident, version=1, content_hash=HASH_A)


def goal_signature(ident: str = "leaf-goal") -> Any:
    from agent_orchestrator.contracts.htn import GoalSignature

    return GoalSignature(
        signature_id=ident,
        version=1,
        parameter_schema_ref=vref("params"),
        output_schema_ref=vref("outputs"),
        statement=f"the {ident} is produced",
        coverage_criteria=(CRITERION,),
    )


def task_binding(
    task_id: str, duty: str, form: TaskForm, **overrides: Any
) -> TaskSemanticBindingV1:
    fields: dict[str, Any] = {
        "task_id": TaskRef(task_id),
        "obligation_id": ObligationId(duty),
        "contract_revision": 1,
        "contract_hash": HASH_A,
        "form": form,
        "goal_signature": goal_signature(),
        "requirement_refs": (CRITERION,),
        "semantic_scope": SCOPE,
    }
    if form is TaskForm.PRIMITIVE:
        fields["operator_ref"] = vref("operator")
    fields.update(overrides)
    return TaskSemanticBindingV1(**fields)


def criterion(
    criterion_id: str = CRITERION,
    *,
    requirement_class: RequirementClass = RequirementClass.REQUIRED_OUTCOME,
    checks: tuple[str, ...] = ("leaf-suite",),
) -> Criterion:
    return Criterion(
        criterion_id=criterion_id,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"{criterion_id} holds",
        requirement_class=requirement_class,
        evaluation_kind=EvaluationKind.DETERMINISTIC,
        required_evidence_policy=RequiredEvidencePolicy(required_check_ids=checks),
    )


def requirements(
    mission_id: str,
    *,
    revision: int = 1,
    criteria: tuple[Criterion, ...] = (),
    delivery_contract_ref: str | None = None,
) -> RequirementsRevision:
    chosen = criteria or (criterion(),)
    expression: Any = CriterionExpr(chosen[0].criterion_id)
    if len(chosen) > 1:
        from agent_orchestrator.contracts.resolution import AllExpr

        expression = AllExpr(children=tuple(CriterionExpr(item.criterion_id) for item in chosen))
    return RequirementsRevision(
        revision_id=RequirementsRevisionId(f"req-{revision}"),
        mission_id=mission_id,
        revision=revision,
        criteria=chosen,
        success_expression=expression,
        delivery_contract_ref=delivery_contract_ref,
    )


def review_binding(
    mission_id: str, duty: str, task_id: str, manifest_hash: str, *, revision: int = 1
) -> ReviewBinding:
    return ReviewBinding(
        mission_id=mission_id,
        obligation_id=duty,
        subject_ref=tref(TypedRefKind.TASK, task_id),
        requirements_revision=revision,
        input_manifest_hash=manifest_hash,
        policy_ref=tref(TypedRefKind.SOURCE, "review-policy-1"),
    )


def review_package(
    package_id: str,
    binding: ReviewBinding,
    revision: RequirementsRevision,
    *,
    purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT,
    method_instance_id: str | None = None,
) -> ReviewPackage:
    return ReviewPackage(
        package_id=ReviewPackageId(package_id),
        purpose=purpose,
        binding=binding,
        criteria=revision.criteria,
        success_expression=revision.success_expression,
        candidate_refs=(tref(TypedRefKind.ARTIFACT, f"cand-{package_id}", digest=HASH_C),),
        producer_agent_ids=("agent-worker",),
        reviewer_workspace_access=WorkspaceAccess.READ_ONLY,
        requirements_content_hash=revision.content_hash(),
        method_instance_id=(
            None if method_instance_id is None else MethodInstanceId(method_instance_id)
        ),
    )


def review_record(
    record_id: str,
    package: ReviewPackage,
    *,
    verdict: ReviewVerdict = ReviewVerdict.ACCEPT,
    outcomes: tuple[CriterionOutcome, ...] | None = None,
) -> ReviewRecord:
    chosen = outcomes or tuple(
        CriterionOutcome(
            criterion_id=item.criterion_id,
            verdict=CriterionVerdict.PASS,
            check_execution=CheckExecution.SUCCEEDED,
            evidence_refs=(receipt_ref(f"{item.criterion_id}-receipt"),),
        )
        for item in package.criteria
    )
    return ReviewRecord(
        record_id=ReviewRecordId(record_id),
        package_id=package.package_id,
        purpose=package.purpose,
        binding=package.binding,
        reviewer_agent_id="agent-reviewer",
        reviewer_turn_id="turn-1",
        evidence_manifest_hash=HASH_A,
        criteria=chosen,
        verdict=verdict,
    )


def accept_witness(
    witness_id: str,
    task_id: str,
    *,
    purpose: WitnessPurpose = WitnessPurpose.ACCEPT,
    scope_epoch: int = 0,
    not_after_ms: int | None = None,
    freshness: Validity = Validity.CURRENT,
    truth: TruthValue = TruthValue.TRUE,
    support_revision: int = 1,
) -> ValidityWitness:
    return ValidityWitness(
        witness_id=witness_id,
        consumer_ref=tref(TypedRefKind.TASK, task_id),
        purpose=purpose,
        truth=truth,
        freshness=freshness,
        availability=Availability.READABLE,
        decision=WitnessDecision.USABLE,
        scope_id=SCOPE,
        scope_epoch=scope_epoch,
        support_revision=support_revision,
        as_of_ms=NOW_MS - 1_000,
        not_after_ms=not_after_ms,
    )


def independence() -> IndependenceFacts:
    return IndependenceFacts(producer_agent_ids=("agent-worker",))


def read_set(**overrides: Any) -> SemanticReadSet:
    fields: dict[str, Any] = {
        # 2 is the latest revision this world holds; a read-set records what the
        # *Mission* is at, not what the subject's own package was cut from.
        "requirements_revision": 2,
        "manager_epoch": 0,
        "scope_epochs": (ScopeEpochRead(scope_id=SCOPE, validity_epoch=0),),
    }
    fields.update(overrides)
    return SemanticReadSet(**fields)


@dataclass
class World:
    service: ResolutionService
    mission: Any
    revision: RequirementsRevision
    manifest_hash: str
    leaf_package: ReviewPackage
    leaf_record: ReviewRecord
    root_package: ReviewPackage
    root_record: ReviewRecord
    root_revision: RequirementsRevision
    #: Receipt ids the library refused to hold — see :meth:`delivery`.
    unrecorded: set[str] = dataclasses.field(default_factory=set)

    @property
    def store(self) -> Store:
        return self.service.store

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.service.store)

    @property
    def duties(self) -> ObligationStore:
        return ObligationStore(self.service.store)

    def read_set(self, **overrides: Any) -> SemanticReadSet:
        """A read-set that names what the *Mission* is currently at.

        The revision is read from the store rather than hard-coded: a world that
        publishes a later requirements revision must make every command re-read it,
        which is exactly the property ``_check_reads`` exists to enforce.
        """

        latest = self.semantics.latest_requirements_revision(self.mission.id)
        fields: dict[str, Any] = {
            "requirements_revision": 0 if latest is None else int(latest.revision),
            "manager_epoch": 0,
            "scope_epochs": (ScopeEpochRead(scope_id=SCOPE, validity_epoch=0),),
        }
        fields.update(overrides)
        return SemanticReadSet(**fields)

    def principal(self, **overrides: Any) -> ResolutionPrincipal:
        fields: dict[str, Any] = {
            "principal_id": PRINCIPAL,
            "scope_id": SCOPE,
            "manager_epoch": 0,
        }
        fields.update(overrides)
        return ResolutionPrincipal(**fields)

    def accept(self, command: AcceptReviewCommand | None = None, principal: Any = None) -> Any:
        return self.service.accept_review(
            command if command is not None else self.accept_command(),
            principal if principal is not None else self.principal(),
        )

    def resolve(
        self, command: CommitGoalResolutionCommand | None = None, principal: Any = None
    ) -> Any:
        return self.service.commit_goal_resolution(
            command if command is not None else self.resolution_command(),
            principal if principal is not None else self.principal(),
        )

    def accept_command(self, **overrides: Any) -> AcceptReviewCommand:
        fields: dict[str, Any] = {
            "command_id": "cmd-accept-1",
            "mission_id": self.mission.id,
            "task_id": LEAF_TASK,
            "obligation_id": LEAF_DUTY,
            "acceptance_id": "acc-leaf-1",
            "package": self.leaf_package,
            "record": self.leaf_record,
            "requirements": self.revision,
            "witness_id": "wit-leaf",
            "independence": independence(),
            "posture": ExecutionPosture(),
            "read_set": self.read_set(),
            "accepted_at_ms": NOW_MS,
            "artifact_refs": (),
            "purpose": ReviewPurpose.TASK_CONTENT,
            "issued_by": PRINCIPAL,
            "scope_id": SCOPE,
        }
        fields.update(overrides)
        return AcceptReviewCommand(**fields)

    def resolution(self, **overrides: Any) -> GoalResolution:
        fields: dict[str, Any] = {
            "resolution_id": GoalResolutionId("res-root-1"),
            "mission_id": self.mission.id,
            "obligation_id": ROOT_DUTY,
            "goal_task_id": ROOT_TASK,
            "requirements_version": int(self.root_revision.revision),
            "contract_revision": 1,
            "method_instance_id": INSTANCE,
            "input_manifest_hash": self.manifest_hash,
            "artifact_refs": (),
            "child_resolution_ids": (),
            "criteria": tuple(
                ResolutionCriterion(criterion_id=item.criterion_id, verdict=CriterionVerdict.PASS)
                for item in self.root_revision.criteria
            ),
            "review_receipt_id": str(self.root_record.record_id),
            "verdict": ReviewVerdict.ACCEPT,
            "validity": Validity.CURRENT,
        }
        fields.update(overrides)
        return GoalResolution(**fields)

    def resolution_command(self, **overrides: Any) -> CommitGoalResolutionCommand:
        fields: dict[str, Any] = {
            "command_id": "cmd-resolve-1",
            "mission_id": self.mission.id,
            "resolution": self.resolution(),
            "package": self.root_package,
            "record": self.root_record,
            "requirements": self.root_revision,
            "witness_id": "wit-root",
            "independence": independence(),
            "posture": ExecutionPosture(),
            "read_set": self.read_set(),
            "decided_at_ms": NOW_MS,
            "purpose": ReviewPurpose.COMPOSITION,
            "compound": CompoundFacts(
                selected_method_legal=True,
                contributing_occurrence_ids=(LEAF_OCCURRENCE,),
                composition_obligation_passed=True,
            ),
            "issued_by": PRINCIPAL,
            "scope_id": SCOPE,
        }
        fields.update(overrides)
        return CommitGoalResolutionCommand(**fields)

    def child_acceptance(self, **overrides: Any) -> Acceptance:
        fields: dict[str, Any] = {
            "acceptance_id": AcceptanceId("acc-leaf-1"),
            "mission_id": self.mission.id,
            "task_id": TaskRef(LEAF_TASK),
            "obligation_id": ObligationId(LEAF_DUTY),
            "requirements_revision": 1,
            "contract_revision": 1,
            "input_manifest_hash": self.manifest_hash,
            "review_record_id": ReviewRecordId(str(self.leaf_record.record_id)),
            "accepted_at_ms": NOW_MS,
            "validity": Validity.CURRENT,
        }
        fields.update(overrides)
        return Acceptance(**fields)

    def delivery_receipt(
        self, stage: DeliveryStage, *, acceptance_id: str = "acc-leaf-1"
    ) -> DeliveryReceipt:
        """The receipt value, unrecorded.  ``delivery()`` is what puts it in the store."""

        # The id carries the quoted acceptance whenever it is not the default one, so
        # two receipts for the same stage but different acceptances are two records
        # rather than one overwriting the other.
        suffix = "" if acceptance_id == "acc-leaf-1" else f"-{acceptance_id}"
        return DeliveryReceipt(
            receipt_id=f"dlv-{stage!s}{suffix}".lower(),
            mission_id=self.mission.id,
            acceptance_id=AcceptanceId(acceptance_id),
            stage=stage,
            observed_at_ms=NOW_MS,
            operation_id=(
                "op-1" if stage in {DeliveryStage.SENT, DeliveryStage.CONFIRMED} else None
            ),
        )

    def delivery(self, stage: DeliveryStage, *, acceptance_id: str = "acc-leaf-1") -> str:
        """Record one delivery receipt and return the **id** the command names.

        P2.3c part 2 moved the receipt out of the command and into the library, so a
        test that wants a root to be deliverable has to make the library hold the
        record — which is the whole point of the change.  A receipt the library
        refuses to hold (an acceptance nobody stored, another Mission's) is returned
        as its id anyway: the gate then refuses it as "no such record", which is the
        same "a receipt is a claim until the store agrees" answer one level earlier.
        """

        receipt = self.delivery_receipt(stage, acceptance_id=acceptance_id)
        try:
            self.service.record_delivery_receipt(
                self.mission.id, receipt, command_id=f"cmd-dlv-{receipt.receipt_id}-{acceptance_id}"
            )
        except StoreError:
            self.unrecorded.add(receipt.receipt_id)
        return receipt.receipt_id

    def spare_record(self, name: str) -> ReviewRecordId:
        """A stored (non-official) review record id for a second acceptance.

        ``acceptances.review_record_id`` is UNIQUE and foreign keys are on, so an
        extra acceptance needs an extra record that really exists.
        """

        binding = review_binding(self.mission.id, LEAF_DUTY, LEAF_TASK, self.manifest_hash)
        package = review_package(f"pkg-{name}", binding, self.revision)
        self.semantics.insert_review_package(package)
        record = review_record(f"rec-{name}", package)
        self.semantics.insert_review_record(record, official=False)
        return ReviewRecordId(str(record.record_id))

    def counts(self) -> dict[str, int]:
        connection = self.store.connection
        return {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in ("acceptances", "goal_resolutions", "plan_commit_receipts")
        }

    def lifecycle(self, duty: str) -> ObligationLifecycle:
        return self.duties.account(self.mission.id, ObligationId(duty)).lifecycle

    def events(self, kind: str) -> list[Any]:
        return [item for item in self.store.list_events(self.mission.id) if item.type == kind]


def build_world(tmp_path: Any, *, mode: str = HIERARCHICAL_SEMANTICS, key: str = "p23c") -> World:
    service = ResolutionService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(
        MissionSpec(
            goal="交付一个可验收的层次结果",
            success_criteria=("file:a.md",),
            tenant_id="tenant-p23c",
            idempotency_key=key,
            budget=Budget(max_tokens=200_000, max_attempts=12),
            orchestration_semantics_version=mode,
        )
    )
    semantics = HtnStore(service.store)
    duties = ObligationStore(service.store)
    manifest_hash = semantics.insert_input_manifest(mission.id, LEAF_TASK, {"inputs": []})

    leaf_revision = requirements(mission.id, revision=1)
    root_revision = requirements(
        mission.id,
        revision=2,
        criteria=(criterion(ROOT_CRITERION, checks=("root-suite",)),),
        delivery_contract_ref=None,
    )
    semantics.insert_requirements_revision(leaf_revision)
    semantics.insert_requirements_revision(root_revision)

    leaf_binding = review_binding(mission.id, LEAF_DUTY, LEAF_TASK, manifest_hash, revision=1)
    leaf_package = review_package("pkg-leaf", leaf_binding, leaf_revision)
    leaf_record = review_record("rec-leaf", leaf_package)
    semantics.insert_review_package(leaf_package)
    semantics.insert_review_record(leaf_record, official=True)

    root_binding = review_binding(mission.id, ROOT_DUTY, ROOT_TASK, manifest_hash, revision=2)
    root_package = review_package(
        "pkg-root",
        root_binding,
        root_revision,
        purpose=ReviewPurpose.COMPOSITION,
        method_instance_id=INSTANCE,
    )
    root_record = review_record("rec-root", root_package)
    semantics.insert_review_package(root_package)
    semantics.insert_review_record(root_record, official=True)

    for duty, signature in ((LEAF_DUTY, CRITERION), (ROOT_DUTY, ROOT_CRITERION)):
        duties.register(
            Obligation(
                obligation_id=ObligationId(duty),
                mission_id=mission.id,
                requirement_refs=(signature,),
                goal_signature_id="leaf-goal" if duty == LEAF_DUTY else "root-goal",
            ),
            recursion_fuel=4,
        )
    semantics.put_task_semantics(mission.id, task_binding(LEAF_TASK, LEAF_DUTY, TaskForm.PRIMITIVE))
    semantics.put_task_semantics(
        mission.id,
        task_binding(
            ROOT_TASK,
            ROOT_DUTY,
            TaskForm.COMPOUND,
            requirement_refs=(ROOT_CRITERION,),
            adopted_method_instance_id=MethodInstanceId(INSTANCE),
        ),
    )
    semantics.insert_method_instance(
        mission.id,
        MethodInstanceDraft(
            instance_id=MethodInstanceId(INSTANCE),
            goal_id=TaskRef(ROOT_TASK),
            obligation_id=ObligationId(ROOT_DUTY),
            method_ref=MethodRef(method_id="m-root", version=1, content_hash=HASH_B),
            child_bindings=(
                ChildBinding(
                    instance_id=MethodInstanceId(INSTANCE),
                    slot_key="leaf",
                    occurrence_id=OccurrenceId(LEAF_OCCURRENCE),
                    obligation_id=ObligationId(LEAF_DUTY),
                    requiredness=Requiredness.REQUIRED,
                ),
            ),
            goal_occurrence_id=OccurrenceId(ROOT_OCCURRENCE),
            plan_revision=0,
        ),
        state="ADOPTED",
    )
    semantics.insert_validity_witness(
        mission.id, accept_witness("wit-leaf", LEAF_TASK), subject=NO_SUBJECT
    )
    semantics.insert_validity_witness(
        mission.id, accept_witness("wit-root", ROOT_TASK), subject=NO_SUBJECT
    )
    return World(
        service=service,
        mission=mission,
        revision=leaf_revision,
        manifest_hash=manifest_hash,
        leaf_package=leaf_package,
        leaf_record=leaf_record,
        root_package=root_package,
        root_record=root_record,
        root_revision=root_revision,
    )


@pytest.fixture
def world(tmp_path: Any) -> World:
    return build_world(tmp_path)


def refusal(call: Any, *args: Any, **kwargs: Any) -> str:
    return refusal_error(call, *args, **kwargs).reason


def refusal_error(call: Any, *args: Any, **kwargs: Any) -> ResolutionCommitRejected:
    with pytest.raises(ResolutionCommitRejected) as caught:
        call(*args, **kwargs)
    return caught.value


def observation(observation_id: str, proposition: str, *, polarity: bool) -> Any:
    """One stored observation.  A later one for the same proposition refutes it."""

    from agent_orchestrator.contracts.evidence_state import (
        ObservationRecord,
        QueryCompleteness,
    )

    return ObservationRecord(
        observation_id=observation_id,
        proposition_key=proposition,
        polarity=polarity,
        source_ref=receipt_ref(f"{observation_id}-source"),
        observed_at_ms=NOW_MS,
        recorded_at_ms=NOW_MS,
        coverage=(
            QueryCompleteness.BEST_EFFORT
            if polarity
            else QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
        ),
        coverage_scope=None if polarity else SCOPE,
        query_watermark_ms=None if polarity else NOW_MS,
        observer_id="observer-1",
    )


# ======================================================================================
# 1. The door, identity and idempotency
# ======================================================================================


def test_a_legacy_mission_is_refused_at_the_door(tmp_path: Any) -> None:
    legacy = build_world(tmp_path, mode=LEGACY_SEMANTICS, key="legacy")
    assert refusal(legacy.accept) == "SEMANTICS_NOT_HIERARCHICAL"
    assert legacy.counts()["acceptances"] == 0


def test_an_unsigned_command_is_not_attributed_to_the_presenter(world: World) -> None:
    assert refusal(world.accept, world.accept_command(issued_by="")) == "PRINCIPAL_MISMATCH"


def test_authorship_is_not_reassignable_at_delivery(world: World) -> None:
    command = world.accept_command(issued_by="someone-else")
    assert refusal(world.accept, command) == "PRINCIPAL_MISMATCH"


def test_a_principal_may_not_commit_into_another_scope(world: World) -> None:
    command = world.accept_command(scope_id="other-scope")
    assert refusal(world.accept, command) == "SCOPE_NOT_AUTHORIZED"


def test_identity_is_checked_before_the_receipt_is_read(world: World) -> None:
    """A forged replay of someone else's command_id must not read back their receipt."""

    world.accept()
    original = world.accept_command()
    assert refusal(world.accept, original, ResolutionPrincipal("attacker", SCOPE, 0)) == (
        "PRINCIPAL_MISMATCH"
    )


def test_the_same_accept_command_twice_is_one_acceptance_and_one_receipt(world: World) -> None:
    first = world.accept()
    before = world.counts()
    second = world.accept()
    assert second.replayed is True
    assert second.acceptance == first.acceptance
    assert world.counts() == before


def test_the_same_command_id_with_another_intent_is_a_conflict(world: World) -> None:
    world.accept()
    other = world.accept_command(acceptance_id="acc-leaf-2")
    assert refusal(world.accept, other) == "COMMAND_PAYLOAD_CONFLICT"


def test_the_same_resolution_command_twice_is_one_resolution(world: World) -> None:
    world.accept()
    first = world.resolve()
    before = world.counts()
    second = world.resolve()
    assert second.replayed is True
    assert second.resolution_id == first.resolution_id
    assert world.counts() == before


def test_a_terminal_mission_accepts_no_new_acceptance(world: World) -> None:
    world.service.cancel_mission(world.mission.id)
    assert refusal(world.accept) == "MISSION_NOT_WRITABLE"


# ======================================================================================
# 2. Binding checks: contract, inputs, artefacts, review identity
# ======================================================================================


def test_a_task_with_no_semantic_binding_is_corruption(world: World) -> None:
    command = world.accept_command(task_id="task-nobody")
    assert refusal(world.accept, command) == "MISSING_SEMANTIC_BINDING"


def test_a_command_naming_another_duty_than_the_task_serves_is_refused(world: World) -> None:
    command = world.accept_command(obligation_id=ROOT_DUTY)
    assert refusal(world.accept, command) == "BINDING_MISMATCH"


def test_a_package_bound_to_another_subject_is_refused(world: World) -> None:
    binding = review_binding(world.mission.id, LEAF_DUTY, ROOT_TASK, world.manifest_hash)
    package = review_package("pkg-other", binding, world.revision)
    world.semantics.insert_review_package(package)
    record = review_record("rec-other", package)
    world.semantics.insert_review_record(record, official=True)
    command = world.accept_command(package=package, record=record)
    assert refusal(world.accept, command) == "BINDING_MISMATCH"


def test_a_record_that_does_not_describe_its_package_is_refused(world: World) -> None:
    other = dataclasses.replace(world.leaf_record, package_id=ReviewPackageId("pkg-root"))
    command = world.accept_command(record=other)
    assert refusal(world.accept, command) == "REVIEW_IDENTITY_MISMATCH"


def test_a_purpose_may_not_be_relabelled_at_delivery(world: World) -> None:
    command = world.accept_command(purpose=ReviewPurpose.MISSION_FINAL)
    assert refusal(world.accept, command) == "REVIEW_PURPOSE_MISMATCH"


def test_a_package_that_is_not_stored_cannot_be_quoted(world: World) -> None:
    binding = review_binding(world.mission.id, LEAF_DUTY, LEAF_TASK, world.manifest_hash)
    package = review_package("pkg-unstored", binding, world.revision)
    command = world.accept_command(package=package, record=review_record("rec-x", package))
    assert refusal(world.accept, command) == "REVIEW_NOT_STORED"


def test_a_package_edited_on_the_way_in_is_refused_by_content_hash(world: World) -> None:
    tampered = dataclasses.replace(
        world.leaf_package,
        candidate_refs=(tref(TypedRefKind.ARTIFACT, "cand-swapped", digest=HASH_B),),
    )
    record = dataclasses.replace(world.leaf_record)
    command = world.accept_command(package=tampered, record=record)
    assert refusal(world.accept, command) == "REVIEW_CONTENT_MISMATCH"


def test_a_record_that_is_not_the_official_one_is_refused(world: World) -> None:
    second = review_record("rec-leaf-2", world.leaf_package)
    world.semantics.insert_review_record(second, official=False)
    command = world.accept_command(record=second)
    assert refusal(world.accept, command) == "REVIEW_NOT_OFFICIAL"


def test_a_requirements_revision_that_is_not_stored_is_refused(world: World) -> None:
    other = requirements(world.mission.id, revision=9)
    binding = review_binding(
        world.mission.id, LEAF_DUTY, LEAF_TASK, world.manifest_hash, revision=9
    )
    package = review_package("pkg-r9", binding, other)
    world.semantics.insert_review_package(package)
    record = review_record("rec-r9", package)
    world.semantics.insert_review_record(record, official=True)
    command = world.accept_command(package=package, record=record, requirements=other)
    assert refusal(world.accept, command) == "REQUIREMENTS_NOT_STORED"


def test_requirements_are_never_quietly_relaxed(world: World) -> None:
    """Same revision number, different content — the stored bytes decide (I01)."""

    relaxed = requirements(
        world.mission.id,
        revision=1,
        criteria=(criterion(CRITERION, requirement_class=RequirementClass.PREFERENCE),),
    )
    command = world.accept_command(requirements=relaxed)
    assert refusal(world.accept, command) == "REQUIREMENTS_MISMATCH"


def test_a_package_and_a_command_that_disagree_about_the_revision_are_refused(
    world: World,
) -> None:
    command = world.accept_command(requirements=world.root_revision)
    assert refusal(world.accept, command) == "REQUIREMENTS_MISMATCH"


def test_an_input_manifest_nobody_stored_is_refused(world: World) -> None:
    binding = review_binding(world.mission.id, LEAF_DUTY, LEAF_TASK, HASH_C)
    package = review_package("pkg-nomanifest", binding, world.revision)
    world.semantics.insert_review_package(package)
    record = review_record("rec-nomanifest", package)
    world.semantics.insert_review_record(record, official=True)
    command = world.accept_command(package=package, record=record)
    assert refusal(world.accept, command) == "INPUT_MANIFEST_UNKNOWN"


# ======================================================================================
# 3. Scope epoch, read-set and the support-set revision (AER §7)
# ======================================================================================


def test_a_stale_manager_epoch_refuses_the_command(world: World) -> None:
    # The epoch table starts empty and ``bump_epoch`` writes 0 for the first row, so
    # the scope has to be raised twice to actually move past what the command read.
    world.semantics.bump_epoch(world.mission.id, SCOPE, bumped_by="manager-1")
    assert world.semantics.bump_epoch(world.mission.id, SCOPE, bumped_by="manager-1") == 1
    assert refusal(world.accept) == "MANAGER_EPOCH_STALE"


def test_a_principal_holding_an_old_epoch_is_refused(world: World) -> None:
    assert refusal(world.accept, None, world.principal(manager_epoch=5)) == "MANAGER_EPOCH_STALE"


def test_a_stale_requirements_read_refuses_the_command(world: World) -> None:
    # Revision 2 is the latest this world holds, so a read taken at 1 is stale.
    command = world.accept_command(read_set=world.read_set(requirements_revision=1))
    assert refusal(world.accept, command) == "READ_SET_STALE"


def test_a_stale_scope_epoch_read_refuses_the_command(world: World) -> None:
    command = world.accept_command(
        read_set=world.read_set(
            scope_epochs=(ScopeEpochRead(scope_id=SCOPE, validity_epoch=7),),
        )
    )
    assert refusal(world.accept, command) == "READ_SET_STALE"


def test_a_support_set_the_store_cannot_recheck_is_unresolved_not_stale(world: World) -> None:
    command = world.accept_command(
        read_set=world.read_set(
            support_sets=(SupportSetRead(support_set_id="ss-1", revision=1, member_digest=HASH_C),),
        )
    )
    assert refusal(world.accept, command) == "READ_SET_UNRESOLVED"


def test_a_goal_read_at_another_contract_revision_is_stale(world: World) -> None:
    command = world.accept_command(
        read_set=world.read_set(
            goal_revisions=(
                ReadItem(
                    kind=ReadItemKind.TASK,
                    id=LEAF_TASK,
                    semantic_revision=9,
                    content_hash=HASH_A,
                ),
            ),
        )
    )
    assert refusal(world.accept, command) == "READ_SET_STALE"


def test_an_acceptance_read_that_no_longer_exists_is_unresolved(world: World) -> None:
    command = world.accept_command(
        read_set=world.read_set(
            acceptance_revisions=(
                ReadItem(
                    kind=ReadItemKind.ACCEPTANCE,
                    id="acc-gone",
                    semantic_revision=1,
                    content_hash=HASH_A,
                ),
            ),
        )
    )
    assert refusal(world.accept, command) == "READ_SET_UNRESOLVED"


# ======================================================================================
# 4. The purpose=ACCEPT witness (§11.5)
# ======================================================================================


def test_a_witness_nobody_stored_is_refused(world: World) -> None:
    assert refusal(world.accept, world.accept_command(witness_id="wit-nobody")) == (
        "WITNESS_UNKNOWN"
    )


def test_a_start_witness_does_not_license_an_acceptance(world: World) -> None:
    world.semantics.insert_validity_witness(
        world.mission.id,
        accept_witness("wit-start", LEAF_TASK, purpose=WitnessPurpose.START, support_revision=4),
        subject=NO_SUBJECT,
    )
    command = world.accept_command(witness_id="wit-start")
    assert refusal(world.accept, command) == "WITNESS_PURPOSE_NOT_ACCEPT"


def test_a_witness_issued_to_another_consumer_is_that_consumers_permission(world: World) -> None:
    command = world.accept_command(witness_id="wit-root")
    assert refusal(world.accept, command) == "WITNESS_CONSUMER_MISMATCH"


def test_a_witness_whose_deadline_has_passed_is_stale(world: World) -> None:
    world.semantics.insert_validity_witness(
        world.mission.id,
        accept_witness("wit-expired", LEAF_TASK, not_after_ms=NOW_MS - 1, support_revision=2),
        subject=NO_SUBJECT,
    )
    command = world.accept_command(witness_id="wit-expired")
    assert refusal(world.accept, command) == "WITNESS_STALE"


def test_a_witness_taken_before_the_epoch_barrier_is_stale(world: World) -> None:
    world.semantics.insert_validity_witness(
        world.mission.id,
        accept_witness("wit-old-epoch", LEAF_TASK, scope_epoch=3, support_revision=3),
        subject=NO_SUBJECT,
    )
    command = world.accept_command(
        witness_id="wit-old-epoch",
        read_set=world.read_set(),
    )
    assert refusal(world.accept, command) == "WITNESS_STALE"


# ======================================================================================
# 5. The formula: independence and in-flight facts must be stated
# ======================================================================================


def test_the_command_cannot_be_built_without_independence_facts() -> None:
    with pytest.raises(TypeError):
        AcceptReviewCommand(  # type: ignore[call-arg]
            command_id="c",
            mission_id="m",
            task_id="t",
            obligation_id="o",
            acceptance_id="a",
            package=None,  # type: ignore[arg-type]
            record=None,  # type: ignore[arg-type]
            requirements=None,  # type: ignore[arg-type]
            witness_id="w",
        )


def test_a_non_independence_facts_value_is_refused(world: World) -> None:
    with pytest.raises(ContractError, match="IndependenceFacts"):
        world.accept_command(independence=object())


def test_a_non_posture_value_is_refused(world: World) -> None:
    with pytest.raises(ContractError, match="ExecutionPosture"):
        world.accept_command(posture=object())


def test_a_self_review_is_not_acceptable(world: World) -> None:
    command = world.accept_command(
        independence=IndependenceFacts(producer_agent_ids=("agent-reviewer",))
    )
    assert refusal(world.accept, command) == "NOT_ACCEPTABLE"
    assert world.counts()["acceptances"] == 0


def test_a_reviewer_who_could_edit_the_candidate_is_not_independent(world: World) -> None:
    command = world.accept_command(
        independence=IndependenceFacts(
            producer_agent_ids=("agent-worker",), reviewer_can_write_candidate=True
        )
    )
    assert refusal(world.accept, command) == "NOT_ACCEPTABLE"


def test_a_rework_verdict_is_not_an_acceptance(world: World) -> None:
    record = review_record("rec-rework", world.leaf_package, verdict=ReviewVerdict.REWORK)
    world.semantics.insert_review_record(record, official=False)
    world.store.connection.execute(
        "UPDATE review_records SET official = 0 WHERE record_id = 'rec-leaf'"
    )
    world.store.connection.execute(
        "UPDATE review_records SET official = 1 WHERE record_id = 'rec-rework'"
    )
    command = world.accept_command(record=record)
    assert refusal(world.accept, command) == "NOT_ACCEPTABLE"


def test_a_required_check_that_never_ran_is_not_a_pass(world: World) -> None:
    record = review_record(
        "rec-notrun",
        world.leaf_package,
        outcomes=(
            CriterionOutcome(
                criterion_id=CRITERION,
                verdict=CriterionVerdict.UNKNOWN,
                check_execution=CheckExecution.NOT_RUN,
                evidence_refs=(receipt_ref("r"),),
            ),
        ),
    )
    world.store.connection.execute(
        "UPDATE review_records SET official = 0 WHERE record_id = 'rec-leaf'"
    )
    world.semantics.insert_review_record(record, official=True)
    command = world.accept_command(record=record)
    assert refusal(world.accept, command) == "NOT_ACCEPTABLE"


def test_an_unowned_critical_operation_blocks_the_acceptance(world: World) -> None:
    command = world.accept_command(
        posture=ExecutionPosture(unowned_critical_operation_ids=("op-9",))
    )
    assert refusal(world.accept, command) == "CRITICAL_OPERATION_UNOWNED"
    assert world.counts()["acceptances"] == 0


def test_a_pending_cancellation_blocks_the_acceptance(world: World) -> None:
    command = world.accept_command(posture=ExecutionPosture(cancellation_requested=True))
    assert refusal(world.accept, command) == "CANCELLATION_PENDING"


# ======================================================================================
# 6. The happy accept path, and what it deliberately does not do
# ======================================================================================


def test_a_clean_acceptance_is_written_with_the_bindings_it_quotes(world: World) -> None:
    receipt = world.accept()
    stored = world.semantics.get_acceptance("acc-leaf-1")
    assert stored == receipt.acceptance
    assert stored.input_manifest_hash == world.manifest_hash
    assert str(stored.review_record_id) == "rec-leaf"
    assert stored.validity is Validity.CURRENT


def test_an_acceptance_leaves_the_duty_open(world: World) -> None:
    """§25.1 decision 4: ACCEPT and GoalResolution are two actions."""

    world.accept()
    assert world.lifecycle(LEAF_DUTY) is ObligationLifecycle.UNSATISFIED
    assert world.duties.account(world.mission.id, ObligationId(LEAF_DUTY)).resolution_ref is None


def test_the_acceptance_event_names_the_review_account(world: World) -> None:
    world.accept()
    events = world.events(ACCEPTANCE_COMMITTED)
    assert len(events) == 1
    assert events[0].payload["review_account"] == "task"
    assert events[0].payload["acceptance_id"] == "acc-leaf-1"


def test_the_accept_receipt_records_the_decision_it_was_built_from(world: World) -> None:
    receipt = world.accept()
    assert receipt.decision is not None and receipt.decision.acceptable
    assert receipt.commit.output_identity["kind"] == "acceptance"
    assert receipt.replayed is False


# ======================================================================================
# 7. commit_goal_resolution: compound, coverage and the duty lifecycle
# ======================================================================================


def test_a_compound_with_no_child_acceptance_is_refused(world: World) -> None:
    assert refusal(world.resolve) == "COMPOUND_FACTS_CONTRADICT_STORE"
    assert world.counts()["goal_resolutions"] == 0


def test_a_compound_whose_required_child_is_missing_is_refused_by_the_formula(
    world: World,
) -> None:
    """The store says nothing contributed, and the command agrees — the formula refuses."""

    command = world.resolution_command(
        compound=CompoundFacts(
            selected_method_legal=True,
            contributing_occurrence_ids=(),
            composition_obligation_passed=True,
        )
    )
    assert refusal(world.resolve, command) == "NOT_ACCEPTABLE"
    assert world.counts()["goal_resolutions"] == 0


def test_a_stale_child_acceptance_does_not_count_as_a_contribution(world: World) -> None:
    world.semantics.insert_acceptance(world.child_acceptance(validity=Validity.STALE))
    assert refusal(world.resolve) == "COMPOUND_FACTS_CONTRADICT_STORE"


def test_an_illegal_selected_method_is_refused(world: World) -> None:
    world.accept()
    command = world.resolution_command(
        compound=CompoundFacts(
            selected_method_legal=False,
            contributing_occurrence_ids=(LEAF_OCCURRENCE,),
            composition_obligation_passed=True,
        )
    )
    assert refusal(world.resolve, command) == "NOT_ACCEPTABLE"


def test_a_failed_composition_obligation_is_refused(world: World) -> None:
    world.accept()
    command = world.resolution_command(
        compound=CompoundFacts(
            selected_method_legal=True,
            contributing_occurrence_ids=(LEAF_OCCURRENCE,),
            composition_obligation_passed=False,
        )
    )
    assert refusal(world.resolve, command) == "NOT_ACCEPTABLE"


def test_a_missing_root_requirement_forms_zero_resolutions(world: World) -> None:
    world.accept()
    resolution = world.resolution(
        criteria=(ResolutionCriterion(criterion_id="c-unrelated", verdict=CriterionVerdict.PASS),)
    )
    command = world.resolution_command(resolution=resolution)
    assert refusal(world.resolve, command) == "ROOT_CRITERION_MISSING"
    assert world.counts()["goal_resolutions"] == 0
    assert world.lifecycle(ROOT_DUTY) is ObligationLifecycle.UNSATISFIED


def test_a_resolution_may_not_overrule_the_review_it_binds(world: World) -> None:
    world.accept()
    resolution = world.resolution(
        criteria=(ResolutionCriterion(criterion_id=ROOT_CRITERION, verdict=CriterionVerdict.FAIL),)
    )
    command = world.resolution_command(resolution=resolution)
    assert refusal(world.resolve, command) == "RESOLUTION_CONTRADICTS_REVIEW"


def test_a_resolution_whose_verdict_is_not_accept_is_not_a_satisfaction(world: World) -> None:
    world.accept()
    resolution = world.resolution(verdict=ReviewVerdict.INCONCLUSIVE)
    command = world.resolution_command(resolution=resolution)
    assert refusal(world.resolve, command) == "RESOLUTION_VERDICT_NOT_ACCEPT"


def test_a_resolution_pointing_at_a_retired_method_instance_is_refused(world: World) -> None:
    world.accept()
    world.semantics.set_method_instance_state(world.mission.id, INSTANCE, "RETIRED")
    assert refusal(world.resolve) == "METHOD_INSTANCE_NOT_ADOPTED"


def test_a_resolution_binding_an_unknown_child_resolution_is_refused(world: World) -> None:
    world.accept()
    resolution = world.resolution(child_resolution_ids=("res-nobody",))
    command = world.resolution_command(resolution=resolution)
    assert refusal(world.resolve, command) == "CHILD_RESOLUTION_UNKNOWN"


def test_a_duty_that_is_already_satisfied_is_not_resolved_twice(world: World) -> None:
    world.accept()
    world.resolve()
    again = world.resolution_command(
        command_id="cmd-resolve-2",
        resolution=world.resolution(resolution_id=GoalResolutionId("res-root-2")),
    )
    assert refusal(world.resolve, again) == "OBLIGATION_NOT_OPEN"


def test_a_clean_resolution_satisfies_the_duty_with_its_resolution_ref(world: World) -> None:
    world.accept()
    receipt = world.resolve()
    account = world.duties.account(world.mission.id, ObligationId(ROOT_DUTY))
    assert account.lifecycle is ObligationLifecycle.SATISFIED
    assert account.resolution_ref == "res-root-1"
    assert receipt.account is not None and receipt.account.resolution_ref == "res-root-1"
    assert world.semantics.adopted_goal_resolution(world.mission.id, ROOT_DUTY) is not None


def test_the_resolution_event_names_the_contributing_occurrences(world: World) -> None:
    world.accept()
    world.resolve()
    events = world.events(GOAL_RESOLUTION_COMMITTED)
    assert len(events) == 1
    assert events[0].payload["contributing_occurrences"] == [LEAF_OCCURRENCE]
    assert events[0].payload["review_account"] == "parent_compound_task"


def test_an_admitted_demand_is_released_when_the_duty_is_satisfied(world: World) -> None:
    """TG decision 9: withdrawing the share is part of resolving the duty."""

    world.duties.admit_demand(world.mission.id, ObligationId(ROOT_DUTY))
    world.accept()
    world.resolve()
    account = world.duties.account(world.mission.id, ObligationId(ROOT_DUTY))
    assert account.lifecycle is ObligationLifecycle.SATISFIED
    assert account.has_admitted_demand is False


# ======================================================================================
# 8. The Mission-root delivery gate (§6.3, AER §6.1)
# ======================================================================================


def root_world(tmp_path: Any) -> World:
    """A world whose root requirements declare a delivery contract."""

    world = build_world(tmp_path, key="p23c-root")
    delivering = requirements(
        world.mission.id,
        revision=3,
        criteria=(criterion(ROOT_CRITERION, checks=("root-suite",)),),
        delivery_contract_ref="delivery-contract-1",
    )
    world.semantics.insert_requirements_revision(delivering)
    binding = review_binding(
        world.mission.id, ROOT_DUTY, ROOT_TASK, world.manifest_hash, revision=3
    )
    package = review_package(
        "pkg-final",
        binding,
        delivering,
        purpose=ReviewPurpose.MISSION_FINAL,
        method_instance_id=INSTANCE,
    )
    record = review_record("rec-final", package)
    world.semantics.insert_review_package(package)
    world.semantics.insert_review_record(record, official=True)
    world.root_package = package
    world.root_record = record
    world.root_revision = delivering
    return world


def root_command(world: World, **overrides: Any) -> CommitGoalResolutionCommand:
    fields: dict[str, Any] = {
        "purpose": ReviewPurpose.MISSION_FINAL,
        "is_mission_root": True,
        "read_set": world.read_set(),
        "required_delivery_stage": DeliveryStage.CONFIRMED,
        "delivery_receipts": (world.delivery(DeliveryStage.CONFIRMED),),
    }
    fields.update(overrides)
    return world.resolution_command(**fields)


def test_the_delivery_stage_order_is_how_far_the_output_travelled() -> None:
    assert DELIVERY_STAGE_ORDER[DeliveryStage.CONFIRMED] > DELIVERY_STAGE_ORDER[DeliveryStage.SENT]
    assert DELIVERY_STAGE_ORDER[DeliveryStage.SENT] > DELIVERY_STAGE_ORDER[DeliveryStage.ENQUEUED]
    assert DELIVERY_STAGE_ORDER[DeliveryStage.FAILED] == 0


def test_a_failed_delivery_reaches_no_stage() -> None:
    receipt = DeliveryReceipt(
        receipt_id="dlv-failed",
        mission_id="m",
        acceptance_id=AcceptanceId("a"),
        stage=DeliveryStage.FAILED,
        observed_at_ms=1,
    )
    assert not delivery_reached(receipt, DeliveryStage.PERSISTED)


def test_a_root_resolution_needs_a_mission_final_review(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    command = root_command(world, purpose=ReviewPurpose.MISSION_FINAL)
    # The package really is MISSION_FINAL here; flipping only the command's purpose
    # is caught earlier, by the purpose-relabelling gate.
    assert world.resolve(command).resolution_id == "res-root-1"


def test_a_root_resolution_without_a_required_stage_is_refused(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    command = root_command(world, required_delivery_stage=None, delivery_receipts=())
    assert refusal(world.resolve, command) == "DELIVERY_CONTRACT_UNDECLARED"
    assert world.counts()["goal_resolutions"] == 0


def test_a_root_resolution_whose_delivery_only_persisted_is_refused(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    command = root_command(world, delivery_receipts=(world.delivery(DeliveryStage.PERSISTED),))
    assert refusal(world.resolve, command) == "DELIVERY_STAGE_NOT_REACHED"
    assert world.counts()["goal_resolutions"] == 0
    assert world.lifecycle(ROOT_DUTY) is ObligationLifecycle.UNSATISFIED


def test_a_root_resolution_with_no_receipt_at_all_is_refused(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    command = root_command(world, delivery_receipts=())
    assert refusal(world.resolve, command) == "DELIVERY_STAGE_NOT_REACHED"


def test_a_receipt_quoting_an_acceptance_nobody_stored_is_invalid(tmp_path: Any) -> None:
    """Not "does not count" — refused.  A receipt is a claim until the store agrees."""

    world = root_world(tmp_path)
    world.accept()
    command = root_command(
        world,
        delivery_receipts=(world.delivery(DeliveryStage.CONFIRMED, acceptance_id="acc-nobody"),),
    )
    assert refusal(world.resolve, command) == "DELIVERY_RECEIPT_INVALID"
    assert world.counts()["goal_resolutions"] == 0


def test_a_root_resolution_with_a_confirmed_delivery_is_formed(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    receipt = world.resolve(root_command(world))
    assert receipt.resolution_id == "res-root-1"
    assert world.lifecycle(ROOT_DUTY) is ObligationLifecycle.SATISFIED
    payload = world.events(GOAL_RESOLUTION_COMMITTED)[0].payload
    assert payload["is_mission_root"] is True
    assert payload["required_delivery_stage"] == str(DeliveryStage.CONFIRMED)
    assert payload["delivery_receipt_id"] == "dlv-confirmed"


def test_the_mission_status_string_is_never_consulted_for_the_root(tmp_path: Any) -> None:
    """A Mission row that says COMPLETED is a projection of this decision, not evidence.

    Two halves: the gate's source never reads a status at all, and a root whose
    delivery only reached SENT is refused even though every leaf is accepted.
    """

    # ``co_names`` is every global and attribute name the compiled gate actually
    # reads, so this is a statement about the code rather than about its prose.
    names = ResolutionCommitsMixin._check_delivery.__code__.co_names
    assert "status" not in names
    assert "_require_mission" not in names
    assert "stage" in names and "required_delivery_stage" in names
    world = root_world(tmp_path)
    world.accept()
    command = root_command(world, delivery_receipts=(world.delivery(DeliveryStage.SENT),))
    assert refusal(world.resolve, command) == "DELIVERY_STAGE_NOT_REACHED"
    assert world.counts()["goal_resolutions"] == 0


def test_a_stage_the_requirements_never_asked_for_may_not_be_invented(world: World) -> None:
    world.accept()
    command = world.resolution_command(
        is_mission_root=True,
        purpose=ReviewPurpose.MISSION_FINAL,
        required_delivery_stage=DeliveryStage.CONFIRMED,
    )
    assert refusal(world.resolve, command) == "REVIEW_PURPOSE_MISMATCH"


# ======================================================================================
# 9. Atomicity: a failure in the write half leaves nothing behind
# ======================================================================================


def test_a_failing_event_append_rolls_back_the_acceptance(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the event store is down")

    monkeypatch.setattr(type(world.service), "_emit", explode)
    with pytest.raises(RuntimeError):
        world.accept()
    assert world.counts() == {"acceptances": 0, "goal_resolutions": 0, "plan_commit_receipts": 0}


def test_a_failing_receipt_write_rolls_back_the_resolution_and_the_lifecycle(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.accept()
    before = world.counts()

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the event store is down")

    # ``_emit`` runs *after* the resolution row and the lifecycle move, so this is a
    # failure in the second half of the write — exactly the case a partial commit
    # would leave a satisfied duty with no resolution behind it.
    monkeypatch.setattr(type(world.service), "_emit", explode)
    with pytest.raises(RuntimeError):
        world.resolve()
    assert world.counts() == before
    assert world.lifecycle(ROOT_DUTY) is ObligationLifecycle.UNSATISFIED


def test_a_refused_resolution_writes_no_receipt_either(world: World) -> None:
    before = world.counts()
    refusal(world.resolve)
    assert world.counts() == before


# ======================================================================================
# 10. Mutation self-check
# ======================================================================================


def test_mutant_accepting_without_the_witness_purpose_check_would_reuse_a_start_witness(
    world: World,
) -> None:
    world.semantics.insert_validity_witness(
        world.mission.id,
        accept_witness("wit-start2", LEAF_TASK, purpose=WitnessPurpose.START, support_revision=5),
        subject=NO_SUBJECT,
    )
    real = refusal(world.accept, world.accept_command(witness_id="wit-start2"))
    assert real == "WITNESS_PURPOSE_NOT_ACCEPT"
    # The mutant: treat any stored witness as usable.  The acceptance then succeeds,
    # which is exactly what the real gate must prevent.
    accept_any = world.semantics.get_validity_witness("wit-leaf")
    assert accept_any.purpose is WitnessPurpose.ACCEPT


def test_mutant_defaulting_the_independence_facts_is_refused_not_waved_through(
    world: World,
) -> None:
    """Forgetting to look may not become acceptable (AER §5.3)."""

    permissive = IndependenceFacts()
    assert permissive.producer_agent_ids == ()
    # The frozen package says ``agent-worker`` produced the candidate; a default
    # ``IndependenceFacts()`` says nobody did.  That disagreement is a finding, so
    # the most permissive possible world is refused rather than believed.
    command = world.accept_command(independence=permissive)
    assert refusal(world.accept, command) == "NOT_ACCEPTABLE"
    assert world.counts()["acceptances"] == 0
    # The same command with the facts actually gathered is acceptable.
    assert world.accept().acceptance is not None


def test_mutant_moving_the_lifecycle_on_accept_would_close_the_duty_early(
    world: World,
) -> None:
    world.accept()
    assert world.lifecycle(LEAF_DUTY) is ObligationLifecycle.UNSATISFIED
    world.duties.set_lifecycle(
        world.mission.id,
        ObligationId(LEAF_DUTY),
        ObligationLifecycle.SATISFIED,
        resolution_ref="acc-leaf-1",
    )
    assert world.lifecycle(LEAF_DUTY) is ObligationLifecycle.SATISFIED


def test_mutant_taking_contributions_from_the_command_would_talk_a_child_into_existence(
    world: World,
) -> None:
    claimed = world.resolution_command(
        compound=CompoundFacts(
            selected_method_legal=True,
            contributing_occurrence_ids=(LEAF_OCCURRENCE,),
            composition_obligation_passed=True,
        )
    )
    assert refusal(world.resolve, claimed) == "COMPOUND_FACTS_CONTRADICT_STORE"
    world.accept()
    assert world.resolve(claimed).resolution_id == "res-root-1"


def test_mutant_a_root_gate_that_read_the_mission_row_would_pass_on_a_leaf(
    tmp_path: Any,
) -> None:
    world = root_world(tmp_path)
    world.accept()
    # Every leaf finished and was accepted; the root still needs its delivery stage.
    assert world.semantics.get_acceptance("acc-leaf-1").validity is Validity.CURRENT
    command = root_command(world, delivery_receipts=(world.delivery(DeliveryStage.ENQUEUED),))
    assert refusal(world.resolve, command) == "DELIVERY_STAGE_NOT_REACHED"


def test_mutant_skipping_the_read_set_recheck_would_accept_on_a_stale_read(
    world: World,
) -> None:
    good = world.accept_command()
    stale = world.accept_command(
        command_id="cmd-accept-stale",
        read_set=world.read_set(requirements_revision=1),
    )
    assert refusal(world.accept, stale) == "READ_SET_STALE"
    assert world.accept(good).acceptance is not None


def test_mutant_reusing_one_receipt_kind_for_both_actions_would_confuse_a_replay(
    world: World,
) -> None:
    accepted = world.accept()
    assert accepted.commit.kind == ACCEPTANCE_KIND
    assert accepted.commit.subject_id == "acc-leaf-1"
    resolved = world.resolve()
    assert resolved.commit.kind == GOAL_RESOLUTION_KIND
    assert resolved.commit.subject_id == "res-root-1"
    # A replay of the accept command may not come back as a resolution receipt.
    assert world.accept().commit.kind == ACCEPTANCE_KIND


# ======================================================================================
# 8b. Review fixes: every delivery receipt is re-read, and an invalid one refuses
# ======================================================================================


def test_a_receipt_from_another_mission_is_invalid(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    foreign = dataclasses.replace(
        world.delivery_receipt(DeliveryStage.CONFIRMED), mission_id="mission-elsewhere"
    )
    foreign = dataclasses.replace(foreign, receipt_id="dlv-elsewhere")
    # The library refuses to hold it at all, which is the same answer one step
    # earlier: a receipt for another Mission is not this Mission's record.
    assert (
        refusal(
            world.service.record_delivery_receipt,
            world.mission.id,
            foreign,
            command_id="cmd-dlv-foreign",
        )
        == "DELIVERY_RECEIPT_INVALID"
    )
    command = root_command(world, delivery_receipts=(foreign.receipt_id,))
    assert refusal(world.resolve, command) == "DELIVERY_RECEIPT_INVALID"


def test_a_receipt_quoting_a_stale_acceptance_is_invalid_not_skipped(tmp_path: Any) -> None:
    """The review's mutant R9: a STALE acceptance's receipt used to be *skipped*."""

    world = root_world(tmp_path)
    world.accept()
    world.semantics.insert_acceptance(
        world.child_acceptance(
            acceptance_id="acc-stale",
            validity=Validity.STALE,
            review_record_id=world.spare_record("stale"),
        )
    )
    command = root_command(
        world,
        delivery_receipts=(world.delivery(DeliveryStage.CONFIRMED, acceptance_id="acc-stale"),),
    )
    caught = refusal_error(world.resolve, command)
    assert caught.reason == "DELIVERY_RECEIPT_INVALID"
    assert "STALE" in caught.detail
    assert world.counts()["goal_resolutions"] == 0


def test_a_receipt_quoting_a_revoked_acceptance_is_invalid(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    world.semantics.insert_acceptance(
        world.child_acceptance(
            acceptance_id="acc-revoked",
            validity=Validity.REVOKED,
            review_record_id=world.spare_record("revoked"),
        )
    )
    command = root_command(
        world,
        delivery_receipts=(world.delivery(DeliveryStage.CONFIRMED, acceptance_id="acc-revoked"),),
    )
    assert refusal(world.resolve, command) == "DELIVERY_RECEIPT_INVALID"


def test_a_receipt_quoting_a_duty_outside_the_root_closure_is_invalid(tmp_path: Any) -> None:
    """A receipt for other work does not deliver this goal (AER §6.3)."""

    world = root_world(tmp_path)
    world.accept()
    world.duties.register(
        Obligation(
            obligation_id=ObligationId("obl-unrelated"),
            mission_id=world.mission.id,
            requirement_refs=(CRITERION,),
            goal_signature_id="leaf-goal",
        ),
        recursion_fuel=1,
    )
    world.semantics.insert_acceptance(
        world.child_acceptance(
            acceptance_id="acc-unrelated",
            obligation_id=ObligationId("obl-unrelated"),
            review_record_id=world.spare_record("unrelated"),
        )
    )
    command = root_command(
        world,
        delivery_receipts=(world.delivery(DeliveryStage.CONFIRMED, acceptance_id="acc-unrelated"),),
    )
    caught = refusal_error(world.resolve, command)
    assert caught.reason == "DELIVERY_RECEIPT_INVALID"
    assert "duty closure" in caught.detail


def test_one_invalid_receipt_refuses_even_when_another_would_have_done(
    tmp_path: Any,
) -> None:
    """An invalid receipt is not scanned past: the caller is claiming something wrong."""

    world = root_world(tmp_path)
    world.accept()
    world.semantics.insert_acceptance(
        world.child_acceptance(
            acceptance_id="acc-stale2",
            validity=Validity.STALE,
            review_record_id=world.spare_record("stale2"),
        )
    )
    command = root_command(
        world,
        delivery_receipts=(
            world.delivery(DeliveryStage.CONFIRMED, acceptance_id="acc-stale2"),
            world.delivery(DeliveryStage.CONFIRMED),
        ),
    )
    assert refusal(world.resolve, command) == "DELIVERY_RECEIPT_INVALID"


def test_the_root_duty_closure_is_the_root_plus_its_adopted_children(tmp_path: Any) -> None:
    world = root_world(tmp_path)
    world.accept()
    # The leaf duty is inside the closure, which is why the happy root path works.
    assert world.resolve(root_command(world)).resolution_id == "res-root-1"


# ======================================================================================
# 3b. Review fixes: all eleven read-set channels, one implementation
# ======================================================================================


def test_the_accept_path_and_the_plan_path_share_one_checker() -> None:
    """Two implementations drift; the review found one that had (ADR-13 clause 2)."""

    import agent_orchestrator.orchestrator.plan_commits as plan_commits
    import agent_orchestrator.orchestrator.resolution_commits as module

    assert module.SemanticReadSetChecker is SemanticReadSetChecker
    assert plan_commits.SemanticReadSetChecker is SemanticReadSetChecker
    source = inspect.getsource(ResolutionCommitsMixin._check_reads)
    assert "SemanticReadSetChecker" in source


def test_every_channel_a_read_set_can_carry_is_re_checked() -> None:
    fields = {item.name for item in dataclasses.fields(SemanticReadSet)}
    source = inspect.getsource(SemanticReadSetChecker)
    # ``manager_epoch`` is each caller's own earlier gate; every other field is here.
    for name in fields - {"manager_epoch"}:
        assert name in source, name
    # And the five the review found missing are named in ``verify`` itself.
    verified = inspect.getsource(SemanticReadSetChecker.verify)
    for name in ("method", "observation", "obligation", "authority", "absences"):
        assert name in verified, name
    assert "_check_budget_grant" in verified


def test_a_review_resting_on_a_refuted_observation_is_refused(world: World) -> None:
    """The FACT channel.  A later observation of the same proposition is the refutation."""

    first = observation("obs-1", "prop-1", polarity=True)
    world.semantics.insert_observation(world.mission.id, first)
    item = ReadItem(
        kind=ReadItemKind.FACT,
        id=first.observation_id,
        semantic_revision=1,
        content_hash=content_hash_of(first.to_json()),
    )
    clean = world.accept_command(read_set=world.read_set(observation_revisions=(item,)))
    assert world.accept(clean).acceptance is not None

    # Now somebody observes the opposite.  The row the review read is untouched, and
    # that is exactly why the *set* has to be re-checked (AER scenario I02).
    world.semantics.insert_observation(
        world.mission.id, observation("obs-2", "prop-1", polarity=False)
    )
    later = world.accept_command(
        command_id="cmd-accept-refuted",
        acceptance_id="acc-leaf-2",
        read_set=world.read_set(observation_revisions=(item,)),
    )
    caught = refusal_error(world.accept, later)
    assert caught.reason == "READ_SET_STALE"
    # The detail truncates the hash to twelve characters, so the marker is the prefix:
    # the channel reports *supersession* rather than a hash that happens to differ.
    assert "observation 'obs-1'" in caught.detail
    assert "superseded_b" in caught.detail


def test_a_review_resting_on_a_revoked_authority_is_refused(world: World) -> None:
    """The AUTHORITY channel.  A revoked approval moves its version, nothing else."""

    world.store.put_approval(
        {
            "request_id": "appr-1",
            "kind": "action",
            "mission_id": world.mission.id,
            "subject_key": LEAF_TASK,
            "state": "GRANTED",
            "version": 1,
        }
    )
    granted = world.store.get_approval("appr-1")
    assert granted is not None
    item = ReadItem(
        kind=ReadItemKind.AUTHORITY,
        id="appr-1",
        semantic_revision=1,
        content_hash=content_hash_of(dict(granted)),
    )
    clean = world.accept_command(read_set=world.read_set(authority_revisions=(item,)))
    assert world.accept(clean).acceptance is not None

    world.store.put_approval(
        {
            "request_id": "appr-1",
            "kind": "action",
            "mission_id": world.mission.id,
            "subject_key": LEAF_TASK,
            "state": "REVOKED",
            "version": 2,
        }
    )
    later = world.accept_command(
        command_id="cmd-accept-revoked",
        acceptance_id="acc-leaf-3",
        read_set=world.read_set(authority_revisions=(item,)),
    )
    caught = refusal_error(world.accept, later)
    assert caught.reason == "READ_SET_STALE"
    assert "authority 'appr-1'" in caught.detail


def test_a_review_resting_on_a_re_planned_duty_is_refused(world: World) -> None:
    """The OBLIGATION channel: a duty's shape history is its semantic revision."""

    duty = ObligationId(LEAF_DUTY)
    account = world.duties.account(world.mission.id, duty)
    obligation = world.duties.obligation(world.mission.id, duty)
    item = ReadItem(
        kind=ReadItemKind.OBLIGATION,
        id=LEAF_DUTY,
        semantic_revision=account.shape_changes,
        content_hash=content_hash_of(
            {
                "obligation": obligation.to_json(),
                "lifecycle": str(account.lifecycle),
                "resolution_ref": account.resolution_ref,
            }
        ),
    )
    clean = world.accept_command(read_set=world.read_set(obligation_revisions=(item,)))
    assert world.accept(clean).acceptance is not None

    world.duties.note_shape_change(
        world.mission.id, duty, change=ShapeChange.METHOD_SWITCHED, detail="re-planned"
    )
    later = world.accept_command(
        command_id="cmd-accept-replanned",
        acceptance_id="acc-leaf-4",
        read_set=world.read_set(obligation_revisions=(item,)),
    )
    assert refusal(world.accept, later) == "READ_SET_STALE"


def test_a_method_the_registry_suspended_is_refused(world: World) -> None:
    """The METHOD channel reports the *status*, which is more useful than a hash."""

    item = ReadItem(
        kind=ReadItemKind.METHOD,
        id="m-nobody",
        semantic_revision=1,
        content_hash=HASH_B,
    )
    command = world.accept_command(read_set=world.read_set(method_revisions=(item,)))
    assert refusal(world.accept, command) == "READ_SET_UNRESOLVED"


def test_an_absence_that_became_false_is_refused(world: World) -> None:
    """The absence channel: "there is nothing there" goes stale by becoming false."""

    clean = world.accept_command(
        read_set=world.read_set(
            absences=(
                AbsenceRead(predicate="no_obligation", scope_id="obl-nobody", range_revision=0),
            )
        )
    )
    assert world.accept(clean).acceptance is not None
    broken = world.accept_command(
        command_id="cmd-accept-absence",
        acceptance_id="acc-leaf-5",
        read_set=world.read_set(
            absences=(AbsenceRead(predicate="no_obligation", scope_id=LEAF_DUTY, range_revision=0),)
        ),
    )
    caught = refusal_error(world.accept, broken)
    assert caught.reason == "READ_SET_STALE"
    assert "absence" in caught.detail


def test_an_absence_predicate_nobody_can_recheck_is_unresolved(world: World) -> None:
    command = world.accept_command(
        read_set=world.read_set(
            absences=(
                AbsenceRead(predicate="no_conflicting_operation", scope_id=SCOPE, range_revision=0),
            )
        )
    )
    caught = refusal_error(world.accept, command)
    assert caught.reason == "READ_SET_UNRESOLVED"
    assert "not one this deployment can re-check" in caught.detail


def test_a_claimed_budget_grant_revision_nobody_can_confirm_is_unresolved(
    world: World,
) -> None:
    """Fail closed: a budget the commit cannot re-read is not one it may spend against."""

    command = world.accept_command(read_set=world.read_set(budget_grant_revision=7))
    caught = refusal_error(world.accept, command)
    assert caught.reason == "READ_SET_UNRESOLVED"
    assert "budget_grant_revision" in caught.detail


def test_the_dispatch_control_channels_of_an_eligibility_read_set_resolve(
    world: World,
) -> None:
    """An accept command's read-set is the one ``build_read_set`` produced."""

    item = ReadItem(
        kind=ReadItemKind.TASK,
        id=f"{LEAF_TASK}#dispatch_generation",
        semantic_revision=0,
        content_hash=content_hash_of(
            {"task": LEAF_TASK, "channel": "dispatch_generation", "value": 0}
        ),
    )
    command = world.accept_command(read_set=world.read_set(goal_revisions=(item,)))
    assert world.accept(command).acceptance is not None


def test_a_dispatch_control_channel_read_at_the_wrong_value_is_stale(world: World) -> None:
    item = ReadItem(
        kind=ReadItemKind.TASK,
        id=f"{LEAF_TASK}#dispatch_generation",
        semantic_revision=4,
        content_hash=content_hash_of(
            {"task": LEAF_TASK, "channel": "dispatch_generation", "value": 4}
        ),
    )
    command = world.accept_command(read_set=world.read_set(goal_revisions=(item,)))
    assert refusal(world.accept, command) == "READ_SET_STALE"


# ======================================================================================
# 1b. Review fixes: reason codes, and a replay that does not scan the Mission
# ======================================================================================


def test_every_reason_code_is_upper_snake_case(world: World) -> None:
    """A reason is a machine name the caller branches on, so it has one shape."""

    seen: set[str] = set()
    for call, command in (
        (world.accept, object()),
        (world.resolve, object()),
    ):
        with pytest.raises(ResolutionCommitRejected) as caught:
            call(command)  # type: ignore[arg-type]
        seen.add(caught.value.reason)
    seen.update({"BAD_COMMAND", "BAD_PRINCIPAL"})
    for reason in seen:
        assert reason == reason.upper(), reason
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", reason), reason


def test_a_bad_principal_is_upper_snake_too(world: World) -> None:
    with pytest.raises(ResolutionCommitRejected) as caught:
        world.service.accept_review(world.accept_command(), object())  # type: ignore[arg-type]
    assert caught.value.reason == "BAD_PRINCIPAL"


def test_the_event_key_is_derived_from_the_command_id(world: World) -> None:
    """``append_event``'s unique key *is* the one-command-one-commit guarantee."""

    receipt = world.accept()
    event = world.events(ACCEPTANCE_COMMITTED)[0]
    assert event.idempotency_key == command_idempotency_key(
        world.mission.id, "cmd-accept-1", ACCEPTANCE_COMMITTED
    )
    assert receipt.commit.event_id == event.id


def test_a_replay_lookup_does_not_read_the_whole_mission(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay lookup is a keyed read into migration 17, not a scan of the log.

    P2.3c part 1 had to page the Mission's events because ``plan_commit_receipts`` is
    plan-shaped and ``storage/`` was not its to change; it got the common case down to
    one indexed ``count_events``.  Part 2 owns storage, so ``(mission_id, command_id)``
    is a primary key and *neither* the first command nor its replay reads the log.
    """

    calls: list[int] = []
    real = Store.list_events

    def counted(self: Store, mission_id: str, **kwargs: Any) -> Any:
        calls.append(1)
        return real(self, mission_id, **kwargs)

    monkeypatch.setattr(Store, "list_events", counted)
    world.accept()
    assert calls == []
    world.accept()
    assert calls == []


def test_a_replay_of_the_wrong_kind_is_a_conflict(world: World) -> None:
    """One command id may not come back as the other action's receipt."""

    world.accept()
    resolution = world.resolution_command(command_id="cmd-accept-1")
    assert refusal(world.resolve, resolution) == "COMMAND_PAYLOAD_CONFLICT"


# ======================================================================================
# 7b. Review fixes: a contribution needs a live duty, keyed by occurrence
# ======================================================================================


def test_an_acceptance_on_a_cancelled_duty_is_not_a_contribution(world: World) -> None:
    world.accept()
    world.duties.set_lifecycle(
        world.mission.id, ObligationId(LEAF_DUTY), ObligationLifecycle.CANCELLED
    )
    assert refusal(world.resolve) == "COMPOUND_FACTS_CONTRADICT_STORE"


def test_an_acceptance_on_a_superseded_duty_is_not_a_contribution(world: World) -> None:
    world.accept()
    world.duties.set_lifecycle(
        world.mission.id, ObligationId(LEAF_DUTY), ObligationLifecycle.SUPERSEDED
    )
    command = world.resolution_command(
        compound=CompoundFacts(
            selected_method_legal=True,
            contributing_occurrence_ids=(),
            composition_obligation_passed=True,
        )
    )
    assert refusal(world.resolve, command) == "NOT_ACCEPTABLE"


def test_the_event_records_which_acceptance_carried_which_occurrence(world: World) -> None:
    world.accept()
    world.resolve()
    payload = world.events(GOAL_RESOLUTION_COMMITTED)[0].payload
    assert payload["contributing_acceptances"] == {LEAF_OCCURRENCE: ["acc-leaf-1"]}


def test_mutant_ignoring_the_duty_lifecycle_would_count_a_cancelled_contribution(
    world: World,
) -> None:
    world.accept()
    assert world.resolve().resolution_id == "res-root-1"
    # The same store with the duty cancelled first refuses, which is the difference
    # the review asked for: an acceptance is not a contribution on a dead duty.
    other = build_world(pathlib.Path(tempfile.mkdtemp()), key="p23c-cancel")
    other.accept()
    other.duties.set_lifecycle(
        other.mission.id, ObligationId(LEAF_DUTY), ObligationLifecycle.CANCELLED
    )
    assert refusal(other.resolve) == "COMPOUND_FACTS_CONTRADICT_STORE"
