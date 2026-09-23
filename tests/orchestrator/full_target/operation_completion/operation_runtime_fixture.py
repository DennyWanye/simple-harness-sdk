"""Test-only builder for an honestly materialized file-publish Operation."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_FULL_TARGET = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_plan_commit import _admitted_single_root
from test_completion_spec_approval import _api, _approval_world, _command

from agent_orchestrator.api.approvals import ApprovalApi
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.operation_intents import SubmitOperationIntentV2
from agent_orchestrator.contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import Reservation, mission_account
from agent_orchestrator.orchestrator.operation_materialization import OperationMaterializationRuntime
from agent_orchestrator.orchestrator.operation_proposal_review import ActionProposalReviewCoordinator
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.operation_profiles import BuiltinOperationProfiles
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore


@dataclass(frozen=True)
class MaterializedOperationFixture:
    world: Any
    action: dict[str, Any]
    connectors: dict[str, Any]
    deployment: DeploymentPolicy
    publish: FilePublishConnector
    runtime: OperationMaterializationRuntime
    intent_id: str
    plan_command: Any


def _passing_critic(criteria) -> str:
    return "<critic_verdict>" + json.dumps({
        "verdict": "PASS", "findings": [],
        "mission_criteria": [{"criterion": item, "met": True, "reason": "frozen evidence matches"}
                             for item in criteria],
    }) + "</critic_verdict>"


def _review_dispatch(world, *, subject: str, package_id: str, intent_id: str):
    intent = world.service.create_service_intent(
        kind="plan", subject_id=subject, mission_id=world.mission.id,
        account_id=mission_account(world.mission.id), creation_key=subject,
        input_id="attempt-input", input_hash=hashlib.sha256(subject.encode()).hexdigest(),
        config={"role": "operation_proposal_reviewer", "review_package_id": package_id,
                "operation_intent_id": intent_id},
        reservation=Reservation(tokens=0, cost_micros=0),
    )
    world.service.claim_intent(intent.intent_id, owner="operation-fixture", lease_seconds=60)
    world.service.record_agent_created(
        intent.intent_id, agent_id="operation-proposal-reviewer-agent",
        expected_turn_id="operation-proposal-reviewer-turn",
    )
    world.service.record_submitted(
        intent.intent_id, receipt={"turn_id": "operation-proposal-reviewer-turn", "seq": 1}
    )
    return world.store.get_intent(intent.intent_id)


def materialized_file_publish(tmp_path: Path, *, approve_action: bool = True) -> MaterializedOperationFixture:
    """Produce a real approved T0/T1 action without handing it to the connector."""
    published = tmp_path / "published"
    published.mkdir()
    publish = FilePublishConnector(published, tmp_path / "publish-ledger")
    connectors = {"file_publish": publish}
    deployment = DeploymentPolicy(enabled_connectors=("file_publish",))
    profiles = BuiltinOperationProfiles(connectors)

    world, requirements = _approval_world(tmp_path)
    original = world.mission
    world.mission = dataclasses.replace(
        original,
        success_criteria=(*original.success_criteria,
                          "action:file_publish.publish:reports/final.json"),
        version=original.version + 1,
    )
    world.store.update_mission(world.mission, expected_version=original.version)
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

    attempt, worker = world.service.create_attempt(
        task.id, role="worker", model="fixture-worker", prompt_version="fixture-worker-v1",
        context_version="operation-runtime-v1", reservation=Reservation(1_000, 0),
        intent_config={"message": "prepare the approved publish action"}, input_hash="a" * 64,
    )
    world.service.claim_intent(worker.intent_id, owner="fixture", lease_seconds=60)
    world.service.record_agent_created(
        worker.intent_id, agent_id="fixture-worker", expected_turn_id="worker-turn"
    )
    world.service.record_submitted(worker.intent_id, receipt={"turn_id": "worker-turn", "seq": 1})
    candidate = {
        "connector": "file_publish", "operation": "publish", "target": "reports/final.json",
        "params": {"artifact_path": "report.json"}, "reason": "publish the reviewed result",
    }
    body = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    cas = world.service._source_artifact_store
    assert isinstance(cas, ArtifactStore)
    digest = cas.put_bytes(body)
    artifact = Artifact(
        id="artifact-operation-candidate", mission_id=world.mission.id, task_id=task.id,
        attempt_id=attempt.id, type="file", path="report.json", version=1,
        content_hash=hashlib.sha256(body).hexdigest(), size_bytes=len(body),
        produced_by="fixture-worker", storage_uri=str(cas.path_for(digest)),
    )
    result = ResultEnvelope(
        id="result-operation-candidate", mission_id=world.mission.id, task_id=task.id,
        attempt_id=attempt.id, outcome="candidate", summary="Prepared a scoped publish action.",
        claims=(ClaimProposal(content="publish candidate prepared", confidence=0.9),), evidence=(),
        artifacts=(artifact.path,), proposed_tasks=(), used_knowledge=(), risks=(), cost={},
    )
    stored = world.service.record_result(
        attempt.id, envelope=result, turn_id="worker-turn", artifacts=(artifact,), usage_refs=(),
        port_claims=(PortClaim(port_key="report", path=artifact.path),),
    )
    world.service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id, layer=layer, status="PASS", detail={"producer": "CommitService"}
        )
    world.service.accept_result(stored.envelope.id, verifier_results=())
    acceptance = HtnStore(world.store).list_acceptances(world.mission.id)[0]

    captured: dict[str, Any] = {}
    def prepare_review(sources, payloads, package_id):
        coordinator = ActionProposalReviewCoordinator(
            world.store, connectors, deployment, profiles, profiles.policy_for(sources)
        )
        captured["coordinator"] = coordinator
        captured["draft"] = coordinator.prepare_review(sources, payloads, package_id=package_id)
        return captured["draft"]

    runtime = OperationMaterializationRuntime(
        connectors, deployment, profiles, profiles.policy_for, prepare_review, object()
    )
    world.service.bind_operation_materialization_runtime(runtime)
    command = SubmitOperationIntentV2.from_json({
        "schema_version": 2, "mission_id": world.mission.id,
        "idempotency_key": "publish-final-report", "intent_source": {"kind": "USER_COMMAND"},
        "candidate_artifact_ref": TypedRef(
            TypedRefKind.ARTIFACT, artifact.id, artifact.version, artifact.content_hash,
            Provenance.TOOL).to_json(),
        "prepared_acceptance_refs": [TypedRef(
            TypedRefKind.ACCEPTANCE, str(acceptance.acceptance_id), 1,
            content_hash_of(acceptance.to_json()), Provenance.TOOL).to_json()],
        "supersedes_intent_id": None,
        "completion_slot": {"spec_hash": approved.spec_hash, "effect_key": "deliver-report"},
    })
    submitted = world.service.submit_operation_intent(
        command, tenant_id=world.mission.tenant_id, principal=Principal("operation-owner")
    )
    draft = captured["draft"]
    dispatch = _review_dispatch(
        world, subject="operation-review:" + submitted["intent_id"],
        package_id=str(draft.package.package_id), intent_id=submitted["intent_id"],
    )
    with world.store.transaction():
        review = captured["coordinator"].record_critic_verdict(
            draft, record_id="operation-review-record:" + submitted["intent_id"],
            dispatch_intent_id=dispatch.intent_id,
            reviewer_agent_id="operation-proposal-reviewer-agent",
            reviewer_turn_id="operation-proposal-reviewer-turn",
            raw_critic_text=_passing_critic(c.criterion_id for c in draft.package.criteria),
        )
    materialized = world.service.materialize_reviewed_operation(
        intent_id=submitted["intent_id"], command_id="materialize:" + submitted["intent_id"],
        official_review_ref=TypedRef(
            TypedRefKind.REVIEW, str(review.record.record_id), 1,
            content_hash_of(review.record.to_json())),
        service_authority=runtime.service_authority,
    )
    action = world.store.get_action(materialized["action_key"])
    assert action is not None
    if approve_action:
        ApprovalApi(world.service, Principal("operation-approver"), deployment=deployment).approve(
            action["approval_request_id"], nonce="approve-real-publish"
        )
    action = world.store.get_action(materialized["action_key"])
    assert action is not None and action["state"] == ("APPROVED" if approve_action else "AWAITING_APPROVAL")
    return MaterializedOperationFixture(
        world, action, connectors, deployment, publish, runtime, submitted["intent_id"], plan
    )
