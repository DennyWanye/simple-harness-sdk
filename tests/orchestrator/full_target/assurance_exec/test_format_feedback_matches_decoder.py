# SPDX-License-Identifier: Apache-2.0
"""The format-repair hint states the decoder's own limits (real run 2026-09-28).

mission-655daf8071519553: one assessment reason was 2069 characters; the repair
round said only "TEXT_INVALID", the reviewer sent the same reply back and the
Mission failed. The hint now names the rule; this pins it to the decoder.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.assurance.checks import ReviewReply
from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.orchestrator.assurance_review_transport import _FORMAT_FEEDBACK


def _reply(reason: str) -> dict:
    return {
        "schema_version": 2,
        "verdict": "ACCEPT",
        "assessments": [{"criterion_id": "c-1", "verdict": "PASS", "evidence_ids": ["ev-1"],
                         "reason": reason, "limitations": []}],
        "findings": [],
    }


def test_the_reason_limit_in_the_hint_is_the_decoders():
    assert "1 to 2000 characters" in _FORMAT_FEEDBACK["TEXT_INVALID"]
    ReviewReply.from_json(_reply("x" * 2000))
    with pytest.raises(AssuranceError) as caught:
        ReviewReply.from_json(_reply("x" * 2001))
    assert caught.value.code == "TEXT_INVALID"


def test_every_hinted_code_is_one_the_decoder_raises():
    from pathlib import Path

    import agent_orchestrator.assurance.checks as checks
    import agent_orchestrator.assurance.codec as codec

    source = Path(codec.__file__).read_text() + Path(checks.__file__).read_text()
    for code in _FORMAT_FEEDBACK:
        assert f'"{code}"' in source, code


def _exhausted_store(payloads):
    import json
    from types import SimpleNamespace

    rows = [(json.dumps(p),) for p in payloads]
    return SimpleNamespace(connection=SimpleNamespace(
        execute=lambda sql, params: SimpleNamespace(fetchall=lambda: list(rows))))


def _orch(payloads):
    from types import SimpleNamespace

    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    fake = SimpleNamespace(store=_exhausted_store(payloads))
    fake._exhausted_reviews = Orchestrator._exhausted_reviews.__get__(fake)
    return fake


def test_a_stop_after_an_unreadable_final_review_says_so():
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    content = {"review_key": "assurance-content:x", "reason": "R"}
    final = {"review_key": "assurance-mission-final:k", "reason": "REVIEW_FORMAT_REPAIR_EXHAUSTED"}
    assert Orchestrator._final_review_unreadable_detail(_orch([content, final]), "m1") == {
        "final_review": {"reason": "REVIEW_FORMAT_REPAIR_EXHAUSTED",
                         "review_key": "assurance-mission-final:k"}}
    assert Orchestrator._final_review_unreadable_detail(_orch([content]), "m1") == {}


def test_an_operation_outcome_review_that_ran_out_is_not_a_legal_wait(monkeypatch):
    """2026-09-29 真机第七局：一份发布的结果审阅两次都没做成（第二次被重启打断），任务一直
    挂在"等发布结果"上。重试用完后不再算合法等待，交给卡死检测停下，并在停止说明里写明。"""
    from types import SimpleNamespace

    import agent_orchestrator.orchestrator.scoped_content_review as scoped
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    outcome = {"review_key": "assurance-operation-outcome:a9", "reason": "REVIEW_TURN_RETRY_EXHAUSTED"}
    fake = _orch([outcome])
    monkeypatch.setattr(scoped, "uses_completion_protocol", lambda store, mission_id: True)
    assert Orchestrator._has_pending_operation_completion(fake, SimpleNamespace(id="m1")) is False
    assert Orchestrator._final_review_unreadable_detail(fake, "m1") == {
        "operation_outcome_review": outcome}
