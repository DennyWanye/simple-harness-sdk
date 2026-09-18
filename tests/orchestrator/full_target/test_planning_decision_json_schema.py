# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-A2b: the PlanningDecision JSON Schema, its golden fixtures and packaging.

V2 §14 fixes the schema file, §46 fixes the fixture directory and the addendum §四
fixes what this slice has to prove *without* adding a ``jsonschema`` dependency:

* the schema ships inside the package (``importlib.resources`` can read it);
* ``$id``, ``required``, every ``enum`` and every declared limit equal the Python
  constants / enums of :mod:`agent_orchestrator.contracts.planning_decisions`;
* every ``$ref`` resolves inside ``$defs`` and every fixed-shape object is closed;
* each ``valid/*.json`` golden fixture decodes and re-encodes identically;
* each contract-decidable ``invalid/*.json`` fixture is rejected by ``from_json``,
  while admission-stage fixtures stay decodable and are marked for H1-F.

The Python strict codec stays the runtime authority: no JSON-Schema engine is
imported here, and no schema keyword is trusted to validate the wire object.  The
comparisons below walk the schema and refuse any declared value that has no Python
mirror, so relaxing one side alone is a red test rather than a silent drift.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    ENVELOPE_FIELDS,
    MAX_PD_ALTERNATIVES,
    MAX_PD_ARGUMENTS,
    MAX_PD_ASSUMPTIONS,
    MAX_PD_BINDINGS,
    MAX_PD_BLOCKERS,
    MAX_PD_EVIDENCE_QUESTIONS,
    MAX_PD_HUMAN_OPTIONS,
    MAX_PD_RATIONALE_CHARS,
    MAX_PD_REASON_REFS,
    MAX_PD_REPLAN_TRIGGERS,
    MAX_PD_UNCERTAINTIES,
    MAX_PD_WAIT_REFS,
    MAX_PLANNING_REF_ID,
    MAX_SUBJECT_KEY_CHARS,
    MIN_PD_EVIDENCE_QUESTIONS,
    PLANNING_DECISION_SCHEMA_VERSION,
    PLANNING_DECISION_V1,
    AlternativeDisposition,
    AssumptionRisk,
    BindExistingGoalMode,
    BlockerCode,
    PlanningDecisionEnvelopeV1,
    PlanningDecisionRejectionCode,
    PlanningDecisionType,
    PlanningRefKind,
    RepairKind,
    ResumableIf,
    UncertaintySeverity,
)
from agent_orchestrator.contracts.semantic_base import MAX_ID, MAX_LIST, MAX_TEXT

SCHEMA_PACKAGE = "agent_orchestrator.contracts.schemas"
SCHEMA_NAME = f"{PLANNING_DECISION_V1}.schema.json"
SCHEMA_ID = "urn:simpleharness:planning-decision:v1"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "planning_decision_v1"
VALID_DIR = FIXTURE_ROOT / "valid"
INVALID_DIR = FIXTURE_ROOT / "invalid"

#: Every closed enum the wire envelope can carry, keyed by its JSON pointer.  Each
#: enum is declared exactly once in ``$defs`` and referenced everywhere else, so the
#: set of ``enum`` nodes in the schema is exactly this map.
SCHEMA_ENUM_MIRRORS: dict[str, type[Any]] = {
    "#/$defs/decisionType": PlanningDecisionType,
    "#/$defs/planningRefKind": PlanningRefKind,
    "#/$defs/assumptionRisk": AssumptionRisk,
    "#/$defs/uncertaintySeverity": UncertaintySeverity,
    "#/$defs/alternativeDisposition": AlternativeDisposition,
    "#/$defs/blockerCode": BlockerCode,
    "#/$defs/resumableIf": ResumableIf,
    "#/$defs/repairKind": RepairKind,
    "#/$defs/bindExistingGoalMode": BindExistingGoalMode,
}

#: Every declared count/length cap, keyed by JSON pointer -> Python constant.  These
#: are the limits of V2 §16 and the per-field caps the strict codec enforces.
SCHEMA_LIMIT_MIRRORS: dict[str, int] = {
    "#/properties/subject_key/maxLength": MAX_SUBJECT_KEY_CHARS,
    "#/properties/rationale/maxLength": MAX_PD_RATIONALE_CHARS,
    "#/properties/reason_refs/maxItems": MAX_PD_REASON_REFS,
    "#/properties/assumptions/maxItems": MAX_PD_ASSUMPTIONS,
    "#/properties/uncertainties/maxItems": MAX_PD_UNCERTAINTIES,
    "#/properties/alternatives/maxItems": MAX_PD_ALTERNATIVES,
    "#/properties/replan_triggers/maxItems": MAX_PD_REPLAN_TRIGGERS,
    "#/$defs/planningRef/properties/id/maxLength": MAX_PLANNING_REF_ID,
    "#/$defs/planningRef/properties/content_hash/maxLength": 64,
    "#/$defs/versionedTypeRef/properties/id/maxLength": MAX_ID,
    "#/$defs/versionedTypeRef/properties/content_hash/maxLength": 64,
    "#/$defs/assumption/properties/key/maxLength": MAX_ID,
    "#/$defs/assumption/properties/statement/maxLength": MAX_TEXT,
    "#/$defs/assumption/properties/suggested_predicate_key/anyOf/0/maxLength": MAX_ID,
    "#/$defs/assumption/properties/required_for/maxItems": len(PlanningDecisionType),
    "#/$defs/uncertainty/properties/statement/maxLength": MAX_TEXT,
    "#/$defs/uncertainty/properties/affects/maxItems": MAX_LIST,
    "#/$defs/uncertainty/properties/affects/items/maxLength": MAX_ID,
    "#/$defs/alternative/properties/label/maxLength": MAX_TEXT,
    "#/$defs/alternative/properties/reason/maxLength": MAX_TEXT,
    "#/$defs/replanTrigger/properties/description/maxLength": MAX_TEXT,
    "#/$defs/replanTrigger/properties/referenced_predicates/maxItems": MAX_LIST,
    "#/$defs/replanTrigger/properties/referenced_predicates/items/maxLength": MAX_ID,
    "#/$defs/refinePayload/properties/bindings/maxProperties": MAX_PD_BINDINGS,
    "#/$defs/repairReplaceMethodPayload/properties/bindings/maxProperties": MAX_PD_BINDINGS,
    "#/$defs/repairProposeSuccessorPayload/properties/bindings/maxProperties": MAX_PD_BINDINGS,
    "#/$defs/bindGoalPayload/properties/step/maxLength": MAX_ID,
    "#/$defs/blockedItem/properties/detail/maxLength": MAX_TEXT,
    "#/$defs/declareBlockedPayload/properties/blockers/maxItems": MAX_PD_BLOCKERS,
    "#/$defs/declareBlockedPayload/properties/resumable_if/maxItems": MAX_LIST,
    "#/$defs/waitPayload/properties/wait_for/maxItems": MAX_PD_WAIT_REFS,
    "#/$defs/waitPayload/properties/reason/maxLength": MAX_TEXT,
    "#/$defs/noChangePayload/properties/reason/maxLength": MAX_TEXT,
    "#/$defs/evidenceQuestion/properties/predicate_key/maxLength": MAX_ID,
    "#/$defs/evidenceQuestion/properties/arguments/maxProperties": MAX_PD_ARGUMENTS,
    "#/$defs/evidenceQuestion/properties/purpose/maxLength": MAX_TEXT,
    "#/$defs/requestEvidencePayload/properties/questions/minItems": MIN_PD_EVIDENCE_QUESTIONS,
    "#/$defs/requestEvidencePayload/properties/questions/maxItems": MAX_PD_EVIDENCE_QUESTIONS,
    "#/$defs/humanOption/properties/key/maxLength": MAX_ID,
    "#/$defs/humanOption/properties/label/maxLength": MAX_TEXT,
    "#/$defs/requestHumanPayload/properties/question/maxLength": MAX_TEXT,
    "#/$defs/requestHumanPayload/properties/options/maxItems": MAX_PD_HUMAN_OPTIONS,
}

#: The only subschemas allowed to keep arbitrary extra keys (V2 §32: the codec never
#: inspects domain parameter maps, and PROPOSE_METHOD's inner ``method_proposal`` is
#: validated by H1-C's MethodProposal codec).
OPEN_MAP_POINTERS = frozenset(
    {
        "#/$defs/refinePayload/properties/bindings",
        "#/$defs/repairReplaceMethodPayload/properties/bindings",
        "#/$defs/repairProposeSuccessorPayload/properties/bindings",
        "#/$defs/evidenceQuestion/properties/arguments",
        "#/$defs/proposeMethodPayload/properties/method_proposal",
        "#/properties/payload",
    }
)

PAYLOAD_DEF_BY_DECISION_TYPE = {
    "REFINE": "#/$defs/refinePayload",
    "PROPOSE_METHOD": "#/$defs/proposeMethodPayload",
    "REQUEST_EVIDENCE": "#/$defs/requestEvidencePayload",
    "BIND_EXISTING_GOAL": "#/$defs/bindGoalPayload",
    "DECLARE_BLOCKED": "#/$defs/declareBlockedPayload",
    "WAIT": "#/$defs/waitPayload",
    "NO_CHANGE": "#/$defs/noChangePayload",
    "REQUEST_HUMAN": "#/$defs/requestHumanPayload",
}
REPAIR_PAYLOAD_BY_KIND = {
    "REPLACE_METHOD": "#/$defs/repairReplaceMethodPayload",
    "PROPOSE_SUCCESSOR": "#/$defs/repairProposeSuccessorPayload",
}
REPAIR_PAYLOAD_REFS = (
    "#/$defs/repairReplaceMethodPayload",
    "#/$defs/repairProposeSuccessorPayload",
)

#: Codes whose fixture is a case descriptor for the H1-C block scanner rather than a
#: decodable envelope (model prose, not a wire object).
TEXT_LEVEL_CODES = frozenset(
    {"DECISION_BLOCK_MISSING", "MULTIPLE_DECISIONS", "MIXED_PROTOCOL_BLOCKS"}
)
CONTRACT_LAYER_CODES = frozenset(
    {
        "UNKNOWN_FIELD",
        "MALFORMED_DECISION",
        "MODEL_SET_SYSTEM_FIELD",
        "DECISION_TYPE_UNKNOWN",
        "STRUCTURE_INVALID",
    }
)


def _walk(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    """Yield ``(json_pointer, node)`` for the node and everything under it."""

    yield "#/" + "/".join(path), node
    if isinstance(node, dict):
        for key, item in node.items():
            yield from _walk(item, (*path, key))
    elif isinstance(node, list):
        for position, item in enumerate(node):
            yield from _walk(item, (*path, str(position)))


def _load_schema() -> dict[str, Any]:
    text = resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_NAME).read_text(encoding="utf-8")
    return json.loads(text)


def _schema_nodes(keyword: str) -> dict[str, Any]:
    return {
        pointer: node[keyword]
        for pointer, node in _walk(_load_schema())
        if isinstance(node, dict) and keyword in node
    }


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_paths() -> list[Path]:
    return sorted(VALID_DIR.glob("*.json"))


def _invalid_cases() -> list[tuple[Path, Path]]:
    cases = []
    for path in sorted(INVALID_DIR.glob("*.json")):
        if path.name.endswith(".expect.json"):
            continue
        cases.append((path, path.with_name(f"{path.stem}.expect.json")))
    return cases


def _payload_pointer(schema: dict[str, Any], raw: dict[str, Any]) -> str:
    decision_type = raw["decision_type"]
    if decision_type == "REPAIR":
        return REPAIR_PAYLOAD_BY_KIND[raw["payload"]["repair_kind"]]
    return PAYLOAD_DEF_BY_DECISION_TYPE[decision_type]


# --------------------------------------------------------------------------------------
# The schema file itself: packaging, identity, required fields
# --------------------------------------------------------------------------------------


def test_schema_ships_and_is_readable_through_importlib_resources() -> None:
    path = resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_NAME)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["$id"] == SCHEMA_ID


def test_schema_name_is_derived_from_the_protocol_constant() -> None:
    assert PLANNING_DECISION_SCHEMA_VERSION == 1
    assert SCHEMA_NAME == f"{PLANNING_DECISION_V1}.schema.json"


def test_schema_identity_and_envelope_shape() -> None:
    schema = _load_schema()
    assert schema["$schema"] == DRAFT_2020_12
    assert schema["$id"] == SCHEMA_ID
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(ENVELOPE_FIELDS)
    assert set(schema["properties"]) == set(ENVELOPE_FIELDS)
    assert schema["properties"]["schema_version"] == {"const": PLANNING_DECISION_SCHEMA_VERSION}


# --------------------------------------------------------------------------------------
# Enums and limits: every declared value is the Python value
# --------------------------------------------------------------------------------------


def test_every_declared_enum_equals_its_python_enum() -> None:
    found = _schema_nodes("enum")
    assert not sorted(set(SCHEMA_ENUM_MIRRORS) - set(found)), "mirrored enum missing from schema"
    assert not sorted(set(found) - set(SCHEMA_ENUM_MIRRORS)), "schema enum without Python mirror"
    for pointer, values in found.items():
        assert values == [member.value for member in SCHEMA_ENUM_MIRRORS[pointer]], pointer


def test_every_declared_cap_equals_its_python_constant() -> None:
    found: dict[str, Any] = {}
    for keyword in ("maxLength", "maxItems", "maxProperties", "minItems"):
        for pointer, value in _schema_nodes(keyword).items():
            found[f"{pointer}/{keyword}"] = value
    assert not sorted(set(SCHEMA_LIMIT_MIRRORS) - set(found)), "mirrored cap missing from schema"
    assert not sorted(set(found) - set(SCHEMA_LIMIT_MIRRORS)), "schema cap without Python mirror"
    for pointer, value in found.items():
        assert value == SCHEMA_LIMIT_MIRRORS[pointer], pointer


def test_every_declared_min_length_is_the_non_blank_rule() -> None:
    # ``identifier()`` / ``text()`` reject blank strings; ``minItems`` is separate.
    for pointer, value in _schema_nodes("minLength").items():
        assert value == 1, pointer


def test_revision_minima_are_one_and_hashes_are_lowercase_sha256() -> None:
    defs = _load_schema()["$defs"]
    pattern = "^[a-f0-9]{64}$"
    assert defs["planningRef"]["properties"]["semantic_revision"]["minimum"] == 1
    assert defs["versionedTypeRef"]["properties"]["version"]["minimum"] == 1
    assert defs["planningRef"]["properties"]["content_hash"]["pattern"] == pattern
    assert defs["versionedTypeRef"]["properties"]["content_hash"]["pattern"] == pattern


def test_every_ref_resolves_inside_defs() -> None:
    schema = _load_schema()
    defs = set(schema["$defs"])
    refs = list(_schema_nodes("$ref").values())
    assert refs, "the schema declares no $ref at all"
    for ref in refs:
        assert ref.startswith("#/$defs/"), ref
        assert ref.split("/")[2] in defs, ref


# --------------------------------------------------------------------------------------
# Structure: closed objects, decision_type -> payload binding, sub-shapes
# --------------------------------------------------------------------------------------


def test_every_fixed_shape_object_is_closed() -> None:
    for pointer, node in _walk(_load_schema()):
        if not isinstance(node, dict) or node.get("type") != "object":
            continue
        if "properties" not in node:
            continue
        if pointer in OPEN_MAP_POINTERS:  # pragma: no cover - the open maps declare no properties
            assert node.get("additionalProperties") is not False, pointer
            continue
        assert node.get("additionalProperties") is False, pointer


def test_decision_type_binds_to_exactly_one_payload_shape() -> None:
    schema = _load_schema()
    bindings: dict[str, Any] = {}
    for entry in schema["allOf"]:
        condition = entry["if"]["properties"]["decision_type"]["const"]
        bindings[condition] = entry["then"]["properties"]["payload"]
    assert set(bindings) == {member.value for member in PlanningDecisionType}
    assert bindings["REPAIR"] == {"oneOf": [{"$ref": ref} for ref in REPAIR_PAYLOAD_REFS]}
    for decision_type, shape in bindings.items():
        if decision_type == "REPAIR":
            continue
        assert shape == {"$ref": PAYLOAD_DEF_BY_DECISION_TYPE[decision_type]}


def test_payload_defs_are_complete_and_match_the_codec_required_fields() -> None:
    defs = _load_schema()["$defs"]
    expected = {
        "refinePayload": ["method_ref", "bindings"],
        "repairReplaceMethodPayload": [
            "repair_kind",
            "rejected_method_instance",
            "replacement_method_ref",
            "bindings",
        ],
        "repairProposeSuccessorPayload": [
            "repair_kind",
            "old_task_ref",
            "obligation_ref",
            "goal_type_ref",
            "bindings",
        ],
        "bindGoalPayload": [
            "mode",
            "consumer_method_instance_ref",
            "step",
            "goal_ref",
            "resolution_ref",
        ],
        "declareBlockedPayload": ["blockers", "resumable_if"],
        "waitPayload": ["wait_for", "reason"],
        "noChangePayload": ["reason"],
        "requestEvidencePayload": ["questions"],
        "requestHumanPayload": ["question", "options", "blocking"],
        "proposeMethodPayload": ["method_proposal"],
    }
    for name, required in expected.items():
        assert defs[name]["required"] == required, name
        assert sorted(defs[name]["properties"]) == sorted(required), name


def test_repair_payload_defs_are_discriminated_per_repair_kind() -> None:
    defs = _load_schema()["$defs"]
    assert defs["repairKind"]["enum"] == [member.value for member in RepairKind]
    for name, const in (
        ("repairReplaceMethodPayload", RepairKind.REPLACE_METHOD.value),
        ("repairProposeSuccessorPayload", RepairKind.PROPOSE_SUCCESSOR.value),
    ):
        node = defs[name]["properties"]["repair_kind"]
        assert node["$ref"] == "#/$defs/repairKind", name
        assert node["const"] == const, name


def test_method_instance_refs_are_pinned_to_their_kind() -> None:
    defs = _load_schema()["$defs"]
    for name, field in (
        ("repairReplaceMethodPayload", "rejected_method_instance"),
        ("bindGoalPayload", "consumer_method_instance_ref"),
    ):
        node = defs[name]["properties"][field]
        kinds = [
            branch["properties"]["kind"]["const"]
            for branch in node["allOf"]
            if "properties" in branch
        ]
        assert kinds == [PlanningRefKind.METHOD_INSTANCE.value], (name, field)


def test_bind_goal_payload_pins_share_active_without_a_resolution() -> None:
    node = _load_schema()["$defs"]["bindGoalPayload"]
    then_shapes = {
        entry["if"]["properties"]["mode"]["const"]: entry["then"]["properties"]["resolution_ref"]
        for entry in node["allOf"]
    }
    assert set(then_shapes) == {member.value for member in BindExistingGoalMode}
    assert then_shapes[BindExistingGoalMode.SHARE_ACTIVE.value] == {"type": "null"}
    assert then_shapes[BindExistingGoalMode.REUSE_ACCEPTED.value] == {"$ref": "#/$defs/planningRef"}


def test_declare_blocked_other_requires_a_detail() -> None:
    node = _load_schema()["$defs"]["blockedItem"]
    assert node["properties"]["code"] == {"$ref": "#/$defs/blockerCode"}
    condition = [
        entry for entry in node["allOf"] if entry["if"]["properties"]["code"]["const"] == "OTHER"
    ]
    assert condition, "blockedItem must special-case code=OTHER"
    assert condition[0]["then"]["required"] == ["detail"]


def test_open_domain_maps_keep_no_system_field_keyword() -> None:
    # §32: bindings/arguments are domain maps; the schema must not scan their keys.
    for pointer in (
        "#/$defs/refinePayload/properties/bindings",
        "#/$defs/repairReplaceMethodPayload/properties/bindings",
        "#/$defs/repairProposeSuccessorPayload/properties/bindings",
        "#/$defs/evidenceQuestion/properties/arguments",
        "#/$defs/proposeMethodPayload/properties/method_proposal",
    ):
        node = _load_schema()
        for step in pointer.removeprefix("#/").split("/"):
            node = node[step]
        assert node.get("additionalProperties") is not False, pointer
        assert "propertyNames" not in node, pointer


def test_top_level_objects_are_closed_and_unique_arrays_are_declared() -> None:
    schema = _load_schema()
    assert schema["properties"]["reason_refs"]["uniqueItems"] is True
    assert schema["properties"]["reason_refs"]["items"] == {"$ref": "#/$defs/planningRef"}


# --------------------------------------------------------------------------------------
# Golden fixtures: valid
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", _valid_paths(), ids=lambda path: path.stem)
def test_valid_fixture_decodes_and_round_trips(path: Path) -> None:
    raw = _read(path)
    envelope = PlanningDecisionEnvelopeV1.from_json(raw)
    assert envelope.to_json() == raw
    assert PlanningDecisionEnvelopeV1.from_json(envelope.to_json()) == envelope


@pytest.mark.parametrize("path", _valid_paths(), ids=lambda path: path.stem)
def test_valid_fixture_payload_keys_match_the_schema_def_it_binds_to(path: Path) -> None:
    raw = _read(path)
    schema = _load_schema()
    payload_def = schema["$defs"][_payload_pointer(schema, raw).split("/")[2]]
    assert sorted(raw["payload"]) == sorted(payload_def["required"])


def test_valid_fixtures_number_at_least_eleven_and_cover_every_shape() -> None:
    paths = _valid_paths()
    assert len(paths) >= 11
    covered = set()
    for path in paths:
        raw = _read(path)
        kind = raw["payload"].get("repair_kind") if raw["decision_type"] == "REPAIR" else None
        covered.add((raw["decision_type"], kind))
    assert covered == {
        ("REFINE", None),
        ("REPAIR", "REPLACE_METHOD"),
        ("REPAIR", "PROPOSE_SUCCESSOR"),
        ("BIND_EXISTING_GOAL", None),
        ("DECLARE_BLOCKED", None),
        ("WAIT", None),
        ("NO_CHANGE", None),
        ("REQUEST_EVIDENCE", None),
        ("REQUEST_HUMAN", None),
        ("PROPOSE_METHOD", None),
    }


def test_valid_fixtures_cover_both_bind_existing_goal_modes() -> None:
    modes = {
        _read(path)["payload"]["mode"]
        for path in _valid_paths()
        if _read(path)["decision_type"] == "BIND_EXISTING_GOAL"
    }
    assert modes == {member.value for member in BindExistingGoalMode}


def test_domain_parameter_maps_are_not_scanned_for_system_field_names() -> None:
    # §32: only *structural* keys are forbidden; a domain map keeps its keys even
    # when one of them happens to look like a system field name.
    raw = _read(VALID_DIR / "refine.json")
    raw["payload"]["bindings"] = {"plan_revision": 7, "operation_id": "op-1"}
    envelope = PlanningDecisionEnvelopeV1.from_json(raw)
    assert envelope.to_json()["payload"]["bindings"] == {"plan_revision": 7, "operation_id": "op-1"}


# --------------------------------------------------------------------------------------
# Golden fixtures: invalid
# --------------------------------------------------------------------------------------


def test_every_invalid_fixture_has_a_sibling_expectation_file() -> None:
    for path, expect in _invalid_cases():
        assert expect.is_file(), f"{path.name} has no expectation file"
        label = _read(expect)
        assert label["expected_stage"] in {"codec", "admission"}, path.name
        assert label["expected_code"] in {member.value for member in PlanningDecisionRejectionCode}
        assert label["checked_in"] in {"H1-A2b", "H1-F"}, path.name


def test_every_expectation_file_belongs_to_a_fixture() -> None:
    fixtures = {path.name for path, _ in _invalid_cases()}
    answers = {answer.name for answer in sorted(INVALID_DIR.glob("*.expect.json"))}
    assert answers == {f"{Path(name).stem}.expect.json" for name in fixtures}


def test_every_rejection_code_has_at_least_one_invalid_fixture() -> None:
    covered = {_read(expect)["expected_code"] for _, expect in _invalid_cases()}
    assert covered == {member.value for member in PlanningDecisionRejectionCode}


def test_contract_layer_fixtures_are_checked_in_now() -> None:
    checked_now = {
        _read(expect)["expected_code"]
        for _, expect in _invalid_cases()
        if _read(expect)["checked_in"] == "H1-A2b"
    }
    # The addendum §四 list: unknown/missing fields, payload mismatch, over-limit,
    # duplicate refs, model-written system fields, SHARE_ACTIVE + resolution_ref.
    assert checked_now == CONTRACT_LAYER_CODES


def test_admission_layer_fixtures_are_deferred_to_h1_f() -> None:
    deferred = {
        _read(expect)["expected_code"]
        for _, expect in _invalid_cases()
        if _read(expect)["checked_in"] == "H1-F"
    }
    assert deferred == {member.value for member in PlanningDecisionRejectionCode} - (
        CONTRACT_LAYER_CODES
    )


@pytest.mark.parametrize(
    ("path", "expect"),
    _invalid_cases(),
    ids=lambda value: value.stem.replace(".expect", "") if isinstance(value, Path) else None,
)
def test_invalid_fixture_matches_its_declared_stage(path: Path, expect: Path) -> None:
    label = _read(expect)
    raw = _read(path)
    if label["expected_code"] in TEXT_LEVEL_CODES:
        # Decided while the H1-C scanner extracts the block from model prose.
        assert label == {
            "expected_stage": "codec",
            "expected_code": label["expected_code"],
            "checked_in": "H1-F",
            "scenario": label["scenario"],
        }
        assert isinstance(raw, dict) and "model_reply" in raw, path.name
        return
    if label["checked_in"] == "H1-A2b":
        assert label["expected_stage"] == "codec", path.name
        with pytest.raises(ContractError):
            PlanningDecisionEnvelopeV1.from_json(raw)
        return
    assert label["expected_stage"] == "admission", path.name
    # Structurally valid: the rejection belongs to H1-F's admission layer, so the
    # envelope must still decode today.
    PlanningDecisionEnvelopeV1.from_json(raw)
