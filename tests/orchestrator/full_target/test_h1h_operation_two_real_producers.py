"""O02: two formal T0 producers sharing a target never alias in the H1 reader."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_OPERATION = _HERE / "operation_completion"
for _directory in (_HERE, _OPERATION):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from operation_runtime_fixture import _passing_critic, _review_dispatch  # noqa: E402
from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import _api, _approval_world, _command  # noqa: E402

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.operation_intents import SubmitOperationIntentV2
from agent_orchestrator.contracts.semantic_base import Provenance, TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.action_commits import ActionCommitError
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.operation_materialization import OperationMaterializationRuntime
from agent_orchestrator.orchestrator.operation_proposal_review import ActionProposalReviewCoordinator
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.operation_profiles import BuiltinOperationProfiles
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.runtime.planning_operations import OperationEffect, StoreOperationReader, build_operation_snapshot
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.store import StoreConflict


def test_o02_two_real_t0_producers_same_target_keep_exact_action_links(tmp_path) -> None:
    published = tmp_path / "published"
    published.mkdir()
    connector = FilePublishConnector(published, tmp_path / "publish-ledger")
    connectors = {"file_publish": connector}
    deployment = DeploymentPolicy(enabled_connectors=("file_publish",))
    profiles = BuiltinOperationProfiles(connectors)

    world, requirements = _approval_world(tmp_path)
    original = world.mission
    world.mission = dataclasses.replace(
        original,
        success_criteria=(
            *original.success_criteria,
            "action:file_publish.publish:reports/shared.json",
        ),
        version=original.version + 1,
    )
    world.store.update_mission(world.mission, expected_version=original.version)
    world.service.begin_planning(world.mission.id)
    completion = _command(requirements, milestone="CONTENT_HASH_VERIFIED")
    first_effect = completion["proposal"]["effects"][0]
    first_effect["milestone_policy_ref"] = profiles.milestone_policy_ref.to_json()
    first_effect["evidence_policy_ref"] = profiles.evidence_policy_ref.to_json()
    second_effect = {
        **first_effect,
        "effect_key": "deliver-shared-second",
        "criterion_ids": list(first_effect["criterion_ids"]),
        "source_slot_key": "approved-delivery-slot-second",
    }
    completion["proposal"]["effects"] = [first_effect, second_effect]
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
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="o02-two-producers-v1",
        reservation=Reservation(1_000, 0),
        intent_config={"message": "prepare two independently authorized publish candidates"},
        input_hash="2" * 64,
    )
    world.service.claim_intent(worker.intent_id, owner="o02-fixture", lease_seconds=60)
    world.service.record_agent_created(
        worker.intent_id, agent_id="o02-worker", expected_turn_id="o02-worker-turn"
    )
    world.service.record_submitted(
        worker.intent_id, receipt={"turn_id": "o02-worker-turn", "seq": 1}
    )
    cas = world.service._source_artifact_store
    assert isinstance(cas, ArtifactStore)
    artifacts = []
    for label in ("first", "second"):
        candidate_path = f"candidate-{label}.json"
        candidate = {
            "connector": "file_publish",
            "operation": "publish",
            "target": "reports/shared.json",
            "params": {"artifact_path": candidate_path},
            "reason": f"publish the independently reviewed {label} result",
        }
        body = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
        digest = cas.put_bytes(body)
        artifact = Artifact(
            id=f"artifact-o02-{label}",
            mission_id=world.mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            type="file",
            path=candidate_path,
            version=1,
            content_hash=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            produced_by="o02-worker",
            storage_uri=str(cas.path_for(digest)),
        )
        artifacts.append(artifact)
    result = ResultEnvelope(
        id="result-o02-two-producers",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="Prepared two exact operation candidates with one shared target.",
        claims=(ClaimProposal(content="two publish candidates prepared", confidence=0.9),),
        evidence=(),
        artifacts=tuple(item.path for item in artifacts),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    stored = world.service.record_result(
        attempt.id,
        envelope=result,
        turn_id="o02-worker-turn",
        artifacts=tuple(artifacts),
        usage_refs=(),
        port_claims=(PortClaim(port_key="report", path=artifacts[0].path),),
    )
    world.service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id, layer=layer, status="PASS", detail={"producer": "CommitService"}
        )
    world.service.accept_result(stored.envelope.id, verifier_results=())
    acceptance = HtnStore(world.store).list_acceptances(world.mission.id)[0]

    captured = {}

    def prepare_review(sources, payloads, package_id):
        coordinator = ActionProposalReviewCoordinator(
            world.store, connectors, deployment, profiles, profiles.policy_for(sources)
        )
        draft = coordinator.prepare_review(sources, payloads, package_id=package_id)
        captured[str(package_id)] = (coordinator, draft)
        return draft

    runtime = OperationMaterializationRuntime(
        connectors, deployment, profiles, profiles.policy_for, prepare_review, object()
    )
    world.service.bind_operation_materialization_runtime(runtime)
    materialized = []
    effect_keys = ("deliver-report", "deliver-shared-second")
    for index, (artifact, effect_key) in enumerate(zip(artifacts, effect_keys), 1):
        command = SubmitOperationIntentV2.from_json(
            {
                "schema_version": 2,
                "mission_id": world.mission.id,
                "idempotency_key": f"o02-submit-{index}",
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
                "completion_slot": {"spec_hash": approved.spec_hash, "effect_key": effect_key},
            }
        )
        submitted = world.service.submit_operation_intent(
            command,
            tenant_id=world.mission.tenant_id,
            principal=Principal("operation-owner"),
        )
        coordinator, draft = captured[next(reversed(captured))]
        dispatch = _review_dispatch(
            world,
            subject="operation-review:" + submitted["intent_id"],
            package_id=str(draft.package.package_id),
            intent_id=submitted["intent_id"],
        )
        with world.store.transaction():
            review = coordinator.record_critic_verdict(
                draft,
                record_id="operation-review-record:" + submitted["intent_id"],
                dispatch_intent_id=dispatch.intent_id,
                reviewer_agent_id="operation-proposal-reviewer-agent",
                reviewer_turn_id="operation-proposal-reviewer-turn",
                raw_critic_text=_passing_critic(c.criterion_id for c in draft.package.criteria),
            )
        materialized.append(
            world.service.materialize_reviewed_operation(
                intent_id=submitted["intent_id"],
                command_id="materialize:" + submitted["intent_id"],
                official_review_ref=TypedRef(
                    TypedRefKind.REVIEW,
                    str(review.record.record_id),
                    1,
                    content_hash_of(review.record.to_json()),
                ),
                service_authority=runtime.service_authority,
            )
        )

    actions = [world.store.get_action(item["action_key"]) for item in materialized]
    assert all(action is not None for action in actions)
    first, second = actions
    assert first["target"] == second["target"] == "reports/shared.json"
    assert first["params_hash"] != second["params_hash"]
    assert first["action_key"] != second["action_key"]
    admission = PlanningAdmissionStore(world.store)
    links = admission.list_operation_action_links(world.mission.id)
    assert len(links) == 2
    assert len({link["operation_id"] for link in links}) == 2
    assert len({link["operation_occurrence_id"] for link in links}) == 2
    assert {link["action_key"] for link in links} == {first["action_key"], second["action_key"]}
    assert {link["params_hash"] for link in links} == {first["params_hash"], second["params_hash"]}

    snapshot = build_operation_snapshot(
        world.mission.id, reader=StoreOperationReader(world.store)
    )
    assert len(snapshot.effects) == 2
    assert {row.operation_id for row in snapshot.bindings} == {
        link["operation_id"] for link in links
    }
    assert {row.action_key for row in snapshot.actions} == {
        first["action_key"], second["action_key"]
    }

    # Inject a lost-outcome state into one original, formally materialized
    # action. The other action shares its target but must remain not-started.
    # This is a reader fault scenario, not a claim that a connector was called.
    world.service._set_action_state(first["action_key"], "UNKNOWN", handoffs=1)
    changed = build_operation_snapshot(world.mission.id, reader=StoreOperationReader(world.store))
    expected = {
        link["operation_id"]: (OperationEffect.UNRESOLVED if link["action_key"] == first["action_key"]
                               else OperationEffect.NOT_HANDED_OFF)
        for link in links
    }
    assert dict(changed.effects) == expected

    original_link = admission.get_operation_action_link(links[0]["operation_id"])
    assert original_link is not None
    alias = dict(links[1])
    alias["operation_id"] = links[0]["operation_id"]
    alias["link_json"] = json.dumps(alias, sort_keys=True, separators=(",", ":"))
    before_actions = tuple(world.store.list_actions(world.mission.id))
    before_events = tuple(world.store.list_events(world.mission.id))
    with pytest.raises(StoreConflict, match="operation_action_link_conflict"):
        admission.put_operation_action_link(alias)
    assert admission.get_operation_action_link(links[0]["operation_id"]) == original_link
    assert tuple(world.store.list_actions(world.mission.id)) == before_actions
    assert tuple(world.store.list_events(world.mission.id)) == before_events
