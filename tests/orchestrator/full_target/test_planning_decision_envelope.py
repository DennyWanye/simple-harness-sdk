# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-A2a red tests: PlanningDecisionEnvelopeV1, every payload and the closed enums.

This slice adds the wire object of V2 §13-§30 plus the addendum-2 payload shapes:
the 16th ``PlanningRefKind`` member, ``VersionedTypeRefV1``, the six small closed
enums and the four sub-structures, the ten per-decision payload variants (seven
executable, three decode-only), the strict envelope codec, and the canonical
decision JSON / hash of §15.

The JSON Schema file, the golden fixture directory and the packaging work stay in
H1-A2b; nothing here reads or writes those files.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    MAX_PD_ALTERNATIVES,
    MAX_PD_ARGUMENTS,
    MAX_PD_ASSUMPTIONS,
    MAX_PD_BINDINGS,
    MAX_PD_BLOCKERS,
    MAX_PD_EVIDENCE_QUESTIONS,
    MAX_PD_HUMAN_OPTIONS,
    MAX_PD_REASON_REFS,
    MAX_PD_REPLAN_TRIGGERS,
    MAX_PD_UNCERTAINTIES,
    MAX_PD_WAIT_REFS,
    MIN_PD_EVIDENCE_QUESTIONS,
    AlternativeDisposition,
    BindExistingGoalMode,
    BlockerCode,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionType,
    PlanningRefKind,
    RefineDecision,
    RepairKind,
    RepairReplaceMethodDecision,
    RequestHumanDecision,
    ResumableIf,
    UncertaintySeverity,
    WaitDecision,
    canonical_decision_hash,
    canonical_decision_json,
)
from simple_harness.contracts import canonical_json

HASH_A = "a" * 64
HASH_B = "b" * 64

TEN_ENVELOPE_FIELDS = (
    "schema_version",
    "decision_type",
    "subject_key",
    "rationale",
    "reason_refs",
    "assumptions",
    "payload",
    "uncertainties",
    "alternatives",
    "replan_triggers",
)


def _ref(kind: str = "task", ident: str = "t-1", rev: int = 1, digest: str = HASH_A) -> dict:
    return {"kind": kind, "id": ident, "semantic_revision": rev, "content_hash": digest}


def _method_ref(ident: str = "code.fix", digest: str = HASH_A) -> dict:
    return _ref("method", ident, 1, digest)


def _instance_ref(ident: str = "mi-1") -> dict:
    return _ref("method_instance", ident, 2, HASH_A)


def _envelope(decision_type: str, payload: object, **overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "decision_type": decision_type,
        "subject_key": "subject-root",
        "rationale": "选择一个已注册且当前可适用的方法。",
        "reason_refs": [],
        "assumptions": [],
        "payload": payload,
        "uncertainties": [],
        "alternatives": [],
        "replan_triggers": [],
    }
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------------------
# Valid payloads: one canonical sample per decision type, round-tripped exactly.
# --------------------------------------------------------------------------------------

REFINE_PAYLOAD = {"method_ref": _method_ref(), "bindings": {"target": "x"}}
REPLACE_METHOD_PAYLOAD = {
    "repair_kind": "REPLACE_METHOD",
    "rejected_method_instance": _instance_ref(),
    "replacement_method_ref": _method_ref("code.alt"),
    "bindings": {},
}
PROPOSE_SUCCESSOR_PAYLOAD = {
    "repair_kind": "PROPOSE_SUCCESSOR",
    "old_task_ref": _ref("task", "t-1"),
    "obligation_ref": _ref("obligation", "o-1"),
    "goal_type_ref": {"id": "goal.type", "version": 1, "content_hash": HASH_A},
    "bindings": {},
}
BIND_REUSE_PAYLOAD = {
    "mode": "REUSE_ACCEPTED",
    "consumer_method_instance_ref": _instance_ref("mi-2"),
    "step": "inspect",
    "goal_ref": _ref("task", "g-1"),
    "resolution_ref": _ref("resolution", "r-1"),
}
BIND_SHARE_PAYLOAD = {
    "mode": "SHARE_ACTIVE",
    "consumer_method_instance_ref": _instance_ref("mi-2"),
    "step": "inspect",
    "goal_ref": _ref("task", "g-1"),
    "resolution_ref": None,
}
DECLARE_BLOCKED_PAYLOAD = {
    "blockers": [{"code": "NO_USABLE_METHOD", "detail": "no method applies"}],
    "resumable_if": ["new_method_admitted"],
}
WAIT_PAYLOAD = {"wait_for": [_ref("review", "rv-1")], "reason": "wait for dispatch"}
NO_CHANGE_PAYLOAD = {"reason": "current method still valid"}
REQUEST_EVIDENCE_PAYLOAD = {
    "questions": [
        {
            "predicate_key": "pred.x",
            "arguments": {"k": 1},
            "purpose": "confirm the fact",
            "blocking": True,
        }
    ]
}
REQUEST_HUMAN_PAYLOAD = {
    "question": "which option?",
    "options": [{"key": "a", "label": "A"}],
    "blocking": False,
}
PROPOSE_METHOD_PAYLOAD = {"method_proposal": {"id": "m1", "steps": [{"name": "do"}]}}

VALID_DECISIONS = [
    ("REFINE", REFINE_PAYLOAD),
    ("REPAIR", REPLACE_METHOD_PAYLOAD),
    ("REPAIR", PROPOSE_SUCCESSOR_PAYLOAD),
    ("BIND_EXISTING_GOAL", BIND_REUSE_PAYLOAD),
    ("BIND_EXISTING_GOAL", BIND_SHARE_PAYLOAD),
    ("DECLARE_BLOCKED", DECLARE_BLOCKED_PAYLOAD),
    ("WAIT", WAIT_PAYLOAD),
    ("NO_CHANGE", NO_CHANGE_PAYLOAD),
    ("REQUEST_EVIDENCE", REQUEST_EVIDENCE_PAYLOAD),
    ("REQUEST_HUMAN", REQUEST_HUMAN_PAYLOAD),
    ("PROPOSE_METHOD", PROPOSE_METHOD_PAYLOAD),
]


@pytest.mark.parametrize(("decision_type", "payload"), VALID_DECISIONS)
def test_valid_payload_round_trips(decision_type: str, payload: dict) -> None:
    raw = _envelope(decision_type, payload)
    envelope = PlanningDecisionEnvelopeV1.from_json(raw)
    assert envelope.to_json() == raw
    assert PlanningDecisionEnvelopeV1.from_json(envelope.to_json()) == envelope


def test_to_json_has_exactly_the_ten_envelope_fields() -> None:
    envelope = PlanningDecisionEnvelopeV1.from_json(_envelope("REFINE", REFINE_PAYLOAD))
    assert set(envelope.to_json()) == set(TEN_ENVELOPE_FIELDS)


# --------------------------------------------------------------------------------------
# The 16th ref kind and the closed small enums (§17, addendum-2 §2/§3)
# --------------------------------------------------------------------------------------


def test_evidence_question_bounds_are_pinned() -> None:
    # Addendum 2 section 3: REQUEST_EVIDENCE carries 1-8 questions.
    assert MIN_PD_EVIDENCE_QUESTIONS == 1
    assert MAX_PD_EVIDENCE_QUESTIONS == 8


def test_planning_ref_kind_now_includes_method_instance() -> None:
    values = [member.value for member in PlanningRefKind]
    assert values == [
        "task",
        "obligation",
        "method",
        "requirements",
        "artifact",
        "source",
        "observation",
        "review",
        "acceptance",
        "resolution",
        "operation",
        "tool_receipt",
        "knowledge",
        "authority",
        "capability",
        "method_instance",
    ]
    assert len(PlanningRefKind) == 16
    assert PlanningRefKind.METHOD_INSTANCE == "method_instance"


def test_uncertainty_severity_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in UncertaintySeverity] == [
        ("LOW", "LOW"),
        ("MEDIUM", "MEDIUM"),
        ("HIGH", "HIGH"),
    ]


def test_alternative_disposition_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in AlternativeDisposition] == [
        ("CONSIDERED", "CONSIDERED"),
        ("REJECTED", "REJECTED"),
        ("DEFERRED", "DEFERRED"),
    ]


def test_blocker_code_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in BlockerCode] == [
        ("NO_USABLE_METHOD", "NO_USABLE_METHOD"),
        ("EVIDENCE_INSUFFICIENT", "EVIDENCE_INSUFFICIENT"),
        ("AUTHORIZATION_MISSING", "AUTHORIZATION_MISSING"),
        ("CAPABILITY_MISSING", "CAPABILITY_MISSING"),
        ("STRUCTURE_UNSAT", "STRUCTURE_UNSAT"),
        ("OTHER", "OTHER"),
    ]


def test_resumable_if_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in ResumableIf] == [
        ("NEW_METHOD_ADMITTED", "new_method_admitted"),
        ("EVIDENCE_UPDATED", "evidence_updated"),
        ("AUTHORIZATION_GRANTED", "authorization_granted"),
        ("HUMAN_RESOLVED", "human_resolved"),
        ("PLAN_REVISION_CHANGED", "plan_revision_changed"),
    ]


def test_repair_kind_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in RepairKind] == [
        ("REPLACE_METHOD", "REPLACE_METHOD"),
        ("PROPOSE_SUCCESSOR", "PROPOSE_SUCCESSOR"),
    ]


def test_bind_existing_goal_mode_members_are_pinned() -> None:
    assert [(m.name, m.value) for m in BindExistingGoalMode] == [
        ("REUSE_ACCEPTED", "REUSE_ACCEPTED"),
        ("SHARE_ACTIVE", "SHARE_ACTIVE"),
    ]


def test_uncertainty_severity_decode_path_is_closed() -> None:
    # The member list above only pins the enum class.  The *decode* path must also
    # reject an unknown severity, so the value can never ride in as a bare string.
    raw = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        uncertainties=[{"statement": "u", "severity": "BOGUS", "affects": []}],
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_alternative_disposition_decode_path_is_closed() -> None:
    raw = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        alternatives=[
            {"method_ref": None, "label": "l", "disposition": "BOGUS", "reason": "r"}
        ],
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_assumption_risk_decode_path_is_closed() -> None:
    raw = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        assumptions=[
            {
                "key": "a",
                "statement": "s",
                "required_for": ["REFINE"],
                "risk": "BOGUS",
                "suggested_predicate_key": None,
            }
        ],
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_replan_suggested_decision_decode_path_is_closed() -> None:
    raw = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        replan_triggers=[
            {"description": "d", "referenced_predicates": [], "suggested_decision": "BOGUS"}
        ],
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


# --------------------------------------------------------------------------------------
# Envelope: ten required fields, unknown fields, and the decision-type/payload dispatch
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", TEN_ENVELOPE_FIELDS)
def test_each_envelope_field_is_required(field: str) -> None:
    raw = _envelope("REFINE", REFINE_PAYLOAD)
    del raw[field]
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_envelope_rejects_unknown_field() -> None:
    raw = _envelope("REFINE", REFINE_PAYLOAD, model_says_approved=True)
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


@pytest.mark.parametrize("schema_version", [2, 0, "1", True])
def test_schema_version_must_be_exactly_one(schema_version: object) -> None:
    raw = _envelope("REFINE", REFINE_PAYLOAD, schema_version=schema_version)
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_subject_key_bounds() -> None:
    assert (
        PlanningDecisionEnvelopeV1.from_json(
            _envelope("REFINE", REFINE_PAYLOAD, subject_key="s")
        ).subject_key
        == "s"
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(
            _envelope("REFINE", REFINE_PAYLOAD, subject_key="s" * 257)
        )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REFINE", REFINE_PAYLOAD, subject_key=""))


def test_rationale_bounds() -> None:
    assert (
        PlanningDecisionEnvelopeV1.from_json(
            _envelope("REFINE", REFINE_PAYLOAD, rationale="x" * 4000)
        ).rationale
        == "x" * 4000
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(
            _envelope("REFINE", REFINE_PAYLOAD, rationale="x" * 4001)
        )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REFINE", REFINE_PAYLOAD, rationale=""))


@pytest.mark.parametrize(
    ("decision_type", "payload"),
    [
        ("WAIT", NO_CHANGE_PAYLOAD),
        ("NO_CHANGE", WAIT_PAYLOAD),
        ("REFINE", DECLARE_BLOCKED_PAYLOAD),
        ("DECLARE_BLOCKED", REFINE_PAYLOAD),
    ],
)
def test_payload_shape_must_match_decision_type(decision_type: str, payload: dict) -> None:
    # A shape that decodes for one decision type is not accepted under another.
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope(decision_type, payload))


def test_direct_construction_rejects_a_mismatched_payload_instance() -> None:
    wait = WaitDecision.from_json(WAIT_PAYLOAD)
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1(
            schema_version=1,
            decision_type=PlanningDecisionType.REFINE,
            subject_key="subject-root",
            rationale="x",
            reason_refs=(),
            assumptions=(),
            payload=wait,
            uncertainties=(),
            alternatives=(),
            replan_triggers=(),
        )


def test_unknown_decision_type_is_rejected() -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("SELECT_METHOD", REFINE_PAYLOAD))


# --------------------------------------------------------------------------------------
# reason_refs: four-tuple dedupe and the §16 array limits (upper bound + 1)
# --------------------------------------------------------------------------------------


def test_reason_refs_duplicate_four_tuple_is_rejected() -> None:
    raw = _envelope(
        "REFINE", REFINE_PAYLOAD, reason_refs=[_ref("task", "t-1"), _ref("task", "t-1")]
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(raw)


def test_reason_refs_differing_only_in_hash_are_not_duplicates() -> None:
    raw = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        reason_refs=[_ref("task", "t-1", 1, HASH_A), _ref("task", "t-1", 1, HASH_B)],
    )
    assert len(PlanningDecisionEnvelopeV1.from_json(raw).reason_refs) == 2


def _assumptions(count: int) -> list[dict]:
    return [
        {
            "key": f"a{i}",
            "statement": "assume",
            "required_for": ["REFINE"],
            "risk": "LOW",
            "suggested_predicate_key": None,
        }
        for i in range(count)
    ]


def _alternatives(count: int) -> list[dict]:
    return [
        {"method_ref": None, "label": f"l{i}", "disposition": "CONSIDERED", "reason": "x"}
        for i in range(count)
    ]


def _uncertainties(count: int) -> list[dict]:
    return [{"statement": "u", "severity": "LOW", "affects": []} for _ in range(count)]


def _replan_triggers(count: int) -> list[dict]:
    return [
        {"description": "d", "referenced_predicates": [], "suggested_decision": "REFINE"}
        for _ in range(count)
    ]


def test_reason_refs_limit_upper_bound_plus_one() -> None:
    ok = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        reason_refs=[_ref("task", f"t{i}") for i in range(MAX_PD_REASON_REFS)],
    )
    assert len(PlanningDecisionEnvelopeV1.from_json(ok).reason_refs) == MAX_PD_REASON_REFS
    over = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        reason_refs=[_ref("task", f"t{i}") for i in range(MAX_PD_REASON_REFS + 1)],
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(over)


def test_assumptions_limit_upper_bound_plus_one() -> None:
    ok = _envelope("REFINE", REFINE_PAYLOAD, assumptions=_assumptions(MAX_PD_ASSUMPTIONS))
    assert len(PlanningDecisionEnvelopeV1.from_json(ok).assumptions) == MAX_PD_ASSUMPTIONS
    over = _envelope("REFINE", REFINE_PAYLOAD, assumptions=_assumptions(MAX_PD_ASSUMPTIONS + 1))
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(over)


def test_alternatives_limit_upper_bound_plus_one() -> None:
    ok = _envelope("REFINE", REFINE_PAYLOAD, alternatives=_alternatives(MAX_PD_ALTERNATIVES))
    assert len(PlanningDecisionEnvelopeV1.from_json(ok).alternatives) == MAX_PD_ALTERNATIVES
    over = _envelope("REFINE", REFINE_PAYLOAD, alternatives=_alternatives(MAX_PD_ALTERNATIVES + 1))
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(over)


def test_uncertainties_limit_upper_bound_plus_one() -> None:
    ok = _envelope("REFINE", REFINE_PAYLOAD, uncertainties=_uncertainties(MAX_PD_UNCERTAINTIES))
    assert len(PlanningDecisionEnvelopeV1.from_json(ok).uncertainties) == MAX_PD_UNCERTAINTIES
    over = _envelope(
        "REFINE", REFINE_PAYLOAD, uncertainties=_uncertainties(MAX_PD_UNCERTAINTIES + 1)
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(over)


def test_replan_triggers_limit_upper_bound_plus_one() -> None:
    ok = _envelope(
        "REFINE", REFINE_PAYLOAD, replan_triggers=_replan_triggers(MAX_PD_REPLAN_TRIGGERS)
    )
    assert len(PlanningDecisionEnvelopeV1.from_json(ok).replan_triggers) == MAX_PD_REPLAN_TRIGGERS
    over = _envelope(
        "REFINE", REFINE_PAYLOAD, replan_triggers=_replan_triggers(MAX_PD_REPLAN_TRIGGERS + 1)
    )
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(over)


# --------------------------------------------------------------------------------------
# Payload negative samples: at least three per decision type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"bindings": {}},  # missing method_ref
        {"method_ref": _method_ref(), "bindings": {}, "extra": 1},  # unknown field
        {"method_ref": "code.fix", "bindings": {}},  # wrong type
        {"method_ref": _method_ref(), "bindings": {f"k{i}": i for i in range(MAX_PD_BINDINGS + 1)}},
    ],
)
def test_refine_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REFINE", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "repair_kind": "PROPOSE_SUCCESSOR",
            "rejected_method_instance": _instance_ref(),
            "replacement_method_ref": _method_ref("code.alt"),
            "bindings": {},
        },
        {
            "repair_kind": "REPLACE_METHOD",
            "rejected_method_instance": {
                "id": "mi-1",
                "semantic_revision": 2,
                "content_hash": HASH_A,
            },
            "replacement_method_ref": _method_ref("code.alt"),
            "bindings": {},
        },
        {
            "repair_kind": "REPLACE_METHOD",
            "rejected_method_instance": _ref("method", "mi-1"),
            "replacement_method_ref": _method_ref("code.alt"),
            "bindings": {},
        },
        {
            "repair_kind": "REPLACE_METHOD",
            "rejected_method_instance": _instance_ref(),
            "replacement_method_ref": _method_ref("code.alt"),
            "bindings": {},
            "extra": 1,
        },
    ],
)
def test_replace_method_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REPAIR", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "repair_kind": "REPLACE_METHOD",
            "old_task_ref": _ref("task", "t-1"),
            "obligation_ref": _ref("obligation", "o-1"),
            "goal_type_ref": {"id": "goal.type", "version": 1, "content_hash": HASH_A},
            "bindings": {},
        },
        {
            "repair_kind": "PROPOSE_SUCCESSOR",
            "old_task_ref": _ref("task", "t-1"),
            "obligation_ref": _ref("obligation", "o-1"),
            "goal_type_ref": {"id": "goal.type", "version": 0, "content_hash": HASH_A},
            "bindings": {},
        },
        {
            "repair_kind": "PROPOSE_SUCCESSOR",
            "old_task_ref": _ref("task", "t-1"),
            "obligation_ref": _ref("obligation", "o-1"),
            "goal_type_ref": {"id": "goal.type", "version": 1, "content_hash": HASH_A.upper()},
            "bindings": {},
        },
    ],
)
def test_propose_successor_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REPAIR", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "SHARE_ACTIVE",
            "consumer_method_instance_ref": _instance_ref("mi-2"),
            "step": "inspect",
            "goal_ref": _ref("task", "g-1"),
            "resolution_ref": _ref("resolution", "r-1"),
        },
        {
            "mode": "REUSE_ACCEPTED",
            "consumer_method_instance_ref": _instance_ref("mi-2"),
            "step": "inspect",
            "goal_ref": _ref("task", "g-1"),
            "resolution_ref": None,
        },
        {
            "mode": "FORCE_IT",
            "consumer_method_instance_ref": _instance_ref("mi-2"),
            "step": "inspect",
            "goal_ref": _ref("task", "g-1"),
            "resolution_ref": None,
        },
        {
            "mode": "SHARE_ACTIVE",
            "consumer_method_instance_ref": _ref("method", "mi-2"),
            "step": "inspect",
            "goal_ref": _ref("task", "g-1"),
            "resolution_ref": None,
        },
    ],
)
def test_bind_existing_goal_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("BIND_EXISTING_GOAL", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"blockers": [{"code": "OTHER"}], "resumable_if": []},
        {"blockers": [{"code": "NOT_A_CODE", "detail": "x"}], "resumable_if": []},
        {"blockers": [{"code": "NO_USABLE_METHOD", "detail": "x"}], "resumable_if": ["nope"]},
        {
            "blockers": [{"code": "NO_USABLE_METHOD", "detail": "x"}] * (MAX_PD_BLOCKERS + 1),
            "resumable_if": [],
        },
    ],
)
def test_declare_blocked_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("DECLARE_BLOCKED", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "wait_for": [_ref("review", f"rv{i}") for i in range(MAX_PD_WAIT_REFS + 1)],
            "reason": "x",
        },
        {"wait_for": [], "reason": "   "},
        {"wait_for": ["not-a-ref"], "reason": "x"},
    ],
)
def test_wait_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("WAIT", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": ""},
        {"reason": 123},
        {"reason": "x", "extra": 1},
    ],
)
def test_no_change_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("NO_CHANGE", payload))


def _question(blocking: bool = True) -> dict:
    return {"predicate_key": "p", "arguments": {}, "purpose": "why", "blocking": blocking}


@pytest.mark.parametrize(
    "payload",
    [
        {"questions": []},
        {"questions": [_question() for _ in range(9)]},
        {"questions": [{"arguments": {}, "purpose": "why", "blocking": True}]},
        {
            "questions": [
                {"predicate_key": "p", "arguments": {}, "purpose": "why", "blocking": True, "x": 1}
            ]
        },
        {
            "questions": [
                {
                    "predicate_key": "p",
                    "arguments": {f"k{i}": i for i in range(MAX_PD_ARGUMENTS + 1)},
                    "purpose": "why",
                    "blocking": True,
                }
            ]
        },
    ],
)
def test_request_evidence_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REQUEST_EVIDENCE", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "question": "q",
            "options": [{"key": "a", "label": "A"}] * (MAX_PD_HUMAN_OPTIONS + 1),
            "blocking": False,
        },
        {"question": "q", "options": [{"key": "a"}], "blocking": False},
        {"question": "q", "options": [], "blocking": "no"},
        {"question": "q", "options": [], "blocking": False, "extra": 1},
    ],
)
def test_request_human_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("REQUEST_HUMAN", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"method_proposal": ["not", "an", "object"]},
        {},
        {"method_proposal": {}, "extra": 1},
    ],
)
def test_propose_method_payload_negatives(payload: dict) -> None:
    with pytest.raises(ContractError):
        PlanningDecisionEnvelopeV1.from_json(_envelope("PROPOSE_METHOD", payload))


# --------------------------------------------------------------------------------------
# Canonical JSON / hash: key order is irrelevant, array order matters (§15)
# --------------------------------------------------------------------------------------


# A fixed, fully specified sample and the exact bytes / digest §15 requires.  The
# values are pinned literally so no mutation of the canonicalisation (dropping
# ``sort_keys``, hashing something other than the canonical bytes) can slide past.
CANONICAL_SAMPLE = {
    "schema_version": 1,
    "decision_type": "REFINE",
    "subject_key": "subject-root",
    "rationale": "选择已注册且当前可适用的方法。",
    "reason_refs": [{"kind": "task", "id": "t-1", "semantic_revision": 1, "content_hash": HASH_A}],
    "assumptions": [],
    "payload": {
        "method_ref": {
            "kind": "method",
            "id": "code.fix",
            "semantic_revision": 2,
            "content_hash": HASH_A,
        },
        "bindings": {"target": "x"},
    },
    "uncertainties": [],
    "alternatives": [],
    "replan_triggers": [],
}
CANONICAL_SAMPLE_JSON = (
    '{"alternatives":[],"assumptions":[],"decision_type":"REFINE",'
    '"payload":{"bindings":{"target":"x"},"method_ref":{"content_hash":"' + HASH_A + '",'
    '"id":"code.fix","kind":"method","semantic_revision":2}},'
    '"rationale":"选择已注册且当前可适用的方法。",'
    '"reason_refs":[{"content_hash":"' + HASH_A + '","id":"t-1","kind":"task",'
    '"semantic_revision":1}],"replan_triggers":[],"schema_version":1,'
    '"subject_key":"subject-root","uncertainties":[]}'
)
CANONICAL_SAMPLE_HASH = "18bb6817f7117b20de4c9da5d9f9411f542cef5d37188f89adcd3e2ac8d5751f"


def _independent_canonical_json(value: object) -> str:
    # Deliberately not the module helper: sorted keys, compact separators, no
    # whitespace, exactly what §15 defines.
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def test_canonical_decision_json_matches_an_independent_canonicalisation() -> None:
    envelope = PlanningDecisionEnvelopeV1.from_json(CANONICAL_SAMPLE)
    assert canonical_decision_json(envelope) == _independent_canonical_json(envelope.to_json())
    # §15: the module helper must agree with the shared contract canonical_json,
    # which does not escape non-ASCII characters (the sample's rationale is CJK).
    assert canonical_decision_json(envelope) == canonical_json(envelope.to_json())


def test_canonical_decision_json_and_hash_are_pinned_literals() -> None:
    envelope = PlanningDecisionEnvelopeV1.from_json(CANONICAL_SAMPLE)
    assert canonical_decision_json(envelope) == CANONICAL_SAMPLE_JSON
    assert canonical_decision_hash(envelope) == CANONICAL_SAMPLE_HASH
    assert (
        hashlib.sha256(CANONICAL_SAMPLE_JSON.encode("utf-8")).hexdigest() == CANONICAL_SAMPLE_HASH
    )


def test_canonical_json_and_hash_ignore_object_key_order() -> None:
    left = _envelope("REFINE", REFINE_PAYLOAD, assumptions=_assumptions(1))
    # Same content, different top-level and nested insertion order.
    reordered = dict(reversed(list(left.items())))
    reordered["payload"] = {"bindings": {"target": "x"}, "method_ref": _method_ref()}
    right = reordered
    e1 = PlanningDecisionEnvelopeV1.from_json(left)
    e2 = PlanningDecisionEnvelopeV1.from_json(right)
    assert canonical_decision_json(e1) == canonical_decision_json(e2)
    assert canonical_decision_hash(e1) == canonical_decision_hash(e2)
    # A real digest, not a placeholder or empty string.
    assert len(canonical_decision_hash(e1)) == 64


def test_canonical_hash_depends_on_array_order() -> None:
    forward = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        reason_refs=[_ref("task", "t-1", 1, HASH_A), _ref("task", "t-1", 1, HASH_B)],
    )
    backward = _envelope(
        "REFINE",
        REFINE_PAYLOAD,
        reason_refs=[_ref("task", "t-1", 1, HASH_B), _ref("task", "t-1", 1, HASH_A)],
    )
    e1 = PlanningDecisionEnvelopeV1.from_json(forward)
    e2 = PlanningDecisionEnvelopeV1.from_json(backward)
    assert canonical_decision_json(e1) != canonical_decision_json(e2)
    assert canonical_decision_hash(e1) != canonical_decision_hash(e2)


def test_canonical_decision_hash_is_a_lowercase_sha256() -> None:
    envelope = PlanningDecisionEnvelopeV1.from_json(_envelope("REFINE", REFINE_PAYLOAD))
    digest = canonical_decision_hash(envelope)
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


# --------------------------------------------------------------------------------------
# Direct payload codec spot checks (constructed, not only decoded through the envelope)
# --------------------------------------------------------------------------------------


def test_refine_decision_direct_round_trip_uses_planning_ref() -> None:
    payload = RefineDecision.from_json(REFINE_PAYLOAD)
    assert payload.to_json() == REFINE_PAYLOAD


def test_replace_method_decision_direct_round_trip() -> None:
    payload = RepairReplaceMethodDecision.from_json(REPLACE_METHOD_PAYLOAD)
    assert payload.to_json() == REPLACE_METHOD_PAYLOAD


def test_request_human_decision_direct_round_trip() -> None:
    payload = RequestHumanDecision.from_json(REQUEST_HUMAN_PAYLOAD)
    assert payload.to_json() == REQUEST_HUMAN_PAYLOAD
