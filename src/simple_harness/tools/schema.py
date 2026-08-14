# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Small fail-closed JSON Schema subset used for Tool arguments."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from simple_harness.contracts import thaw_json

_SUPPORTED_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "title",
        "description",
        "default",
    }
)
_RESERVED_FIELDS = frozenset(
    {
        "_host",
        "__host",
        "_simple_harness",
        "__simple_harness",
        "api_key",
        "authorization",
        "authorization_ref",
        "call_id",
        "credential",
        "credentials",
        "effect_id",
        "password",
        "request_id",
        "run_id",
        "secret",
        "session_id",
        "token",
    }
)


class SchemaDefinitionError(ValueError):
    """A Tool schema is invalid or uses an unsupported semantic."""


class ArgumentsValidationError(ValueError):
    """Model-supplied arguments do not conform to their Tool schema."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _validate_resource_bounds(
    value: Any,
    *,
    max_bytes: int,
    max_depth: int,
    max_nodes: int,
    schema: bool,
) -> None:
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    nodes = properties = required = enum_entries = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        nodes += 1
        if nodes > max_nodes:
            raise SchemaDefinitionError("$: JSON node limit exceeded")
        if depth > max_depth:
            raise SchemaDefinitionError("$: JSON depth limit exceeded")
        if isinstance(current, (Mapping, list, tuple)):
            identity = id(current)
            if identity in active:
                raise SchemaDefinitionError("$: cyclic JSON containers are forbidden")
            active.add(identity)
            stack.append((current, depth, True))
        if isinstance(current, Mapping):
            if not all(isinstance(key, str) for key in current):
                raise SchemaDefinitionError("$: JSON object keys must be strings")
            if schema and isinstance(current.get("properties"), Mapping):
                count = len(current["properties"])
                properties += count
                if count > 64 or properties > 256:
                    raise SchemaDefinitionError("$: schema property limit exceeded")
            if schema and isinstance(current.get("required"), list):
                required += len(current["required"])
                if required > 256:
                    raise SchemaDefinitionError("$: schema required limit exceeded")
            if schema and isinstance(current.get("enum"), list):
                count = len(current["enum"])
                enum_entries += count
                if count > 64 or enum_entries > 256:
                    raise SchemaDefinitionError("$: schema enum limit exceeded")
            for key, child in current.items():
                if schema and len(key.encode("utf-8")) > 4096:
                    raise SchemaDefinitionError("$: JSON key exceeds 4096 bytes")
                stack.append((child, depth + 1, False))
        elif isinstance(current, (list, tuple)):
            for child in current:
                stack.append((child, depth + 1, False))
        elif isinstance(current, str):
            if schema and len(current.encode("utf-8")) > 4096:
                raise SchemaDefinitionError("$: JSON string exceeds 4096 bytes")
        elif isinstance(current, float) and not math.isfinite(current):
            raise SchemaDefinitionError("$: JSON numbers must be finite")
    try:
        encoded = json.dumps(
            thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaDefinitionError("$: value is not strict JSON") from exc
    if len(encoded) > max_bytes:
        raise SchemaDefinitionError("$: canonical JSON byte limit exceeded")


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return len(left) == len(right) and all(
            key in right and _json_equal(value, right[key])
            for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _validate_schema_node(schema: Any, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise SchemaDefinitionError(f"{path}: schema must be an object")
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaDefinitionError(
            f"{path}: unsupported keyword(s): {', '.join(sorted(unknown))}"
        )
    expected_type = schema.get("type")
    if expected_type not in _SUPPORTED_TYPES:
        raise SchemaDefinitionError(f"{path}.type: one supported type is required")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise SchemaDefinitionError(f"{path}.enum: non-empty array required")
        for item in enum:
            if not _matches_type(item, expected_type):
                raise SchemaDefinitionError(f"{path}.enum: item has wrong type")
    if "const" in schema and not _matches_type(schema["const"], expected_type):
        raise SchemaDefinitionError(f"{path}.const: value has wrong type")

    if expected_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or not all(
            isinstance(key, str) for key in properties
        ):
            raise SchemaDefinitionError(f"{path}.properties: object required")
        additional = schema.get("additionalProperties", False)
        if additional is not False:
            raise SchemaDefinitionError(
                f"{path}.additionalProperties: must be false for Tool input"
            )
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or not all(isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
            or not set(required) <= set(properties)
        ):
            raise SchemaDefinitionError(
                f"{path}.required: unique property names are required"
            )
        for name, child in properties.items():
            if normalize_field_name(name) in _RESERVED_FIELDS:
                raise SchemaDefinitionError(f"{path}.properties.{name}: reserved field")
            _validate_schema_node(child, f"{path}.properties.{name}")
    elif any(key in schema for key in ("properties", "required", "additionalProperties")):
        raise SchemaDefinitionError(f"{path}: object keywords require type=object")

    if expected_type == "array":
        if "items" not in schema:
            raise SchemaDefinitionError(f"{path}.items: schema required")
        _validate_schema_node(schema["items"], f"{path}.items")
        _validate_bounds(schema, path, "minItems", "maxItems")
    elif "items" in schema or "minItems" in schema or "maxItems" in schema:
        raise SchemaDefinitionError(f"{path}: array keywords require type=array")

    if expected_type == "string":
        _validate_bounds(schema, path, "minLength", "maxLength")
    elif "minLength" in schema or "maxLength" in schema:
        raise SchemaDefinitionError(f"{path}: length keywords require type=string")

    if expected_type in {"integer", "number"}:
        for keyword in ("minimum", "maximum"):
            if keyword in schema and (
                isinstance(schema[keyword], bool)
                or not isinstance(schema[keyword], (int, float))
                or not math.isfinite(schema[keyword])
            ):
                raise SchemaDefinitionError(f"{path}.{keyword}: finite number required")
        if (
            "minimum" in schema
            and "maximum" in schema
            and schema["minimum"] > schema["maximum"]
        ):
            raise SchemaDefinitionError(f"{path}: minimum exceeds maximum")
    elif "minimum" in schema or "maximum" in schema:
        raise SchemaDefinitionError(f"{path}: numeric bounds require number type")


def _validate_bounds(
    schema: Mapping[str, Any], path: str, minimum: str, maximum: str
) -> None:
    for keyword in (minimum, maximum):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], int)
            or schema[keyword] < 0
        ):
            raise SchemaDefinitionError(f"{path}.{keyword}: non-negative integer required")
    if minimum in schema and maximum in schema and schema[minimum] > schema[maximum]:
        raise SchemaDefinitionError(f"{path}: {minimum} exceeds {maximum}")


def normalize_field_name(value: str) -> str:
    return re.sub(r"[-\s]+", "_", value.strip().casefold())


def validate_tool_schema(schema: Mapping[str, Any]) -> None:
    _validate_resource_bounds(
        schema,
        max_bytes=32768,
        max_depth=12,
        max_nodes=256,
        schema=True,
    )
    _validate_schema_node(schema, "$")
    if schema.get("type") != "object":
        raise SchemaDefinitionError("$: Tool input schema root must be an object")


def validate_arguments(arguments: Any, schema: Mapping[str, Any]) -> None:
    validate_argument_resource_bounds(arguments)
    _validate_value(arguments, schema, "$")


def validate_argument_resource_bounds(arguments: Any) -> None:
    """Reject hostile JSON graphs before recursive detach/freeze operations."""

    _validate_resource_bounds(
        arguments,
        max_bytes=65536,
        max_depth=12,
        max_nodes=1024,
        schema=False,
    )


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    expected_type = schema["type"]
    if not _matches_type(value, expected_type):
        raise ArgumentsValidationError(path, f"expected {expected_type}")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise ArgumentsValidationError(path, "value is not in enum")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise ArgumentsValidationError(path, "value does not equal const")

    if expected_type == "object":
        properties = schema.get("properties", {})
        for name in value:
            normalized = normalize_field_name(str(name))
            if normalized in _RESERVED_FIELDS:
                raise ArgumentsValidationError(f"{path}.{name}", "reserved field")
            if name not in properties:
                raise ArgumentsValidationError(
                    f"{path}.{name}", "additional property is not allowed"
                )
        for name in schema.get("required", []):
            if name not in value:
                raise ArgumentsValidationError(f"{path}.{name}", "required property missing")
        for name, child in properties.items():
            if name in value:
                _validate_value(value[name], child, f"{path}.{name}")
    elif expected_type == "array":
        length = len(value)
        if length < schema.get("minItems", 0):
            raise ArgumentsValidationError(path, "array is too short")
        if "maxItems" in schema and length > schema["maxItems"]:
            raise ArgumentsValidationError(path, "array is too long")
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    elif expected_type == "string":
        length = len(value)
        if length < schema.get("minLength", 0):
            raise ArgumentsValidationError(path, "string is too short")
        if "maxLength" in schema and length > schema["maxLength"]:
            raise ArgumentsValidationError(path, "string is too long")
    elif expected_type in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentsValidationError(path, "number is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgumentsValidationError(path, "number is above maximum")


__all__ = (
    "ArgumentsValidationError",
    "SchemaDefinitionError",
    "validate_arguments",
    "validate_tool_schema",
)
