"""One real T0 -> T1 -> file publish -> T3 completion chain."""

from __future__ import annotations

import pytest

import asyncio
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import (  # noqa: E402
    _api,
    _approval_world,
    _command,
)

from agent_orchestrator.api.approvals import ApprovalApi
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope, TaskStatus
from agent_orchestrator.contracts.operation_intents import SubmitOperationIntentV2
from agent_orchestrator.contracts.semantic_base import (
    Provenance,
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import Reservation, mission_account
from agent_orchestrator.orchestrator.operation_materialization import (
    OperationMaterializationRuntime,
)
from agent_orchestrator.orchestrator.operation_outcomes import (
    accept_operation_outcome,
    persist_operation_outcome_review,
    prepare_operation_outcome_review,
    record_operation_outcome_review,
)
from agent_orchestrator.orchestrator.operation_proposal_review import (
    ActionProposalReviewCoordinator,
)
from agent_orchestrator.runtime.actions import ActionExecutor
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.operation_profiles import BuiltinOperationProfiles
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.operation_intent_store import OperationIntentStore


def _passing_critic(criteria) -> str:
    return "<critic_verdict>" + json.dumps(
        {
            "verdict": "PASS",
            "findings": [],
            "mission_criteria": [
                {"criterion": criterion, "met": True, "reason": "frozen evidence matches"}
                for criterion in criteria
            ],
        }
    ) + "</critic_verdict>"


def _submit_review_intent(world, *, subject, role, package_id, extra):
    intent = world.service.create_service_intent(
        kind="plan",
        subject_id=subject,
        mission_id=world.mission.id,
        account_id=mission_account(world.mission.id),
        creation_key=subject,
        input_id="attempt-input",
        input_hash=hashlib.sha256(subject.encode()).hexdigest(),
        config={"role": role, "review_package_id": package_id, **extra},
        reservation=Reservation(tokens=0, cost_micros=0),
    )
    world.service.claim_intent(intent.intent_id, owner="operation-test-runtime", lease_seconds=60)
    agent_id = role + "-agent"
    turn_id = role + "-turn"
    world.service.record_agent_created(
        intent.intent_id, agent_id=agent_id, expected_turn_id=turn_id
    )
    world.service.record_submitted(intent.intent_id, receipt={"turn_id": turn_id, "seq": 1})
    return world.store.get_intent(intent.intent_id), agent_id, turn_id


def _exhaust_by_interruption(world, binding_id: str) -> str:
    """The outcome review's two calls ran out and the second was interrupted by a restart."""
    from agent_orchestrator.orchestrator.failure_classes import record_review_interruption
    from agent_orchestrator.orchestrator.operation_outcomes import outcome_review_key

    key = outcome_review_key(world.mission.id, binding_id)
    world.store.insert_receipt(
        commit_id="assurance-review-format-exhausted:" + key, kind="AssuranceReviewFormatExhausted",
        subject_id=key, base_version=0, proposal_hash="0" * 64,
        receipt={"mission_id": world.mission.id, "review_key": key, "classification_ref": {},
                 "reason": "REVIEW_TURN_RETRY_EXHAUSTED"})
    record_review_interruption(world.store, mission_id=world.mission.id, review_key=key, ordinal=2,
                               intent_id="intent-" + binding_id, error_code="base_agent_driver_exception")
    return key


@pytest.mark.parametrize("interrupted_first", [False, True])
def test_operation_t0_t3_runs_real_file_publish_and_commits_formal_completion(
    tmp_path, interrupted_first: bool
) -> None:
    """``interrupted_first``（2026-09-29 真机第七局一类）：这份发布的结果审阅两次调用用完、
    第 2 次被重启打断——不是审阅员的结论。重审一次：新审阅包、新审阅编号，同一份回执；
    验收认重审版的清单；重审也用完才算到头（最多多给一次机会）。

    **Mutation**: drop the retake manifest field, or accept only the original manifest at
    acceptance, or allow a second retake → red."""
    (tmp_path / "published").mkdir()
    publish = FilePublishConnector(tmp_path / "published", tmp_path / "publish-ledger")
    connectors = {"file_publish": publish}
    deployment = DeploymentPolicy(enabled_connectors=("file_publish",))
    profiles = BuiltinOperationProfiles(connectors)

    world, requirements = _approval_world(tmp_path)
    # This is the user-authored Mission charter in the fixture.  The real action gate
    # below re-reads it; the action candidate cannot enlarge this target scope.
    original_mission = world.mission
    world.mission = dataclasses.replace(
        original_mission,
        success_criteria=(
            *original_mission.success_criteria,
            "action:file_publish.publish:reports/final.json",
        ),
        version=original_mission.version + 1,
    )
    world.store.update_mission(world.mission, expected_version=original_mission.version)
    world.service.begin_planning(world.mission.id)
    completion = _command(requirements, milestone="CONTENT_HASH_VERIFIED")
    completion["proposal"]["effects"][0]["milestone_policy_ref"] = (
        profiles.milestone_policy_ref.to_json()
    )
    completion["proposal"]["effects"][0]["evidence_policy_ref"] = (
        profiles.evidence_policy_ref.to_json()
    )
    approved = _api(world).approve(completion)
    plan, _ = _admitted_single_root(world, requirements, outputs=(("report", "report.schema"),))
    occurrence = str(plan.network.root_occurrence_ids[0])
    task_id = world.store.connection.execute(
        "SELECT task_id FROM plan_memberships WHERE mission_id=? AND revision=1 AND occurrence_id=?",
        (world.mission.id, occurrence),
    ).fetchone()["task_id"]
    task = world.store.get_task(task_id)
    assert task is not None

    attempt, worker_intent = world.service.create_attempt(
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="operation-runtime-v1",
        reservation=Reservation(tokens=1_000, cost_micros=0),
        intent_config={"message": "prepare the approved publish action"},
        input_hash="a" * 64,
    )
    world.service.claim_intent(worker_intent.intent_id, owner="fixture", lease_seconds=60)
    world.service.record_agent_created(
        worker_intent.intent_id, agent_id="fixture-worker", expected_turn_id="worker-turn"
    )
    world.service.record_submitted(
        worker_intent.intent_id, receipt={"turn_id": "worker-turn", "seq": 1}
    )
    candidate = {
        "connector": "file_publish",
        "operation": "publish",
        "target": "reports/final.json",
        "params": {"artifact_path": "report.json"},
        "reason": "publish the reviewed result",
    }
    body = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    cas = world.service._source_artifact_store
    assert isinstance(cas, ArtifactStore)
    digest = cas.put_bytes(body)
    artifact = Artifact(
        id="artifact-operation-candidate",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path="report.json",
        version=1,
        content_hash=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        produced_by="fixture-worker",
        storage_uri=str(cas.path_for(digest)),
    )
    result = ResultEnvelope(
        id="result-operation-candidate",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="Prepared a scoped publish action.",
        claims=(ClaimProposal(content="publish candidate prepared", confidence=0.9),),
        evidence=(),
        artifacts=(artifact.path,),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    stored = world.service.record_result(
        attempt.id,
        envelope=result,
        turn_id="worker-turn",
        artifacts=(artifact,),
        usage_refs=(),
        port_claims=(PortClaim(port_key="report", path=artifact.path),),
    )
    world.service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id, layer=layer, status="PASS", detail={"producer": "CommitService"}
        )
    world.service.accept_result(stored.envelope.id, verifier_results=())
    acceptance = HtnStore(world.store).list_acceptances(world.mission.id)[0]
    accepted_outputs = HtnStore(world.store).list_acceptance_outputs(world.mission.id)
    assert len(accepted_outputs) == 1

    captured = {}

    def prepare_proposal_review(sources, payloads, package_id):
        coordinator = ActionProposalReviewCoordinator(
            world.store,
            connectors,
            deployment,
            profiles,
            profiles.policy_for(sources),
        )
        captured["coordinator"] = coordinator
        captured["proposal"] = coordinator.prepare_review(
            sources, payloads, package_id=package_id
        )
        return captured["proposal"]

    runtime = OperationMaterializationRuntime(
        connectors=connectors,
        deployment=deployment,
        profiles=profiles,
        policy_for=profiles.policy_for,
        prepare_review=prepare_proposal_review,
        service_authority=object(),
    )
    world.service.bind_operation_materialization_runtime(runtime)
    submit = SubmitOperationIntentV2.from_json(
        {
            "schema_version": 2,
            "mission_id": world.mission.id,
            "idempotency_key": "publish-final-report",
            "intent_source": {"kind": "USER_COMMAND"},
            "candidate_artifact_ref": TypedRef(
                TypedRefKind.ARTIFACT,
                artifact.id,
                artifact.version,
                artifact.content_hash,
                Provenance.TOOL,
            ).to_json(),
            "prepared_acceptance_refs": [
                TypedRef(
                    TypedRefKind.ACCEPTANCE,
                    str(acceptance.acceptance_id),
                    1,
                    content_hash_of(acceptance.to_json()),
                    Provenance.TOOL,
                ).to_json()
            ],
            "supersedes_intent_id": None,
            "completion_slot": {
                "spec_hash": approved.spec_hash,
                "effect_key": "deliver-report",
            },
        }
    )
    submitted = world.service.submit_operation_intent(
        submit, tenant_id=world.mission.tenant_id, principal=Principal("operation-owner")
    )
    proposal = captured["proposal"]
    proposal_dispatch, reviewer, proposal_turn = _submit_review_intent(
        world,
        subject="operation-review:" + submitted["intent_id"],
        role="operation_proposal_reviewer",
        package_id=str(proposal.package.package_id),
        extra={"operation_intent_id": submitted["intent_id"]},
    )
    with world.store.transaction():
        proposal_review = captured["coordinator"].record_critic_verdict(
            proposal,
            record_id="operation-review-record:" + submitted["intent_id"],
            dispatch_intent_id=proposal_dispatch.intent_id,
            reviewer_agent_id=reviewer,
            reviewer_turn_id=proposal_turn,
            raw_critic_text=_passing_critic(c.criterion_id for c in proposal.package.criteria),
        )
    materialized = world.service.materialize_reviewed_operation(
        intent_id=submitted["intent_id"],
        command_id="materialize:" + submitted["intent_id"],
        official_review_ref=TypedRef(
            TypedRefKind.REVIEW,
            str(proposal_review.record.record_id),
            1,
            content_hash_of(proposal_review.record.to_json()),
        ),
        service_authority=runtime.service_authority,
    )
    action = world.store.get_action(materialized["action_key"])
    assert action is not None
    from types import SimpleNamespace
    from agent_orchestrator.orchestrator.operation_runtime import dispatch_materialized_operations
    loop = SimpleNamespace(store=world.store, actions=ActionExecutor(
        world.service, connectors, deployment, owner="operation-runtime",
        source_storage_roots=(tmp_path / "artifacts",)))
    assert not asyncio.run(dispatch_materialized_operations(loop, world.mission.id))
    assert not tuple(publish.root.glob("*.md"))
    ApprovalApi(
        world.service, Principal("operation-approver"), deployment=deployment
    ).approve(action["approval_request_id"], nonce="approve-real-publish")
    assert asyncio.run(dispatch_materialized_operations(loop, world.mission.id))
    executed = world.store.get_action(action["action_key"])
    assert not asyncio.run(dispatch_materialized_operations(loop, world.mission.id))
    assert executed is not None and executed["state"] == "SUCCEEDED"
    published_path = Path(executed["receipt"]["after"]["path"])
    assert published_path.is_relative_to(publish.root)
    assert published_path.read_bytes() == body

    prepared = prepare_operation_outcome_review(
        world.store, intent_id=submitted["intent_id"], connectors=connectors, profiles=profiles
    )
    with world.store.transaction():
        persist_operation_outcome_review(world.service, prepared, runtime=runtime)
    if interrupted_first:
        from agent_orchestrator.orchestrator.operation_outcomes import (
            outcome_exhaustion_is_final,
            outcome_retake_due,
        )

        completion = OperationCompletionStore(world.store)
        first = prepared
        first_key = _exhaust_by_interruption(world, first.binding_id)
        bindings = completion.list_outcome_bindings_for_intent(world.mission.id, submitted["intent_id"])
        assert outcome_retake_due(world.store, world.mission.id, bindings)
        assert not outcome_exhaustion_is_final(world.store, world.mission.id, first_key)
        prepared = prepare_operation_outcome_review(
            world.store, intent_id=submitted["intent_id"], connectors=connectors,
            profiles=profiles, retake=True)
        assert prepared.binding_id != first.binding_id
        assert prepared.manifest == {**first.manifest, "review_retake": 1}
        with world.store.transaction():
            persist_operation_outcome_review(world.service, prepared, runtime=runtime)
        bindings = completion.list_outcome_bindings_for_intent(world.mission.id, submitted["intent_id"])
        assert len(bindings) == 2
        assert not outcome_retake_due(world.store, world.mission.id, bindings), "one retake only"
        assert not outcome_exhaustion_is_final(world.store, world.mission.id, first_key)
    outcome_dispatch, _, outcome_turn = _submit_review_intent(
        world,
        subject="operation-outcome-review:" + prepared.binding_id,
        role="operation_outcome_reviewer",
        package_id=str(prepared.package.package_id),
        extra={"outcome_binding_id": prepared.binding_id},
    )
    with world.store.transaction():
        outcome_review = record_operation_outcome_review(
            world.store,
            mission_id=world.mission.id,
            binding_id=prepared.binding_id,
            dispatch=outcome_dispatch,
            turn_id=outcome_turn,
            text=_passing_critic(c.criterion_id for c in prepared.package.criteria),
        )
    assert str(outcome_review.verdict) == "ACCEPT"
    completion_receipt = accept_operation_outcome(
        world.service,
        mission_id=world.mission.id,
        binding_id=prepared.binding_id,
        service_authority=runtime.service_authority,
    )

    contribution = OperationCompletionStore(world.store).get_outcome_binding_exact(
        world.mission.id, prepared.binding_id
    )
    finished = world.store.get_task(task.id)
    assert contribution is not None
    assert completion_receipt.acceptance.acceptance_id == "acc-" + prepared.binding_id
    persisted = HtnStore(world.store).find_acceptance_receipt(
        world.mission.id, "accept:" + prepared.binding_id
    )
    assert persisted is not None
    assert persisted.event_id == completion_receipt.commit.event_id
    assert persisted.output_identity == completion_receipt.commit.output_identity
    assert persisted.intent_hash == completion_receipt.commit.intent_hash
    scoped = OperationCompletionStore(world.store).get_acceptance_scope_exact(
        world.mission.id, str(completion_receipt.acceptance.acceptance_id)
    )
    assert scoped is not None
    assert HtnStore(world.store).list_delivery_receipts(world.mission.id)
    assert finished is not None and finished.status is TaskStatus.COMPLETED
    assert [event.type for event in world.store.list_events(world.mission.id)][-1] == (
        "OperationOutcomeAccepted"
    )
    assert OperationIntentStore(world.store).get(submitted["intent_id"]) is not None
    assert HtnStore(world.store).list_acceptance_outputs(world.mission.id) == accepted_outputs
    events_before = world.store.list_events(world.mission.id)
    replay = accept_operation_outcome(
        world.service,
        mission_id=world.mission.id,
        binding_id=prepared.binding_id,
        service_authority=runtime.service_authority,
    )
    assert replay.replayed
    assert replay.acceptance == completion_receipt.acceptance
    assert world.store.list_events(world.mission.id) == events_before
    assert published_path.read_bytes() == body
    if interrupted_first:
        # the retake ran out too: now the effect has reached its end, nothing more to wait for
        _exhaust_by_interruption(world, prepared.binding_id)
        assert outcome_exhaustion_is_final(world.store, world.mission.id, first_key)
