# SPDX-License-Identifier: Apache-2.0
"""Registered CheckSpec contract and exact built-in verifier documents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import AssuranceError, canonical, digest, fields, integer, one_of, text, unique_texts
from .refs import Pin

DOCUMENT_NAMES = frozenset(
    {
        "local-verification-input-v1.schema.json",
        "local-check-receipt-v1.schema.json",
        "local-layer-scope-v1.json",
        "check-spec-v1.schema.json",
        "check-binding-v2.schema.json",
        "common.schema.json",
    }
)


def document_pin(name: str) -> Pin:
    if name not in DOCUMENT_NAMES:
        raise AssuranceError("CHECK_SCHEMA_UNREGISTERED")
    raw = (Path(__file__).parent / "schemas" / name).read_bytes()
    return Pin("assurance/schemas/" + name, 1, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True, slots=True)
class CheckSpec:
    checker_id: str
    checker_version: str
    implementation_hash: str
    assertion_key: str
    execution_kind: str
    input_schema_ref: Pin
    result_schema_ref: Pin
    scope_rule_ref: Pin
    environment_requirements: tuple[str, ...]
    max_runtime_ms: int

    def __post_init__(self) -> None:
        for value in (self.checker_id, self.checker_version, self.assertion_key):
            text(value)
        digest(self.implementation_hash)
        one_of(self.execution_kind, {"LOCAL_RECORDED", "EXECUTOR"})
        if any(
            not isinstance(value, Pin)
            for value in (self.input_schema_ref, self.result_schema_ref, self.scope_rule_ref)
        ):
            raise AssuranceError("CHECK_SCHEMA_PIN_REQUIRED")
        object.__setattr__(
            self,
            "environment_requirements",
            unique_texts(list(self.environment_requirements), maximum=32),
        )
        integer(self.max_runtime_ms, minimum=1, maximum=120_000)
        canonical(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "implementation_hash": self.implementation_hash,
            "assertion_key": self.assertion_key,
            "execution_kind": self.execution_kind,
            "input_schema_ref": self.input_schema_ref.to_json(),
            "result_schema_ref": self.result_schema_ref.to_json(),
            "scope_rule_ref": self.scope_rule_ref.to_json(),
            "environment_requirements": list(self.environment_requirements),
            "max_runtime_ms": self.max_runtime_ms,
        }

    @classmethod
    def from_json(cls, value: object) -> CheckSpec:
        row = fields(
            value,
            {
                "schema_version",
                "checker_id",
                "checker_version",
                "implementation_hash",
                "assertion_key",
                "execution_kind",
                "input_schema_ref",
                "result_schema_ref",
                "scope_rule_ref",
                "environment_requirements",
                "max_runtime_ms",
            },
        )
        if integer(row["schema_version"]) != 1:
            raise AssuranceError("CHECK_SPEC_VERSION")
        args = {key: value for key, value in row.items() if key != "schema_version"}
        for key in ("input_schema_ref", "result_schema_ref", "scope_rule_ref"):
            args[key] = Pin.from_json(args[key])
        args["environment_requirements"] = unique_texts(
            args["environment_requirements"], maximum=32
        )
        return cls(**args)


def local_layer_spec(layer: str, implementation_hash: str) -> CheckSpec:
    one_of(layer, {"format_check", "rule_check"})
    return CheckSpec(
        checker_id="local-verifier:" + layer,
        checker_version="1",
        implementation_hash=implementation_hash,
        assertion_key=f"{layer}:exact-layer-v1",
        execution_kind="LOCAL_RECORDED",
        input_schema_ref=document_pin("local-verification-input-v1.schema.json"),
        result_schema_ref=document_pin("local-check-receipt-v1.schema.json"),
        scope_rule_ref=document_pin("local-layer-scope-v1.json"),
        environment_requirements=(
            "exact_recorder_and_checker_code",
            "exact_python_platform_environment",
        ),
        max_runtime_ms=120_000,
    )
