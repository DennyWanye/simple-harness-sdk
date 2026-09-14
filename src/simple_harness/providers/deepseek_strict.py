# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed, bounded projection of Tool inputs for DeepSeek strict beta."""

from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import combinations
from typing import Any

DEEPSEEK_STRICT_TOOL_SCHEMA_MODE = "deepseek-strict-v1"
DEEPSEEK_STRICT_ADAPTER_KEY = "openai-compatible.chat-completions.deepseek-strict-v1"

_MAX_OPTIONAL_PER_OBJECT = 3
_MAX_OUTPUT_NODES = 2048
_MAX_OUTPUT_BYTES = 131072
# These are the source Tool-schema semantics supported by the compiler. Unknown
# keywords, including existing patterns and combinators, are never discarded.
_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minLength", "maxLength", "minimum", "maximum",
    "title", "description", "default",
})


def _output_nodes(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1 + sum(_output_nodes(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_output_nodes(item) for item in value)
    return 1


def _compile(schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    # Tool package initialization imports ProviderToolSpec. Defer this import
    # until serialization so providers can be imported without a package cycle.
    from simple_harness.tools.schema import SchemaDefinitionError

    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise SchemaDefinitionError(
            f"{path}: unsupported strict keyword(s): {', '.join(sorted(unknown))}"
        )
    result = dict(schema)
    declared = schema["type"]
    if not isinstance(declared, str) or declared not in {
        "object", "array", "string", "integer", "number", "boolean",
    }:
        raise SchemaDefinitionError(f"{path}: unsupported strict type")
    if declared == "object":
        if schema.get("additionalProperties") is not False:
            raise SchemaDefinitionError(f"{path}: strict objects must explicitly be closed")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        property_names = sorted(properties)
        optional = [name for name in property_names if name not in required]
        if len(optional) > _MAX_OPTIONAL_PER_OBJECT:
            raise SchemaDefinitionError(f"{path}: more than three optional properties")
        compiled = {
            name: _compile(properties[name], f"{path}.properties.{name}")
            for name in property_names
        }
        if optional:
            variants = []
            for count in range(len(optional) + 1):
                for subset in combinations(optional, count):
                    included = set(required) | set(subset)
                    names = [name for name in property_names if name in included]
                    variants.append({
                        "type": "object",
                        "properties": {name: compiled[name] for name in names},
                        "required": names,
                        "additionalProperties": False,
                    })
            result.pop("properties", None)
            result.pop("required", None)
            result.pop("additionalProperties", None)
            result["anyOf"] = variants
        elif "properties" in schema:
            result["properties"] = compiled
    elif declared == "array":
        result["items"] = _compile(schema["items"], f"{path}.items")
    elif declared == "string":
        if "minLength" in schema or "maxLength" in schema:
            lower = schema.get("minLength", 0)
            upper = schema.get("maxLength", "")
            result.pop("minLength", None)
            result.pop("maxLength", None)
            # `$` may also match just before a final newline. An absolute-end
            # negative lookahead enforces the original JSON string length.
            result["pattern"] = f"^[\\s\\S]{{{lower},{upper}}}(?![\\s\\S])"
    if _output_nodes(result) > _MAX_OUTPUT_NODES:
        raise SchemaDefinitionError(f"{path}: strict schema expansion node limit exceeded")
    return result


def compile_deepseek_strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project a bounded closed Tool schema without changing accepted arguments.

    Raises SchemaDefinitionError for unknown or unrepresentable source schemas.
    The caller passes a detached plain JSON mapping; neither input nor Tool
    argument names are changed by this pure compiler.
    """
    from simple_harness.tools.schema import SchemaDefinitionError, validate_tool_schema

    validate_tool_schema(schema)
    result = _compile(schema, "$")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        raise SchemaDefinitionError("$: strict schema expansion byte limit exceeded")
    return result
