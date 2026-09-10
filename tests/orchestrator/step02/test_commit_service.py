# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · slice A3/A4: the Commit Service is the single writer — idempotent mission
creation, checked task proposals with replayable receipts, Reserve on attempt
creation, dispatch identity recording, result acceptance / rejection, stop paths and
the Mission-level judgment that is independent of the Task PASS (D21)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimStatus,
    MissionStatus,
    MissionStopReason,
    ResultEnvelope,
    TaskStatus,
    ids,
)
from agent_orchestrator.governance.budgets import BudgetExhausted, UsageFact
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionConflict,
    MissionSpec,
    Reservation,
    TaskProposal,
    task_account,
)
from agent_orchestrator.storage.store import Store

SPEC = MissionSpec(
    goal="在隔离工作区实现 parse_kv 并通过测试",
    success_criteria=("pytest:tests/test_parse_kv.py",),
    tenant_id="tenant-a",
    idempotency_key="mission-1",
    allowed_tools=("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"),
    budget=Budget(max_tokens=10_000, max_attempts=2, max_cost_micros=None),
)
PROPOSAL = TaskProposal(
    goal="实现 parse_kv(text) -> dict",
    rationale="唯一任务，直接满足 Mission 目标",
    success_criteria=("tests/test_parse_kv.py 通过",),
    verification_policy=("format_check", "rule_check", "code_test"),
    allowed_tools=("workspace_read_file", "workspace_write_file", "run_tests"),
    budget=Budget(max_tokens=8_000, max_attempts=2),
)


def _service(tmp_path):
    return CommitService(Store.open(tmp_path / "orchestrator.db"))


def _envelope(attempt_id, task_id, mission_id, artifact_ids=("artifact-x",)):
    return ResultEnvelope(
        id="result-tmp",
        mission_id=mission_id,
        task_id=task_id,
        attempt_id=attempt_id,
        outcome="candidate",
        summary="实现完成",
        claims=(),
        evidence=list(artifact_ids),
        artifacts=list(artifact_ids),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )


def _artifact(attempt_id, task_id, mission_id, artifact_id="artifact-x"):
    return Artifact(
        id=artifact_id,
        mission_id=mission_id,
        task_id=task_id,
        attempt_id=attempt_id,
        type="file",
        path="parse_kv.py",
        version=1,
        content_hash="0" * 64,
        size_bytes=10,
        produced_by="agent-1",
    )


def _drive_to_running(service, mission_id, task):
    attempt, intent = service.create_attempt(
        task.id,
        role="worker",
        model="agent-model",
        prompt_version="worker-v1",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"config": {}, "message": "do"},
        input_hash="h",
    )
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id="agent-1", expected_turn_id="turn-1")
    service.record_submitted(intent.intent_id, receipt={"turn_id": "turn-1", "seq": 1})
    return service.store.get_attempt(attempt.id)


def test_mission_creation_is_idempotent_and_conflict_checked(tmp_path):
    service = _service(tmp_path)
    mission, created = service.create_mission(SPEC)
    assert created and mission.status is MissionStatus.CREATED
    again, created_again = service.create_mission(SPEC)
    assert not created_again and again == mission
    assert service.store.count_events(mission.id, "MissionCreated") == 1
    with pytest.raises(MissionConflict):
        service.create_mission(replace(SPEC, goal="别的目标"))


def test_task_proposal_is_checked_and_replays_the_same_receipt(tmp_path):
    service = _service(tmp_path)
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    with pytest.raises(CommitRejected):  # tools outside the Mission
        service.commit_task_proposal(
            mission.id,
            replace(PROPOSAL, allowed_tools=("shell",)),
            base_version=planning.version,
            source={},
        )
    with pytest.raises(CommitRejected):  # budget above the Mission's
        service.commit_task_proposal(
            mission.id,
            replace(PROPOSAL, budget=Budget(max_tokens=20_000)),
            base_version=planning.version,
            source={},
        )
    with pytest.raises(CommitRejected):  # stale base version
        service.commit_task_proposal(mission.id, PROPOSAL, base_version=1, source={})
    task, receipt = service.commit_task_proposal(
        mission.id, PROPOSAL, base_version=planning.version, source={"planner": "p1"}
    )
    assert task.status is TaskStatus.READY and task.id == ids.task_id(mission.id, 1)
    assert service.store.get_mission(mission.id).status is MissionStatus.ACTIVE
    task2, receipt2 = service.commit_task_proposal(
        mission.id, PROPOSAL, base_version=planning.version, source={"planner": "p1"}
    )
    assert task2 == task and receipt2 == receipt
    assert service.store.count_events(mission.id, "TaskCommitted") == 1
    with pytest.raises(CommitRejected):  # a second, different task is refused in step 2
        service.commit_task_proposal(
            mission.id,
            replace(PROPOSAL, goal="第二个任务"),
            base_version=service.store.get_mission(mission.id).version,
            source={},
        )


def test_attempt_reserve_dispatch_identity_and_result_acceptance(tmp_path):
    service = _service(tmp_path)
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    task, _ = service.commit_task_proposal(
        mission.id, PROPOSAL, base_version=planning.version, source={}
    )
    attempt = _drive_to_running(service, mission.id, task)
    assert attempt.status is AttemptStatus.RUNNING
    assert attempt.agent_id == "agent-1" and attempt.turn_id == "turn-1"
    assert attempt.task_version == task.version
    assert service.store.get_task(task.id).status is TaskStatus.ACTIVE
    with service.store.transaction():
        assert service.ledger.account(task_account(task.id)).reserved_tokens == 4_000
    with pytest.raises(CommitRejected):  # only one open Attempt per Task in step 2
        service.create_attempt(
            task.id,
            role="worker",
            model="m",
            prompt_version="v",
            context_version="c",
            reservation=Reservation(1, 0),
            intent_config={},
            input_hash="h",
        )
    envelope = _envelope(attempt.id, task.id, mission.id)
    with pytest.raises(CommitRejected):  # identity forged
        service.record_result(
            attempt.id,
            envelope=replace(envelope, attempt_id="someone-else"),
            turn_id="turn-1",
            artifacts=[_artifact(attempt.id, task.id, mission.id)],
            usage_refs=(),
        )
    stored = service.record_result(
        attempt.id,
        envelope=envelope,
        turn_id="turn-1",
        artifacts=[_artifact(attempt.id, task.id, mission.id)],
        usage_refs=("provider-request:r1",),
    )
    duplicate = service.record_result(
        attempt.id, envelope=envelope, turn_id="turn-1", artifacts=[], usage_refs=()
    )
    assert duplicate == stored
    assert service.store.count_events(mission.id, "ResultSubmitted") == 1
    assert service.store.get_task(task.id).status is TaskStatus.VERIFYING
    service.start_verification(stored.envelope.id)
    assert service.store.get_attempt(attempt.id).status is AttemptStatus.VERIFYING
    service.import_usage(attempt.id, mission.id, [UsageFact("inv-1", 100, 50, None)])
    completed = service.accept_result(
        stored.envelope.id, verifier_results=[{"layer": "code_test", "status": "PASS"}]
    )
    assert completed.status is TaskStatus.COMPLETED and completed.accepted_artifacts == (
        "artifact-x",
    )
    assert service.store.get_attempt(attempt.id).status is AttemptStatus.COMPLETED
    # Mission is NOT completed by the Task PASS alone (D21)
    assert service.store.get_mission(mission.id).status is MissionStatus.ACTIVE
    with service.store.transaction():
        account = service.ledger.account(task_account(task.id))
    assert (
        account.reserved_tokens == 0
        and account.settled_tokens == 150
        and account.unpriced_settlements == 1
    )
    assert service.store.count_events(mission.id, "BudgetReleased") == 1
    judged = service.judge_mission(
        mission.id,
        judgments=[{"criterion": SPEC.success_criteria[0], "met": True, "layer": "code_test"}],
        summary="全部通过",
    )
    assert judged.status is MissionStatus.COMPLETED
    assert judged.stop_reason == str(MissionStopReason.VERIFICATION_PASSED)
    assert service.store.count_events(mission.id, "MissionSuccessJudged") == 1
    assert service.judge_mission(mission.id, judgments=[], summary="") == judged  # idempotent


def test_failed_verification_retry_and_stop_paths(tmp_path):
    service = _service(tmp_path)
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    task, _ = service.commit_task_proposal(
        mission.id, PROPOSAL, base_version=planning.version, source={}
    )
    attempt = _drive_to_running(service, mission.id, task)
    stored = service.record_result(
        attempt.id,
        envelope=_envelope(attempt.id, task.id, mission.id),
        turn_id="turn-1",
        artifacts=[_artifact(attempt.id, task.id, mission.id)],
        usage_refs=(),
    )
    service.start_verification(stored.envelope.id)
    claims = service.store.list_claims(stored.envelope.id)
    active = service.fail_result(
        stored.envelope.id,
        failures=[{"layer": "code_test", "status": "FAIL", "summary": "1 failed"}],
    )
    assert active.status is TaskStatus.ACTIVE
    first = service.store.get_attempt(attempt.id)
    assert (
        first.status is AttemptStatus.RETRY_WAIT
        and first.failure["reason"] == "verification_failed"
    )
    assert (
        all(c.status is ClaimStatus.REJECTED for c in service.store.list_claims(stored.envelope.id))
        or not claims
    )
    # second (retry) attempt with feedback and retry_of
    second, _ = service.create_attempt(
        task.id,
        role="worker",
        model="m",
        prompt_version="v",
        context_version="c",
        reservation=Reservation(4_000, 0),
        intent_config={},
        input_hash="h2",
        retry_of=first.id,
        feedback=("code_test: 1 failed",),
    )
    assert (
        second.retry_of == first.id
        and second.ordinal == 2
        and second.feedback == ("code_test: 1 failed",)
    )
    assert service.store.get_task(task.id).attempt_count == 2
    # a third attempt exceeds max_attempts=2 → BudgetExhausted, nothing written
    service.mark_attempt_lost(second.id, reason="test")
    with pytest.raises(BudgetExhausted) as exc:
        service.create_attempt(
            task.id,
            role="worker",
            model="m",
            prompt_version="v",
            context_version="c",
            reservation=Reservation(10, 0),
            intent_config={},
            input_hash="h3",
        )
    assert exc.value.dimension == "attempts"
    assert len(service.store.list_attempts(task.id)) == 2
    stopped = service.stop_task(
        task.id, stop_reason=MissionStopReason.MAX_ATTEMPTS_REACHED, detail={"max_attempts": 2}
    )
    assert stopped.status is TaskStatus.FAILED
    failed_mission = service.store.get_mission(mission.id)
    assert failed_mission.status is MissionStatus.FAILED
    assert failed_mission.stop_reason == "max_attempts_reached"
    assert len(failed_mission.final_report["completed_parts"]) == 2
    assert service.store.count_events(mission.id, "MissionFailed") == 1


def test_reject_result_and_cancel_from_verifying(tmp_path):
    service = _service(tmp_path)
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    task, _ = service.commit_task_proposal(
        mission.id, PROPOSAL, base_version=planning.version, source={}
    )
    attempt = _drive_to_running(service, mission.id, task)
    rejected = service.reject_result(
        attempt.id,
        turn_id="turn-1",
        reason="envelope_invalid",
        detail={"error": "no envelope block"},
    )
    assert (
        rejected.status is AttemptStatus.RETRY_WAIT
        and rejected.failure["reason"] == "envelope_invalid"
    )
    assert service.store.get_task(task.id).status is TaskStatus.ACTIVE  # formal Task unchanged
    assert service.store.count_events(mission.id, "ResultRejected") == 1
    # cancel while a second attempt is verifying: VERIFYING → ACTIVE → CANCELLED (D15')
    second = _drive_to_running(service, mission.id, service.store.get_task(task.id))
    stored = service.record_result(
        second.id,
        envelope=_envelope(second.id, task.id, mission.id),
        turn_id="turn-1",
        artifacts=[_artifact(second.id, task.id, mission.id)],
        usage_refs=(),
    )
    service.start_verification(stored.envelope.id)
    cancelled = service.cancel_mission(mission.id)
    assert cancelled.status is MissionStatus.CANCELLED
    assert service.store.get_task(task.id).status is TaskStatus.CANCELLED
    assert service.store.get_attempt(second.id).status is AttemptStatus.CANCELLED
