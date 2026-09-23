# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure compilation of approved completion requirements into plan scopes.

``approved_criterion_coverage`` is not an approval flag.  Its entries must be the
real :class:`CarriedCriterion` values resolved from admitted Method contracts (and,
optionally, their persisted :class:`ObligationCoverage` projection).  The caller is
responsible for obtaining those values through the registry-backed plan compiler;
this function rejects mappings and booleans rather than accepting an assertion of
authority.

The current contracts contain no approved ``effect_key -> child occurrence`` field.
Consequently effects stay owned by the unique root of their Obligation.  A future
explicit owner mapping belongs in the approved Method/Task contract, not here as a
heuristic over operator names, prose, capabilities, or side-effect kinds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...contracts.htn import (
    ObligationCoverage,
    SideEffectKind,
    TaskForm,
    TaskSemanticBindingV1,
)
from ...contracts.models import ContractError
from ...contracts.operation_completion import (
    CompletionPinV1,
    CompletionScopeRole,
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
    validate_scope_owners,
    validate_spec_scope_coverage,
)
from ...contracts.semantic_base import content_hash_of
from ...graph.task_network import TaskNetworkSnapshot
from ...orchestrator.accepted_outputs import CarriedCriterion
from ...orchestrator.leaf_acceptance import LEAF_LOCAL_CRITERION, LEAF_REVIEW_POLICY


class CompletionScopeCompilationError(ContractError):
    """The adopted plan cannot prove a complete, unambiguous completion scope."""


@dataclass(frozen=True, slots=True)
class _LocalTaskContentProjection:
    """Authority for a preparation leaf's plan-time local review range.

    The projection is deliberately derived rather than supplied by the caller.  Its
    Task contract is pinned on the emitted Scope; the fixed policy later supplies
    the evidence gates when leaf acceptance reviews the produced artifacts.
    """

    criterion_id: str
    policy_ref: str
    required_output_ports: tuple[str, ...]


def _local_task_content_projection(
    binding: TaskSemanticBindingV1,
) -> _LocalTaskContentProjection | None:
    """Return the existing leaf-policy projection, or refuse to invent content.

    A required output is the Task contract's proof that the preparation leaf owes a
    concrete result.  It is not root-criterion or effect authority; the local
    criterion's fixed review policy expressly vouches only for those outputs.
    """

    required = tuple(sorted(str(port.port_key) for port in binding.output_ports if port.required))
    if not required:
        return None
    return _LocalTaskContentProjection(
        criterion_id=LEAF_LOCAL_CRITERION,
        policy_ref=LEAF_REVIEW_POLICY,
        required_output_ports=required,
    )


def _plan_identity(plan: TaskNetworkSnapshot) -> str:
    """Mirror the Plan Commit's persisted structural snapshot identity."""

    return content_hash_of(
        {
            "mission_id": str(plan.mission_id),
            "occurrences": sorted(str(item.occurrence_id) for item in plan.occurrences),
            "adopted_instances": sorted(str(item) for item in plan.adopted_instance_ids),
            "order": sorted(
                [str(item.before), str(item.after), str(item.release_condition)]
                for item in plan.order_constraints
            ),
            "data": sorted(item.requirement_id for item in plan.data_requirements),
        }
    )


def _fail(detail: str) -> CompletionScopeCompilationError:
    return CompletionScopeCompilationError(f"OP_COMPLETION_SCOPE_UNRESOLVED: {detail}")


def compile_completion_scopes(
    spec: OperationCompletionRequirementsV1,
    plan: TaskNetworkSnapshot,
    approved_criterion_coverage: Sequence[CarriedCriterion | ObligationCoverage],
    *,
    plan_ref: PlanRevisionPinV1,
) -> tuple[OccurrenceCompletionScopeV1, ...]:
    """Compile immutable per-occurrence scopes without inferring effect authority."""

    if not isinstance(spec, OperationCompletionRequirementsV1):
        raise _fail("spec must be an OperationCompletionRequirementsV1")
    if not isinstance(plan, TaskNetworkSnapshot):
        raise _fail("plan must be a TaskNetworkSnapshot")
    if not isinstance(plan_ref, PlanRevisionPinV1):
        raise _fail("plan_ref must be a PlanRevisionPinV1")
    if isinstance(approved_criterion_coverage, (str, bytes, bytearray)) or not isinstance(
        approved_criterion_coverage, Sequence
    ):
        raise _fail("approved criterion coverage must be a sequence of resolved records")
    if str(plan.mission_id) != spec.mission_id:
        raise _fail("Spec and plan belong to different Missions")
    if int(plan.plan_revision) != int(plan_ref.revision):
        raise _fail("plan_ref revision does not identify the supplied plan")
    if _plan_identity(plan) != plan_ref.snapshot_hash:
        raise _fail("plan_ref snapshot hash does not identify the supplied plan")

    occurrences = {str(item.occurrence_id): item for item in plan.occurrences}
    bindings = {str(item.task_id): item for item in plan.task_bindings}
    if len(occurrences) != len(plan.occurrences) or len(bindings) != len(plan.task_bindings):
        raise _fail("plan contains duplicate occurrence or Task identities")

    adopted = {str(item) for item in plan.adopted_instance_ids}
    children: dict[str, set[str]] = {key: set() for key in occurrences}
    parents: dict[str, set[str]] = {key: set() for key in occurrences}
    for draft in plan.method_instances:
        if str(draft.instance_id) not in adopted:
            continue
        parent = str(draft.effective_goal_occurrence_id)
        if parent not in occurrences:
            raise _fail(f"adopted Method parent {parent!r} is absent")
        for child in draft.child_bindings:
            child_id = str(child.occurrence_id)
            if child_id not in occurrences:
                raise _fail(f"adopted Method child {child_id!r} is absent")
            children[parent].add(child_id)
            parents[child_id].add(parent)

    roots = tuple(str(item) for item in plan.root_occurrence_ids)
    if len(roots) != 1:
        raise _fail("completion scopes need one explicit Mission root occurrence")

    owner_by_effect: dict[str, str] = {}
    for effect in spec.effects:
        candidates = [
            occurrence_id
            for occurrence_id, occurrence in occurrences.items()
            if str(occurrence.obligation_id) == effect.obligation_id
            and not any(
                str(occurrences[parent].obligation_id) == effect.obligation_id
                for parent in parents[occurrence_id]
            )
        ]
        if len(candidates) != 1:
            raise _fail(
                f"effect {effect.effect_key!r} has {len(candidates)} roots in "
                f"Obligation {effect.obligation_id!r}"
            )
        owner_by_effect[effect.effect_key] = candidates[0]

    paired: list[CarriedCriterion] = []
    flattened: list[ObligationCoverage] = []
    for item in approved_criterion_coverage:
        if isinstance(item, CarriedCriterion):
            paired.append(item)
        elif isinstance(item, ObligationCoverage):
            flattened.append(item)
        else:
            raise _fail("coverage entries must be real CarriedCriterion/ObligationCoverage values")

    content_keys = set(spec.content_criterion_ids)
    effect_criteria = {criterion for effect in spec.effects for criterion in effect.criterion_ids}
    spec_content: dict[str, set[str]] = {key: set() for key in occurrences}
    mapped_spec_content: set[str] = set()
    reviewed_content: dict[str, set[str]] = {key: set() for key in occurrences}
    local_review_ids: dict[str, set[str]] = {key: set() for key in occurrences}
    linked_leaves: set[str] = set()
    effect_linked: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()
    authoritative_flat = tuple(flattened) or tuple(plan.obligation_coverage)

    def ancestors_including(start: str) -> set[str]:
        found = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            for parent in parents[current]:
                if parent not in found:
                    found.add(parent)
                    pending.append(parent)
        return found

    pending: list[tuple[CarriedCriterion, str, str]] = []
    for item in paired:
        occurrence_id = str(item.occurrence_id)
        if occurrence_id not in occurrences:
            raise _fail(f"criterion coverage names unknown occurrence {occurrence_id!r}")
        if str(occurrences[occurrence_id].task_id) != str(item.task_id):
            raise _fail(f"criterion coverage Task mismatch at {occurrence_id!r}")
        criterion_id = str(item.parent_criterion_id)
        pair = (str(item.parent_task_id), criterion_id, occurrence_id)
        if pair in seen_pairs:
            raise _fail(f"criterion coverage repeats {criterion_id!r} at {occurrence_id!r}")
        seen_pairs.add(pair)
        supported = any(
            str(coverage.obligation_id) == str(occurrences[occurrence_id].obligation_id)
            and criterion_id in coverage.criterion_ids
            and occurrence_id in {str(value) for value in coverage.covered_by}
            for coverage in authoritative_flat
        )
        if not supported:
            raise _fail(f"criterion coverage for {criterion_id!r} is not in the adopted plan")
        linked_leaves.add(occurrence_id)
        pending.append((item, occurrence_id, criterion_id))

    # A criterion link may terminate on another compound and become the parent
    # criterion of that compound's adopted Method.  Follow that exact occurrence
    # chain to prove the local projection, while keeping only the Spec-rooted
    # criterion in ``spec_content``.  Task names alone are insufficient because the
    # same Task contract may occur more than once in one Plan.
    local_at: set[tuple[str, str]] = set()
    local_source: dict[tuple[str, str], tuple[str, str]] = {}
    while pending:
        deferred: list[tuple[CarriedCriterion, str, str]] = []
        progressed = False
        for item, occurrence_id, criterion_id in pending:
            local_parent: str | None = None
            if criterion_id not in content_keys and criterion_id not in effect_criteria:
                candidates = [
                    candidate
                    for candidate in ancestors_including(occurrence_id)
                    if str(occurrences[candidate].task_id) == str(item.parent_task_id)
                    and (candidate, criterion_id) in local_at
                ]
                if len(candidates) > 1:
                    raise _fail(
                        f"local criterion {criterion_id!r} has ambiguous ancestor coverage"
                    )
                if not candidates:
                    deferred.append((item, occurrence_id, criterion_id))
                    continue
                local_parent = candidates[0]
            if criterion_id in content_keys:
                parent_candidates = [
                    candidate
                    for candidate in ancestors_including(occurrence_id)
                    if str(occurrences[candidate].task_id) == str(item.parent_task_id)
                ]
                if len(parent_candidates) != 1:
                    raise _fail(
                        f"criterion coverage for {criterion_id!r} has "
                        f"{len(parent_candidates)} matching parent Task occurrences"
                    )
                spec_content[parent_candidates[0]].add(criterion_id)
                mapped_spec_content.add(criterion_id)
                source_occurrence = parent_candidates[0]
            elif criterion_id in effect_criteria:
                # A composition link proves decomposition coverage, never moves
                # effect ownership from its approved Obligation root to a leaf.
                # The leaf still needs its own concrete preparation criterion.
                progressed = True
                continue
            else:
                assert local_parent is not None
                source_occurrence = local_parent
            leaf_criterion_id = str(item.leaf_criterion_id)
            if leaf_criterion_id in content_keys and leaf_criterion_id != criterion_id:
                raise _fail(
                    f"leaf criterion {leaf_criterion_id!r} collides with another Spec criterion"
                )
            reviewed_content[occurrence_id].add(leaf_criterion_id)
            if leaf_criterion_id not in content_keys:
                target = (occurrence_id, leaf_criterion_id)
                # Multiple consumers may share one goal. They must trace this
                # local criterion to the same approved root obligation, even if
                # the immediate parent differs along the two decomposition paths.
                source = ((source_occurrence, criterion_id) if criterion_id in content_keys
                          else local_source[(source_occurrence, criterion_id)])
                previous = local_source.get(target)
                if previous is not None and previous != source:
                    raise _fail(
                        f"local criterion {leaf_criterion_id!r} at {occurrence_id!r} "
                        "has multiple parent mappings"
                    )
                local_source[target] = source
                local_review_ids[occurrence_id].add(leaf_criterion_id)
                local_at.add(target)
            progressed = True
        if not progressed:
            unresolved = sorted({criterion for _, _, criterion in deferred})
            raise _fail(
                f"criterion coverage names {unresolved}, which has no Spec-rooted ancestor link"
            )
        pending = deferred

    # Task criteria describe that Task's own review range.  In a multi-node adopted
    # Method they do not replace the Method's parent->leaf coverage mapping.  A
    # single-node plan has no Method, so its Task contract is the complete authority.
    for occurrence_id, occurrence in occurrences.items():
        binding = bindings.get(str(occurrence.task_id))
        if binding is None:
            raise _fail(f"occurrence {occurrence_id!r} has no Task semantic binding")
        declared = set(binding.goal_signature.coverage_criteria)
        declared_content = content_keys & declared
        spec_content[occurrence_id].update(declared_content)
        reviewed_content[occurrence_id].update(declared_content)
        declared_local = declared - content_keys - effect_criteria
        reviewed_content[occurrence_id].update(declared_local)
        local_review_ids[occurrence_id].update(declared_local)
        if effect_criteria & declared:
            effect_linked.add(occurrence_id)

    authoritative_spec_content = (
        set().union(*spec_content.values())
        if len(occurrences) == 1 and not adopted
        else mapped_spec_content
    )
    if content_keys and authoritative_spec_content != content_keys:
        missing = sorted(content_keys - authoritative_spec_content)
        raise _fail(f"content criteria have no approved Method coverage: {missing}")

    # The established leaf-review policy gives a preparation leaf a strictly local
    # review criterion when neither its Task contract nor its adopted Method links a
    # criterion.  Required output ports make the promised local result concrete.
    # This projection never enters ``spec_content`` and therefore cannot satisfy a
    # root requirement or authorize an effect.
    for occurrence_id, occurrence in occurrences.items():
        binding = bindings.get(str(occurrence.task_id))
        if binding is None:
            raise _fail(f"occurrence {occurrence_id!r} has no Task semantic binding")
        if (
            binding.form is TaskForm.PRIMITIVE
            and not reviewed_content[occurrence_id]
        ):
            projection = _local_task_content_projection(binding)
            if projection is not None:
                reviewed_content[occurrence_id].add(projection.criterion_id)
                local_review_ids[occurrence_id].add(projection.criterion_id)

    def descendants(start: str, visiting: frozenset[str] = frozenset()) -> set[str]:
        if start in visiting:
            raise _fail("adopted Method occurrences contain a refinement cycle")
        found = {start}
        for child in children[start]:
            found.update(descendants(child, visiting | {start}))
        return found

    scopes: list[OccurrenceCompletionScopeV1] = []
    for occurrence_id, occurrence in sorted(occurrences.items()):
        binding = bindings.get(str(occurrence.task_id))
        if binding is None:
            raise _fail(f"occurrence {occurrence_id!r} has no Task semantic binding")
        owned = sorted(key for key, owner in owner_by_effect.items() if owner == occurrence_id)
        below = descendants(occurrence_id)
        required = sorted(key for key, owner in owner_by_effect.items() if owner in below)
        # Spec-facing criteria aggregate through the hierarchy.  Local Task review
        # criteria stay on the exact leaf whose outputs the policy reviews; making a
        # compound inherit them would force root review to re-review preparation and
        # could be mistaken for root requirement coverage downstream.
        content = set().union(*(spec_content[item] for item in below))
        content.update(reviewed_content[occurrence_id])

        if occurrence_id in effect_linked and not owned:
            raise _fail(
                f"occurrence {occurrence_id!r} carries an effect criterion but has no "
                "approved effect owner"
            )
        if (
            binding.form is TaskForm.PRIMITIVE
            and not owned
            and binding.side_effect_kind
            in (
                SideEffectKind.EXTERNAL_STATE_WRITE,
                SideEffectKind.EXTERNAL_EVENT_WRITE,
            )
        ):
            raise _fail(
                f"primitive {occurrence_id!r} looks effect-bearing but has no approved effect owner"
            )
        if required:
            if binding.form is TaskForm.PRIMITIVE and not content:
                raise _fail(
                    f"effect owner {occurrence_id!r} has no reviewable preparation output"
                )
            role = (
                CompletionScopeRole.AGGREGATE
                if binding.form is TaskForm.COMPOUND
                else CompletionScopeRole.MIXED
            )
        else:
            if not content:
                raise _fail(f"occurrence {occurrence_id!r} has no provable completion contribution")
            role = CompletionScopeRole.CONTENT
        scopes.append(
            OccurrenceCompletionScopeV1(
                schema_version=1,
                mission_id=spec.mission_id,
                requirements_ref=spec.requirements_ref,
                spec_hash=spec.content_hash(),
                plan_ref=plan_ref,
                occurrence_id=occurrence_id,
                task_ref=CompletionPinV1(
                    id=str(binding.task_id),
                    revision=int(binding.contract_revision),
                    content_hash=binding.contract_hash,
                ),
                obligation_id=str(binding.obligation_id),
                role=role,
                content_criterion_ids=tuple(sorted(content)),
                required_effect_keys=tuple(required),
                owned_effect_keys=tuple(owned),
            )
        )

    compiled = tuple(scopes)
    local_content_by_scope = {
        occurrence_id: tuple(sorted(local_review_ids[occurrence_id]))
        for occurrence_id in occurrences
    }
    validate_spec_scope_coverage(
        spec,
        compiled,
        root_occurrence_id=roots[0],
        local_content_criterion_ids_by_occurrence=local_content_by_scope,
    )
    validate_scope_owners(spec, compiled)
    return compiled


__all__ = ("CompletionScopeCompilationError", "compile_completion_scopes")
