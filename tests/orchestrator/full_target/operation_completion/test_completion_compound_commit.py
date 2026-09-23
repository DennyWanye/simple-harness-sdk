"""OC2 red oracle: nested local composition criteria must survive scope compilation.

The real two-level HTN fixture has one approved root requirement (``c-root``),
an intermediate compound-local criterion (``c-sub``), and a leaf-local review
criterion (``c-leaf-verified``).  The latter two are composition identities,
not root requirements.  This test intentionally preserves that distinction.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_htn_end_to_end import HIERARCHICAL_SEMANTICS, ROOT_DUTY, ROOT_TASK, build_world
from test_nested_compound_composition import _inner, _outer, _task_of
from test_nested_compound_refinement import _proposal

from agent_orchestrator.api.operation_completion import OperationCompletionApi
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.htn import TaskForm
from agent_orchestrator.contracts.operation_completion import PlanRevisionPinV1
from agent_orchestrator.contracts.resolution import (
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    EvaluationKind,
    RequirementClass,
    RequirementsRevision,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.completion_status import read_occurrence_completion
from agent_orchestrator.orchestrator.composition_review import CompositionAcceptanceAssembly
from agent_orchestrator.orchestrator.operation_completion import OperationCompletionReader
from agent_orchestrator.orchestrator.scoped_composition_review import read_compound_projection
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore


def _approved_root_only_spec(world, *, package_version=6) -> RequirementsRevision:
    """Bind the real protocol and approve only the actual root requirement."""

    if package_version == 7:
        from agent_orchestrator.orchestrator.planning_protocol_binding import bind_planning_protocol
        bind_planning_protocol(world.store, world.mission.id, "planning-decision-v1")
    else:
        PlanningDecisionStore(world.store).bind_mission_protocol(
            world.mission.id,
            protocol_version="planning-decision-v1",
            package_version=6,
            prompt_version="planner-hierarchical-v9",
            binding_hash="a" * 64,
        )
    requirements = RequirementsRevision(
        revision_id="nested-root-requirements-1",  # type: ignore[arg-type]
        mission_id=world.mission.id,
        revision=1,
        criteria=(
            Criterion(
                criterion_id="c-root",
                revision=1,
                origin=CriterionOrigin.USER_EXPLICIT,
                statement="the mission root is satisfied",
                requirement_class=RequirementClass.REQUIRED_OUTCOME,
                evaluation_kind=EvaluationKind.SEMANTIC,
            ),
        ),
        success_expression=CriterionExpr("c-root"),
        authority_subject="authenticated-user-confirmation",
    )
    HtnStore(world.store).insert_requirements_revision(requirements)
    OperationCompletionApi(
        world.service,
        tenant_id=world.mission.tenant_id,
        principal=Principal("human-completion-fixture"),
    ).approve(
        {
            "mission_id": world.mission.id,
            "command_id": "approve-nested-root-only",
            "expected_requirements_ref": {
                "kind": "requirements",
                "id": str(requirements.revision_id),
                "revision": 1,
                "content_hash": requirements.content_hash(),
            },
            "proposal": {
                "schema_version": 1,
                "mission_id": world.mission.id,
                "requirements_ref": {
                    "id": str(requirements.revision_id),
                    "revision": 1,
                    "content_hash": requirements.content_hash(),
                },
                "mode": "CONTENT_ONLY",
                "content_criterion_ids": ["c-root"],
                "effects": [],
            },
        }
    )
    return requirements


def _accept_leaf_through_core(world, task, *, tmp_path: Path):
    """Produce a real Result and let CommitService create the scoped Acceptance."""

    attempt, intent = world.service.create_attempt(
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="oc2-nested-leaf-v1",
        reservation=Reservation(tokens=1_000, cost_micros=0),
        intent_config={"message": "produce the nested leaf result"},
        input_hash="e" * 64,
        inputs=(),
    )
    world.service.claim_intent(intent.intent_id, owner="oc2-nested-owner", lease_seconds=60)
    world.service.record_agent_created(
        intent.intent_id, agent_id="fixture-worker", expected_turn_id="turn-nested-leaf"
    )
    world.service.record_submitted(
        intent.intent_id, receipt={"turn_id": "turn-nested-leaf", "seq": 1}
    )
    body = b'{"nested_leaf":"verified"}\n'
    artifact_path = tmp_path / "nested-leaf-result.json"
    artifact_path.write_bytes(body)
    cas = ArtifactStore(tmp_path / "nested-leaf-cas")
    artifact_digest = cas.put_bytes(body)
    artifact = Artifact(
        id="artifact-nested-" + hashlib.sha256(body).hexdigest()[:24],
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=str(artifact_path),
        version=1,
        content_hash=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        produced_by="fixture-worker",
        storage_uri=str(cas.path_for(artifact_digest)),
    )
    result = ResultEnvelope(
        id="result-nested-leaf",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="The nested leaf produced its required result.",
        claims=(ClaimProposal(content="nested leaf verified", confidence=0.9),),
        evidence=(str(artifact_path),),
        artifacts=(str(artifact_path),),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    stored = world.service.record_result(
        attempt.id,
        envelope=result,
        turn_id="turn-nested-leaf",
        artifacts=(artifact,),
        usage_refs=(),
        port_claims=(PortClaim(port_key="result", path=artifact.path),),
    )
    world.service.start_verification(stored.envelope.id)
    for layer in ("format_check", "rule_check", "code_test", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id,
            layer=layer,
            status="PASS",
            detail={"producer": "CommitService", "layer": layer},
        )
    accepted = world.service.accept_result(stored.envelope.id, verifier_results=())
    return accepted, attempt, stored


def completed_nested_world(tmp_path, *, package_version=6, with_reuse_consumer=False):
    """OC2: the second refinement retains local `c-sub` authority.

    The first plan is legal: the root's ``c-root`` is carried to the inner
    compound, where the Method declares local ``c-sub`` review.  The second
    refinement is also semantically legal: it maps only that local ``c-sub`` to
    the leaf's ``c-leaf-verified``.  The compiler must commit revision 2 without
    adding ``c-sub`` to the approved root Spec.
    """

    world = build_world(tmp_path, key="oc2-nested-local-chain", mode=HIERARCHICAL_SEMANTICS)
    requirements = _approved_root_only_spec(world, package_version=package_version)
    env = world.env
    env.register_type(
        "plan.subgoal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-sub",),
        domain="plan",
    )
    env.register_type(
        "plan.act",
        parameters=(("subject", "string"),),
        outputs=(("verdict", "plan.verdict"),),
        capabilities=("plan.read",),
        criteria=("c-done",),
        domain="plan",
    )
    outer, inner = _outer(), _inner()
    if with_reuse_consumer:
        from dataclasses import replace
        from htn_world import param, step
        from agent_orchestrator.contracts.htn import CriterionLink
        from agent_orchestrator.contracts.htn import ReusePolicy
        from agent_orchestrator.planning.htn.registry import TaskTypeCatalog
        env.register_type("plan.consumer", form=TaskForm.COMPOUND,
            parameters=(("subject", "string"),), criteria=("c-sub",), domain="plan")
        catalog = TaskTypeCatalog()
        for item in env.catalog.task_types():
            catalog.register(replace(item, reuse_policy=ReusePolicy.REUSE_ACCEPTED)
                if item.goal_signature.signature_id == "plan.subgoal" else item)
        env.catalog = catalog
        outer = replace(outer, steps=(*outer.steps,
            step("consumer", "plan.consumer", TaskForm.COMPOUND, {"subject": param("subject")})),
            composition=replace(outer.composition, criterion_links=(*outer.composition.criterion_links,
                CriterionLink("c-root", "consumer", "c-sub", "consumer contributes the same root criterion"))))
    for contract in (outer, inner):
        receipt = env.admit(contract)
        assert receipt.admitted, receipt.problems
        HtnStore(world.store).register_method(
            contract, env.registry.registration(contract.method_ref())
        )

    first = world.dispatch.apply_planner_reply(
        world.mission.id,
        _proposal(
            outer,
            goal_id=ROOT_TASK,
            obligation_id=ROOT_DUTY,
            revision=0,
            proposal_id="oc2-outer",
        ),
        principal=world.principal,
        command_id="oc2-commit-outer",
    )
    assert first.committed, first.last_reason
    assert HtnStore(world.store).active_plan_revision(world.mission.id).revision == 1

    world.dispatch.advance_compound_phases(world.mission.id)
    inner_task = _task_of(world, "plan.subgoal")
    first_network = world.network()
    inner_spec = next(item for item in first_network.occurrences if str(item.task_id) == inner_task)
    # This is deliberately *not* a Spec amendment.  `c-sub` remains the actual
    # intermediate local identity and `c-leaf-verified` stays leaf-local.
    second = world.dispatch.apply_planner_reply(
        world.mission.id,
        _proposal(
            inner,
            goal_id=inner_task,
            obligation_id=str(inner_spec.obligation_id),
            revision=1,
            proposal_id="oc2-inner",
        ),
        principal=world.principal,
        command_id="oc2-commit-inner",
    )
    assert second.committed, second.last_reason
    active = HtnStore(world.store).active_plan_revision(world.mission.id)
    assert active is not None and active.revision == 2
    network = world.network()
    assert network.plan_revision == 2
    root_occurrence = network.root_occurrence_ids[0]
    inner_occurrence = next(
        item.occurrence_id for item in network.occurrences if str(item.task_id) == inner_task
    )
    leaf_occurrence = next(
        item.occurrence_id
        for item in network.occurrences
        if str(network.binding_for_occurrence(item.occurrence_id).goal_signature.signature_id)
        == "plan.leaf"
    )
    reader = OperationCompletionReader(world.store)
    plan_ref = PlanRevisionPinV1(revision=active.revision, snapshot_hash=active.snapshot_hash)
    root_scope = reader.read_scope(world.mission.id, plan_ref, str(root_occurrence))
    inner_scope = reader.read_scope(world.mission.id, plan_ref, str(inner_occurrence))
    leaf_scope = reader.read_scope(world.mission.id, plan_ref, str(leaf_occurrence))
    assert root_scope.content_criterion_ids == ("c-root",)
    assert inner_scope.content_criterion_ids == ("c-sub",)
    assert leaf_scope.content_criterion_ids == ("c-leaf-verified",)
    projection = read_compound_projection(
        world.store, world.mission.id, str(inner_occurrence), inner_task
    )
    assert tuple(item.criterion_id for item in projection.criteria) == ("c-sub",)
    # Planning scopes are not a composition verdict: no leaf result exists yet,
    # so neither local `c-sub` nor the root formula may have a GoalResolution.
    assert HtnStore(world.store).list_goal_resolutions(world.mission.id) == ()
    assert HtnStore(world.store).latest_requirements_revision(world.mission.id) == requirements

    world.admit_demand()
    world.dispatch.issue_input_witnesses(world.mission.id, network, now_ms=1_000_000)
    world.dispatch.issue_start_witnesses(world.mission.id, now_ms=1_000_000)
    leaf_task = world.store.get_task(str(network.occurrence(leaf_occurrence).task_id))
    assert leaf_task is not None
    accepted_leaf, attempt, stored = _accept_leaf_through_core(world, leaf_task, tmp_path=tmp_path)
    assert accepted_leaf.status.name == "COMPLETED"
    assert world.store.get_attempt(attempt.id).status.name == "COMPLETED"
    assert world.store.get_result(stored.envelope.id).verification_state == "DONE"

    before_composition_requirements = HtnStore(world.store).latest_requirements_revision(
        world.mission.id
    )
    formed = CompositionAcceptanceAssembly(
        world.store, world.service, dispatch=world.dispatch
    ).resolve_ready(world.mission.id)
    assert len(formed) == 1
    resolution = formed[0].resolution
    assert str(resolution.goal_task_id) == inner_task
    assert {item.criterion_id for item in resolution.criteria} == {"c-sub"}
    assert HtnStore(world.store).latest_requirements_revision(world.mission.id) == (
        before_composition_requirements
    )

    inner_current = read_occurrence_completion(world.store, world.mission.id, str(inner_occurrence))
    root_current = read_occurrence_completion(world.store, world.mission.id, str(root_occurrence))
    assert inner_current.content_ready and inner_current.complete
    assert not root_current.content_ready and not root_current.complete
    resolutions = HtnStore(world.store).list_goal_resolutions(world.mission.id)
    assert {str(item.goal_task_id) for item in resolutions} == {inner_task}

    return world, inner_occurrence, leaf_occurrence, resolution


def test_oc2_nested_local_chain_is_not_rewritten_as_root_requirement(tmp_path) -> None:
    completed_nested_world(tmp_path)
