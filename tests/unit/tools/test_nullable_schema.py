"""Bounded nullable extension; no general union/combinator support."""
import copy

import pytest

from simple_harness.contracts import freeze_json
from simple_harness.tools.schema import (
    ArgumentsValidationError, SchemaDefinitionError, validate_arguments, validate_tool_schema,
)


def wrapper(child, *, required=False):
    return {"type": "object", "properties": {"value": child},
            "required": ["value"] if required else [], "additionalProperties": False}


def test_nullable_preserves_constraints_required_enum_const_and_frozen_input():
    examples = [
        ("string", {"minLength": 1, "maxLength": 2}, "ok", ["", "abc", 1]),
        ("integer", {"minimum": 1, "maximum": 2}, 1, [0, 3, True, 1.5]),
        ("number", {"minimum": 1, "maximum": 2}, 1.5, [0, 3, True]),
        ("boolean", {}, False, [0, "false"]),
        ("array", {"minItems": 1, "maxItems": 1, "items": {"type": "string"}},
         ["ok"], [[], ["a", "b"], [1]]),
        ("object", {"properties": {"x": {"type": "string"}}, "required": ["x"],
                    "additionalProperties": False}, {"x": "ok"}, [{}, {"x": "ok", "extra": 1}]),
    ]
    for base, constraints, good, invalid in examples:
        for types in ([base, "null"], ["null", base]):
            schema = wrapper({"type": types, **constraints}, required=True)
            before = copy.deepcopy(schema)
            validate_tool_schema(schema)
            for args in ({"value": None}, {"value": good}):
                validate_arguments(args, schema)
                validate_arguments(freeze_json(args), freeze_json(schema))
            for args in ({}, *({"value": bad} for bad in invalid)):
                with pytest.raises(ArgumentsValidationError):
                    validate_arguments(args, schema)
            assert schema == before
    for restriction in ({"enum": ["ok"]}, {"const": "ok"}):
        schema = wrapper({"type": ["string", "null"], **restriction})
        validate_tool_schema(schema)
        with pytest.raises(ArgumentsValidationError):
            validate_arguments({"value": None}, schema)
    for restriction in ({"enum": [None, "ok"]}, {"const": None}):
        schema = wrapper({"type": ["string", "null"], **restriction})
        validate_tool_schema(schema)
        validate_arguments({"value": None}, schema)
    schema = wrapper({"type": ["string", "null"]})
    validate_arguments({}, schema)  # Optional is still separate from nullable.
    with pytest.raises(ArgumentsValidationError):
        validate_arguments({"value": "x", "authorization": "forbidden"}, schema)
    with pytest.raises(SchemaDefinitionError, match="byte limit"):
        validate_arguments({"value": "x" * 65536}, schema)


def test_nullable_rejects_malformed_definitions_and_keeps_legacy_string_strict():
    for raw in ([], ["null"], ["string"], ["null", "null"], ["string", "string"],
                ["string", "integer"], ["string", "integer", "null"],
                [["string"], "null"], [1, "null"], ["unknown", "null"], {}, None):
        with pytest.raises(SchemaDefinitionError):
            validate_tool_schema(wrapper({"type": raw}))
    for child in (
        {"type": ["string", "null"], "anyOf": [{"type": "string"}]},
        {"type": ["string", "null"], "minimum": 1},
        {"type": ["string", "null"], "minLength": -1},
        {"type": ["array", "null"]},
        {"type": ["object", "null"], "additionalProperties": True},
        {"type": ["object", "null"], "properties": {"authorization": {"type": "string"}}},
        {"type": ["string", "null"], "enum": [1]},
        {"type": ["string", "null"], "const": False},
    ):
        with pytest.raises(SchemaDefinitionError):
            validate_tool_schema(wrapper(child))
    with pytest.raises(SchemaDefinitionError, match="root"):
        validate_tool_schema({"type": ["object", "null"], "properties": {}})
    old = wrapper({"type": "string", "minLength": 1})
    validate_tool_schema(old)
    for value in (None, ""):
        with pytest.raises(ArgumentsValidationError):
            validate_arguments({"value": value}, old)
    validate_arguments({"value": "null"}, old)  # A string is never normalized.
