# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""User decision 2026-09-26: a planning-model reply's unknown keys are dropped.

Seventh desktop run of the book-club Mission: the method synthesiser answered twice
with an otherwise valid proposal plus ``rationale`` inside the method and then
``review_feedback_note`` at the top, both were refused, and the Mission failed in
planning.  Review replies and Worker claims stay strict (not decoded here).
"""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.unknown_fields import decode_dropping_unknown


def _strict(value):
    """A stand-in contract with the codec's own refusal wording."""
    allowed = {"method": {"steps"}, "top": {"method", "rationale"}}
    extra = sorted(set(value) - allowed["top"])
    if extra:
        raise ContractError(f"method_proposal has unknown fields: {extra}")
    extra = sorted(set(value["method"]) - allowed["method"])
    if extra:
        raise ContractError(f"method_proposal.method has unknown fields: {extra}")
    for i, step in enumerate(value["method"]["steps"]):
        extra = sorted(set(step) - {"id"})
        if extra:
            raise ContractError(f"method_proposal.method.steps[{i}] has unknown fields: {extra}")
    return value


def test_unknown_keys_are_dropped_where_the_refusal_names_them() -> None:
    raw = {"method": {"steps": [{"id": "a"}, {"id": "b", "note": "x"}], "rationale": "why"},
           "rationale": "kept: the top level names it", "review_feedback_note": "fixed"}
    decoded = decode_dropping_unknown(_strict, raw, root_names=("method_proposal",))
    assert decoded == {"method": {"steps": [{"id": "a"}, {"id": "b"}]}, "rationale": "kept: the top level names it"}
    assert raw["review_feedback_note"] == "fixed"  # the caller's raw reply is not mutated


def test_other_refusals_stay_refusals() -> None:
    with pytest.raises(ContractError, match="must be"):
        decode_dropping_unknown(lambda value: (_ for _ in ()).throw(ContractError("x must be a string")),
                                {}, root_names=("method_proposal",))


def test_an_ambiguous_unnamed_holder_is_not_guessed() -> None:
    def refuse(value):
        if any("rationale" in item for item in value["items"]):
            raise ContractError("item has unknown fields: ['rationale']")
        return value

    with pytest.raises(ContractError, match="unknown fields"):
        decode_dropping_unknown(refuse, {"items": [{"rationale": 1}, {"rationale": 2}]}, root_names=("list",))
    assert decode_dropping_unknown(refuse, {"items": [{"rationale": 1}, {}]}, root_names=("list",)) == {"items": [{}, {}]}


def test_the_real_method_proposal_codec_drops_the_synthesiser_extras() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "htn"))
    from htn_world import load_proposal

    from agent_orchestrator.planning.planner import parse_method_proposal
    from agent_orchestrator.testing.fixtures import method_proposal_step

    method = dict(load_proposal("valid")["method"])
    clean = parse_method_proposal(method_proposal_step(method))
    # The seventh run's two replies: ``rationale`` inside the method, then
    # ``review_feedback_note`` beside it.  Both decode to the same method.
    noisy = parse_method_proposal(method_proposal_step(
        {**method, "rationale": "为什么这样拆"}, extras={"review_feedback_note": "已按反馈修改"}))
    assert noisy.method == clean.method and noisy.rationale == clean.rationale
    # A declared status is a known key and is still kept for admission to refuse.
    claimed = parse_method_proposal(method_proposal_step(method, extras={"registry_status": "ADMITTED"}))
    assert str(claimed.declared_status) == "ADMITTED"
    # Nothing is invented: a proposal without its required method is still refused.
    with pytest.raises(ContractError, match="missing required fields"):
        parse_method_proposal("<method_proposal>{\"review_feedback_note\": \"x\"}</method_proposal>")
