# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable, system-authored claim assessments; not a model output protocol."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any

from .models import ContractError, sha256_hex

ASSESSMENT_SCHEMA_VERSION = 1
ASSESSMENT_VERDICTS = frozenset({"PASS", "FAIL", "INCONCLUSIVE", "NEEDS_HUMAN"})


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("assessment JSON keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractError("assessment contains a non-JSON value")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"assessment {name} must be non-empty text")
    return value


def content_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError(f"assessment {name} must be a SHA-256 hash")
    return value


@dataclass(frozen=True, slots=True)
class CriterionAssessmentV1:
    criterion_id: str
    task_contract_revision: str
    claim_id: str
    claim_revision: int
    output_ref: str
    output_hash: str
    evidence_refs: tuple[Mapping[str, Any], ...]
    source_versions: Mapping[str, str]
    verifier_adapter_id: str
    version: str
    checked_scope: Mapping[str, Any]
    verdict: str
    receipt_id: str
    provenance: Mapping[str, Any]
    schema: int = ASSESSMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != ASSESSMENT_SCHEMA_VERSION:
            raise ContractError("unsupported assessment schema")
        for name in (
            "criterion_id",
            "claim_id",
            "output_ref",
            "verifier_adapter_id",
            "version",
            "receipt_id",
        ):
            required_text(getattr(self, name), name)
        for name in ("task_contract_revision", "output_hash"):
            content_hash(getattr(self, name), name)
        if type(self.claim_revision) is not int or self.claim_revision < 1:
            raise ContractError("assessment claim_revision must be a positive integer")
        if self.verdict not in ASSESSMENT_VERDICTS:
            raise ContractError("unknown assessment verdict")
        if not isinstance(self.evidence_refs, (tuple, list)) or not self.evidence_refs:
            raise ContractError("assessment evidence_refs must be non-empty")
        if any(not isinstance(ref, Mapping) or not ref for ref in self.evidence_refs):
            raise ContractError("assessment evidence_refs must contain resolution objects")
        object.__setattr__(self, "evidence_refs", freeze_json(self.evidence_refs))
        for name in ("source_versions", "checked_scope", "provenance"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or not value:
                raise ContractError(f"assessment {name} must be a non-empty object")
            object.__setattr__(self, name, freeze_json(value))
        for path, version in self.source_versions.items():
            required_text(path, "source path")
            content_hash(version, "source version")
        if self.receipt_id != self.expected_receipt_id():
            raise ContractError("assessment receipt does not match its content")

    def to_json(self) -> dict[str, Any]:
        return {field.name: thaw_json(getattr(self, field.name)) for field in fields(self)}

    def expected_receipt_id(self) -> str:
        return "assessment-" + sha256_hex(
            {key: value for key, value in self.to_json().items() if key != "receipt_id"}
        )

    @classmethod
    def create(cls, **values: Any) -> CriterionAssessmentV1:
        data = {"schema": ASSESSMENT_SCHEMA_VERSION, **values}
        data.pop("receipt_id", None)
        data["receipt_id"] = "assessment-" + sha256_hex(data)
        return cls.from_json(data)

    @classmethod
    def from_json(cls, value: object) -> CriterionAssessmentV1:
        if not isinstance(value, Mapping):
            raise ContractError("assessment must be an object")
        expected = {field.name for field in fields(cls)}
        if set(value) != expected:
            raise ContractError("assessment fields are missing or unknown")
        data = dict(value)
        refs = data["evidence_refs"]
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
            raise ContractError("assessment evidence_refs must be a sequence")
        data["evidence_refs"] = tuple(refs)
        return cls(**data)


__all__ = ("ASSESSMENT_SCHEMA_VERSION", "CriterionAssessmentV1")
