# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 UI 全量点击：同一结果被同一原因反复拒绝验收时，不能每 2～3 秒无限重跑核验。"""
from types import SimpleNamespace

from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator


def _fake():
    failed = []
    fake = SimpleNamespace(
        _verdict_refusals={}, VERDICT_REFUSAL_LIMIT=Orchestrator.VERDICT_REFUSAL_LIMIT,
        _owner="owner", _note=lambda text: None,
        commit=SimpleNamespace(fail_result=lambda rid, failures, owner: failed.append((rid, failures))),
    )
    return fake, failed


def test_same_refusal_three_times_fails_the_result_with_the_reason():
    fake, failed = _fake()
    error = CommitRejected("doc5 acceptance requires the actual same-result Critic proof")
    for _ in range(Orchestrator.VERDICT_REFUSAL_LIMIT - 1):
        assert Orchestrator._verdict_refused(fake, "r1", error) is True
    assert failed == []
    Orchestrator._verdict_refused(fake, "r1", error)
    assert len(failed) == 1
    detail = failed[0][1][0]["detail"]
    assert detail["reason"] == "acceptance_refused" and "Critic proof" in detail["error"]
    assert "r1" not in fake._verdict_refusals


def test_a_different_reason_restarts_the_count():
    fake, failed = _fake()
    Orchestrator._verdict_refused(fake, "r1", CommitRejected("a"))
    Orchestrator._verdict_refused(fake, "r1", CommitRejected("a"))
    Orchestrator._verdict_refused(fake, "r1", CommitRejected("b"))
    assert failed == [] and fake._verdict_refusals["r1"][1] == 1
