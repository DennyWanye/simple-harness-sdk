# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P34 fragment proposals and system-authored, immutable scope projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from .assessments import content_hash, freeze_json, thaw_json
from .models import ContractError, canonical_json, sha256_hex

FRAGMENT_SCHEMA_VERSION = 1
SCOPE_PROJECTION_VERSION = "whole-criterion-v1"


def _object(value: Any, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != names:
        raise ContractError(f"fragment {label} fields missing or unknown")
    return dict(value)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("fragment text must be nonempty")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError("fragment integer out of range")
    return value


def _list(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError("fragment list required")
    # dataclasses.replace reuses the already frozen MappingProxyType members.
    # Compare their wire representation without weakening duplicate validation.
    if len({canonical_json(thaw_json(item)) for item in value}) != len(value):
        raise ContractError("fragment duplicate entry")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class FragmentProposalV1:
    origin: Mapping[str, Any]
    criterion_ids: tuple[str, ...]
    claim_refs: tuple[Mapping[str, Any], ...]
    material_refs: tuple[Mapping[str, Any], ...]
    rationale: str
    schema_version: int = FRAGMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported fragment schema")
        origin = _object(
            self.origin,
            {"mission_id", "task_id", "attempt_id", "result_id", "task_revision_id"},
            "origin",
        )
        for value in origin.values():
            _text(value)
        content_hash(origin["task_revision_id"], "fragment revision")
        criteria = _list(self.criterion_ids)
        if not criteria:
            raise ContractError("fragment criterion_ids must be nonempty")
        for value in criteria:
            _text(value)
        claims = _list(self.claim_refs)
        for value in claims:
            item = _object(value, {"claim_id", "claim_revision"}, "claim")
            _text(item["claim_id"])
            _integer(item["claim_revision"], 1)
        if len({item["claim_id"] for item in claims}) != len(claims):
            raise ContractError("fragment duplicate claim identity")
        materials = _list(self.material_refs)
        if not materials:
            raise ContractError("fragment material_refs must be nonempty")
        for value in materials:
            if not isinstance(value, Mapping):
                raise ContractError("fragment material must be an object")
            if value.get("kind") == "artifact":
                item = _object(
                    value,
                    {"kind", "artifact_id", "content_hash", "byte_start", "byte_end_exclusive"},
                    "artifact",
                )
                _text(item["artifact_id"])
                content_hash(item["content_hash"], "fragment artifact")
                if _integer(item["byte_end_exclusive"], 1) <= _integer(item["byte_start"]):
                    raise ContractError("fragment artifact range must be nonempty")
            elif value.get("kind") == "citation":
                item = _object(value, {"kind", "receipt_id", "citation_index"}, "citation")
                _text(item["receipt_id"])
                _integer(item["citation_index"])
            else:
                raise ContractError("unknown fragment material kind")
        _text(self.rationale)
        object.__setattr__(self, "origin", freeze_json(origin))
        object.__setattr__(self, "criterion_ids", tuple(criteria))
        object.__setattr__(self, "claim_refs", freeze_json(claims))
        object.__setattr__(self, "material_refs", freeze_json(materials))

    def to_json(self) -> dict[str, Any]:
        return {field.name: thaw_json(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_json(cls, value: Any) -> FragmentProposalV1:
        return cls(**_object(value, {field.name for field in fields(cls)}, "proposal"))


@dataclass(frozen=True, slots=True)
class TaskRevisionV1:
    mission_id: str
    task_id: str
    origin_intent_id: str
    observed_task_version: int
    contract: Mapping[str, Any]
    task_contract_revision: str
    mission_contract_revision: str
    execution_constraints: Mapping[str, Any]
    constraints_revision: str
    criteria: tuple[Mapping[str, Any], ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("contract", "execution_constraints", "criteria"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

    @property
    def revision_id(self) -> str:
        return sha256_hex(
            {
                name: getattr(self, name)
                for name in (
                    "schema_version",
                    "mission_id",
                    "task_id",
                    "task_contract_revision",
                    "mission_contract_revision",
                    "constraints_revision",
                )
            }
        )

    def to_json(self) -> dict[str, Any]:
        return {field.name: thaw_json(getattr(self, field.name)) for field in fields(self)} | {
            "revision_id": self.revision_id
        }


@dataclass(frozen=True, slots=True)
class ScopeProjectionV1:
    fragment_id: str
    origin_revision: Mapping[str, Any]
    criteria: tuple[Mapping[str, Any], ...]
    outside_scope: tuple[Mapping[str, Any], ...]
    claim_refs: tuple[Mapping[str, Any], ...]
    material_refs: tuple[Mapping[str, Any], ...]
    input_closure: Mapping[str, Any]
    projection_version: str = SCOPE_PROJECTION_VERSION

    def __post_init__(self) -> None:
        for name in (
            "origin_revision",
            "criteria",
            "outside_scope",
            "claim_refs",
            "material_refs",
            "input_closure",
        ):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

    def to_json(self) -> dict[str, Any]:
        return {field.name: thaw_json(getattr(self, field.name)) for field in fields(self)}

    @property
    def projection_hash(self) -> str:
        return sha256_hex(self.to_json())
