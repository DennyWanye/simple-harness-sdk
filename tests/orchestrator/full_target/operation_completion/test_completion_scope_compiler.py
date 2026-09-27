"""OC2: compile frozen completion scopes from adopted HTN structure.

Source: Operation Completion addendum §2.2.  These are compiler-boundary
oracles, deliberately separate from OCC-01/T0/T3: they use a real
``TaskNetworkSnapshot``, semantic Task contracts, adopted method membership,
plan receipt hash, ``ObligationCoverage`` and ``CarriedCriterion`` records.  No
test derives an owner from an action's prose, capability, or operation name.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from agent_orchestrator.contracts.htn import ObligationCoverage, SideEffectKind, TaskForm
from agent_orchestrator.contracts.operation_completion import (
    CompletionMode,
    CompletionScopeRole,
    OperationCompletionRequirementsV1,
    PlanRevisionPinV1,
)
from agent_orchestrator.orchestrator.accepted_outputs import CarriedCriterion
from agent_orchestrator.orchestrator.leaf_acceptance import LEAF_LOCAL_CRITERION
from agent_orchestrator.planning.htn.completion_scopes import (
    CompletionScopeCompilationError,
    _plan_identity,
    compile_completion_scopes,
)
from agent_orchestrator.storage.htn_store import HtnStore

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_plan_commits import ROOT_DUTY, ROOT_TASK, _world  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _committed_network(tmp_path):
    """Commit the ordinary full-target plan and retain its authoritative receipt hash."""

    world = _world(tmp_path, key="completion-scope-compiler")
    world.commit()
    # ``World`` deliberately exposes the compiled plan through its refinement
    # bundle.  There is no ``World.network()`` convenience API: retaining this
    # concrete TaskNetworkSnapshot proves the compiler sees the same structure
    # sent to the real plan-commit writer.
    plan = world.bundle.network
    stored = HtnStore(world.store).active_plan_revision(world.mission.id)
    assert stored is not None
    return (
        world,
        plan,
        PlanRevisionPinV1(
            revision=stored.revision,
            snapshot_hash=stored.snapshot_hash,
        ),
    )


def _single_primitive_root(plan):
    """Derive a valid one-node network from the committed primitive contract.

    This is intentionally a typed ``TaskNetworkSnapshot`` derived from the
    full-target compiler's actual primitive occurrence and semantic binding;
    it is not a mapping-shaped stand-in for the planner.  A one-node plan has
    no adopted Method, which is the contract's primitive-MIXED-root boundary.
    """

    primitive = next(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    binding = next(
        binding for binding in plan.task_bindings if binding.task_id == primitive.task_id
    )
    one_node = dataclasses.replace(
        plan,
        occurrences=(primitive,),
        task_bindings=(binding,),
        method_instances=(),
        adopted_instance_ids=(),
        root_occurrence_ids=(primitive.occurrence_id,),
        order_constraints=(),
        data_requirements=(),
        typed_edges=(),
        obligation_coverage=(),
        required_obligations=(primitive.obligation_id,),
    )
    return (
        primitive,
        one_node,
        PlanRevisionPinV1(
            revision=one_node.plan_revision,
            snapshot_hash=_plan_identity(one_node),  # exact compiler structural pin
        ),
    )


def _spec(mission_id: str) -> OperationCompletionRequirementsV1:
    return OperationCompletionRequirementsV1.from_json(
        {
            "schema_version": 1,
            "mission_id": mission_id,
            "requirements_ref": {
                "id": "requirements-completion-scope",
                "revision": 1,
                "content_hash": HASH_A,
            },
            "mode": "REQUIRED_EFFECTS",
            "content_criterion_ids": ["criterion-report"],
            "effects": [
                {
                    "effect_key": "deliver-report",
                    "obligation_id": ROOT_DUTY,
                    "criterion_ids": ["criterion-delivery"],
                    "required_milestone": "DELIVERED",
                    "milestone_policy_ref": {
                        "id": "milestone-policy",
                        "revision": 1,
                        "content_hash": HASH_B,
                    },
                    "evidence_policy_ref": {
                        "id": "evidence-policy",
                        "revision": 1,
                        "content_hash": HASH_C,
                    },
                    "source_slot_key": "approved-delivery-slot",
                }
            ],
        }
    )


def _content_only_spec(mission_id: str) -> OperationCompletionRequirementsV1:
    """A real CONTENT_ONLY approval document for the one-root compiler case."""

    raw = _spec(mission_id).to_json()
    raw["mode"] = CompletionMode.CONTENT_ONLY
    raw["effects"] = []
    return OperationCompletionRequirementsV1.from_json(raw)


def _approved_coverage(plan) -> tuple[CarriedCriterion | ObligationCoverage, ...]:
    """Use typed coverage records against actual adopted primitive occurrences."""

    leaves = tuple(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    assert len(leaves) == 2
    coverage = ObligationCoverage(
        obligation_id=ROOT_DUTY,
        criterion_ids=("criterion-report",),
        covered_by=tuple(item.occurrence_id for item in leaves),
    )
    carried = tuple(
        CarriedCriterion(
            parent_task_id=ROOT_TASK,
            parent_criterion_id="criterion-report",
            occurrence_id=item.occurrence_id,
            task_id=str(item.task_id),
            leaf_criterion_id=f"leaf-{position}",
            evidence_requirement="approved report content",
        )
        for position, item in enumerate(leaves, start=1)
    )
    return (*carried, coverage)


def _one_linked_leaf_coverage(plan, linked) -> tuple[CarriedCriterion | ObligationCoverage, ...]:
    """Leave the other preparation leaf to its Task-local review projection."""

    return (
        CarriedCriterion(
            parent_task_id=ROOT_TASK,
            parent_criterion_id="criterion-report",
            occurrence_id=linked.occurrence_id,
            task_id=str(linked.task_id),
            leaf_criterion_id="linked-report-content",
            evidence_requirement="approved report content",
        ),
        ObligationCoverage(
            obligation_id=ROOT_DUTY,
            criterion_ids=("criterion-report",),
            covered_by=(linked.occurrence_id,),
        ),
    )


def _single_root_coverage(plan, primitive) -> tuple[CarriedCriterion | ObligationCoverage, ...]:
    """Provide the real typed coverage records for one primitive root."""

    return (
        CarriedCriterion(
            parent_task_id=str(primitive.task_id),
            parent_criterion_id="criterion-report",
            occurrence_id=primitive.occurrence_id,
            task_id=str(primitive.task_id),
            leaf_criterion_id="criterion-report",
            evidence_requirement="approved report content",
        ),
        ObligationCoverage(
            obligation_id=primitive.obligation_id,
            criterion_ids=("criterion-report",),
            covered_by=(primitive.occurrence_id,),
        ),
    )


def test_oc2_default_root_owns_effect_and_same_obligation_siblings_remain_content(tmp_path) -> None:
    """§2.2 default owner: root aggregate owns; siblings do not inherit effects."""

    world, plan, plan_ref = _committed_network(tmp_path)
    spec = _spec(world.mission.id)
    scopes = compile_completion_scopes(spec, plan, _approved_coverage(plan), plan_ref=plan_ref)

    root = str(plan.root_occurrence_ids[0])
    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    assert by_occurrence[root].role is CompletionScopeRole.AGGREGATE
    assert by_occurrence[root].required_effect_keys == ("deliver-report",)
    assert by_occurrence[root].owned_effect_keys == ("deliver-report",)
    sibling_scopes = [
        by_occurrence[str(item.occurrence_id)]
        for item in plan.occurrences
        if item.form is TaskForm.PRIMITIVE
    ]
    assert all(scope.role is CompletionScopeRole.CONTENT for scope in sibling_scopes)
    assert all(
        not scope.required_effect_keys and not scope.owned_effect_keys for scope in sibling_scopes
    )


def test_oc2_preparation_leaf_freezes_existing_local_review_scope(tmp_path) -> None:
    """A required Task output may use the fixed leaf policy without covering the root."""

    world, plan, plan_ref = _committed_network(tmp_path)
    leaves = tuple(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    local_leaf = next(
        item
        for item in leaves
        if any(
            port.required
            for binding in plan.task_bindings
            if binding.task_id == item.task_id and not binding.goal_signature.coverage_criteria
            for port in binding.output_ports
        )
    )
    linked = next(item for item in leaves if item.occurrence_id != local_leaf.occurrence_id)

    scopes = compile_completion_scopes(
        _spec(world.mission.id),
        plan,
        _one_linked_leaf_coverage(plan, linked),
        plan_ref=plan_ref,
    )

    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    local_scope = by_occurrence[str(local_leaf.occurrence_id)]
    root_scope = by_occurrence[str(plan.root_occurrence_ids[0])]
    assert local_scope.role is CompletionScopeRole.CONTENT
    assert local_scope.content_criterion_ids == (LEAF_LOCAL_CRITERION,)
    assert "criterion-report" not in local_scope.content_criterion_ids
    assert LEAF_LOCAL_CRITERION not in root_scope.content_criterion_ids
    assert "criterion-report" in root_scope.content_criterion_ids
    assert not local_scope.required_effect_keys
    assert not local_scope.owned_effect_keys


def test_oc2_local_review_scope_requires_a_declared_required_output(tmp_path) -> None:
    """The fixed policy is not authority to invent content for an output-free leaf."""

    world, plan, plan_ref = _committed_network(tmp_path)
    leaves = tuple(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    local_leaf = next(
        item
        for item in leaves
        if any(
            port.required
            for binding in plan.task_bindings
            if binding.task_id == item.task_id and not binding.goal_signature.coverage_criteria
            for port in binding.output_ports
        )
    )
    linked = next(item for item in leaves if item.occurrence_id != local_leaf.occurrence_id)
    changed_plan = dataclasses.replace(
        plan,
        task_bindings=tuple(
            dataclasses.replace(
                binding,
                output_ports=tuple(
                    dataclasses.replace(port, required=False) for port in binding.output_ports
                ),
            )
            if binding.task_id == local_leaf.task_id
            else binding
            for binding in plan.task_bindings
        ),
    )

    with pytest.raises(
        CompletionScopeCompilationError, match="no provable completion contribution"
    ):
        compile_completion_scopes(
            _spec(world.mission.id),
            changed_plan,
            _one_linked_leaf_coverage(changed_plan, linked),
            plan_ref=plan_ref,
        )


def test_oc2_single_primitive_mission_root_is_a_real_mixed_effect_owner(tmp_path) -> None:
    """§2.2 permits one adopted primitive root to carry the required effect."""

    _, plan, _ = _committed_network(tmp_path)
    primitive, single_root, plan_ref = _single_primitive_root(plan)
    scopes = compile_completion_scopes(
        _spec(str(single_root.mission_id)),
        single_root,
        _single_root_coverage(single_root, primitive),
        plan_ref=plan_ref,
    )

    assert len(scopes) == 1
    assert scopes[0].occurrence_id == str(primitive.occurrence_id)
    assert scopes[0].role is CompletionScopeRole.MIXED
    assert scopes[0].required_effect_keys == ("deliver-report",)
    assert scopes[0].owned_effect_keys == ("deliver-report",)


def test_oc2_content_only_single_real_root_is_content_not_implicit_aggregate(tmp_path) -> None:
    """§2.2 CONTENT_ONLY retains a one-root scope without inventing an effect."""

    _, plan, _ = _committed_network(tmp_path)
    primitive, single_root, plan_ref = _single_primitive_root(plan)
    scopes = compile_completion_scopes(
        _content_only_spec(str(single_root.mission_id)),
        single_root,
        _single_root_coverage(single_root, primitive),
        plan_ref=plan_ref,
    )

    assert len(scopes) == 1
    assert scopes[0].occurrence_id == str(primitive.occurrence_id)
    assert scopes[0].role is CompletionScopeRole.CONTENT
    assert scopes[0].content_criterion_ids == ("criterion-report",)
    assert not scopes[0].required_effect_keys
    assert not scopes[0].owned_effect_keys


def test_oc2_distinct_obligation_effect_is_owned_by_its_child_and_aggregated_at_root(
    tmp_path,
) -> None:
    """§2.2: effects aggregate upward but retain the obligation-specific owner."""

    world, plan, plan_ref = _committed_network(tmp_path)
    primitives = tuple(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    delegated = primitives[0]
    delegated_obligation = "obligation-delivery"
    changed_plan = dataclasses.replace(
        plan,
        occurrences=tuple(
            dataclasses.replace(item, obligation_id=delegated_obligation)
            if item.occurrence_id == delegated.occurrence_id
            else item
            for item in plan.occurrences
        ),
        task_bindings=tuple(
            dataclasses.replace(binding, obligation_id=delegated_obligation)
            if str(binding.task_id) == str(delegated.task_id)
            else binding
            for binding in plan.task_bindings
        ),
    )
    raw = _spec(world.mission.id).to_json()
    raw["effects"].append(
        {
            "effect_key": "deliver-to-recipient",
            "obligation_id": delegated_obligation,
            "criterion_ids": ["criterion-recipient-delivery"],
            "required_milestone": "DELIVERED",
            "milestone_policy_ref": {
                "id": "milestone-policy-recipient",
                "revision": 1,
                "content_hash": HASH_B,
            },
            "evidence_policy_ref": {
                "id": "evidence-policy-recipient",
                "revision": 1,
                "content_hash": HASH_C,
            },
            "source_slot_key": "approved-recipient-slot",
        }
    )
    spec = OperationCompletionRequirementsV1.from_json(raw)
    coverage = tuple(
        item for item in _approved_coverage(plan) if not isinstance(item, ObligationCoverage)
    ) + (
        ObligationCoverage(
            obligation_id=ROOT_DUTY,
            criterion_ids=("criterion-report",),
            covered_by=(primitives[1].occurrence_id,),
        ),
        ObligationCoverage(
            obligation_id=delegated_obligation,
            criterion_ids=("criterion-report",),
            covered_by=(delegated.occurrence_id,),
        ),
    )

    scopes = compile_completion_scopes(spec, changed_plan, coverage, plan_ref=plan_ref)
    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    root = by_occurrence[str(changed_plan.root_occurrence_ids[0])]
    child = by_occurrence[str(delegated.occurrence_id)]
    assert root.required_effect_keys == ("deliver-report", "deliver-to-recipient")
    assert root.owned_effect_keys == ("deliver-report",)
    assert child.role is CompletionScopeRole.MIXED
    assert child.required_effect_keys == ("deliver-to-recipient",)
    assert child.owned_effect_keys == ("deliver-to-recipient",)


@pytest.mark.parametrize("fault", ("ambiguous_root", "missing_coverage", "effect_like_child"))
def test_oc2_ambiguous_or_unproven_scope_is_rejected_without_owner_guessing(
    tmp_path, fault: str
) -> None:
    """§2.2 fail closed: method/action text never supplies missing authority."""

    world, plan, plan_ref = _committed_network(tmp_path)
    spec = _spec(world.mission.id)
    coverage = _approved_coverage(plan)
    if fault == "ambiguous_root":
        primitives = tuple(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
        broken_plan = dataclasses.replace(
            plan,
            root_occurrence_ids=(plan.root_occurrence_ids[0], primitives[0].occurrence_id),
        )
        # Root membership is deliberately not an effect-owner heuristic.  The
        # snapshot pin remains structurally valid, but two roots make ownership
        # ambiguous and must be rejected rather than selecting the first one.
        with pytest.raises(CompletionScopeCompilationError, match="one explicit Mission root"):
            compile_completion_scopes(spec, broken_plan, coverage, plan_ref=plan_ref)
        return
    if fault == "missing_coverage":
        root_occurrence = next(
            item for item in plan.occurrences if item.occurrence_id == plan.root_occurrence_ids[0]
        )
        broken_plan = dataclasses.replace(
            plan,
            task_bindings=tuple(
                dataclasses.replace(
                    binding,
                    goal_signature=dataclasses.replace(
                        binding.goal_signature,
                        coverage_criteria=("criterion-report",),
                    ),
                )
                if binding.task_id == root_occurrence.task_id
                else binding
                for binding in plan.task_bindings
            ),
        )
        with pytest.raises(CompletionScopeCompilationError, match="content criteria"):
            compile_completion_scopes(spec, broken_plan, (), plan_ref=plan_ref)
        return

    primitive = next(item for item in plan.occurrences if item.form is TaskForm.PRIMITIVE)
    changed_bindings = tuple(
        dataclasses.replace(
            binding,
            side_effect_kind=(
                SideEffectKind.EXTERNAL_EVENT_WRITE
                if str(binding.task_id) == str(primitive.task_id)
                else binding.side_effect_kind
            ),
        )
        for binding in plan.task_bindings
    )
    broken_plan = dataclasses.replace(plan, task_bindings=changed_bindings)
    with pytest.raises(CompletionScopeCompilationError, match="effect-bearing"):
        compile_completion_scopes(spec, broken_plan, coverage, plan_ref=plan_ref)


def test_oc2_plan_or_spec_task_identity_mismatch_never_reuses_a_scope(tmp_path) -> None:
    """§2.2 exact pins: stale plan/spec/task identity is not a reusable scope."""

    world, plan, plan_ref = _committed_network(tmp_path)
    spec = _spec(world.mission.id)
    wrong_ref = PlanRevisionPinV1(revision=plan_ref.revision, snapshot_hash="f" * 64)

    with pytest.raises(CompletionScopeCompilationError, match="snapshot hash"):
        compile_completion_scopes(spec, plan, _approved_coverage(plan), plan_ref=wrong_ref)

    other_spec = dataclasses.replace(spec, mission_id="other-mission")
    with pytest.raises(CompletionScopeCompilationError, match="different Missions"):
        compile_completion_scopes(other_spec, plan, _approved_coverage(plan), plan_ref=plan_ref)


@pytest.mark.parametrize("has_output", [True, False])
def test_effect_only_primitive_requires_real_preparation_output(tmp_path, has_output):
    world, plan, _ = _committed_network(tmp_path)
    primitive, single, _ = _single_primitive_root(plan)
    binding = single.task_bindings[0]
    binding = dataclasses.replace(binding,
        goal_signature=dataclasses.replace(binding.goal_signature,
            coverage_criteria=("criterion-delivery",)),
        output_ports=binding.output_ports if has_output else ())
    if has_output:
        assert any(port.required for port in binding.output_ports)
    single = dataclasses.replace(single, task_bindings=(binding,))
    spec = dataclasses.replace(_spec(world.mission.id), content_criterion_ids=())
    pin = PlanRevisionPinV1(revision=single.plan_revision, snapshot_hash=_plan_identity(single))
    if not has_output:
        with pytest.raises(CompletionScopeCompilationError, match="no reviewable preparation output"):
            compile_completion_scopes(spec, single, (), plan_ref=pin)
        return
    scope, = compile_completion_scopes(spec, single, (), plan_ref=pin)
    assert scope.role is CompletionScopeRole.MIXED
    assert scope.content_criterion_ids == (LEAF_LOCAL_CRITERION,)
    assert scope.required_effect_keys == ("deliver-report",)
    from agent_orchestrator.orchestrator.scoped_content_review import _scoped_local_criteria
    from agent_orchestrator.orchestrator.leaf_acceptance import LayerOutcome
    local = _scoped_local_criteria(binding, scope, (LayerOutcome("critic_review", "PASS"),), ())
    criterion = local[LEAF_LOCAL_CRITERION]
    assert criterion.required_evidence_policy.required_check_ids == ("critic_review",)
    assert "not for the root goal or any external effect" in criterion.statement


def test_a_linked_leaf_is_reviewed_on_its_links_not_on_every_criterion_its_type_declares(
    tmp_path,
) -> None:
    """Desktop 2026-09-27: the desktop leaf types declare every root criterion.  Each
    step of a five-file plan was reviewed for all five files, judged the other four
    UNKNOWN, and could never pass once it wrote only its own.  A leaf the Method links
    is reviewed on its link; the root keeps the root criterion.

    **Mutation**: drop the linked-leaf branch → red (each leaf scope also names
    ``criterion-report``)."""

    world, plan, plan_ref = _committed_network(tmp_path)
    spec = _content_only_spec(world.mission.id)
    leaves = {str(item.task_id) for item in plan.occurrences if item.form is TaskForm.PRIMITIVE}
    declaring = dataclasses.replace(
        plan,
        task_bindings=tuple(
            dataclasses.replace(
                binding,
                goal_signature=dataclasses.replace(
                    binding.goal_signature, coverage_criteria=("criterion-report",)),
            )
            if str(binding.task_id) in leaves
            else binding
            for binding in plan.task_bindings
        ),
    )
    scopes = compile_completion_scopes(spec, declaring, _approved_coverage(plan), plan_ref=plan_ref)
    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    leaf_scopes = [
        by_occurrence[str(item.occurrence_id)]
        for item in plan.occurrences
        if item.form is TaskForm.PRIMITIVE
    ]
    assert sorted(scope.content_criterion_ids for scope in leaf_scopes) == [("leaf-1",), ("leaf-2",)]
    root = by_occurrence[str(plan.root_occurrence_ids[0])]
    assert "criterion-report" in root.content_criterion_ids


def test_a_linked_leaf_does_not_take_an_effect_its_type_declares_for_every_step(tmp_path) -> None:
    """Desktop 2026-09-27 (NEXT-TG-1.0 2A upstream run): "写 slugify.py … 最后把 NOTES.md
    发布到授权目录".  The desktop leaf types declare every root criterion, the publish
    criterion included, so every linked step "carried an effect criterion" with no
    owner and the same deterministic REFINE was refused three times — any Mission
    with a publish requirement and a multi-step Method failed planning.  As with
    content, a linked leaf's blanket declaration is not its own: the effect stays with
    its Obligation root and the leaves stay content.

    **Mutation**: drop the linked-leaf condition on ``effect_linked`` → red
    (``OP_COMPLETION_SCOPE_UNRESOLVED … carries an effect criterion``)."""

    world, plan, plan_ref = _committed_network(tmp_path)
    spec = _spec(world.mission.id)
    leaves = {str(item.task_id) for item in plan.occurrences if item.form is TaskForm.PRIMITIVE}
    declaring = dataclasses.replace(
        plan,
        task_bindings=tuple(
            dataclasses.replace(
                binding,
                goal_signature=dataclasses.replace(
                    binding.goal_signature,
                    coverage_criteria=("criterion-report", "criterion-delivery")),
            )
            if str(binding.task_id) in leaves
            else binding
            for binding in plan.task_bindings
        ),
    )
    scopes = compile_completion_scopes(spec, declaring, _approved_coverage(plan), plan_ref=plan_ref)
    by_occurrence = {scope.occurrence_id: scope for scope in scopes}
    root = by_occurrence[str(plan.root_occurrence_ids[0])]
    assert root.role is CompletionScopeRole.AGGREGATE
    assert root.owned_effect_keys == ("deliver-report",)
    for item in plan.occurrences:
        if item.form is TaskForm.PRIMITIVE:
            leaf = by_occurrence[str(item.occurrence_id)]
            assert leaf.role is CompletionScopeRole.CONTENT
            assert not leaf.owned_effect_keys and not leaf.required_effect_keys
