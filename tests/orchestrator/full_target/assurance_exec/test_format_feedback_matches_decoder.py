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


def test_a_stop_after_an_unreadable_final_review_says_so():
    from types import SimpleNamespace

    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    events = [
        SimpleNamespace(type="AssuranceReviewFormatExhausted",
                        payload={"review_key": "assurance-content:x", "reason": "R"}),
        SimpleNamespace(type="AssuranceReviewFormatExhausted",
                        payload={"review_key": "assurance-mission-final:k",
                                 "reason": "REVIEW_FORMAT_REPAIR_EXHAUSTED"}),
    ]
    fake = SimpleNamespace(store=SimpleNamespace(iter_events=lambda mission_id: iter(events)))
    assert Orchestrator._final_review_unreadable_detail(fake, "m1") == {
        "final_review": {"reason": "REVIEW_FORMAT_REPAIR_EXHAUSTED",
                         "review_key": "assurance-mission-final:k"}}
    fake = SimpleNamespace(store=SimpleNamespace(iter_events=lambda mission_id: iter(events[:1])))
    assert Orchestrator._final_review_unreadable_detail(fake, "m1") == {}
