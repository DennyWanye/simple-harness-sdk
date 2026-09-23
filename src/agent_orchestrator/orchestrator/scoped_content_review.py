# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct TASK_CONTENT review scope from persisted production identities.

No Requirements revision is synthesized here. A local review remains local; the
root expression and all effect slots are evaluated by their completion consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..artifacts.paths import normalise_workspace_path
from ..contracts.htn import OccurrenceId, TaskForm, TaskRef
from ..contracts.models import ContractError
from ..contracts.operation_completion import (
    CompletionPinV1,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
)
from ..contracts.resolution import (
    AllExpr,
    Criterion,
    CriterionExpr,
    RequirementsRevision,
    SuccessExpression,
)
from ..contracts.semantic_base import TypedRef, TypedRefKind
from ..storage.htn_store import HtnStore
from ..storage.store import Store
from .operation_completion import OperationCompletionError, OperationCompletionReader


def uses_completion_protocol(store: Store, mission_id: str) -> bool:
    row = store.connection.execute(
        "SELECT protocol_version FROM mission_planning_protocols WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    return row is not None and row[0] == "planning-decision-v1"


@dataclass(frozen=True, slots=True)
class ScopedTaskContent:
    requirements: RequirementsRevision
    spec: OperationCompletionRequirementsV1
    scope: OccurrenceCompletionScopeV1
    criteria: tuple[Criterion, ...]
    expression: SuccessExpression
    artifacts: tuple[CompletionPinV1, ...]
    producer_agent_id: str


@dataclass(frozen=True, slots=True)
class ScopedTaskCheckPolicy:
    requirements: RequirementsRevision
    scope: OccurrenceCompletionScopeV1
    criteria: tuple[Criterion, ...]


def read_task_check_policy_projection(
    store: Store, mission_id: str, task_id: str,
) -> ScopedTaskCheckPolicy:
    """Original scoped criteria before review, with declared checks, never fake results.

    Assurance approval cannot depend on a prior Critic PASS. The Task's declared
    verification requirements are retained for local derived criteria, including
    unavailable checkers; absence of recorded successful layers never drops them.
    """
    from .accepted_outputs import carried_criteria_for

    with store.read_view():
        prompt_scope = task_content_prompt_scope(store, mission_id, task_id)
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        task = store.get_task(task_id)
        binding = htn.task_semantics_of(mission_id, task_id)
        if task is None or task.mission_id != mission_id or active is None or binding is None:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "Task unavailable")
        members = [m for m in htn.list_plan_memberships(mission_id, active.revision)
                   if str(m.task_id) == task_id]
        if len(members) != 1:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "ambiguous occurrence")
        scope = OperationCompletionReader(store).read_scope(mission_id,
            PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash),
            str(members[0].occurrence_id))
        if scope.content_hash() != prompt_scope["scope_hash"]:
            raise OperationCompletionError("OP_EFFECT_SCOPE_STALE", "scope changed")
        carried = carried_criteria_for(htn, mission_id, active.revision, members[0].occurrence_id)
        local = _scoped_local_criteria(binding, scope, (), carried)
        requirements = htn.get_requirements_revision(mission_id, scope.requirements_ref.revision)
        spec = OperationCompletionReader(store).read_requirements(mission_id,
            TypedRef(kind=TypedRefKind.REQUIREMENTS, **scope.requirements_ref.to_json()))
        root_catalogue = {item.criterion_id:item for item in requirements.criteria}
        criteria = []
        for key in scope.content_criterion_ids:
            if key in spec.content_criterion_ids:
                criteria.append(root_catalogue[key])
            else:
                criterion = local[key]
                criteria.append(replace(criterion, required_evidence_policy=replace(
                    criterion.required_evidence_policy,
                    required_check_ids=tuple(task.verification_policy))))
        return ScopedTaskCheckPolicy(requirements,scope,tuple(criteria))


def _scoped_local_criteria(binding, scope, layers, carried):
    from .leaf_acceptance import criteria_for, local_content_criterion, LEAF_LOCAL_CRITERION
    local = {c.criterion_id: c for c in criteria_for(binding, layers, carried=carried)}
    # The frozen compiler may give an effect-only leaf a local preparation
    # criterion. It is grounded in required output ports, never in effect success.
    if LEAF_LOCAL_CRITERION in scope.content_criterion_ids and LEAF_LOCAL_CRITERION not in local:
        from ..planning.htn.completion_scopes import _local_task_content_projection
        if _local_task_content_projection(binding) is None:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "no required preparation output")
        local[LEAF_LOCAL_CRITERION] = local_content_criterion(binding, layers)
    return local


def task_content_prompt_scope(store: Store, mission_id: str, task_id: str,
                              *, attempt_id: str | None = None) -> dict:
    """Resolve local review criteria before the Critic runs, without claiming PASS."""
    from .accepted_outputs import carried_criteria_for
    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        binding = htn.task_semantics_of(mission_id, task_id)
        if active is None or binding is None or binding.form is not TaskForm.PRIMITIVE:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "planned primitive required")
        members = [m for m in htn.list_plan_memberships(mission_id, active.revision)
                   if str(m.task_id) == task_id]
        if len(members) != 1:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "ambiguous occurrence")
        scope = OperationCompletionReader(store).read_scope(mission_id,
            PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash),
            str(members[0].occurrence_id))
        if attempt_id is not None:
            from .completion_inputs import load_completion_result_inputs
            result = store.find_result_for_attempt(attempt_id)
            frozen = None if result is None else load_completion_result_inputs(store, result)
            if frozen is None or frozen.scope != scope or result.envelope.task_id != task_id:
                raise OperationCompletionError("OP_CONTENT_REVIEW_UNAVAILABLE", "original review inputs differ")
        carried = carried_criteria_for(htn, mission_id, active.revision, members[0].occurrence_id)
        local = _scoped_local_criteria(binding, scope, (), carried)
        requirements = htn.get_requirements_revision(mission_id, scope.requirements_ref.revision)
        catalogue = {c.criterion_id: c for c in requirements.criteria}
        allowed = tuple(scope.content_criterion_ids)
        if not allowed or set(allowed) - set(local):
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "local criteria differ")
        return {"purpose": "TASK_CONTENT", "scope_id": scope.scope_id,
                "scope_hash": scope.content_hash(), "task_ref": scope.task_ref.to_json(),
                "plan_revision": active.revision, "requirements_ref": scope.requirements_ref.to_json(),
                "criteria": [{"criterion_id": key, "statement": catalogue.get(key, local[key]).statement}
                             for key in allowed],
                "pending_effect_keys": list(scope.required_effect_keys)}


def read_task_content_projection(
    store: Store,
    mission_id: str,
    task_id: str,
    result_id: str,
) -> ScopedTaskContent:
    # Local import avoids a module cycle; the established leaf policy is shared,
    # never redefined from a model reply or a caller-supplied criterion catalogue.
    from .accepted_outputs import carried_criteria_for
    from .leaf_acceptance import layer_outcomes

    with store.read_view():
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        binding = htn.task_semantics_of(mission_id, task_id)
        if active is None or binding is None or binding.form is not TaskForm.PRIMITIVE:
            raise OperationCompletionError(
                "OP_COMPLETION_SCOPE_UNRESOLVED", "a planned primitive Task is required"
            )
        members = [
            m
            for m in htn.list_plan_memberships(mission_id, active.revision)
            if str(m.task_id) == task_id
        ]
        if len(members) != 1:
            raise OperationCompletionError(
                "OP_COMPLETION_SCOPE_UNRESOLVED", "Task occurrence is missing or ambiguous"
            )
        reader = OperationCompletionReader(store)
        scope = reader.read_scope(
            mission_id,
            PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash),
            str(members[0].occurrence_id),
        )
        spec = reader.read_requirements(
            mission_id, TypedRef(kind=TypedRefKind.REQUIREMENTS, **scope.requirements_ref.to_json())
        )
        requirements = htn.get_requirements_revision(mission_id, scope.requirements_ref.revision)
        result = store.get_result(result_id)
        if (
            result is None
            or result.envelope.mission_id != mission_id
            or result.envelope.task_id != task_id
            or result.verification_state != "DONE"
            or result.verdict != "PASS"
        ):
            raise OperationCompletionError("OP_CONTENT_REVIEW_UNAVAILABLE", "no verified result")
        attempt = store.get_attempt(result.envelope.attempt_id)
        if attempt is None or attempt.task_id != task_id or not attempt.agent_id:
            raise OperationCompletionError(
                "OP_CONTENT_REVIEW_UNAVAILABLE", "result Attempt differs"
            )
        layers = layer_outcomes(store.list_verifications(result_id))
        if not layers or not any(item.layer == "critic_review" and item.passed for item in layers):
            raise OperationCompletionError(
                "OP_CONTENT_REVIEW_UNAVAILABLE", "no recorded Critic PASS"
            )
        if any(item.conclusive and not item.passed for item in layers):
            raise OperationCompletionError(
                "OP_CONTENT_REVIEW_UNAVAILABLE", "verification disagrees"
            )
        carried = carried_criteria_for(htn, mission_id, active.revision, members[0].occurrence_id)
        local = _scoped_local_criteria(binding, scope, layers, carried).values()
        # Remove effect criteria from TASK_CONTENT: a verifier of prepared bytes
        # cannot attest an external milestone. Scope compiler already proved the
        # remaining IDs from this Task / adopted Method / fixed local policy.
        allowed = set(scope.content_criterion_ids)
        projected = tuple(item for item in local if item.criterion_id in allowed)
        if not projected or {item.criterion_id for item in projected} != allowed:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "review scope differs")
        # Preserve any approved Spec-facing criterion's original evidence policy.
        root_catalogue = {item.criterion_id: item for item in requirements.criteria}
        criteria = tuple(
            root_catalogue[item.criterion_id]
            if item.criterion_id in spec.content_criterion_ids
            else item
            for item in projected
        )
        passed_checks = {item.layer for item in layers if item.passed}
        for criterion in criteria:
            missing = set(criterion.required_evidence_policy.required_check_ids) - passed_checks
            if missing:
                raise OperationCompletionError(
                    "OP_CONTENT_REVIEW_UNAVAILABLE",
                    f"required checks have no recorded PASS: {sorted(missing)}",
                )
        expression: SuccessExpression = CriterionExpr(criteria[0].criterion_id)
        if len(criteria) > 1:
            expression = AllExpr(children=tuple(CriterionExpr(c.criterion_id) for c in criteria))
        artifacts = []
        for artifact_id in result.artifacts:
            artifact = store.get_artifact(artifact_id)
            if (
                artifact is None
                or artifact.mission_id != mission_id
                or artifact.task_id != task_id
                or artifact.attempt_id != attempt.id
                or artifact.verification_status != "VERIFIED"
            ):
                raise ContractError("scoped content artifact differs from the verified result")
            artifacts.append(
                CompletionPinV1(
                    id=artifact.id, revision=artifact.version, content_hash=artifact.content_hash
                )
            )
        return ScopedTaskContent(
            requirements, spec, scope, criteria, expression, tuple(artifacts), attempt.agent_id
        )


def read_task_content_candidate(
    store: Store, mission_id: str, task_id: str, result_id: str
) -> ScopedTaskContent:
    """Freeze the original candidate before its Critic has made a judgement.

    Criteria come from the approved Scope/check-policy projection, never from
    already-passing layers. Artifact identity is checked but no VERIFIED status
    or successful check is invented. Acceptance keeps its stricter reader above.
    """
    from .completion_inputs import load_completion_result_inputs

    with store.read_view():
        projected = read_task_check_policy_projection(store, mission_id, task_id)
        result = store.get_result(result_id)
        if result is None or result.envelope.mission_id != mission_id or result.envelope.task_id != task_id:
            raise OperationCompletionError("OP_CONTENT_REVIEW_UNAVAILABLE", "candidate result unavailable")
        attempt = store.get_attempt(result.envelope.attempt_id)
        frozen = load_completion_result_inputs(store, result)
        if (attempt is None or attempt.task_id != task_id or not attempt.agent_id
            or frozen is None or frozen.scope != projected.scope):
            raise OperationCompletionError("OP_CONTENT_REVIEW_UNAVAILABLE", "candidate differs from frozen Attempt scope")
        spec = OperationCompletionReader(store).read_requirements(mission_id,
            TypedRef(kind=TypedRefKind.REQUIREMENTS, **projected.scope.requirements_ref.to_json()))
        if not projected.criteria:
            raise OperationCompletionError("OP_COMPLETION_SCOPE_UNRESOLVED", "candidate criteria unavailable")
        expression: SuccessExpression = CriterionExpr(projected.criteria[0].criterion_id)
        if len(projected.criteria) > 1:
            expression = AllExpr(tuple(CriterionExpr(item.criterion_id) for item in projected.criteria))
        artifacts = []
        for artifact_id in result.artifacts:
            artifact = store.get_artifact(artifact_id)
            if (artifact is None or artifact.mission_id != mission_id or artifact.task_id != task_id
                or artifact.attempt_id != attempt.id):
                raise OperationCompletionError("OP_CONTENT_REVIEW_UNAVAILABLE", "candidate artifact identity differs")
            artifacts.append(CompletionPinV1(id=artifact.id, revision=artifact.version,
                                             content_hash=artifact.content_hash))
        return ScopedTaskContent(projected.requirements, spec, projected.scope,
            projected.criteria, expression, tuple(artifacts), attempt.agent_id)


def validate_scoped_command(store: Store, command: object) -> ScopedTaskContent:
    """Re-read the authoritative projection inside the Acceptance transaction."""
    from ..contracts.resolution import ReviewPurpose
    from ..contracts.semantic_base import Provenance, content_hash_of
    from .accepted_outputs import output_ports_in_revision
    from .completion_inputs import load_completion_result_inputs
    from .leaf_acceptance import (
        LEAF_REVIEW_POLICY,
        accepted_outputs_for,
        layer_outcomes,
        outcomes_for,
    )
    from .resolution_commits import AcceptReviewCommand, ResolutionCommitRejected

    if (
        not isinstance(command, AcceptReviewCommand)
        or command.purpose is not ReviewPurpose.TASK_CONTENT
    ):
        raise ResolutionCommitRejected("OP_CONTENT_REVIEW_UNAVAILABLE", "TASK_CONTENT is required")
    result_id = command.source.get("result_id")
    if not isinstance(result_id, str) or not result_id:
        raise ResolutionCommitRejected("OP_CONTENT_REVIEW_UNAVAILABLE", "no result identity")
    projection = read_task_content_projection(store, command.mission_id, command.task_id, result_id)
    result = store.get_result(result_id)
    frozen = load_completion_result_inputs(store, result)
    if frozen is None or command.package.binding.input_manifest_hash != frozen.frozen.manifest_hash:
        raise ResolutionCommitRejected(
            "OP_CONTENT_REVIEW_UNAVAILABLE", "review input differs from the original Attempt"
        )
    package = command.package
    task_ref = package.binding.subject_ref
    policy = package.binding.policy_ref
    candidates = package.candidate_refs
    expected_refs = {(ref.id, ref.revision, ref.content_hash) for ref in projection.artifacts}
    actual_refs = {
        (ref.id, ref.revision, ref.content_hash)
        for ref in command.artifact_refs
        if ref.kind is TypedRefKind.ARTIFACT
    }
    if (
        command.requirements != projection.requirements
        or task_ref.kind is not TypedRefKind.TASK
        or task_ref.id != projection.scope.task_ref.id
        or task_ref.revision != projection.scope.task_ref.revision
        or task_ref.content_hash != projection.scope.task_ref.content_hash
        or command.policy_ref != LEAF_REVIEW_POLICY
        or policy is None
        or policy.kind is not TypedRefKind.SOURCE
        or policy.id != LEAF_REVIEW_POLICY
        or policy.revision != 1
        or policy.content_hash != content_hash_of(LEAF_REVIEW_POLICY)
        or len(candidates) != 1
        or candidates[0].kind is not TypedRefKind.ARTIFACT
        or candidates[0].id != result_id
        or candidates[0].revision != 1
        or candidates[0].content_hash != content_hash_of(result_id)
        or actual_refs != expected_refs
        or len(command.artifact_refs) != len(expected_refs)
        or any(ref.produced_by is not Provenance.TOOL for ref in command.artifact_refs)
        or tuple(command.independence.producer_agent_ids) != (projection.producer_agent_id,)
        or tuple(package.producer_agent_ids) != (projection.producer_agent_id,)
    ):
        raise ResolutionCommitRejected(
            "OP_CONTENT_REVIEW_UNAVAILABLE", "frozen review identity differs"
        )
    for output in command.outputs:
        artifact = store.get_artifact(output.artifact_id)
        if (
            artifact is None
            or (artifact.id, artifact.version, artifact.content_hash) not in expected_refs
            or output.content_hash != artifact.content_hash
            or output.source_revision != str(artifact.version)
            or str(output.producer_task_ref) != command.task_id
            or str(output.producer_occurrence) != projection.scope.occurrence_id
            or output.producer_result_id != result_id
            or output.acceptance_id != command.acceptance_id
            or output.source_identity.path != normalise_workspace_path(artifact.path)
        ):
            raise ResolutionCommitRejected(
                "OP_CONTENT_REVIEW_UNAVAILABLE", "output is not a reviewed result artifact"
            )
    semantics = HtnStore(store)
    ports = output_ports_in_revision(
        semantics,
        command.mission_id,
        frozen.frozen.plan_revision,
        OccurrenceId(projection.scope.occurrence_id),
        command.task_id,
    )
    expected_outputs = accepted_outputs_for(
        ports,
        occurrence=OccurrenceId(projection.scope.occurrence_id),
        task_id=TaskRef(command.task_id),
        result_id=result_id,
        acceptance_id=command.acceptance_id,
        support_revision=len(semantics.list_observations(command.mission_id)),
        artifacts=tuple(store.get_artifact(ref.id) for ref in projection.artifacts),
        namespace="workspace",
        claims=frozen.port_claims,
    )
    if command.outputs != expected_outputs:
        raise ResolutionCommitRejected(
            "OP_CONTENT_REVIEW_UNAVAILABLE", "review outputs differ from durable result claims"
        )
    from ..storage.assurance_store import AssuranceStore

    if AssuranceStore(store).lane(command.mission_id) == "ASSURANCE_1_1":
        # Assurance 1.1: the review is the official record the authenticated
        # importer stored for this package, never a projection of local layers
        # (its V1 criteria deliberately hide SEMANTIC grades). The critic layer,
        # when it was recorded, must name that same official record.
        official = semantics.official_review_record(str(command.package.package_id))
        if official is None or official.to_json() != command.record.to_json():
            raise ResolutionCommitRejected(
                "OP_CONTENT_REVIEW_UNAVAILABLE", "review is not the official Assurance record"
            )
        for row in store.list_verifications(result_id):
            if row["layer"] != "critic_review" or row["status"] not in {"PASS", "FAIL"}:
                continue
            named = dict(row.get("detail") or {}).get("official_review_record_id")
            if named is not None and str(named) != str(command.record.record_id):
                raise ResolutionCommitRejected(
                    "OP_CONTENT_REVIEW_UNAVAILABLE",
                    "recorded critic layer names a different official record",
                )
        return projection
    expected_outcomes = outcomes_for(
        projection.criteria,
        layer_outcomes(store.list_verifications(result_id)),
        result_id=result_id,
    )
    if command.record.criteria != expected_outcomes:
        raise ResolutionCommitRejected(
            "OP_CONTENT_REVIEW_UNAVAILABLE", "review differs from recorded verification"
        )
    return projection
