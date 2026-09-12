# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""B oracle：新增 citation 严格解析，空 citation 不改变旧 code 信封及 hash。"""

from dataclasses import FrozenInstanceError

import pytest

from agent_orchestrator.contracts import (
    CONTRACT_SCHEMA_VERSION,
    ClaimProposal,
    ContractError,
    ResultEnvelope,
    SourceCitation,
    sha256_hex,
)
from simple_harness.contracts import canonical_json


def citation_json(**changes):
    return {
        "path": "sources/notes.md",
        "version": "a" * 64,
        "start_line": 2,
        "end_line": 4,
        "quote": "来源记载。",
        **changes,
    }


def legacy_envelope():
    # v1 已序列化形状，不使用新增实现来生成 expected。
    return {
        "id": "result-1",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "outcome": "candidate",
        "summary": "code result",
        "claims": [
            {
                "content": "tested",
                "confidence": 0.8,
                "status": "PROPOSED",
                "type": "statement",
                "evidence": ["pytest:tests/test_x.py"],
                "key": None,
                "stance": "affirms",
                "supersedes": None,
                "contradicts": [],
            }
        ],
        "evidence": ["pytest:tests/test_x.py"],
        "artifacts": ["x.py"],
        "proposed_tasks": [],
        "used_knowledge": [],
        "risks": [],
        "cost": {},
    }


def test_empty_citations_preserve_legacy_envelope_bytes_and_hash():
    expected = legacy_envelope()
    envelope = ResultEnvelope.from_json(expected)
    assert CONTRACT_SCHEMA_VERSION == 3
    assert envelope.claims[0].citations == ()
    assert canonical_json(envelope.to_json()) == canonical_json(expected)
    assert envelope.result_hash == sha256_hex(expected)
    explicit_empty = legacy_envelope()
    explicit_empty["claims"][0]["citations"] = []
    assert ResultEnvelope.from_json(explicit_empty).to_json() == expected


def test_citations_round_trip_and_change_result_hash():
    expected = legacy_envelope()
    before = ResultEnvelope.from_json(expected).result_hash
    expected["claims"][0]["citations"] = [citation_json()]
    result = ResultEnvelope.from_json(expected)
    assert result.claims[0].citations == (SourceCitation.from_json(citation_json()),)
    assert result.to_json() == expected
    assert ResultEnvelope.from_json(result.to_json()) == result
    assert result.result_hash != before
    assert result.claims[0].status.value == "PROPOSED"


@pytest.mark.parametrize("field", ["path", "version", "start_line", "end_line", "quote"])
def test_citation_requires_every_field(field):
    raw = citation_json()
    raw.pop(field)
    with pytest.raises(ContractError):
        SourceCitation.from_json(raw)


@pytest.mark.parametrize("field", ["start_line", "end_line"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_citation_line_numbers_are_integers_not_bools(field, value):
    with pytest.raises(ContractError):
        SourceCitation.from_json(citation_json(**{field: value}))


@pytest.mark.parametrize("start,end", [(0, 1), (-1, 3), (4, 2), (1, 10**9)])
def test_integer_line_range_is_left_to_resolver(start, end):
    citation = SourceCitation.from_json(citation_json(start_line=start, end_line=end))
    assert (citation.start_line, citation.end_line) == (start, end)


def test_unknown_fields_are_rejected_at_both_levels():
    with pytest.raises(ContractError, match="unknown"):
        SourceCitation.from_json(citation_json(verified=True))
    with pytest.raises(ContractError, match="unknown"):
        ClaimProposal.from_json({"content": "x", "confidence": 1, "citation": citation_json()})
    with pytest.raises(ContractError, match="unknown"):
        ClaimProposal.from_json(
            {
                "content": "x",
                "confidence": 1,
                "citations": [citation_json(trust="trusted")],
            }
        )


@pytest.mark.parametrize("citations", [None, "", {}, ["not an object"]])
def test_citation_collection_is_strict(citations):
    with pytest.raises(ContractError):
        ClaimProposal.from_json({"content": "x", "confidence": 1, "citations": citations})


def test_citation_is_immutable_and_constructor_does_not_accept_untyped_items():
    citation = SourceCitation.from_json(citation_json())
    with pytest.raises(FrozenInstanceError):
        citation.quote = "changed"
    with pytest.raises(ContractError):
        ClaimProposal(content="x", confidence=1, citations=[citation_json()])
    items = [citation]
    claim = ClaimProposal(content="x", confidence=1, citations=items)
    items.clear()
    assert claim.citations == (citation,)


@pytest.mark.parametrize("field", ["path", "version", "quote"])
def test_citation_text_fields_do_not_coerce_other_types(field):
    with pytest.raises(ContractError):
        SourceCitation.from_json(citation_json(**{field: 123}))
