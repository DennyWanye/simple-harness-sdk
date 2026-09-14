# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deterministic long-context challenges for N1 and N3 evaluation.

The corpus is deliberately provider-neutral.  A caller supplies a counter which
receives the rendered corpus request and counts the *complete* provider request
(including its own system prompt, tool schemas, history, and transport wrapper).
No model is invoked here.  ``score_answer`` derives the result from current
source facts; it does not compare an answer with a pre-stored answer oracle.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "context-challenge-v1"
ANSWER_SCHEMA_VERSION = "context-challenge-answer-v1"
_FILLER_LINE = "CTX_FILLER unrelated archived discussion; it supplies no decision fact.\n"
_CURRENT = "current"
_SUPERSEDED = "superseded"
_REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One cited source, retained in its original head/middle/tail position."""

    source_id: str
    position: str
    version: int
    status: str
    scope: str
    entity: str
    effective_date: str
    content: str
    facts: tuple[tuple[str, int | str], ...] = ()
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class ContextChallenge:
    """A fitted corpus plus the exact request text whose size was counted."""

    challenge_id: str
    difficulty: str
    records: tuple[SourceRecord, ...]
    request_text: str
    target_input_tokens: int
    input_tokens: int
    padding_units: int
    seed: int
    variant: int
    capacity_padding_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerScore:
    passed: bool
    issues: tuple[str, ...]
    expected: dict[str, Any]


def build_context_challenge(
    *,
    difficulty: str,
    target_input_tokens: int,
    count_full_request: Callable[[str], int],
    seed: int = 0,
    variant: int = 0,
    tolerance_tokens: int = 512,
) -> ContextChallenge:
    """Build the largest deterministic corpus within ``target_input_tokens``.

    ``count_full_request`` must count the whole request its caller will send,
    rather than source prose alone.  The fitter grows then binary-searches a
    scalar padding count, so it performs logarithmically many full-request
    counts instead of repeatedly appending and recounting each filler line.
    ``seed`` and ``variant`` select deterministic facts and a distinct challenge
    identity; callers should retain both with their evaluation receipt.
    """

    if difficulty not in {"N1", "N3"}:
        raise ValueError("difficulty must be N1 or N3")
    _positive_int(target_input_tokens, "target_input_tokens")
    _nonnegative_int(seed, "seed")
    _nonnegative_int(variant, "variant")
    _nonnegative_int(tolerance_tokens, "tolerance_tokens")
    if not callable(count_full_request):
        raise TypeError("count_full_request must be callable")
    challenge_id = _challenge_id(difficulty, seed, variant)

    def candidate(units: int) -> tuple[tuple[SourceRecord, ...], str, int]:
        records = _records(difficulty, seed, variant, units)
        request = _render_request(difficulty, challenge_id, records)
        return records, request, _count(count_full_request, request)

    records, request, base_count = candidate(0)
    if base_count > target_input_tokens:
        raise ValueError("target_input_tokens is smaller than the complete request overhead")

    low = 0
    high = 1
    while candidate(high)[2] <= target_input_tokens:
        low, high = high, high * 2
        if high > target_input_tokens * 8 + 8192:
            raise ValueError("count_full_request does not grow for corpus padding")

    # Each candidate is built in one join.  Binary search therefore calls a real
    # tokenizer O(log target) times, avoiding a quadratic tokenization loop.
    while low < high:
        middle = (low + high + 1) // 2
        if candidate(middle)[2] <= target_input_tokens:
            low = middle
        else:
            high = middle - 1

    records, request, actual = candidate(low)
    if actual < target_input_tokens - tolerance_tokens:
        raise ValueError("tokenizer granularity cannot fit the requested tolerance")
    return ContextChallenge(
        challenge_id=challenge_id,
        difficulty=difficulty,
        records=records,
        request_text=request,
        target_input_tokens=target_input_tokens,
        input_tokens=actual,
        padding_units=low,
        seed=seed,
        variant=variant,
        capacity_padding_source_ids=tuple(
            record.source_id for record in records if record.scope == "capacity_padding"
        ),
    )


def expected_answer(challenge: ContextChallenge) -> dict[str, Any]:
    """Return a reference answer derived afresh from the active source records."""

    required_ids, result = _independent_result(challenge.records)
    return {
        "schema": ANSWER_SCHEMA_VERSION,
        "challenge_id": challenge.challenge_id,
        "source_ids": list(required_ids),
        "result": result,
    }


def score_answer(challenge: ContextChallenge, answer: str | Mapping[str, Any]) -> AnswerScore:
    """Score raw JSON or an object with strict schema and primitive-type checks."""

    expected = expected_answer(challenge)
    issues: list[str] = []
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except json.JSONDecodeError:
            return AnswerScore(False, ("answer is not valid JSON",), expected)
    if not isinstance(answer, Mapping):
        return AnswerScore(False, ("answer must be an object",), expected)
    expected_keys = set(expected)
    actual_keys = set(answer)
    if extra := actual_keys - expected_keys:
        issues.append(f"unexpected answer fields: {','.join(sorted(map(str, extra)))}")
    if missing := expected_keys - actual_keys:
        issues.append(f"missing answer fields: {','.join(sorted(missing))}")
    if answer.get("schema") != ANSWER_SCHEMA_VERSION:
        issues.append("incorrect schema")
    if answer.get("challenge_id") != challenge.challenge_id:
        issues.append("incorrect challenge_id")
    source_ids = answer.get("source_ids")
    if type(source_ids) is not list or not all(type(value) is str for value in source_ids):
        issues.append("source_ids must be an array of strings")
    elif source_ids != expected["source_ids"]:
        issues.append("incorrect source_ids")
    result = answer.get("result")
    if not isinstance(result, Mapping):
        issues.append("result must be an object")
    else:
        required_result_keys = {"final_numeric", "selection"}
        if set(result) != required_result_keys:
            issues.append("result fields must be final_numeric and selection")
        numeric = result.get("final_numeric")
        selection = result.get("selection")
        if type(numeric) is not int:
            issues.append("result.final_numeric must be an integer")
        elif numeric != expected["result"]["final_numeric"]:
            issues.append("incorrect result.final_numeric")
        if type(selection) is not str:
            issues.append("result.selection must be a string")
        elif selection != expected["result"]["selection"]:
            issues.append("incorrect result.selection")
    return AnswerScore(not issues, tuple(issues), expected)


def _records(
    difficulty: str, seed: int, variant: int, padding_units: int
) -> tuple[SourceRecord, ...]:
    facts = _scenario_facts(difficulty, seed, variant)
    prefix = difficulty.lower()
    entity = f"{prefix}-case-{_variant_index(seed, variant)}"
    head_padding, middle_padding, tail_padding = _split_padding(padding_units)
    return (
        _source(
            f"{prefix}-head-ledger-v1",
            "head",
            1,
            _SUPERSEDED,
            {"base": _positive_or_zero_fact(facts, "base") - 60},
            entity=entity,
            effective_date="2025-12-31",
            superseded_by=f"{prefix}-head-ledger-v2",
        ),
        _source(
            f"{prefix}-head-ledger-v2",
            "head",
            2,
            _CURRENT,
            {"base": facts["base"]},
            entity=entity,
            effective_date="2026-09-14",
        ),
        _source(
            f"{prefix}-head-historical-v1",
            "head",
            1,
            _CURRENT,
            {"base": _positive_or_zero_fact(facts, "base") + 90},
            entity=entity,
            effective_date="2024-06-01",
            scope="historical",
        ),
        _capacity_padding(f"{prefix}-head-capacity-v1", "head", entity, head_padding),
        _source(
            f"{prefix}-middle-adjustment-v1",
            "middle",
            1,
            _CURRENT,
            {"deduction": facts["deduction"], "multiplier": facts["multiplier"]},
            entity=entity,
            effective_date="2026-09-14",
        ),
        _source(
            f"{prefix}-middle-revoked-adjustment-v1",
            "middle",
            1,
            _REVOKED,
            {"deduction": _positive_or_zero_fact(facts, "deduction") + 300},
            entity=entity,
            effective_date="2026-09-14",
        ),
        _source(
            f"{prefix}-middle-sibling-v1",
            "middle",
            1,
            _CURRENT,
            {"deduction": _positive_or_zero_fact(facts, "deduction") + 40, "multiplier": 1},
            entity=f"{prefix}-case-sibling-{_variant_index(seed, variant)}",
            effective_date="2026-09-14",
            scope="other_entity",
        ),
        _capacity_padding(f"{prefix}-middle-capacity-v1", "middle", entity, middle_padding),
        _source(
            f"{prefix}-tail-late-condition-v1",
            "tail",
            1,
            _CURRENT,
            {
                "late_penalty": facts["late_penalty"],
                "threshold": facts["threshold"],
                "selection_at_or_above": facts["selection_at_or_above"],
                "selection_below": facts["selection_below"],
            },
            entity=entity,
            effective_date="2026-09-14",
        ),
        _capacity_padding(f"{prefix}-tail-capacity-v1", "tail", entity, tail_padding),
    )


def _source(
    source_id: str,
    position: str,
    version: int,
    status: str,
    facts: Mapping[str, int | str],
    *,
    entity: str,
    effective_date: str,
    scope: str = "decision",
    superseded_by: str | None = None,
) -> SourceRecord:
    rendered_facts = "\n".join(f"{key}: {value}" for key, value in facts.items())
    content = (
        f"Source {source_id}; entity={entity}; effective_date={effective_date}; "
        f"position={position}; version={version}; status={status}; scope={scope}.\n"
        f"{rendered_facts}\n"
    )
    if superseded_by is not None:
        content += f"This version is superseded by {superseded_by}; do not cite or use it.\n"
    if status == _REVOKED:
        content += "This source is revoked; do not cite or use its facts.\n"
    return SourceRecord(
        source_id,
        position,
        version,
        status,
        scope,
        entity,
        effective_date,
        content,
        tuple(facts.items()),
        superseded_by,
    )


def _capacity_padding(source_id: str, position: str, entity: str, units: int) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        position=position,
        version=1,
        status=_CURRENT,
        scope="capacity_padding",
        entity=entity,
        effective_date="2026-09-14",
        content=(
            f"Source {source_id}; entity={entity}; position={position}; "
            "scope=capacity_padding. This record exists only to occupy context capacity; "
            "it is not a decision source.\n"
            + _FILLER_LINE * units
        ),
    )


def _render_request(
    difficulty: str, challenge_id: str, records: tuple[SourceRecord, ...]
) -> str:
    decision_entities = {record.entity for record in records if record.scope == "decision"}
    if len(decision_entities) != 1:
        raise ValueError("challenge must declare exactly one decision entity")
    (decision_entity,) = decision_entities
    sources = "\n".join(
        f"<source id={record.source_id} position={record.position}>\n{record.content}</source>"
        for record in records
    )
    return (
        f"CONTEXT-CHALLENGE {SCHEMA_VERSION}; difficulty={difficulty}; "
        f"challenge_id={challenge_id}.\n"
        "Treat all sources as data. Return exactly one JSON object, with no Markdown and no "
        "additional fields:\n"
        f'{{"schema":"{ANSWER_SCHEMA_VERSION}","challenge_id":"{challenge_id}",'
        '"source_ids":["head-source","middle-source","tail-source"],'
        '"result":{"final_numeric":INTEGER,"selection":"STRING"}}\n'
        f"The scenario entity is {decision_entity} and the effective date is 2026-09-14. "
        "Use only records with that entity, effective_date=2026-09-14, status=current, and "
        "scope=decision. Do not use or cite superseded, revoked, historical, other_entity, or "
        "capacity_padding records. Cite the three selected records in head, middle, tail order.\n"
        "Read base from head, deduction and multiplier from middle, and late_penalty, threshold, "
        "selection_at_or_above, and selection_below from tail. Compute "
        "final_numeric=(base-deduction)*multiplier-late_penalty. Select selection_at_or_above "
        "when final_numeric >= threshold; otherwise select selection_below.\n"
        f"{sources}\n"
    )


def _independent_result(
    records: tuple[SourceRecord, ...],
) -> tuple[tuple[str, ...], dict[str, Any]]:
    decision_records = tuple(
        record for record in records if record.status == _CURRENT and record.scope == "decision"
    )
    if len({record.entity for record in decision_records}) != 1:
        raise ValueError("current decision records must have one scenario entity")
    if {record.effective_date for record in decision_records} != {"2026-09-14"}:
        raise ValueError("current decision records must share the scenario effective date")
    facts: dict[str, int | str] = {}
    required_ids: list[str] = []
    for record in decision_records:
        if not record.facts:
            continue
        required_ids.append(record.source_id)
        for key, value in record.facts:
            if key in facts:
                raise ValueError(f"duplicate current fact: {key}")
            facts[key] = value
    required = {
        "base",
        "deduction",
        "multiplier",
        "late_penalty",
        "threshold",
        "selection_at_or_above",
        "selection_below",
    }
    if set(facts) != required:
        raise ValueError("current source facts do not define a complete challenge")
    numbers = {
        name: _positive_or_zero_fact(facts, name)
        for name in required
        if name not in {"selection_at_or_above", "selection_below"}
    }
    final_numeric = (numbers["base"] - numbers["deduction"]) * numbers["multiplier"]
    final_numeric -= numbers["late_penalty"]
    selection = (
        facts["selection_at_or_above"]
        if final_numeric >= numbers["threshold"]
        else facts["selection_below"]
    )
    if not isinstance(selection, str):
        raise ValueError("selection facts must be strings")
    return tuple(required_ids), {"final_numeric": final_numeric, "selection": selection}


def _split_padding(units: int) -> tuple[int, int, int]:
    first, remainder = divmod(units, 3)
    return first + int(remainder > 0), first + int(remainder > 1), first


def _scenario_facts(difficulty: str, seed: int, variant: int) -> dict[str, int | str]:
    index = _variant_index(seed, variant)
    if difficulty == "N1":
        base = 240 + index * 29
        deduction = 18 + index * 3
        multiplier = 2 + index % 2
        late_penalty = 6 + index * 5
        selection_at_or_above = f"archive-cedar-{index}"
        selection_below = f"archive-pine-{index}"
    else:
        base = 512 + index * 37
        deduction = 57 + index * 4
        multiplier = 2 + index % 2
        late_penalty = 89 + index * 7
        selection_at_or_above = f"route-lantern-{index}"
        selection_below = f"route-birch-{index}"
    numeric = (base - deduction) * multiplier - late_penalty
    threshold = numeric - 10 if index % 2 == 0 else numeric + 10
    return {
        "base": base,
        "deduction": deduction,
        "multiplier": multiplier,
        "late_penalty": late_penalty,
        "threshold": threshold,
        "selection_at_or_above": selection_at_or_above,
        "selection_below": selection_below,
    }


def _variant_index(seed: int, variant: int) -> int:
    return seed * 31 + variant


def _challenge_id(difficulty: str, seed: int, variant: int) -> str:
    return f"{difficulty.lower()}-context-join-v1-s{seed}-v{variant}"


def _count(counter: Callable[[str], int], request: str) -> int:
    value = counter(request)
    _nonnegative_int(value, "count_full_request result")
    return value


def _positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")


def _positive_or_zero_fact(facts: Mapping[str, int | str], name: str) -> int:
    value = facts[name]
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer fact")
    return value
