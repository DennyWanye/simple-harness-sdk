# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Step 2 · slice A1: the six §26 contracts round-trip, refuse malformed input, and the
§25 state machines admit exactly the drawn transitions."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import (
    Attempt,
    AttemptStatus,
    Budget,
    ClaimProposal,
    ClaimStatus,
    ContractError,
    Event,
    IllegalTransition,
    Mission,
    MissionStatus,
    ResultEnvelope,
    ResultOutcome,
    Task,
    TaskStatus,
    assert_attempt_transition,
    assert_claim_transition,
    assert_task_transition,
    ids,
)


def _mission(**overrides):
    base = dict(
        id="mission-abc",
        goal="实现一个字符串解析函数并通过给定测试",
        success_criteria=("tests pass",),
        stop_conditions=("verification_passed", "budget_exhausted"),
        allowed_tools=("workspace_write_file", "run_tests"),
        risk_level="sandbox",
        budget=Budget(max_tokens=100_000, max_attempts=3),
        tenant_id="t1",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key="k1",
    )
    base.update(overrides)
    return Mission(**base)


def test_mission_round_trip_and_validation():
    mission = _mission()
    assert Mission.from_json(mission.to_json()) == mission
    with pytest.raises(ContractError):
        _mission(success_criteria=())
    with pytest.raises(ContractError):
        _mission(allowed_tools=("a", "a"))
    with pytest.raises(ContractError):
        Budget(max_tokens=-1)
    with pytest.raises(ContractError):
        Budget.from_json({"max_money": 1})


def test_budget_fits_within_parent():
    parent = Budget(max_tokens=1000, max_attempts=3)
    assert Budget(max_tokens=500, max_attempts=3).fits_within(parent)
    assert not Budget(max_tokens=None, max_attempts=3).fits_within(parent)  # unlimited child
    assert not Budget(max_tokens=1001, max_attempts=3).fits_within(parent)
    assert Budget(max_tokens=10, max_attempts=1, max_cost_micros=5).fits_within(parent)


def test_task_requires_success_criteria_and_known_layers():
    task = Task(
        id="mission-abc:task-1",
        mission_id="mission-abc",
        parent_task_ids=(),
        dependency_ids=(),
        goal="实现 parse_kv",
        rationale="唯一任务",
        success_criteria=("pytest 通过",),
        verification_policy=("format_check", "rule_check", "code_test"),
        allowed_tools=("run_tests",),
        budget=Budget(max_attempts=3),
        priority=1.0,
        status=TaskStatus.READY,
        version=1,
    )
    assert Task.from_json(task.to_json()) == task
    with pytest.raises(ContractError):
        Task.from_json({**task.to_json(), "success_criteria": []})
    with pytest.raises(ContractError):
        Task.from_json({**task.to_json(), "verification_policy": ["lean_check"]})


def test_result_envelope_strict_parse_and_alias_rule():
    raw = {
        "id": "result-1",
        "task_id": "mission-abc:task-1",
        "attempt_id": "mission-abc:task-1:attempt-1",
        "outcome": "candidate",
        "summary": "实现完成",
        "claims": [{"content": "parse_kv 通过全部测试", "confidence": 0.9}],
        "evidence": ["artifact-1"],
        "artifacts": ["artifact-1"],
        "proposed_tasks": [],
        "used_knowledge": [],
        "risks": [],
        "cost": {"tool_calls": 2},
    }
    envelope = ResultEnvelope.from_json(raw)
    assert envelope.outcome is ResultOutcome.CANDIDATE
    assert envelope.claims[0].status is ClaimStatus.PROPOSED
    assert ResultEnvelope.from_json(envelope.to_json()) == envelope
    assert len(envelope.result_hash) == 64
    # §13 spelling accepted only as the sole id key
    alias = {**raw}
    alias["result_id"] = alias.pop("id")
    assert ResultEnvelope.from_json(alias).id == "result-1"
    with pytest.raises(ContractError):
        ResultEnvelope.from_json({**raw, "result_id": "other"})
    with pytest.raises(ContractError):
        ResultEnvelope.from_json({**raw, "extra": 1})
    with pytest.raises(ContractError):  # an Agent may not self-declare VERIFIED
        ResultEnvelope.from_json(
            {**raw, "claims": [{"content": "x", "confidence": 1, "status": "VERIFIED"}]}
        )
    with pytest.raises(ContractError):
        ResultEnvelope.from_json({**raw, "outcome": "done"})
    with pytest.raises(ContractError):
        ClaimProposal(content="x", confidence=1.5)


def test_attempt_and_event_round_trip():
    attempt = Attempt(
        id="mission-abc:task-1:attempt-1",
        task_id="mission-abc:task-1",
        mission_id="mission-abc",
        role="worker",
        model="agent-model",
        prompt_version="worker-v1",
        context_version="ctx-v1",
        budget_reserved=Budget(max_tokens=1000),
        lease_owner=None,
        lease_expires_at=None,
        status=AttemptStatus.PENDING,
        retry_of=None,
        created_at=1.0,
        version=1,
        ordinal=1,
        creation_key="mission-abc:task-1:attempt-1",
        input_id="attempt-input",
    )
    assert Attempt.from_json(attempt.to_json()) == attempt
    event = Event(
        id="event-1",
        type="MissionCreated",
        trace_id="trace-1",
        mission_id="mission-abc",
        task_id=None,
        attempt_id=None,
        actor_type="system",
        actor_id="orchestrator",
        payload={"goal": "x"},
        idempotency_key="MissionCreated:mission-abc",
        created_at=1.0,
    )
    assert Event.from_json(event.to_json()) == event


def test_state_machines_admit_exactly_the_drawn_transitions():
    assert_task_transition(TaskStatus.BLOCKED, TaskStatus.READY)
    assert_task_transition(TaskStatus.VERIFYING, TaskStatus.ACTIVE)
    with pytest.raises(IllegalTransition):
        assert_task_transition(TaskStatus.COMPLETED, TaskStatus.ACTIVE)
    with pytest.raises(IllegalTransition):
        assert_task_transition(TaskStatus.READY, TaskStatus.COMPLETED)
    assert_attempt_transition(AttemptStatus.PENDING, AttemptStatus.CLAIMED)
    assert_attempt_transition(AttemptStatus.RUNNING, AttemptStatus.LOST)
    with pytest.raises(IllegalTransition):
        assert_attempt_transition(AttemptStatus.PENDING, AttemptStatus.COMPLETED)
    with pytest.raises(IllegalTransition):
        assert_attempt_transition(AttemptStatus.COMPLETED, AttemptStatus.RUNNING)
    assert_claim_transition(ClaimStatus.PROPOSED, ClaimStatus.UNDER_REVIEW)
    assert_claim_transition(ClaimStatus.VERIFIED, ClaimStatus.SUPERSEDED)
    with pytest.raises(IllegalTransition):
        assert_claim_transition(ClaimStatus.PROPOSED, ClaimStatus.VERIFIED)


def test_ids_are_deterministic():
    assert ids.mission_id("t1", "k1") == ids.mission_id("t1", "k1")
    assert ids.mission_id("t1", "k1") != ids.mission_id("t2", "k1")
    assert ids.task_id("mission-x", 1) == "mission-x:task-1"
    assert ids.attempt_id("mission-x:task-1", 2) == "mission-x:task-1:attempt-2"
    assert ids.commit_id({"a": 1}, 3) == ids.commit_id({"a": 1}, 3)
