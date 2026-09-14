# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""N1/N3 deterministic long-context corpus and independent-score coverage."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agent_orchestrator.evaluation.context_challenge import (
    ANSWER_SCHEMA_VERSION,
    build_context_challenge,
    expected_answer,
    score_answer,
)


def _full_request_counter(request_text: str) -> int:
    """A stand-in for a caller that counts its complete request wrapper too."""

    assert request_text.startswith("CONTEXT-CHALLENGE context-challenge-v1")
    return 97 + request_text.count("CTX_FILLER")


@pytest.mark.parametrize(
    ("difficulty", "numeric", "selection"),
    (("N1", 438, "archive-cedar-0"), ("N3", 821, "route-lantern-0")),
)
def test_current_head_middle_tail_join_has_a_structured_independently_scored_answer(
    difficulty, numeric, selection
):
    challenge = build_context_challenge(
        difficulty=difficulty,
        target_input_tokens=1_500,
        count_full_request=_full_request_counter,
    )

    answer = expected_answer(challenge)
    scored = score_answer(challenge, answer)

    assert challenge.input_tokens == 1_500
    assert answer["schema"] == ANSWER_SCHEMA_VERSION
    assert answer["source_ids"] == [
        f"{difficulty.lower()}-head-ledger-v2",
        f"{difficulty.lower()}-middle-adjustment-v1",
        f"{difficulty.lower()}-tail-late-condition-v1",
    ]
    assert answer["result"] == {"final_numeric": numeric, "selection": selection}
    assert scored.passed and scored.issues == ()
    assert "scope=capacity_padding" in challenge.request_text
    assert len(challenge.capacity_padding_source_ids) == 3
    assert '"result":{"final_numeric":INTEGER,"selection":"STRING"}' in challenge.request_text
    assert "Cite the three selected records in head, middle, tail order." in challenge.request_text


@pytest.mark.parametrize(
    ("difficulty", "target_input_tokens"), (("N1", 210_000), ("N3", 450_000))
)
def test_tokenizer_driven_fitting_reaches_n1_and_n3_window_scale_without_quadratic_growth(
    difficulty, target_input_tokens
):
    challenge = build_context_challenge(
        difficulty=difficulty,
        target_input_tokens=target_input_tokens,
        count_full_request=_full_request_counter,
    )

    assert challenge.input_tokens == target_input_tokens
    assert challenge.padding_units == target_input_tokens - 97
    assert challenge.request_text.count("CTX_FILLER") == challenge.padding_units


def test_n3_seed_and_variant_produce_two_distinct_deterministic_210k_requests():
    first = build_context_challenge(
        difficulty="N3",
        target_input_tokens=210_000,
        count_full_request=_full_request_counter,
        seed=7,
        variant=0,
    )
    second = build_context_challenge(
        difficulty="N3",
        target_input_tokens=210_000,
        count_full_request=_full_request_counter,
        seed=7,
        variant=1,
    )

    assert first.input_tokens == second.input_tokens == 210_000
    assert first.challenge_id != second.challenge_id
    assert expected_answer(first)["result"] != expected_answer(second)["result"]


def test_scorer_accepts_whitespace_wrapped_json_and_rejects_boolean_as_numeric():
    challenge = build_context_challenge(
        difficulty="N1", target_input_tokens=1_000, count_full_request=_full_request_counter
    )
    answer = expected_answer(challenge)

    assert score_answer(challenge, " \n" + json.dumps(answer, indent=2) + "\n ").passed
    answer["result"]["final_numeric"] = True
    scored = score_answer(challenge, json.dumps(answer))
    assert not scored.passed
    assert "result.final_numeric must be an integer" in scored.issues


@pytest.mark.parametrize(
    ("case", "mutate"),
    (
        (
            "stale-version",
            lambda answer: answer.update(
                source_ids=["n1-head-ledger-v1", *answer["source_ids"][1:]]
            ),
        ),
        (
            "omitted-late-constraint",
            lambda answer: answer["result"].update(final_numeric=444),
        ),
        (
            "wrong-citation",
            lambda answer: answer.update(
                source_ids=[
                    answer["source_ids"][0],
                    "n1-middle-revoked-adjustment-v1",
                    answer["source_ids"][2],
                ]
            ),
        ),
        ("extra-invalid-answer", lambda answer: answer.update(untrusted_note="invalid")),
    ),
)
def test_scorer_rejects_stale_or_incomplete_or_overbroad_answers(case, mutate):
    challenge = build_context_challenge(
        difficulty="N1", target_input_tokens=1_000, count_full_request=_full_request_counter
    )
    answer = deepcopy(expected_answer(challenge))
    mutate(answer)

    scored = score_answer(challenge, answer)

    assert not scored.passed, case
    if case == "extra-invalid-answer":
        assert scored.issues == ("unexpected answer fields: untrusted_note",)
    else:
        assert scored.issues


def test_counter_must_cover_complete_request_and_fitter_rejects_a_non_counting_counter():
    calls: list[str] = []

    def complete_counter(request_text: str) -> int:
        calls.append(request_text)
        return 250 + request_text.count("CTX_FILLER")

    challenge = build_context_challenge(
        difficulty="N1", target_input_tokens=750, count_full_request=complete_counter
    )
    assert challenge.input_tokens == 750
    assert all("<source id=" in request for request in calls)

    with pytest.raises(ValueError, match="does not grow"):
        build_context_challenge(
            difficulty="N1", target_input_tokens=750, count_full_request=lambda _: 250
        )
