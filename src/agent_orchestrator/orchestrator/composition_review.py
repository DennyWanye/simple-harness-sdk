# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3l / N7: a non-root compound in ``composition_review`` forms a GoalResolution.

A compound whose gating children are accepted parks in ``composition_review``.  The
root has a ``MISSION_FINAL`` coordinator; an inner compound had none, so ORDER
successors (M3's ``revert``) stayed ``WAITING_ORDER`` forever.

This assembly is the missing trigger.  It does **not** ask a model and it does
**not** invent PASS (AER I05):

* the package is a ``ReviewPurpose.COMPOSITION`` anchor over the children's
  CURRENT Acceptances;
* the record transcribes each parent criterion from the linked child's official
  review — missing or non-PASS becomes ``UNKNOWN``;
* ``composition_obligation_passed`` is the record's own verdict;
* ``commit_goal_resolution`` (``is_mission_root=False``) is the only writer.

Why not reuse the root-reviewer service intent: §6.3 / ``leaf_acceptance`` already
say a compound is satisfied out of its children's acceptances, never by a review
of its own.  Those children already had independent TASK_CONTENT reviews.  A
second model call would charge ``PARENT_COMPOUND_TASK`` against a 0-token
compound budget, and M3's assess compound has no root requirements to judge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts.evidence_state import (
    Availability,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from ..contracts.htn import (
    MethodInstanceId,
    OccurrenceId,
    ReadItem,
    ReadItemKind,
    ScopeEpochRead,
    SemanticReadSet,
    TaskForm,
)
from ..contracts.models import ContractError
from ..contracts.resolution import (
    AllExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
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
from ..contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from ..graph.task_network import GATING_REQUIREDNESS
from ..knowledge.validity import NO_SUBJECT, witness_subject
from ..storage.htn_store import HtnStore
from ..storage.store import StoreError
from ..verification.acceptance_rules import CompoundFacts, ExecutionPosture, IndependenceFacts
from .accepted_outputs import carried_criteria_in_revision
from .hierarchical_dispatch import CompoundPhase, HierarchicalDispatch, next_compound_phase
from .resolution_commits import (
    CommitGoalResolutionCommand,
    GoalResolutionReceipt,
    ResolutionCommitRejected,
    ResolutionPrincipal,
)

COMPOSITION_REVIEW_POLICY = "hierarchical-composition-review-v1"
COMPOSITION_REVIEWER = "composition-reviewer"
COMPOSITION_LOCAL_CRITERION = "c-composition"
COMPOSITION_UNCOVERED = "composition_criterion_uncovered"
REVIEWER_ACCESS = WorkspaceAccess.READ_ONLY


@dataclass(frozen=True, slots=True)
class CompositionAcceptanceAssembly:
    """Form a non-root ``GoalResolution`` from children's CURRENT Acceptances."""

    store: Any
    commit: Any
    dispatch: HierarchicalDispatch
    scope_id: str = "mission"
    issued_by: str = "orchestrator"

    @property
    def semantics(self) -> HtnStore:
        return HtnStore(self.store)

    def resolve_ready(self, mission_id: str) -> tuple[GoalResolutionReceipt, ...]:
        """Resolve every non-root compound that is waiting on composition review.

        Idempotent: an occurrence that already has an adopted GoalResolution is
        skipped.  A refusal is noted by raising into the caller; this method
        swallows ``ResolutionCommitRejected`` so one stuck compound does not
        block its siblings.
        """

        view = self.dispatch.read(mission_id)
        formed: list[GoalResolutionReceipt] = []
        for spec in view.network.occurrences:
            if spec.form is not TaskForm.COMPOUND:
                continue
            if spec.occurrence_id in view.network.root_occurrence_ids:
                continue
            if spec.occurrence_id in view.resolved:
                continue
            if any(
                str(item.goal_task_id) == str(spec.task_id)
                for item in self.semantics.list_goal_resolutions(mission_id)
            ):
                continue
            children = {
                binding.occurrence_id: view.outcomes.get(binding.occurrence_id)
                for binding in view.network.adopted_children(spec.occurrence_id)
            }
            phase = next_compound_phase(
                spec,
                view.network,
                view.reports[spec.occurrence_id],
                child_outcomes=children,
                resolved=False,
            )
            if phase is not CompoundPhase.COMPOSITION_REVIEW:
                continue
            try:
                receipt = self.resolve_one(mission_id, spec.occurrence_id)
            except (ContractError, ResolutionCommitRejected, StoreError):
                continue
            if receipt is not None:
                formed.append(receipt)
        if formed:
            self.dispatch.advance_compound_phases(mission_id)
        return tuple(formed)

    def resolve_one(
        self, mission_id: str, occurrence_id: OccurrenceId
    ) -> GoalResolutionReceipt | None:
        """Build the COMPOSITION anchors and commit, or return None if not ready."""

        view = self.dispatch.read(mission_id)
        spec = view.network.occurrence(occurrence_id)
        adopted = view.network.adopted_instance_for(occurrence_id)
        if adopted is None:
            return None
        instance_id = str(adopted.instance_id)
        gating = [
            binding
            for binding in view.network.adopted_children(occurrence_id)
            if binding.requiredness in GATING_REQUIREDNESS
        ]
        accepted = self._accepted_children(mission_id, gating)
        if any(str(binding.occurrence_id) not in accepted for binding in gating):
            return None
        binding = view.network.binding_for_occurrence(occurrence_id)
        now_ms = int(self.store.now * 1000)
        producers = self._producers(mission_id, accepted)
        revision = self._requirements(mission_id, binding, occurrence_id)
        manifest = self._manifest_hash(mission_id, str(spec.task_id))
        package = self._package(
            mission_id,
            spec,
            binding,
            revision,
            accepted,
            producers,
            manifest_hash=manifest,
            method_instance_id=instance_id,
        )
        record = self._record(mission_id, package, occurrence_id, accepted, gating)
        if record.verdict is not ReviewVerdict.ACCEPT:
            return None
        witness = self._witness(mission_id, str(spec.task_id), now_ms=now_ms)
        resolution = GoalResolution(
            resolution_id=GoalResolutionId(f"res-{occurrence_id}"),
            mission_id=mission_id,
            obligation_id=str(spec.obligation_id),
            goal_task_id=str(spec.task_id),
            requirements_version=int(revision.revision),
            contract_revision=int(binding.contract_revision),
            method_instance_id=instance_id,
            input_manifest_hash=manifest,
            artifact_refs=(),
            child_resolution_ids=(),
            criteria=tuple(
                ResolutionCriterion(criterion_id=item.criterion_id, verdict=item.verdict)
                for item in record.criteria
            ),
            review_receipt_id=str(record.record_id),
            verdict=ReviewVerdict.ACCEPT,
            validity=Validity.CURRENT,
        )
        command = CommitGoalResolutionCommand(
            command_id=f"compose:{occurrence_id}:{int(view.network.plan_revision)}",
            mission_id=mission_id,
            resolution=resolution,
            package=package,
            record=record,
            requirements=revision,
            witness_id=witness.witness_id,
            independence=IndependenceFacts(
                producer_agent_ids=producers,
                reviewer_can_write_candidate=False,
            ),
            posture=ExecutionPosture(),
            read_set=self._read_set(mission_id, binding, revision),
            decided_at_ms=now_ms,
            purpose=ReviewPurpose.COMPOSITION,
            compound=CompoundFacts(
                selected_method_legal=True,
                contributing_occurrence_ids=tuple(sorted(accepted)),
                composition_obligation_passed=record.verdict is ReviewVerdict.ACCEPT,
            ),
            is_mission_root=False,
            semantic_review_required=True,
            issued_by=self.issued_by,
            scope_id=self.scope_id,
            source={"occurrence_id": str(occurrence_id), "policy": COMPOSITION_REVIEW_POLICY},
        )
        principal = ResolutionPrincipal(
            principal_id=self.issued_by,
            scope_id=self.scope_id,
            manager_epoch=self.semantics.epoch(mission_id, self.scope_id),
        )
        return self.commit.commit_goal_resolution(command, principal)

    def _accepted_children(
        self, mission_id: str, gating: Sequence[Any]
    ) -> dict[str, tuple[str, ...]]:
        semantics = self.semantics
        view = self.dispatch.read(mission_id)
        found: dict[str, tuple[str, ...]] = {}
        for binding in gating:
            spec = view.network.occurrence(binding.occurrence_id)
            task_id = str(spec.task_id)
            usable = tuple(
                str(item.acceptance_id)
                for item in semantics.list_acceptances(mission_id)
                if str(item.validity) == "CURRENT" and str(item.task_id) == task_id
            )
            if usable:
                found[str(binding.occurrence_id)] = usable
        return found

    def _producers(
        self, mission_id: str, accepted: Mapping[str, tuple[str, ...]]
    ) -> tuple[str, ...]:
        semantics = self.semantics
        agents: list[str] = []
        for ids in accepted.values():
            for acceptance_id in ids:
                try:
                    acceptance = semantics.get_acceptance(acceptance_id)
                except StoreError:
                    continue
                try:
                    stored = semantics.get_review_record(str(acceptance.review_record_id))
                except StoreError:
                    continue
                try:
                    package = semantics.get_review_package(str(stored.record.package_id))
                except StoreError:
                    continue
                agents.extend(str(item) for item in package.producer_agent_ids)
        return tuple(dict.fromkeys(item for item in agents if item))

    def _requirements(
        self, mission_id: str, binding: Any, occurrence_id: OccurrenceId
    ) -> RequirementsRevision:
        semantics = self.semantics
        latest = semantics.latest_requirements_revision(mission_id)
        criteria = self._criteria(binding, occurrence_id)
        expression: Any = CriterionExpr(criteria[0].criterion_id)
        if len(criteria) > 1:
            expression = AllExpr(
                children=tuple(CriterionExpr(item.criterion_id) for item in criteria)
            )
        revision_no = 1 if latest is None else int(latest.revision) + 1
        candidate = RequirementsRevision(
            revision_id=RequirementsRevisionId(f"req-{mission_id}-{revision_no}-compose"),
            mission_id=mission_id,
            revision=revision_no if latest is None else int(latest.revision),
            criteria=criteria,
            success_expression=expression,
        )
        if latest is not None and latest.content_hash() == candidate.content_hash():
            return latest
        published = (
            candidate
            if latest is None
            else RequirementsRevision(
                revision_id=RequirementsRevisionId(f"req-{mission_id}-{revision_no}-compose"),
                mission_id=mission_id,
                revision=revision_no,
                criteria=criteria,
                success_expression=expression,
            )
        )
        semantics.insert_requirements_revision(published)
        return published

    def _criteria(self, binding: Any, occurrence_id: OccurrenceId) -> tuple[Criterion, ...]:
        names = [str(item) for item in binding.goal_signature.coverage_criteria]
        if not names:
            names = [COMPOSITION_LOCAL_CRITERION]
        policy = RequiredEvidencePolicy(required_check_ids=(), independence_required=False)
        return tuple(
            Criterion(
                criterion_id=name,
                revision=1,
                origin=CriterionOrigin.DERIVED,
                statement=(
                    f"{name} is covered by the accepted children of {occurrence_id}"
                ),
                requirement_class=RequirementClass.REQUIRED_OUTCOME,
                evaluation_kind=EvaluationKind.SEMANTIC,
                required_evidence_policy=policy,
            )
            for name in names
        )

    def _manifest_hash(self, mission_id: str, task_id: str) -> str:
        document: dict[str, Any] = {"consumer_task_ref": str(task_id), "bindings": []}
        return self.semantics.insert_input_manifest(mission_id, str(task_id), document)

    def _package(
        self,
        mission_id: str,
        spec: Any,
        binding: Any,
        revision: RequirementsRevision,
        accepted: Mapping[str, tuple[str, ...]],
        producers: Sequence[str],
        *,
        manifest_hash: str,
        method_instance_id: str,
    ) -> ReviewPackage:
        child_refs = tuple(
            TypedRef(
                kind=TypedRefKind.ACCEPTANCE,
                id=acceptance_id,
                revision=1,
                content_hash=content_hash_of(acceptance_id),
                produced_by=Provenance.TOOL,
            )
            for ids in accepted.values()
            for acceptance_id in ids
        )
        digest = content_hash_of(
            {"occ": str(spec.occurrence_id), "rev": revision.revision}
        )[:32]
        package = ReviewPackage(
            package_id=ReviewPackageId(f"pkg-compose-{digest}"),
            purpose=ReviewPurpose.COMPOSITION,
            binding=ReviewBinding(
                mission_id=mission_id,
                obligation_id=str(spec.obligation_id),
                subject_ref=TypedRef(
                    kind=TypedRefKind.TASK,
                    id=str(spec.task_id),
                    revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
                requirements_revision=int(revision.revision),
                input_manifest_hash=manifest_hash,
                policy_ref=TypedRef(
                    kind=TypedRefKind.SOURCE,
                    id=COMPOSITION_REVIEW_POLICY,
                    revision=1,
                    content_hash=content_hash_of(COMPOSITION_REVIEW_POLICY),
                ),
            ),
            criteria=revision.criteria,
            success_expression=revision.success_expression,
            child_acceptance_refs=child_refs,
            producer_agent_ids=tuple(producers),
            reviewer_workspace_access=REVIEWER_ACCESS,
            requirements_content_hash=revision.content_hash(),
            method_instance_id=MethodInstanceId(method_instance_id),
        )
        try:
            stored = self.semantics.get_review_package(str(package.package_id))
        except StoreError:
            self.semantics.insert_review_package(package)
            return package
        if stored.content_hash() != package.content_hash():
            self.semantics.insert_review_package(package)
        return package

    def _record(
        self,
        mission_id: str,
        package: ReviewPackage,
        occurrence_id: OccurrenceId,
        accepted: Mapping[str, tuple[str, ...]],
        gating: Sequence[Any],
    ) -> ReviewRecord:
        outcomes = self._outcomes(mission_id, package, occurrence_id, accepted)
        passed = all(item.verdict is CriterionVerdict.PASS for item in outcomes) and all(
            str(binding.occurrence_id) in accepted for binding in gating
        )
        record = ReviewRecord(
            record_id=ReviewRecordId(f"rec-{content_hash_of(str(package.package_id))[:32]}"),
            package_id=package.package_id,
            purpose=package.purpose,
            binding=package.binding,
            reviewer_agent_id=COMPOSITION_REVIEWER,
            reviewer_turn_id=f"compose-{occurrence_id}",
            evidence_manifest_hash=content_hash_of(sorted(accepted)),
            criteria=outcomes,
            verdict=ReviewVerdict.ACCEPT if passed else ReviewVerdict.REJECTED,
        )
        official = self.semantics.official_review_record(str(package.package_id))
        if official is None or official.to_json() != record.to_json():
            self.semantics.insert_review_record(record, official=True)
        return record

    def _outcomes(
        self,
        mission_id: str,
        package: ReviewPackage,
        occurrence_id: OccurrenceId,
        accepted: Mapping[str, tuple[str, ...]],
    ) -> tuple[CriterionOutcome, ...]:
        semantics = self.semantics
        active = semantics.active_plan_revision(mission_id)
        links = (
            ()
            if active is None
            else tuple(
                item
                for item in carried_criteria_in_revision(
                    semantics, mission_id, int(active.revision)
                )
                if str(item.parent_task_id)
                == str(package.binding.subject_ref.id)
            )
        )
        by_parent: dict[str, list[Any]] = {}
        for link in links:
            by_parent.setdefault(str(link.parent_criterion_id), []).append(link)
        outcomes: list[CriterionOutcome] = []
        for criterion in package.criteria:
            name = str(criterion.criterion_id)
            # AER I05/I07: ``c-composition`` is a synthetic local name used when
            # the compound's goal signature published no coverage_criteria.  Child
            # acceptances are not a mapping and must not become PASS.
            linked = by_parent.get(name, ())
            verdict = CriterionVerdict.UNKNOWN
            for link in linked:
                child_key = str(link.occurrence_id)
                if child_key not in accepted:
                    continue
                if self._child_covers(mission_id, accepted[child_key], link.leaf_criterion_id):
                    verdict = CriterionVerdict.PASS
                    break
            limitations = (
                (COMPOSITION_UNCOVERED,)
                if verdict is CriterionVerdict.UNKNOWN
                else ()
            )
            outcomes.append(
                CriterionOutcome(
                    criterion_id=name,
                    verdict=verdict,
                    check_execution=(
                        CheckExecution.SUCCEEDED
                        if verdict is CriterionVerdict.PASS
                        else CheckExecution.NOT_RUN
                    ),
                    limitations=limitations,
                )
            )
        del occurrence_id
        return tuple(outcomes)

    def _child_covers(
        self, mission_id: str, acceptance_ids: Sequence[str], leaf_criterion_id: str
    ) -> bool:
        semantics = self.semantics
        for acceptance_id in acceptance_ids:
            try:
                acceptance = semantics.get_acceptance(acceptance_id)
            except StoreError:
                continue
            if str(acceptance.validity) != "CURRENT":
                continue
            try:
                stored = semantics.get_review_record(str(acceptance.review_record_id))
            except StoreError:
                continue
            record = stored.record
            if record.verdict is not ReviewVerdict.ACCEPT:
                continue
            by_id = {str(item.criterion_id): item.verdict for item in record.criteria}
            if by_id.get(str(leaf_criterion_id)) is CriterionVerdict.PASS:
                return True
        del mission_id
        return False

    def _witness(self, mission_id: str, task_id: str, *, now_ms: int) -> ValidityWitness:
        semantics = self.semantics
        epoch = int(semantics.epoch(mission_id, self.scope_id))
        support_revision = len(semantics.list_acceptances(mission_id))
        witness = ValidityWitness(
            witness_id="wit-"
            + content_hash_of(
                {
                    "consumer": str(task_id),
                    "purpose": str(WitnessPurpose.ACCEPT),
                    "scope": self.scope_id,
                    "epoch": epoch,
                    "support_revision": int(support_revision),
                    "subject": NO_SUBJECT,
                    "lane": "composition",
                }
            )[:32],
            consumer_ref=TypedRef(
                kind=TypedRefKind.TASK,
                id=str(task_id),
                revision=1,
                content_hash=content_hash_of(str(task_id)),
            ),
            purpose=WitnessPurpose.ACCEPT,
            truth=TruthValue.TRUE,
            freshness=Validity.CURRENT,
            availability=Availability.READABLE,
            decision=WitnessDecision.USABLE,
            scope_id=self.scope_id,
            scope_epoch=epoch,
            support_revision=int(support_revision),
            as_of_ms=int(now_ms),
        )
        subject = witness_subject(witness)
        try:
            return semantics.get_validity_witness(witness.witness_id)
        except StoreError:
            semantics.insert_validity_witness(mission_id, witness, subject=subject)
            return witness

    def _read_set(
        self, mission_id: str, binding: Any, revision: RequirementsRevision
    ) -> SemanticReadSet:
        epoch = self.semantics.epoch(mission_id, self.scope_id)
        return SemanticReadSet(
            requirements_revision=int(revision.revision),
            manager_epoch=epoch,
            scope_epochs=(ScopeEpochRead(scope_id=self.scope_id, validity_epoch=epoch),),
            goal_revisions=(
                ReadItem(
                    kind=ReadItemKind.TASK,
                    id=str(binding.task_id),
                    semantic_revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
            ),
        )
