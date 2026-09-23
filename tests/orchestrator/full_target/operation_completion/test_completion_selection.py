"""OC2: a real Selection acceptance preserves new-protocol preparation semantics.

The test deliberately drives the public COMPARE commit API.  It does not use a
Selection mock, nor does it invoke the leaf Acceptance or completion Reader as
a substitute for the public winner commit.
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
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.contracts.state_machines import AttemptStatus, TaskStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import code_versions
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.completion_inputs import load_completion_result_inputs
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.store import InjectedCrash

_OWNER = "selection-fixture-owner"


def _selection_policy(synthesis=False) -> dict[str, object]:
    """One candidate is enough to exercise public accept-complete selection."""

    return {
        "schema_version": 1,
        "mode": "COMPARE_THEN_SYNTHESIZE",
        "max_candidates": 2 if synthesis else 1,
        "deadline_seconds": 120.0,
        "tie_break": "verified_rank_then_result_id-v1",
        "synthesis_limit": 1 if synthesis else 0,
        "on_deadline": "best_complete_else_stop",
        "synthesis_reserve": {"tokens": 1000 if synthesis else 0, "cost_micros": 0, "tool_calls": 0},
        "synthesis_attempts_reserved": 1 if synthesis else 0,
    }


def _bind_compare_policy(world, synthesis=False) -> None:
    """Promote and bind a real policy before Selection opens exploration."""

    base = world.store.active_policy()
    params = {
        **base["params"],
        "schema_version": 2,
        "candidates_per_task": 2 if synthesis else 1,
        "search_selection": _selection_policy(synthesis),
    }
    proposal = world.service.propose_policy(
        params,
        manifest={"oracle": "oc2-selection-preparation"},
        source="fixture",
        principal=Principal("test-host"),
    )
    world.service.record_policy_evaluation(
        proposal["proposal_id"],
        verdict="PASSED",
        reasons=["fixture establishes the authenticated policy promotion seam"],
        report_hash=sha256_hex({"fixture": "oc2-selection-preparation"}),
        baseline_version_id=base["version_id"],
        code_versions=code_versions(),
        evidence_kind="fixture",
    )
    world.service.decide_policy(
        proposal["proposal_id"],
        principal=Principal("test-host"),
        decision="approve",
        nonce="oc2-selection-approval",
    )
    world.service.promote_policy(
        proposal["proposal_id"],
        principal=Principal("test-host"),
        cooldown_seconds=0,
        accept_fixture_evidence=True,
    )
    world.service.bind_search_policy(world.mission.id, proposal["version_id"])


def _selection_preparation(tmp_path, *, synthesis=False):
    """Build one real MIXED-scope candidate at Selection's READY boundary."""

    world, requirements = _approval_world(tmp_path)
    world.service.begin_planning(world.mission.id)
    approved = _api(world).approve(_command(requirements))
    command, _ = _admitted_single_root(
        world,
        requirements,
        outputs=(("report", "report.schema"),),
    )
    occurrence_id = str(command.network.root_occurrence_ids[0])
    membership = world.store.connection.execute(
        "SELECT task_id FROM plan_memberships "
        "WHERE mission_id=? AND revision=? AND occurrence_id=?",
        (world.mission.id, 1, occurrence_id),
    ).fetchone()
    assert membership is not None
    task = world.store.get_task(str(membership["task_id"]))
    assert task is not None

    _bind_compare_policy(world, synthesis)
    round_ = world.service.begin_selection_round(task.id, command_id="selection-round-oc2")
    for index in range(2 if synthesis else 1):
        attempt, intent = world.service.create_attempt(
            task.id,
            role="worker",
            model="fixture-worker",
            prompt_version="fixture-worker-v1",
            context_version="selection-preparation-v1",
            reservation=Reservation(tokens=1_000, cost_micros=0),
            intent_config={"message": "produce the approved report"},
            input_hash="c" * 64,
        )
        world.service.claim_intent(intent.intent_id, owner=_OWNER, lease_seconds=60)
        world.service.record_agent_created(
            intent.intent_id, agent_id="fixture-worker", expected_turn_id=f"turn-selection-oc2-{index}"
        )
        world.service.record_submitted(
            intent.intent_id, receipt={"turn_id": f"turn-selection-oc2-{index}", "seq": 1}
        )

        body = ('{"report":"selection verified local preparation %s"}\n' % index).encode()
        artifact_path = tmp_path / "selection-worker-output.json"
        artifact_path.write_bytes(body)
        cas = ArtifactStore(tmp_path / "artifact-cas")
        stored_hash = cas.put_bytes(body)
        artifact = Artifact(
            id="artifact-selection-" + hashlib.sha256(body).hexdigest()[:24],
            mission_id=world.mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            type="file",
            path="report.json" if synthesis else str(artifact_path),
            version=1,
            content_hash=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            produced_by="fixture-worker",
            storage_uri=str(cas.path_for(stored_hash)),
        )
        result = ResultEnvelope(
            id=f"result-selection-oc2-{index}",
            mission_id=world.mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            outcome="candidate",
            summary="The selected worker produced the requested report.",
            claims=(ClaimProposal(content="report prepared", confidence=0.9),),
            evidence=(artifact.path,),
            artifacts=(artifact.path,),
            proposed_tasks=(),
            used_knowledge=(),
            risks=(),
            cost={},
        )
        stored = world.service.record_result(
            attempt.id,
            envelope=result,
            turn_id=f"turn-selection-oc2-{index}",
            artifacts=(artifact,),
            usage_refs=(),
            port_claims=(PortClaim(port_key="report", path=artifact.path),),
        )
        world.service.start_verification(stored.envelope.id)
        for layer in ("format_check", "rule_check", "code_test", "critic_review"):
            world.service.record_verification_layer(
                stored.envelope.id,
                layer=layer,
                status="PASS",
                detail={"producer": "CommitService", "layer": layer},
            )

        round_ = world.service.selection_round(task.id)
        assert round_ is not None
        ready = world.service.record_candidate_ready(
            stored.envelope.id,
            owner=_OWNER,
            round_id=round_["round_id"],
            expected_round_version=round_["version"],
            command_id=f"selection-ready-oc2-{index}",
        )
        assert ready is not None
        round_ = world.service.selection_round(task.id)
        assert round_ is not None
    decision = world.service.decide_selection(
        task.id,
        owner=_OWNER,
        command_id="selection-decision-oc2",
    )
    assert decision is not None and decision["action"] == ("synthesize" if synthesis else "accept_complete")
    round_ = world.service.selection_round(task.id)
    assert round_ is not None
    return world, approved, task, attempt, stored, round_, decision


def _accept_selected(world, stored, round_, decision):
    return world.service.accept_selected_result(
        stored.envelope.id,
        round_id=round_["round_id"],
        decision_id=decision["receipt_id"],
        expected_round_version=round_["version"],
        owner=_OWNER,
        command_id="selection-accept-oc2",
    )


def test_oc2_selection_winner_commits_preparation_not_effect_or_action(tmp_path) -> None:
    """OC2: Selection's public winner commit retains the MIXED preparation state."""

    world, approved, task, attempt, stored, round_, decision = _selection_preparation(tmp_path)
    receipt = _accept_selected(world, stored, round_, decision)

    persisted_task = world.store.get_task(task.id)
    persisted_attempt = world.store.get_attempt(attempt.id)
    persisted_result = world.store.get_result(stored.envelope.id)
    assert receipt["accepted"] is True
    assert persisted_task is not None and persisted_task.status is TaskStatus.VERIFYING
    assert persisted_task.accepted_result_id == stored.envelope.id
    assert persisted_attempt is not None and persisted_attempt.status is AttemptStatus.COMPLETED
    assert persisted_result is not None
    assert persisted_result.verification_state == "DONE" and persisted_result.verdict == "PASS"
    assert world.store.list_actions(world.mission.id) == []

    frozen = load_completion_result_inputs(world.store, persisted_result)
    assert frozen is not None
    contributions = OperationCompletionStore(world.store).list_scoped_contributions(
        world.mission.id, frozen.frozen.scope_id
    )
    assert len(contributions) == 1
    assert contributions[0]["document"].kind == "PREPARATION"
    assert (
        world.store.connection.execute(
            "SELECT count(*) FROM operation_outcome_review_bindings WHERE mission_id=?",
            (world.mission.id,),
        ).fetchone()[0]
        == 0
    )

    before_changes = world.store.connection.total_changes
    replay = _accept_selected(world, stored, round_, decision)
    assert replay == receipt
    assert world.store.connection.total_changes == before_changes
    assert (
        len(
            OperationCompletionStore(world.store).list_scoped_contributions(
                world.mission.id, frozen.frozen.scope_id
            )
        )
        == 1
    )


def test_oc2_selection_acceptance_fault_leaves_ready_candidate_unaccepted(tmp_path) -> None:
    """OC2: Selection cannot retain its receipt if atomic scoped acceptance aborts."""

    world, _, task, attempt, stored, round_, decision = _selection_preparation(tmp_path)
    before_events = tuple(item.to_json() for item in world.store.list_events(world.mission.id))
    world.store.arm("completion_acceptance_after_contribution:operation_completion")

    with pytest.raises(InjectedCrash, match="completion_acceptance_after_contribution"):
        _accept_selected(world, stored, round_, decision)

    persisted_task = world.store.get_task(task.id)
    persisted_attempt = world.store.get_attempt(attempt.id)
    persisted_result = world.store.get_result(stored.envelope.id)
    assert persisted_task is not None and persisted_task.accepted_result_id is None
    assert persisted_attempt is not None and persisted_attempt.status is AttemptStatus.VERIFYING
    assert persisted_result is not None and persisted_result.verification_state == "RUNNING"
    assert (
        world.store.connection.execute(
            "SELECT count(*) FROM operation_acceptance_scopes WHERE mission_id=?",
            (world.mission.id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        tuple(item.to_json() for item in world.store.list_events(world.mission.id)) == before_events
    )
    restored_round = world.service.selection_round(task.id)
    assert restored_round is not None and restored_round["state"] == "DECIDED"


def test_oc2_selection_synthesis_freezes_candidates_separately_from_data(tmp_path):
    world, _, task, _, _, _, decision = _selection_preparation(tmp_path, synthesis=True)
    selected = world.service.selection_input_artifacts(decision["receipt_id"])
    inputs = [{"task_id": a.task_id, "artifact_id": a.id,
               "path": a.path, "content_hash": a.content_hash} for a in selected]
    assert len(inputs) == 2
    arguments = dict(role="synthesizer", model="fixture-worker", prompt_version="fixture-v1",
        context_version="synthesis-v1", reservation=Reservation(tokens=1000, cost_micros=0),
        intent_config={"message": "synthesize the two candidates"}, input_hash="f" * 64,
        selection_decision_id=decision["receipt_id"], selection_owner=_OWNER)
    with pytest.raises(Exception, match="dispatch inputs differ from frozen manifest"):
        world.service.create_attempt(task.id, inputs=[], **arguments)
    assert len(world.store.list_attempts(task.id)) == 2
    # A missing/forged namespace must not acquire authority from a decision id.
    with pytest.raises(Exception, match="dispatch inputs differ from frozen manifest"):
        world.service.create_attempt(task.id,
            inputs=[{**inputs[0], "path": "forged.py"}, inputs[1]], **arguments)
    attempt, intent = world.service.create_attempt(task.id, inputs=inputs, **arguments)
    assert intent.config["completion_inputs"]["manifest"]["bindings"] == []
    files = intent.config["fragment_execution"]["files"]
    assert set(files) == {item["path"] for item in inputs}
    assert {item["artifact_id"] for item in files.values()} == {a.id for a in selected}
    assert intent.config["selection_decision_id"] == decision["receipt_id"]
    assert world.store.get_intent(intent.intent_id).config["inputs"] == inputs
