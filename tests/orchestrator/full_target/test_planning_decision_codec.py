# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-C red tests: the strict PlanningDecision codec (V2 §42, task book C.2).

The codec sits between the model's raw reply and the contract object.  It is a
pure function: it calls the *existing* ``runtime.output_blocks.extract_block``
for the single tagged block, refuses a reply that also carries a legacy or
method block, refuses unknown envelope keys, refuses a model-written system
field in the envelope or in a payload's own keys, decodes through the contract's
``PlanningDecisionEnvelopeV1.from_json``, and canonicalises for the hash.

Every numbered case of the task book's C.2 table has a test here, plus the
explicit contract-error -> rejection-code mapping table the slice requires: the
only mapped condition is an unknown ``decision_type`` (-> ``DECISION_TYPE_UNKNOWN``)
and *everything else* the contract layer refuses falls back to
``MALFORMED_DECISION``.  Nothing here touches the store, the database or
``parse_plan_proposal``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    PLANNING_DECISION_CODEC_VERSION,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionRejectionCode,
    compute_decision_id,
)
from agent_orchestrator.planning.decision_codec import (
    LEGACY_PLAN_BLOCK_TAG,
    METHOD_BLOCK_TAG,
    PLANNING_DECISION_CODEC_V1,
    PLANNING_DECISION_TAG,
    SYSTEM_FIELD_KEYS,
    PlanningDecisionCodecError,
    allocate_decision_id,
    canonical_decision_json,
    decision_payload_hash,
    hash_raw_output,
    parse_planning_decision,
    serialize_planning_decision,
)
from agent_orchestrator.planning.htn.registry import MethodProposal
from agent_orchestrator.planning.htn.seed_methods.loader import fill_content_hashes
from agent_orchestrator.planning.planner import parse_plan_proposal
from agent_orchestrator.runtime.output_blocks import BlockError

HASH_A = "a" * 64
HASH_B = "b" * 64

REJECTION = PlanningDecisionRejectionCode
PROPOSAL_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "htn" / "proposals" / "valid.json"
)

#: V2 §32, transcribed as a literal.  The codec's ``SYSTEM_FIELD_KEYS`` must equal
#: exactly this set, and every one of these keys must be refused at each of the
#: three structural scan positions — a missing key would otherwise slip through to
#: a plain UNKNOWN_FIELD (or, worse, a successful decode).
SECTION_32_SYSTEM_FIELDS = (
    "mission_id",
    "tenant_id",
    "principal",
    "principal_id",
    "scope",
    "scope_id",
    "manager_epoch",
    "budget_account",
    "budget_grant_revision",
    "registry_status",
    "opened_by",
    "authorization_ref",
    "grant_ref",
    "provenance",
    "authored_by",
    "dispatch_generation",
    "plan_revision",
    "expected_plan_revision",
    "operation_id",
    "acceptance_id",
    "approval_id",
    "decision_id",
    "request_id",
)


# --------------------------------------------------------------------------------------
# Builders: one small valid envelope of each shape the codec must accept.
# --------------------------------------------------------------------------------------


def _ref(
    kind: str = "method",
    ident: str = "code.fix-by-patch",
    rev: int = 2,
    digest: str = HASH_A,
) -> dict:
    return {"kind": kind, "id": ident, "semantic_revision": rev, "content_hash": digest}


def _refine_payload(bindings: dict | None = None) -> dict:
    if bindings is None:
        bindings = {"target": "src/app.py"}
    return {"method_ref": _ref(), "bindings": bindings}


def _envelope(decision_type: str = "REFINE", payload: Any = None, **overrides: Any) -> dict:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "decision_type": decision_type,
        "subject_key": "subject-root",
        "rationale": "选择一个已注册且当前可适用的方法。",
        "reason_refs": [],
        "assumptions": [],
        "payload": _refine_payload() if payload is None else payload,
        "uncertainties": [],
        "alternatives": [],
        "replan_triggers": [],
    }
    raw.update(overrides)
    return raw


def _block(raw: Any) -> str:
    body = json.dumps(raw, ensure_ascii=False)
    return f"<{PLANNING_DECISION_TAG}>{body}</{PLANNING_DECISION_TAG}>"


def _code_of(text: str, **kwargs: Any) -> PlanningDecisionRejectionCode:
    with pytest.raises(PlanningDecisionCodecError) as caught:
        parse_planning_decision(text, **kwargs)
    return caught.value.code


def _valid_method_proposal_json() -> dict:
    """The registry's own valid proposal fixture, with its content hashes filled in."""

    payload = json.loads(PROPOSAL_FIXTURE.read_text(encoding="utf-8"))
    filled = fill_content_hashes(payload)
    MethodProposal.from_json(filled)  # the fixture really is decodable
    return filled


# --------------------------------------------------------------------------------------
# C-R1, C-R2: the happy path, prose tolerance and the fenced body.
# --------------------------------------------------------------------------------------


def test_the_module_constants_are_the_protocol_literals() -> None:
    assert PLANNING_DECISION_TAG == "planning_decision"
    assert PLANNING_DECISION_CODEC_V1 == "planning-decision-codec-v1"
    assert PLANNING_DECISION_CODEC_V1 == PLANNING_DECISION_CODEC_VERSION
    assert LEGACY_PLAN_BLOCK_TAG == "plan_revision_proposal"
    assert METHOD_BLOCK_TAG == "method_proposal"


def test_a_valid_refine_block_round_trips_through_serialize() -> None:
    # C-R1: parse -> serialize -> parse; prose *outside* the block is tolerated.
    raw = _envelope()
    text = f"模型先说了两句散文。\n{_block(raw)}\n再补一句解释。\n"

    decision = parse_planning_decision(text)
    assert isinstance(decision, PlanningDecisionEnvelopeV1)
    assert decision.to_json() == raw

    serialized = serialize_planning_decision(decision)
    body = canonical_decision_json(decision)
    expected = f"<{PLANNING_DECISION_TAG}>{body}</{PLANNING_DECISION_TAG}>"
    assert serialized == expected
    assert parse_planning_decision(serialized) == decision


def test_a_fenced_json_block_is_accepted() -> None:
    # C-R2: extract_block strips a ```json fence the model wrote inside the block.
    body = "```json\n" + json.dumps(_envelope(), ensure_ascii=False) + "\n```"
    text = f"<{PLANNING_DECISION_TAG}>\n{body}\n</{PLANNING_DECISION_TAG}>"

    assert parse_planning_decision(text).to_json() == _envelope()


# --------------------------------------------------------------------------------------
# C-N1 .. C-N5: block shape and JSON decode.
# --------------------------------------------------------------------------------------


def test_no_block_at_all_is_a_missing_decision() -> None:
    assert _code_of("这是一段没有任何块的散文。") == REJECTION.DECISION_BLOCK_MISSING


def test_empty_output_is_a_missing_decision() -> None:
    assert _code_of("   ") == REJECTION.DECISION_BLOCK_MISSING


def test_two_decision_blocks_are_multiple_decisions() -> None:
    # C-N2: two <planning_decision> blocks -> MULTIPLE_DECISIONS, never a merge.
    text = _block(_envelope()) + "\n" + _block(_envelope())
    assert _code_of(text) == REJECTION.MULTIPLE_DECISIONS


def test_a_legacy_block_beside_the_decision_is_mixed_protocol_blocks() -> None:
    # C-N3: extract_block succeeds but the reply also carries the old tag.
    legacy = f"<{LEGACY_PLAN_BLOCK_TAG}>{{}}</{LEGACY_PLAN_BLOCK_TAG}>"
    assert _code_of(_block(_envelope()) + legacy) == REJECTION.MIXED_PROTOCOL_BLOCKS


def test_a_method_block_beside_the_decision_is_mixed_protocol_blocks() -> None:
    # C-N4: the planner answered with the synthesizer's block as well.
    method = f"<{METHOD_BLOCK_TAG}>{{}}</{METHOD_BLOCK_TAG}>"
    assert _code_of(_block(_envelope()) + method) == REJECTION.MIXED_PROTOCOL_BLOCKS


def test_invalid_json_is_malformed() -> None:
    # C-N5a: the block body is not JSON.
    text = f"<{PLANNING_DECISION_TAG}>{{this is not json}}</{PLANNING_DECISION_TAG}>"
    assert _code_of(text) == REJECTION.MALFORMED_DECISION


def test_a_non_object_block_is_malformed() -> None:
    # C-N5b: JSON, but a list instead of an object.
    text = f"<{PLANNING_DECISION_TAG}>[1, 2, 3]</{PLANNING_DECISION_TAG}>"
    assert _code_of(text) == REJECTION.MALFORMED_DECISION


def test_the_block_error_table_covers_the_closed_reason_set() -> None:
    # `extract_block` (H1-C does not modify it) can only raise these five reasons,
    # and every one of them must be named in the codec's mapping table — otherwise
    # a real refusal would silently ride the fallback code.
    from agent_orchestrator.planning import decision_codec

    assert set(decision_codec._BLOCK_ERROR_CODES) == {
        "empty_output",
        "block_missing",
        "block_ambiguous",
        "invalid_json",
        "not_an_object",
    }


def test_an_unmapped_block_error_falls_back_to_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    # C.1 lists the mappings `extract_block` can produce; the `.get(..., MALFORMED)`
    # fallback is the guard for any reason it grows later.  Pin it directly: a
    # fallback that became MULTIPLE_DECISIONS (or any other code) is a wrong repair
    # hint for an unclassifiable block, so it must be MALFORMED_DECISION.
    from agent_orchestrator.planning import decision_codec

    def boom(text: str, tag: str) -> dict:
        raise BlockError("a_reason_extract_block_might_grow_later", "synthetic")

    monkeypatch.setattr(decision_codec, "extract_block", boom)
    with pytest.raises(PlanningDecisionCodecError) as caught:
        parse_planning_decision("anything")
    assert caught.value.code == REJECTION.MALFORMED_DECISION


# --------------------------------------------------------------------------------------
# C-N6, C-N7, C-N8: unknown keys and the structural system-field guard.
# --------------------------------------------------------------------------------------


def test_an_unknown_top_level_key_is_unknown_field() -> None:
    # C-N6: a key that is neither a Core field nor a system field.
    assert _code_of(_block(_envelope(model_says_approved=True))) == REJECTION.UNKNOWN_FIELD


def test_the_system_field_keys_are_exactly_section_32() -> None:
    # §32's 23 keys, pinned as a literal so deleting one (e.g. `request_id`) is red
    # rather than a set that quietly shrinks to 22.
    assert set(SYSTEM_FIELD_KEYS) == set(SECTION_32_SYSTEM_FIELDS)
    assert len(SYSTEM_FIELD_KEYS) == 23


@pytest.mark.parametrize("key", SECTION_32_SYSTEM_FIELDS)
def test_a_top_level_system_field_is_model_set_system_field(key: str) -> None:
    # C-N7: every §32 key written at the envelope top level is refused as a system
    # claim, never silently downgraded to a plain UNKNOWN_FIELD.
    text = _block(_envelope(**{key: "model-chose-this"}))
    assert _code_of(text) == REJECTION.MODEL_SET_SYSTEM_FIELD


@pytest.mark.parametrize("key", SECTION_32_SYSTEM_FIELDS)
def test_a_payload_structural_system_field_is_model_set_system_field(key: str) -> None:
    # C-N7: the scan also covers the payload object's own keys (all 23 keys).
    payload = _refine_payload()
    payload[key] = "model-chose-this"
    text = _block(_envelope(payload=payload))
    assert _code_of(text) == REJECTION.MODEL_SET_SYSTEM_FIELD


@pytest.mark.parametrize("key", SECTION_32_SYSTEM_FIELDS)
def test_a_system_field_inside_a_reason_ref_is_model_set_system_field(key: str) -> None:
    # C-N7: the third structural scan position is each reason_refs[] entry.
    ref = _ref("task", "t-1")
    ref[key] = "model-chose-this"
    text = _block(_envelope(reason_refs=[ref]))
    assert _code_of(text) == REJECTION.MODEL_SET_SYSTEM_FIELD


def test_a_system_field_in_a_later_reason_ref_is_still_refused() -> None:
    # C-N7: every entry of reason_refs[] is scanned, not just the first one.
    # ref[0] is clean, so a scan that stops at index 0 would let ref[1]'s
    # `plan_revision` through to the contract layer and mislabel the refusal.
    clean = _ref("task", "t-1", 1, HASH_A)
    tainted = _ref("obligation", "o-1", 1, HASH_B)
    tainted["plan_revision"] = 4
    text = _block(_envelope(reason_refs=[clean, tainted]))
    assert _code_of(text) == REJECTION.MODEL_SET_SYSTEM_FIELD


def test_a_method_parameter_that_shares_a_name_with_a_bound_field_is_a_value() -> None:
    # C-N8: `bindings.scope` is a domain parameter, never scanned (§32).
    payload = _refine_payload(bindings={"scope": "domain-value", "mission_id": "not-a-claim"})
    decision = parse_planning_decision(_block(_envelope(payload=payload)))
    assert decision.payload.bindings == {"scope": "domain-value", "mission_id": "not-a-claim"}


# --------------------------------------------------------------------------------------
# C-N9, C-N10, C-N11, C-N12: the old tag, the old parser, bad refs, bad type.
# --------------------------------------------------------------------------------------


def test_the_old_block_alone_is_a_missing_decision_not_an_envelope() -> None:
    # C-N9: only the legacy tag -> block_missing, never auto-decoded as an envelope.
    legacy = f"<{LEGACY_PLAN_BLOCK_TAG}>{{}}</{LEGACY_PLAN_BLOCK_TAG}>"
    assert _code_of(legacy) == REJECTION.DECISION_BLOCK_MISSING


def test_the_old_parser_still_reports_block_missing_for_the_new_block() -> None:
    # C-N10: parse_plan_proposal is untouched and still says block_missing.
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(_block(_envelope()), mission_id="mission-1")
    assert "block_missing" in str(caught.value)


def test_a_fact_reason_ref_is_malformed() -> None:
    # C-N11: there is no `fact` kind; the contract layer refuses it.
    text = _block(_envelope(reason_refs=[_ref("fact", "f-1", 1, HASH_A)]))
    assert _code_of(text) == REJECTION.MALFORMED_DECISION


def test_an_unknown_decision_type_is_decision_type_unknown() -> None:
    # C-N12: SELECT_METHOD was removed before H1.
    text = _block(_envelope(decision_type="SELECT_METHOD"))
    assert _code_of(text) == REJECTION.DECISION_TYPE_UNKNOWN


def test_a_non_string_decision_type_is_malformed() -> None:
    # A wrong *type* is not an unknown value: it falls back to MALFORMED_DECISION.
    assert _code_of(_block(_envelope(decision_type=7))) == REJECTION.MALFORMED_DECISION


def test_a_missing_decision_type_is_malformed() -> None:
    raw = _envelope()
    del raw["decision_type"]
    assert _code_of(_block(raw)) == REJECTION.MALFORMED_DECISION


# --------------------------------------------------------------------------------------
# The contract-error -> rejection-code mapping table, one test per row.
# --------------------------------------------------------------------------------------


def test_the_mapping_table_only_names_decision_type_unknown_plus_a_fallback() -> None:
    from agent_orchestrator.planning import decision_codec

    rows = decision_codec.CONTRACT_ERROR_CODE_MAPPINGS
    assert [row.code for row in rows] == [
        REJECTION.DECISION_TYPE_UNKNOWN,
        REJECTION.MALFORMED_DECISION,
    ]
    assert rows[-1].matches is None  # the fallback matches everything else


@pytest.mark.parametrize(
    "raw_mutation",
    [
        pytest.param(lambda raw: raw.pop("rationale"), id="missing-rationale"),
        pytest.param(lambda raw: raw.update(schema_version=2), id="wrong-schema-version"),
        pytest.param(
            lambda raw: raw.update(payload={"wait_for": [], "reason": "x"}),
            id="payload-mismatch",
        ),
        pytest.param(
            lambda raw: raw.update(payload={"method_ref": _ref(), "bindings": {}, "bogus": 1}),
            id="unknown-payload-key",
        ),
        pytest.param(lambda raw: raw.update(reason_refs=[_ref("fact", "f-1")]), id="fact-ref"),
    ],
)
def test_every_other_contract_error_falls_back_to_malformed(raw_mutation: Any) -> None:
    raw = _envelope()
    raw_mutation(raw)
    assert _code_of(_block(raw)) == REJECTION.MALFORMED_DECISION


# --------------------------------------------------------------------------------------
# The three decode-only types: parse must succeed and must NOT raise
# DECISION_NOT_ENABLED_IN_PHASE (that decision belongs to admission / H1-F).
# --------------------------------------------------------------------------------------


DECODE_ONLY_PAYLOADS: dict[str, Any] = {
    "REQUEST_EVIDENCE": {
        "questions": [
            {
                "predicate_key": "pred.x",
                "arguments": {"k": 1},
                "purpose": "confirm",
                "blocking": True,
            }
        ]
    },
    "REQUEST_HUMAN": {
        "question": "which?",
        "options": [{"key": "a", "label": "A"}],
        "blocking": False,
    },
}
DECODE_ONLY_PAYLOADS["PROPOSE_METHOD"] = {"method_proposal": _valid_method_proposal_json()}


@pytest.mark.parametrize(("decision_type", "payload"), DECODE_ONLY_PAYLOADS.items())
def test_a_decode_only_type_parses_and_is_not_phase_refused(
    decision_type: str, payload: Any
) -> None:
    decision = parse_planning_decision(_block(_envelope(decision_type, payload)))
    assert decision.decision_type == decision_type


def test_a_propose_method_whose_inner_proposal_is_unreadable_is_malformed() -> None:
    # Addendum-2 §三: the inner method_proposal goes through MethodProposal.from_json.
    payload = {"method_proposal": {"id": "m1", "goal_signature": "sig", "steps": [{"name": "do"}]}}
    assert _code_of(_block(_envelope("PROPOSE_METHOD", payload))) == REJECTION.MALFORMED_DECISION


# --------------------------------------------------------------------------------------
# C-ID, C-ID2, C-HASH: the id formula, the raw hash and canonical stability.
# --------------------------------------------------------------------------------------


def test_allocate_decision_id_delegates_to_the_contract_formula() -> None:
    # §35: one formula only; the codec must not re-implement the digest.
    assert allocate_decision_id(
        request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_A
    ) == compute_decision_id("req-1", 0, HASH_A)


def test_the_same_request_ordinal_and_raw_yield_the_same_id() -> None:
    # C-ID: idempotent replay.
    first = allocate_decision_id(request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_A)
    second = allocate_decision_id(request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_A)
    assert first == second


def test_a_different_raw_yields_a_different_id() -> None:
    # C-ID2: same request+ordinal, different raw output -> a different id.
    first = allocate_decision_id(request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_A)
    second = allocate_decision_id(request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_B)
    assert first != second


def test_the_ordinal_participates_in_the_id() -> None:
    first = allocate_decision_id(request_id="req-1", attempt_ordinal=0, raw_output_hash=HASH_A)
    second = allocate_decision_id(request_id="req-1", attempt_ordinal=1, raw_output_hash=HASH_A)
    assert first != second


def test_hash_raw_output_is_the_sha256_of_the_bytes() -> None:
    # §15: raw_output_hash = sha256(raw bytes).
    payload = "模型原始响应".encode()
    assert hash_raw_output(payload) == hashlib.sha256(payload).hexdigest()
    assert len(hash_raw_output(payload)) == 64


def test_decision_payload_hash_is_the_sha256_of_the_canonical_json() -> None:
    # §15: the model does not submit a hash; the codec derives it.
    decision = parse_planning_decision(_block(_envelope()))
    expected = hashlib.sha256(canonical_decision_json(decision).encode("utf-8")).hexdigest()
    assert decision_payload_hash(decision) == expected
    assert len(decision_payload_hash(decision)) == 64


def test_object_key_order_does_not_change_the_hash_but_array_order_does() -> None:
    # C-HASH: object key order is canonicalised away; array order is preserved.
    first = _envelope()
    reordered = {key: first[key] for key in reversed(list(first))}
    reordered["payload"] = {
        "bindings": first["payload"]["bindings"],
        "method_ref": first["payload"]["method_ref"],
    }

    a = parse_planning_decision(_block(first))
    b = parse_planning_decision(_block(reordered))
    assert a.to_json() == b.to_json()
    assert decision_payload_hash(a) == decision_payload_hash(b)

    refs = [_ref("task", "t-1", 1, HASH_A), _ref("obligation", "o-1", 1, HASH_B)]
    forward = parse_planning_decision(_block(_envelope(reason_refs=refs)))
    backward = parse_planning_decision(_block(_envelope(reason_refs=list(reversed(refs)))))
    assert decision_payload_hash(forward) != decision_payload_hash(backward)


# --------------------------------------------------------------------------------------
# Misc: the error type, and the reserved caller bindings on the pure parse.
# --------------------------------------------------------------------------------------


def test_the_error_is_a_contract_error_and_carries_its_code() -> None:
    with pytest.raises(PlanningDecisionCodecError) as caught:
        parse_planning_decision("prose only")
    error = caught.value
    assert isinstance(error, ContractError)
    assert error.code is REJECTION.DECISION_BLOCK_MISSING


def test_parse_ignores_the_reserved_caller_bindings() -> None:
    # The request id / ordinal / raw hash belong to the caller (H1-F); the decodes
    # themselves are pure and must not depend on them.
    decision = parse_planning_decision(
        _block(_envelope()),
        request_id="req-1",
        attempt_ordinal=3,
        raw_output_hash=HASH_A,
    )
    assert decision.to_json() == _envelope()
