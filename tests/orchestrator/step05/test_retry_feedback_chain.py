# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 真机文档任务：中间夹一次模型接口失败时，前面那次内容拒收的原因不能丢。"""
from types import SimpleNamespace

from agent_orchestrator.orchestrator.event_handler import retry_feedback

REJECTED = {"reason": "result_evidence_kind_not_allowed", "error": "evidence of kind 'pytest' is not allowed"}
TURN = {"reason": "turn_failed", "error": {"error_code": "provider_protocol_error"}}


def _attempt(n, failure, status="RETRY_WAIT"):
    return SimpleNamespace(id=f"a{n}", status=status, failure=failure)


def test_a_turn_failure_does_not_hide_the_earlier_content_rejection():
    attempts = [_attempt(1, REJECTED), _attempt(2, TURN)]
    feedback, verifier = retry_feedback(attempts, attempts[1])
    assert len(feedback) == 2 and feedback[0].startswith("turn_failed")
    assert "pytest" in feedback[1] and verifier == []


def test_a_content_failure_stops_the_walk_back():
    older = {"reason": "verification_failed", "failures": [{"layer": "rule_check", "summary": "old"}]}
    attempts = [_attempt(1, older), _attempt(2, REJECTED)]
    feedback, verifier = retry_feedback(attempts, attempts[1])
    assert feedback == [f"result_evidence_kind_not_allowed: {REJECTED['error']}"] and verifier == []


def test_verifier_details_still_travel_and_no_previous_means_no_feedback():
    failed = {"reason": "inconclusive", "failures": [{"layer": "rule_check", "summary": "s", "detail": {}}]}
    attempts = [_attempt(1, failed), _attempt(2, TURN), _attempt(3, TURN)]
    feedback, verifier = retry_feedback(attempts, attempts[2])
    assert feedback[-1] == "rule_check: s" and len(verifier) == 1 and len(feedback) == 3
    assert retry_feedback(attempts, None) == ([], [])
