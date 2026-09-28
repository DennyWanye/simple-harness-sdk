# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-28 用户决定：只有模型自己做错才扣任务次数。

真机第四、五局：12 次尝试里大半耗在格式、服务出错、执行卡住上，任务因"次数用完"失败。
格式、服务、打断三类失败退还次数（账本与 task.attempt_count 一起退），同一步合计上限 6 次。
"""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import Budget
from agent_orchestrator.orchestrator.commit_service import (
    CommitService,
    MissionSpec,
    NonModelFailuresExhausted,
    Reservation,
    TaskProposal,
    mission_account,
    task_account,
)
from agent_orchestrator.orchestrator.failure_classes import (
    FORMAT,
    INFRA,
    INTERRUPTED,
    INTERRUPTED_REVIEW,
    MODEL,
    NON_MODEL_FAILURE_CAP,
    classify_failure,
)
from agent_orchestrator.storage.store import Store

SPEC = MissionSpec(
    goal="写 wordfreq.py",
    success_criteria=("pytest:tests/test_wordfreq.py",),
    tenant_id="tenant-a",
    idempotency_key="mission-charge",
    allowed_tools=("workspace_read_file", "workspace_write_file", "run_tests"),
    budget=Budget(max_tokens=1_000_000, max_attempts=12, max_cost_micros=None),
)
PROPOSAL = TaskProposal(
    goal="写 wordfreq.py",
    rationale="唯一任务",
    success_criteria=("tests/test_wordfreq.py 通过",),
    verification_policy=("format_check", "rule_check", "code_test"),
    allowed_tools=("workspace_read_file", "workspace_write_file", "run_tests"),
    budget=Budget(max_tokens=900_000, max_attempts=12),
)


def test_the_table_says_whose_fault_each_real_failure_is():
    turn = lambda **error: {"reason": "turn_failed", **error}  # noqa: E731
    assert classify_failure(turn(error={"error_code": "provider_protocol_error",
                                        "source_kind": "tool_parse"}, error_kind="provider_error")) == INFRA
    assert classify_failure(turn(error={"error_code": "x"}, error_kind="provider_unavailable")) == INFRA
    assert classify_failure(turn(error={"error_code": "react_max_turns_exceeded",
                                        "source_kind": "termination"}, error_kind="other")) == MODEL
    assert classify_failure(turn(error={"error_code": "react_max_tool_calls_exceeded"})) == MODEL
    # 2026-09-29 真机第八局：停机/重启期间单轮时限走完，重启后报墙钟超时——被打断，不是模型做错
    assert classify_failure(turn(error={"error_code": "react_wall_clock_exceeded",
                                        "source_kind": "termination"}, error_kind="other")) == INTERRUPTED
    # 调了不存在的工具、参数不合规定：同样标着 tool_parse，但属于模型的错
    assert classify_failure(turn(error={"error_code": "tool_not_exposed", "source_kind": "tool_parse"})) == MODEL
    assert classify_failure(turn(error={"error_code": "invalid_tool_arguments",
                                        "source_kind": "tool_parse"}, error_kind="other")) == MODEL
    assert classify_failure({"reason": "envelope_invalid", "error": "block_missing"}) == FORMAT
    assert classify_failure({"reason": "executor_stalled", "stalled_seconds": 600}) == INTERRUPTED
    assert classify_failure({"reason": "provider_outcome_unknown"}) == INTERRUPTED
    assert classify_failure({"reason": "runtime_unavailable"}) == INFRA
    interrupted = {"layer": "critic_review", "status": "ERROR",
                   "summary": "critic verdict unusable: " + INTERRUPTED_REVIEW}
    assert classify_failure({"reason": "verification_failed", "failures": [interrupted]}) == INTERRUPTED
    assert classify_failure({"reason": "verification_failed", "failures": [
        interrupted, {"layer": "code_test", "status": "FAIL", "summary": "1 failed"}]}) == MODEL
    assert classify_failure({"reason": "verification_failed", "failures": [
        {"layer": "rule_check", "status": "FAIL", "summary": "x"}]}) == MODEL
    # 不在表里的一律按模型做错：宁可多扣，不能漏扣
    assert classify_failure({"reason": "model_echo_mismatch"}) == MODEL
    assert classify_failure({"reason": "something_new"}) == MODEL
    assert classify_failure(None) == MODEL


def _setup(tmp_path):
    service = CommitService(Store.open(tmp_path / "orchestrator.db"))
    mission, _ = service.create_mission(SPEC)
    planning = service.begin_planning(mission.id)
    task, _ = service.commit_task_proposal(mission.id, PROPOSAL, base_version=planning.version, source={})
    return service, mission, task


def _running(service, task, n):
    attempt, intent = service.create_attempt(
        task.id, role="worker", model="m", prompt_version="v", context_version="c",
        reservation=Reservation(tokens=1_000, cost_micros=0), intent_config={}, input_hash=f"h{n}")
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id=f"agent-{n}", expected_turn_id=f"turn-{n}")
    service.record_submitted(intent.intent_id, receipt={"turn_id": f"turn-{n}", "seq": 1})
    return service.store.get_attempt(attempt.id)


def _counts(service, mission, task):
    with service.store.transaction():
        return (service.ledger.account(task_account(task.id)).attempts_created,
                service.ledger.account(mission_account(mission.id)).attempts_created,
                service.store.get_task(task.id).attempt_count)


def test_a_service_error_gives_the_attempt_back_and_a_model_mistake_does_not(tmp_path):
    service, mission, task = _setup(tmp_path)
    first = _running(service, task, 1)
    assert _counts(service, mission, task) == (1, 1, 1)
    service.reject_result(first.id, turn_id="turn-1", reason="turn_failed", detail={
        "error": {"error_code": "provider_protocol_error", "source_kind": "tool_parse"},
        "error_kind": "provider_error"})
    assert _counts(service, mission, task) == (0, 0, 0)  # 账本与任务次数一起退
    assert service.store.count_events(mission.id, "AttemptChargeReleased") == 1
    # 按尝试幂等：同一尝试再退一次不会多退
    with service.store.transaction():
        assert service._release_attempt_charge(service.store.get_attempt(first.id)) is False
    assert _counts(service, mission, task) == (0, 0, 0)
    assert service.store.count_events(mission.id, "AttemptChargeReleased") == 1

    second = _running(service, task, 2)
    service.reject_result(second.id, turn_id="turn-2", reason="turn_failed", detail={
        "error": {"error_code": "react_max_turns_exceeded", "source_kind": "termination"},
        "error_kind": "other"})
    assert _counts(service, mission, task) == (1, 1, 1)  # 模型原地打转：扣
    assert second.ordinal == 2  # 次数退了，尝试编号照常递增，不会撞号

    third = _running(service, task, 3)
    service.mark_attempt_timed_out(third.id, reason="executor_stalled", detail={"stalled_seconds": 900})
    assert _counts(service, mission, task) == (1, 1, 1)


def test_a_step_stops_after_six_failures_that_were_not_the_models_fault(tmp_path):
    service, mission, task = _setup(tmp_path)
    for n in range(1, NON_MODEL_FAILURE_CAP + 1):
        attempt = _running(service, task, n)
        service.reject_result(attempt.id, turn_id=f"turn-{n}", reason="envelope_invalid",
                              detail={"error": "block_missing"})
    assert _counts(service, mission, task) == (0, 0, 0)
    with pytest.raises(NonModelFailuresExhausted) as raised:
        _running(service, task, NON_MODEL_FAILURE_CAP + 1)
    assert raised.value.failure_count == NON_MODEL_FAILURE_CAP
    assert len(service.store.list_attempts(task.id)) == NON_MODEL_FAILURE_CAP  # 什么都没写
