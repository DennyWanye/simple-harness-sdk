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
import re
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
# A minimal Draft 2020-12 subset validator (no jsonschema dependency).
#
# The schema deliberately uses only a small keyword set, so the reverse-acceptance
# test below re-implements exactly those keywords.  It exists to pin the direction
# the mirror tests cannot see: a value the *Python codec* calls canonical must be
# accepted by the *Schema*.  Mutating a schema ``type`` (for example narrowing a
# nullable field back to ``string``) has to turn this test red.
# --------------------------------------------------------------------------------------


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _kind_matches(value: Any, kind: str) -> bool:
    got = _json_kind(value)
    if kind == got:
        return True
    return kind == "number" and got == "integer"


def _type_matches(value: Any, spec: Any) -> bool:
    kinds = spec if isinstance(spec, list) else [spec]
    return any(_kind_matches(value, kind) for kind in kinds)


def _deref(defs: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/$defs/"), ref
        node = defs[ref.split("/")[2]]
    return node


def _validate(schema: dict[str, Any], node: Any, value: Any, path: str = "$") -> list[str]:
    """Return a list of human-readable violations (empty means accepted)."""

    if not isinstance(node, dict):
        return []
    if "$ref" in node:
        target = _deref(schema["$defs"], node)
        siblings = {key: item for key, item in node.items() if key != "$ref"}
        return _validate(schema, target, value, path) + _validate(schema, siblings, value, path)

    errors: list[str] = []
    if "type" in node and not _type_matches(value, node["type"]):
        errors.append(f"{path}: type {node['type']} != {_json_kind(value)}")
    if "const" in node and value != node["const"]:
        errors.append(f"{path}: const {node['const']!r} != {value!r}")
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: {value!r} not in enum")

    if isinstance(value, str):
        if len(value) < node.get("minLength", 0):
            errors.append(f"{path}: shorter than minLength {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{path}: longer than maxLength {node['maxLength']}")
        if "pattern" in node and re.search(node["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern {node['pattern']}")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in node:
        if value < node["minimum"]:
            errors.append(f"{path}: below minimum {node['minimum']}")
    if isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            errors.append(f"{path}: fewer than minItems {node['minItems']}")
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(f"{path}: more than maxItems {node['maxItems']}")
        if node.get("uniqueItems"):
            canon = {json.dumps(item, sort_keys=True) for item in value}
            if len(canon) != len(value):
                errors.append(f"{path}: items are not unique")
        if "items" in node:
            for position, item in enumerate(value):
                errors += _validate(schema, node["items"], item, f"{path}[{position}]")
    if isinstance(value, dict):
        if "maxProperties" in node and len(value) > node["maxProperties"]:
            errors.append(f"{path}: more than maxProperties {node['maxProperties']}")
        for required in node.get("required", []):
            if required not in value:
                errors.append(f"{path}: missing required {required!r}")
        properties = node.get("properties", {})
        if node.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: additional property {key!r} is not allowed")
        for key, sub in properties.items():
            if key in value:
                errors += _validate(schema, sub, value[key], f"{path}.{key}")

    if "anyOf" in node:
        if not any(not _validate(schema, branch, value, path) for branch in node["anyOf"]):
            errors.append(f"{path}: no anyOf branch accepted the value")
    if "oneOf" in node:
        matched = sum(1 for branch in node["oneOf"] if not _validate(schema, branch, value, path))
        if matched != 1:
            errors.append(f"{path}: oneOf matched {matched} branches")
    for branch in node.get("allOf", []):
        errors += _validate(schema, branch, value, path)
    if "if" in node:
        if not _validate(schema, node["if"], value, path):
            if "then" in node:
                errors += _validate(schema, node["then"], value, path)
        elif "else" in node:
            errors += _validate(schema, node["else"], value, path)
    return errors


def _codec_canonical_variants() -> dict[str, dict[str, Any]]:
    """Every codec-legal shape whose canonical JSON exercises a nullable field.

    ``blockedItem.detail`` is the field the verifier flagged (P1-A); the other
    three ``None``-able fields are added so their ``null`` branches cannot be
    deleted without turning this test red (P1-C).
    """

    variants: dict[str, dict[str, Any]] = {}
    for path in _valid_paths():
        variants[path.stem] = _read(path)

    blocked = _read(VALID_DIR / "declare-blocked.json")
    blocked["payload"]["blockers"] = [{"code": "NO_USABLE_METHOD"}]
    variants["variant-blocked-item-without-detail"] = blocked

    alternative = _read(VALID_DIR / "refine.json")
    alternative["alternatives"] = [
        {"method_ref": None, "label": "alt", "disposition": "DEFERRED", "reason": "later"}
    ]
    variants["variant-alternative-without-method-ref"] = alternative

    assumption = _read(VALID_DIR / "refine.json")
    assumption["assumptions"] = [
        {
            "key": "a",
            "statement": "s",
            "required_for": ["REFINE"],
            "risk": "LOW",
            "suggested_predicate_key": None,
        }
    ]
    variants["variant-assumption-without-predicate"] = assumption

    # P1-D/P1-E: the 11 golden fixtures all carry empty ``uncertainties`` and
    # ``replan_triggers``, so the sub-shape ``type`` keywords of those two arrays
    # were never exercised.  A non-empty variant is what makes a flipped
    # ``uncertainty``/``replanTrigger`` sub-shape fail the reverse-acceptance test.
    uncertainty = _read(VALID_DIR / "refine.json")
    uncertainty["uncertainties"] = [
        {"statement": "the target file may move", "severity": "LOW", "affects": ["obligation:o-1"]}
    ]
    variants["variant-uncertainty-with-affects"] = uncertainty

    trigger = _read(VALID_DIR / "refine.json")
    trigger["replan_triggers"] = [
        {
            "description": "the input becomes stale",
            "referenced_predicates": ["pred.input_present"],
            "suggested_decision": "REFINE",
        }
    ]
    variants["variant-replan-trigger"] = trigger

    # ``suggested_predicate_key`` was only ever exercised as ``null``; a non-null
    # string makes the ``string`` branch of its ``anyOf`` load-bearing too.
    predicate = _read(VALID_DIR / "refine.json")
    predicate["assumptions"] = [
        {
            "key": "a",
            "statement": "s",
            "required_for": ["REFINE"],
            "risk": "LOW",
            "suggested_predicate_key": "pred.x",
        }
    ]
    variants["variant-assumption-with-predicate"] = predicate
    return variants


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


def _object_def_names(schema: dict[str, Any]) -> list[str]:
    """Every ``$defs`` entry that is a closed object shape (script-derived, no list)."""

    return sorted(
        name
        for name, node in schema["$defs"].items()
        if isinstance(node, dict) and node.get("type") == "object"
    )


def _codec_legal_shapes() -> dict[str, dict[str, Any]]:
    """The codec-legal envelopes used to exercise every object shape."""

    shapes: dict[str, dict[str, Any]] = {}
    for path in _valid_paths():
        shapes[path.stem] = _read(path)
    shapes.update(_codec_canonical_variants())
    return shapes


def _locate_pointer(root: Any, pointer: str) -> Any:
    node = root
    for step in pointer.removeprefix("#/").split("/") if pointer != "#" else []:
        node = node[int(step)] if step.isdigit() else node[step]
    return node


def _accepts(root: dict[str, Any]) -> bool:
    """True when the Python codec accepts ``root`` (the runtime authority)."""

    try:
        PlanningDecisionEnvelopeV1.from_json(root)
    except ContractError:
        return False
    return True


def _fresh_seed(root_name: str) -> dict[str, Any]:
    """A fresh, mutable copy of one codec-legal seed envelope."""

    return json.loads(json.dumps(_codec_legal_shapes()[root_name]))


def _coverage_seeds() -> dict[str, tuple[str, str]]:
    """Map every object ``$defs`` name to ``(root_name, json_pointer)``.

    The seed is the first value the Schema accepts for that ``$defs`` whose keys
    cover the declared ``properties`` exactly, found by walking the codec-legal
    envelopes.  Everything is derived from the Schema, so renaming a ``$defs``
    entry or adding a field never needs a hand-edited name list here.
    """

    schema = _load_schema()
    pool = _codec_legal_shapes()
    seeds: dict[str, tuple[str, str]] = {}
    for name in _object_def_names(schema):
        properties = set(schema["$defs"][name].get("properties", {}))
        ref = {"$ref": f"#/$defs/{name}"}
        for root_name in sorted(pool):
            for pointer, value in _walk(pool[root_name]):
                if pointer == "#" or not isinstance(value, dict):
                    continue
                if set(value) != properties:
                    continue
                if _validate(schema, ref, value):
                    continue
                seeds[name] = (root_name, pointer)
                break
            if name in seeds:
                break
    return seeds


#: The object ``$defs`` names, resolved once at import for parametrisation.
_OBJECT_DEF_NAMES = _object_def_names(_load_schema())


def test_every_object_def_required_and_properties_match_the_codec() -> None:
    """Coverage lifted to *every* object ``$defs`` (script-enumerated, no list).

    For each object shape the codec's own acceptance path decides which keys are
    mandatory: deleting a key from the seed is required exactly when the codec
    rejects the result.  That derived set must equal the Schema ``required`` list,
    and every declared ``property`` must be covered by the seed (so no extra
    property can hide in the Schema unnoticed).
    """

    schema = _load_schema()
    seeds = _coverage_seeds()
    assert set(seeds) == set(_OBJECT_DEF_NAMES), "no full-coverage codec seed for some $defs"

    for name in _OBJECT_DEF_NAMES:
        root_name, pointer = seeds[name]
        node = schema["$defs"][name]
        properties = set(node["properties"])
        required = set(node.get("required", []))
        assert required <= properties, name
        derived: set[str] = set()
        for field in properties:
            root = _fresh_seed(root_name)
            del _locate_pointer(root, pointer)[field]
            if not _accepts(root):
                derived.add(field)
        assert derived == required, (name, sorted(required), sorted(derived))


@pytest.mark.parametrize("def_name", _OBJECT_DEF_NAMES)
def test_every_object_def_rejects_missing_required_and_unknown_fields(
    def_name: str,
) -> None:
    """Strictness pin (P1-F): each object shape must reject a missing required
    field and an unknown field, exercised through the Python decode path.

    Built from the coverage seeds, so it reaches exactly the object shapes whose
    ``required``/``properties`` a silent Schema edit would otherwise loosen.
    """

    schema = _load_schema()
    seeds = _coverage_seeds()
    root_name, pointer = seeds[def_name]
    required = list(schema["$defs"][def_name].get("required", []))
    assert required, def_name  # every object shape has at least one mandatory key

    for field in required:
        root = _fresh_seed(root_name)
        del _locate_pointer(root, pointer)[field]
        assert not _accepts(root), (def_name, field)

    root = _fresh_seed(root_name)
    _locate_pointer(root, pointer)["__unknown_field__"] = "x"
    assert not _accepts(root), def_name


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


@pytest.mark.parametrize(
    ("name", "raw"),
    _codec_canonical_variants().items(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_codec_canonical_output_is_accepted_by_the_schema(name: str, raw: dict[str, Any]) -> None:
    # Reverse acceptance (P1-B): the Schema must accept every value the Python
    # codec calls canonical.  A one-directional "Schema vs Python constant"
    # mirror cannot see a field whose ``type`` is narrower than the codec's
    # nullable annotation, which is exactly how P1-A slipped through.
    output = PlanningDecisionEnvelopeV1.from_json(raw).to_json()
    errors = _validate(_load_schema(), _load_schema(), output)
    assert errors == [], (name, errors)


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
