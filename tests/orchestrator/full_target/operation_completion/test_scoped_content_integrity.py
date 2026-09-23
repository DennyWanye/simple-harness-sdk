"""Integrity negatives for new-protocol scoped TASK_CONTENT acceptance."""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_completion_plan_commit import _admitted_single_root  # noqa: E402
from test_completion_spec_approval import (  # noqa: E402
    _api,
    _bind_new_protocol,
    _command,
    _criterion,
)
from test_plan_commits import _world  # noqa: E402
from test_scoped_content_commit import _mixed_world  # noqa: E402

from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.resolution import (
    AllExpr,
    CriterionExpr,
    RequiredEvidencePolicy,
    RequirementsRevision,
)
from agent_orchestrator.contracts.semantic_base import Provenance
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly
from agent_orchestrator.orchestrator.operation_completion import OperationCompletionError
from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore


class _Captured(RuntimeError):
    pass


@pytest.mark.parametrize("mutation", ["manifest", "omitted-output", "namespace"])
def test_scoped_review_cannot_replace_frozen_input_or_result_claims(tmp_path, mutation) -> None:
    world, _, _, task, stored, _ = _mixed_world(tmp_path, with_output=True)
    command, principal = _capture_legal_command(world, task, stored.envelope.id)
    if mutation == "manifest":
        other = HtnStore(world.store).insert_input_manifest(
            world.mission.id, task.id, {"different": "not the dispatched input"}
        )
        package = dataclasses.replace(
            command.package,
            binding=dataclasses.replace(command.package.binding, input_manifest_hash=other),
        )
        command = dataclasses.replace(command, package=package)
    elif mutation == "omitted-output":
        command = dataclasses.replace(command, outputs=())
    else:
        output = command.outputs[0]
        command = dataclasses.replace(
            command,
            outputs=(
                dataclasses.replace(
                    output,
                    source_identity=dataclasses.replace(output.source_identity, namespace="other"),
                ),
            ),
        )
    before = _write_counts(world)
    with pytest.raises(ResolutionCommitRejected):
        world.service.accept_review(command, principal)
    assert _write_counts(world) == before


class _CaptureCommit:
    def __init__(self) -> None:
        self.command = None
        self.principal = None

    def accept_review(self, command, principal):
        self.command = command
        self.principal = principal
        raise _Captured("captured before commit")


def _capture_legal_command(world, task, result_id: str):
    from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs

    result = world.store.get_result(result_id)
    frozen = load_completion_result_inputs(world.store, result)
    capture = _CaptureCommit()
    with pytest.raises(_Captured, match="captured before commit"):
        LeafAcceptanceAssembly(world.store, capture).accept(
            world.mission.id,
            task.id,
            result_id=result_id,
            layers=(),
            artifacts=tuple(world.store.get_artifact(key) for key in result.artifacts),
            producer_agent_ids=(),
            reviewer_agent_id=f"critic:{result.envelope.attempt_id}",
            now_ms=int(world.store.now * 1000),
            input_manifest_hash=frozen.frozen.manifest_hash,
            port_claims=frozen.port_claims,
            command_id="integrity-check-command",
        )
    assert capture.command is not None
    assert capture.principal is not None
    return capture.command, capture.principal


def _write_counts(world) -> tuple[int, int, int, int, int]:
    connection = world.store.connection
    return (
        int(connection.execute("SELECT count(*) FROM acceptances").fetchone()[0]),
        int(connection.execute("SELECT count(*) FROM acceptance_outputs").fetchone()[0]),
        int(connection.execute("SELECT count(*) FROM acceptance_commit_receipts").fetchone()[0]),
        int(connection.execute("SELECT count(*) FROM operation_acceptance_scopes").fetchone()[0]),
        len(world.store.list_events(world.mission.id)),
    )


def _required_check_world(tmp_path):
    """Build the real approved Spec before Plan Commit with one named root check."""

    world = _world(tmp_path, key="scoped-content-required-check")
    _bind_new_protocol(world)
    report = dataclasses.replace(
        _criterion("criterion-report"),
        required_evidence_policy=RequiredEvidencePolicy(required_check_ids=("security-scan",)),
    )
    delivered = _criterion("criterion-delivered")
    requirements = RequirementsRevision(
        revision_id="completion-requirements-required-check",  # type: ignore[arg-type]
        mission_id=world.mission.id,
        revision=1,
        criteria=(report, delivered),
        success_expression=AllExpr(
            (CriterionExpr("criterion-report"), CriterionExpr("criterion-delivered"))
        ),
        authority_subject="authenticated-user-confirmation",
    )
    HtnStore(world.store).insert_requirements_revision(requirements)
    _api(world).approve(_command(requirements))
    command, _ = _admitted_single_root(world, requirements)
    occurrence = str(command.network.root_occurrence_ids[0])
    member = world.store.connection.execute(
        "SELECT task_id FROM plan_memberships WHERE mission_id=? AND revision=1 "
        "AND occurrence_id=?",
        (world.mission.id, occurrence),
    ).fetchone()
    assert member is not None
    task = world.store.get_task(str(member["task_id"]))
    assert task is not None
    return world, task


def _verified_result(world, task, tmp_path, *, result_id: str):
    attempt, intent = world.service.create_attempt(
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="scoped-integrity-v1",
        reservation=Reservation(tokens=1_000, cost_micros=0),
        intent_config={"message": "produce the approved report"},
        input_hash="a" * 64,
    )
    world.service.claim_intent(intent.intent_id, owner="fixture-orchestrator", lease_seconds=60)
    world.service.record_agent_created(
        intent.intent_id, agent_id="fixture-worker", expected_turn_id="turn-scoped-integrity"
    )
    world.service.record_submitted(
        intent.intent_id, receipt={"turn_id": "turn-scoped-integrity", "seq": 1}
    )
    body = b'{"report":"scoped integrity"}\n'
    path = tmp_path / f"{result_id}.json"
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    artifact = Artifact(
        id=f"artifact-{result_id}",
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=str(path),
        version=1,
        content_hash=digest,
        size_bytes=len(body),
        produced_by="fixture-worker",
    )
    envelope = ResultEnvelope(
        id=result_id,
        mission_id=world.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="prepared report",
        claims=(ClaimProposal(content="report prepared", confidence=0.9),),
        evidence=(str(path),),
        artifacts=(str(path),),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    stored = world.service.record_result(
        attempt.id,
        envelope=envelope,
        turn_id="turn-scoped-integrity",
        artifacts=(artifact,),
        usage_refs=(),
    )
    world.service.start_verification(result_id)
    # Deliberately no security-scan: an unrelated Critic PASS must not satisfy it.
    world.service.record_verification_layer(
        result_id,
        layer="critic_review",
        status="PASS",
        detail={"producer": "CommitService", "layer": "critic_review"},
    )
    return stored, artifact


def test_scoped_root_required_check_cannot_be_replaced_by_critic_pass(tmp_path) -> None:
    world, task = _required_check_world(tmp_path)
    stored, _ = _verified_result(world, task, tmp_path, result_id="result-required-check")
    before = _write_counts(world)

    with pytest.raises(
        (OperationCompletionError, ResolutionCommitRejected), match="required checks"
    ):
        world.service.accept_result(stored.envelope.id, verifier_results=())

    assert _write_counts(world) == before


def test_scoped_output_cannot_name_same_attempt_artifact_absent_from_result(tmp_path) -> None:
    world, _, _, task, stored, artifact = _mixed_world(tmp_path, with_output=True)
    command, principal = _capture_legal_command(world, task, stored.envelope.id)
    body = b'{"unreviewed":"same attempt"}\n'
    path = tmp_path / "same-attempt-unreviewed.json"
    path.write_bytes(body)
    extra = dataclasses.replace(
        artifact,
        id="artifact-same-attempt-unreviewed",
        path=str(path),
        content_hash=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        verification_status="VERIFIED",
    )
    world.store.upsert_artifact(extra)
    binding = HtnStore(world.store).task_semantics_of(world.mission.id, task.id)
    assert binding is not None
    port = next(item for item in binding.output_ports if item.required)
    assembly = LeafAcceptanceAssembly(world.store, world.service)
    outputs = assembly._outputs(
        world.mission.id,
        binding,
        result_id=stored.envelope.id,
        acceptance_id=command.acceptance_id,
        artifacts=(extra,),
        namespace="workspace",
        port_claims=(PortClaim(port_key=port.port_key, path=extra.path),),
    )
    assert outputs and outputs[0].artifact_id == extra.id
    tampered = dataclasses.replace(command, outputs=outputs)
    before = _write_counts(world)

    with pytest.raises(ResolutionCommitRejected, match="identity|artifact|output|review"):
        world.service.accept_review(tampered, principal)

    assert _write_counts(world) == before


def test_scoped_artifact_provenance_is_system_bound(tmp_path) -> None:
    world, _, _, task, stored, _ = _mixed_world(tmp_path)
    command, principal = _capture_legal_command(world, task, stored.envelope.id)
    assert command.artifact_refs
    tampered = dataclasses.replace(
        command,
        artifact_refs=tuple(
            dataclasses.replace(ref, produced_by=Provenance.MODEL) for ref in command.artifact_refs
        ),
    )
    before = _write_counts(world)

    with pytest.raises(ResolutionCommitRejected, match="identity|artifact|review"):
        world.service.accept_review(tampered, principal)

    assert _write_counts(world) == before
