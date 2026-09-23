# SPDX-License-Identifier: Apache-2.0
"""Host DTO contracts (ASSURANCE-EXEC-1.1 §13) and a dependency-free checker.

The JSON documents next to this module are the approved ``host-*-v1`` wire
schemas. ``validate`` implements exactly the JSON Schema keywords those
documents use, so both the SDK and the Host can assert a body against the
approved contract without adding a runtime dependency. It is a contract
checker, not a general JSON Schema engine.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
CONTRACTS = (
    "host-snapshot-request-v1",
    "host-review-request-v1",
    "host-use-request-v1",
    "host-response-v1",
    "host-review-response-v1",
    "host-use-response-v1",
    "host-error-v1",
)


class ContractViolation(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path or '$'}: {message}")
        self.path = path
        self.message = message


@lru_cache(maxsize=None)
def schema(name: str) -> dict[str, Any]:
    path = _ROOT / (name if name.endswith(".json") else f"{name}.schema.json")
    if not path.is_file():
        raise KeyError(name)
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(ref: str, current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``file#/pointer`` or ``#/pointer`` within ``current``; returns (node, its document)."""
    file_part, _, pointer = ref.partition("#")
    document = schema(file_part) if file_part else current
    node: Any = document
    for token in pointer.strip("/").split("/") if pointer.strip("/") else ():
        node = node[token.replace("~1", "/").replace("~0", "~")]
    return node, document


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractViolation("", f"unsupported type keyword {expected!r}")


def _check(value: Any, node: dict[str, Any], document: dict[str, Any], path: str) -> None:
    if "$ref" in node:
        target, target_document = _resolve(node["$ref"], document)
        _check(value, target, target_document, path)
        rest = {key: item for key, item in node.items() if key != "$ref"}
        if rest:
            _check(value, rest, document, path)
        return
    if "const" in node and value != node["const"]:
        raise ContractViolation(path, f"must equal {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        raise ContractViolation(path, f"must be one of {node['enum']!r}")
    if "type" in node:
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(_type_ok(value, item) for item in types):
            raise ContractViolation(path, f"must be of type {types!r}")
    if "oneOf" in node:
        matches = 0
        for option in node["oneOf"]:
            try:
                _check(value, option, document, path)
                matches += 1
            except ContractViolation:
                continue
        if matches != 1:
            raise ContractViolation(path, f"must match exactly one alternative, matched {matches}")
    if "allOf" in node:
        for option in node["allOf"]:
            _check(value, option, document, path)
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            raise ContractViolation(path, f"shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            raise ContractViolation(path, f"longer than {node['maxLength']}")
        if "pattern" in node and re.search(node["pattern"], value) is None:
            raise ContractViolation(path, f"does not match {node['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            raise ContractViolation(path, f"below {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            raise ContractViolation(path, f"above {node['maximum']}")
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            raise ContractViolation(path, f"fewer than {node['minItems']} items")
        if "maxItems" in node and len(value) > node["maxItems"]:
            raise ContractViolation(path, f"more than {node['maxItems']} items")
        if node.get("uniqueItems"):
            seen = [json.dumps(item, sort_keys=True) for item in value]
            if len(set(seen)) != len(seen):
                raise ContractViolation(path, "items are not unique")
        if "items" in node:
            for index, item in enumerate(value):
                _check(item, node["items"], document, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = node.get("properties", {})
        for key in node.get("required", ()):
            if key not in value:
                raise ContractViolation(path, f"missing required property {key!r}")
        if node.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ContractViolation(path, f"unexpected properties {extra!r}")
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], document, f"{path}.{key}" if path else key)


def validate(name: str, document: Any) -> Any:
    """Raise ``ContractViolation`` unless ``document`` satisfies contract ``name``."""
    root = schema(name)
    _check(document, root, root, "")
    return document


__all__ = ["CONTRACTS", "ContractViolation", "schema", "validate"]
