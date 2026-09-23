# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct a compound's frozen content projection from persisted authority."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.htn import TaskForm
from ..contracts.operation_completion import OccurrenceCompletionScopeV1, PlanRevisionPinV1
from ..contracts.resolution import (
    AllExpr,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    EvaluationKind,
    RequiredEvidencePolicy,
    RequirementClass,
    RequirementsRevision,
)
from ..storage.htn_store import HtnStore
from ..storage.store import Store
from .operation_completion import OperationCompletionError, OperationCompletionReader


@dataclass(frozen=True, slots=True)
class ScopedCompositionProjection:
    requirements: RequirementsRevision
    scope: OccurrenceCompletionScopeV1
    criteria: tuple[Criterion, ...]
    expression: CriterionExpr | AllExpr


def read_compound_projection(
    store: Store, mission_id: str, occurrence_id: str, task_id: str
) -> ScopedCompositionProjection:
    """Read one compound projection; callers cannot supply its criteria."""

    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        if active is None:
            raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "no active Plan")
        members = [
            item
            for item in htn.list_plan_memberships(mission_id, active.revision)
            if str(item.occurrence_id) == str(occurrence_id)
        ]
        binding = htn.task_semantics_of(mission_id, task_id)
        if (
            len(members) != 1
            or str(members[0].task_id) != task_id
            or binding is None
            or binding.form is not TaskForm.COMPOUND
        ):
            raise OperationCompletionError(
                "OP_COMPLETION_SCOPE_UNRESOLVED", "no exact planned compound Task"
            )
        reader = OperationCompletionReader(store)
        scope = reader.read_scope(
            mission_id,
            PlanRevisionPinV1(revision=int(active.revision), snapshot_hash=active.snapshot_hash),
            str(occurrence_id),
        )
        requirements = htn.get_requirements_revision(
            mission_id, int(scope.requirements_ref.revision)
        )
        root_catalogue = {item.criterion_id: item for item in requirements.criteria}
        declared_local = set(binding.goal_signature.coverage_criteria) - set(root_catalogue)
        criteria: list[Criterion] = []
        for criterion_id in scope.content_criterion_ids:
            if criterion_id in root_catalogue:
                criteria.append(root_catalogue[criterion_id])
                continue
            if criterion_id not in declared_local:
                raise OperationCompletionError(
                    "OP_COMPLETION_SCOPE_UNRESOLVED",
                    "compound Scope contains no Task-declared local criterion",
                )
            criteria.append(
                Criterion(
                    criterion_id=criterion_id,
                    revision=1,
                    origin=CriterionOrigin.DERIVED,
                    statement=(
                        f"{criterion_id} is covered by the accepted children of {occurrence_id}"
                    ),
                    requirement_class=RequirementClass.REQUIRED_OUTCOME,
                    evaluation_kind=EvaluationKind.SEMANTIC,
                    required_evidence_policy=RequiredEvidencePolicy(
                        required_check_ids=(), independence_required=False
                    ),
                )
            )
        if not criteria:
            raise OperationCompletionError(
                "OP_COMPLETION_SCOPE_UNRESOLVED", "compound Scope has no content projection"
            )
        frozen_criteria = tuple(criteria)
        expression: CriterionExpr | AllExpr = CriterionExpr(frozen_criteria[0].criterion_id)
        if len(frozen_criteria) > 1:
            expression = AllExpr(
                children=tuple(CriterionExpr(item.criterion_id) for item in frozen_criteria)
            )
        return ScopedCompositionProjection(requirements, scope, frozen_criteria, expression)


__all__ = ("ScopedCompositionProjection", "read_compound_projection")
