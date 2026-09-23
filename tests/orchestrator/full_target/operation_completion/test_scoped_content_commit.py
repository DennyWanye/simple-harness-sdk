"""OCC-11: TASK_CONTENT acceptance consumes a frozen completion scope.

These cases deliberately create a real attempt, result, recorded verification
layers and verified Artifact through CommitService before invoking the leaf
acceptance seam.  A caller-provided tuple of passing layers is never the source
of the scoped projection.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import _api, _approval_world, _command  # noqa: E402

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.state_machines import AttemptStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import CommitRejected, Reservation
from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
from agent_orchestrator.orchestrator.scoped_content_review import read_task_content_projection
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.store import InjectedCrash


def _mixed_world(tmp_path, *, accept_result: bool = True, with_output: bool = False):
    """Commit the real single primitive MIXED scope and produce a verified result."""

    world, requirements = _approval_world(tmp_path)
    world.service.begin_planning(world.mission.id)
    approved = _api(world).approve(_command(requirements))
    command, _ = _admitted_single_root(
        world, requirements, outputs=(("report", "report.schema"),) if with_output else ()
    )
    task_id = str(command.network.root_occurrence_ids[0])
    membership = world.store.connection.execute(
        "SELECT task_id FROM plan_memberships "
        "WHERE mission_id=? AND revision=? AND occurrence_id=?",
        (world.mission.id, 1, task_id),
    ).fetchone()
    assert membership is not None
    task = world.store.get_task(str(membership["task_id"]))
    assert task is not None

    attempt, intent = world.service.create_attempt(
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="scoped-content-v1",
        reservation=Reservation(tokens=1_000, cost_micros=0),
        intent_config={"message": "produce the approved report"},
        input_hash="a" * 64,
    )
    world.service.claim_intent(intent.intent_id, owner="fixture-orchestrator", lease_seconds=60)
    world.service.record_agent_created(
        intent.intent_id, agent_id="fixture-worker", expected_turn_id="turn-scoped-content"
    )
    world.service.record_submitted(
        intent.intent_id, receipt={"turn_id": "turn-scoped-content", "seq": 1}
    )

    body = b'{"report":"verified local preparation"}\n'
    artifact_path = tmp_path / "real-worker-output.json"
    artifact_path.write_bytes(body)
    cas = ArtifactStore(tmp_path / "scoped-cas")
    artifact_digest = cas.put_bytes(body)
    content_hash = hashlib.sha256(body).hexdigest()
    artifact = Artifact(
        id=f"artifact-scoped-{content_hash[:24]}",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=str(artifact_path),
        version=1,
        content_hash=content_hash,
        size_bytes=len(body),
        produced_by="fixture-worker",
        storage_uri=str(cas.path_for(artifact_digest)),
    )
    result = ResultEnvelope(
        id="result-scoped-content",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="The worker produced the requested report.",
        claims=(ClaimProposal(content="report prepared", confidence=0.9),),
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
        turn_id="turn-scoped-content",
        artifacts=(artifact,),
        usage_refs=(),
        port_claims=(PortClaim(port_key="report", path=artifact.path),) if with_output else (),
    )
    world.service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        world.service.record_verification_layer(
            stored.envelope.id,
            layer=layer,
            status="PASS",
            detail={"producer": "CommitService", "layer": layer},
        )
    if accept_result:
        # This is the production acceptance path.  It changes the StoredResult to
        # DONE/PASS, marks the artifact VERIFIED, and commits scoped Acceptance and
        # Contribution in this same transaction.
        task = world.service.accept_result(stored.envelope.id, verifier_results=())
    return world, requirements, approved, task, stored, artifact


def _accept_scoped(world, task, result_id: str):
    """Replay the exact core acceptance command without caller verdicts."""

    stored = world.store.get_result(result_id)
    assert stored is not None
    attempt = world.store.get_attempt(stored.envelope.attempt_id)
    assert attempt is not None
    frozen = load_completion_result_inputs(world.store, stored)
    assert frozen is not None
    reviewer = f"critic:{attempt.id}"

    return LeafAcceptanceAssembly(world.store, world.service).accept(
        world.mission.id,
        task.id,
        result_id=result_id,
        layers=(),
        artifacts=(),
        producer_agent_ids=(),
        reviewer_agent_id=reviewer,
        now_ms=int(world.store.now * 1000),
        input_manifest_hash=frozen.frozen.manifest_hash,
        port_claims=frozen.port_claims,
    )


def test_occ11_projection_uses_only_verified_result_layers_and_artifact_identity(tmp_path) -> None:
    """OCC-11: local TASK_CONTENT evidence excludes the pending external effect."""

    world, requirements, approved, task, stored, artifact = _mixed_world(tmp_path)
    before_requirements = HtnStore(world.store).latest_requirements_revision(world.mission.id)
    assert before_requirements == requirements

    projection = read_task_content_projection(
        world.store, world.mission.id, task.id, stored.envelope.id
    )

    assert projection.requirements == requirements
    assert projection.spec.content_hash() == approved.spec_hash
    assert projection.scope.role == "MIXED"
    assert tuple(item.criterion_id for item in projection.criteria) == ("criterion-report",)
    assert tuple(item.id for item in projection.artifacts) == (artifact.id,)
    assert projection.artifacts[0].content_hash == artifact.content_hash
    assert {row["layer"] for row in world.store.list_verifications(stored.envelope.id)} >= {
        "critic_review"
    }
    assert (
        HtnStore(world.store).latest_requirements_revision(world.mission.id) == before_requirements
    )


def test_occ11_mixed_preparation_acceptance_is_scoped_replay_safe_and_not_an_effect(
    tmp_path,
) -> None:
    """OCC-11: preparation is durable local work, never a synthetic effect PASS."""

    world, requirements, approved, task, stored, artifact = _mixed_world(tmp_path)
    before_requirements = HtnStore(world.store).latest_requirements_revision(world.mission.id)
    before_intents = tuple(world.store.list_intents("PENDING", "CLAIMED", "SUBMITTED"))
    assert task.status is TaskStatus.VERIFYING
    assert task.accepted_result_id == stored.envelope.id
    attempt = world.store.get_attempt(stored.envelope.attempt_id)
    assert attempt is not None
    assert attempt.status is AttemptStatus.COMPLETED
    assert world.store.list_actions(world.mission.id) == []
    with pytest.raises(CommitRejected, match="accepted preparation"):
        world.service.create_attempt(
            task.id,
            role="worker",
            model="fixture-worker",
            prompt_version="fixture-worker-v1",
            context_version="scoped-content-v1",
            reservation=Reservation(tokens=1_000, cost_micros=0),
            intent_config={"message": "must not dispatch another worker"},
            input_hash="b" * 64,
        )

    # The core path already created this receipt.  Reconstructing its exact
    # command is an idempotent replay, not an alternate acceptance writer.
    before_changes = world.store.connection.total_changes
    receipt = _accept_scoped(world, task, stored.envelope.id)
    assert receipt.replayed is True
    assert world.store.connection.total_changes == before_changes
    completion = OperationCompletionStore(world.store)
    contribution = completion.get_acceptance_scope_exact(world.mission.id, receipt.acceptance_id)
    assert contribution is not None
    document = contribution["document"]
    assert document.spec_hash == approved.spec_hash
    assert document.kind == "PREPARATION"
    assert document.content_criterion_ids == ("criterion-report",)
    assert not document.effect_keys
    assert document.outcome_binding_id is None
    assert document.delivery_receipt_ref is None
    assert tuple(item.id for item in document.output_artifact_refs) == (artifact.id,)
    assert (
        HtnStore(world.store).latest_requirements_revision(world.mission.id) == before_requirements
    )
    assert tuple(world.store.list_intents("PENDING", "CLAIMED", "SUBMITTED")) == before_intents

    before_changes = world.store.connection.total_changes
    replay = _accept_scoped(world, task, stored.envelope.id)
    assert replay.replayed is True
    assert replay.acceptance.to_json() == receipt.acceptance.to_json()
    assert replay.commit.to_json() == receipt.commit.to_json()
    assert world.store.connection.total_changes == before_changes
    assert (
        len(completion.list_scoped_contributions(world.mission.id, document.completion_scope_id))
        == 1
    )


def test_occ11_contribution_fault_rolls_back_acceptance_and_scope_together(tmp_path) -> None:
    """OCC-11: no unscoped Acceptance survives a crash after contribution assembly."""

    world, _, _, task, stored, _ = _mixed_world(tmp_path, accept_result=False)
    before_acceptances = tuple(HtnStore(world.store).list_acceptances(world.mission.id))
    before_contributions = world.store.connection.execute(
        "SELECT count(*) FROM operation_acceptance_scopes WHERE mission_id=?", (world.mission.id,)
    ).fetchone()[0]
    before_events = tuple(item.to_json() for item in world.store.list_events(world.mission.id))
    before_actions = tuple(world.store.list_actions(world.mission.id))
    world.store.arm("completion_acceptance_after_contribution:operation_completion")

    with pytest.raises(InjectedCrash, match="completion_acceptance_after_contribution"):
        world.service.accept_result(stored.envelope.id, verifier_results=())

    persisted_result = world.store.get_result(stored.envelope.id)
    persisted_attempt = world.store.get_attempt(stored.envelope.attempt_id)
    persisted_task = world.store.get_task(task.id)
    assert persisted_result is not None and persisted_result.verification_state != "DONE"
    assert persisted_attempt is not None and persisted_attempt.status is not AttemptStatus.COMPLETED
    assert persisted_task is not None
    assert persisted_task.accepted_result_id is None
    assert tuple(HtnStore(world.store).list_acceptances(world.mission.id)) == before_acceptances
    assert (
        world.store.connection.execute(
            "SELECT count(*) FROM operation_acceptance_scopes WHERE mission_id=?",
            (world.mission.id,),
        ).fetchone()[0]
        == before_contributions
    )
    assert (
        tuple(item.to_json() for item in world.store.list_events(world.mission.id)) == before_events
    )
    assert tuple(world.store.list_actions(world.mission.id)) == before_actions
