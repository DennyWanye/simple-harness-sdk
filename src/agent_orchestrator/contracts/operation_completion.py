# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Frozen contracts for the V1.4 operation-completion lane.

These objects describe *what* an accepted contribution covers.  They do not
authorise an operation, interpret a connector receipt, or decide whether a
Mission is complete; those are Commit/reader responsibilities.  Keeping this
module purely structural prevents a caller from turning an arbitrary mapping,
an ``Acceptance`` or a non-empty ``operation_id`` into an effect completion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Self

from simple_harness.contracts import canonical_json

from .models import ContractError
from .semantic_base import (
    content_hash_of,
    enum_of,
    fields_of,
    hash_hex,
    identifier,
    identifiers,
    index,
    schema_version,
    sequence_of,
)

OPERATION_COMPLETION_SCHEMA_VERSION = 1
MAX_COMPLETION_BYTES = 256 * 1024
MAX_COMPLETION_DEPTH = 16
MAX_EFFECT_SLOTS = 64


class CompletionMode(StrEnum):
    CONTENT_ONLY = "CONTENT_ONLY"
    REQUIRED_EFFECTS = "REQUIRED_EFFECTS"


class CompletionScopeRole(StrEnum):
    CONTENT = "CONTENT"
    MIXED = "MIXED"
    AGGREGATE = "AGGREGATE"


class ContributionKind(StrEnum):
    CONTENT = "CONTENT"
    PREPARATION = "PREPARATION"
    OPERATION_EFFECT = "OPERATION_EFFECT"


def _pins(value: object, name: str, *, minimum: int = 0) -> tuple[CompletionPinV1, ...]:
    pins = sequence_of(
        value,
        name,
        lambda item, where: (
            item if isinstance(item, CompletionPinV1) else CompletionPinV1.from_json(item, where)
        ),
        minimum=minimum,
    )
    identity = [(item.id, item.revision, item.content_hash) for item in pins]
    if len(set(identity)) != len(identity):
        raise ContractError(f"{name} must not contain duplicate pins")
    return pins


def _sorted_pins(value: tuple[CompletionPinV1, ...], name: str) -> tuple[CompletionPinV1, ...]:
    ordered = tuple(sorted(value, key=lambda item: (item.id, item.revision, item.content_hash)))
    if value != ordered:
        raise ContractError(f"{name} must be sorted by id, revision, content_hash")
    return value


def _effect_slots(value: object, name: str) -> tuple[RequiredEffectSlotV1, ...]:
    return sequence_of(
        value,
        name,
        lambda item, where: (
            item
            if isinstance(item, RequiredEffectSlotV1)
            else RequiredEffectSlotV1.from_json(item, where)
        ),
        limit=MAX_EFFECT_SLOTS,
    )


def _json_depth(value: object, name: str, depth: int = 0) -> None:
    if depth > MAX_COMPLETION_DEPTH:
        raise ContractError(f"{name} nests deeper than {MAX_COMPLETION_DEPTH} levels")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{name} keys must be strings")
            _json_depth(item, f"{name}.{key}", depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, f"{name}[]", depth + 1)


def _validate_document(value: Mapping[str, Any], name: str) -> None:
    """Apply byte/depth limits to direct constructors as well as byte ingress."""

    _json_depth(value, name)
    if len(canonical_json(dict(value)).encode("utf-8")) > MAX_COMPLETION_BYTES:
        raise ContractError(f"{name} exceeds {MAX_COMPLETION_BYTES} bytes")


def _decode_bytes(value: bytes | bytearray | memoryview, name: str) -> object:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ContractError(f"{name} must be UTF-8 bytes")
    raw = bytes(value)
    if not raw:
        raise ContractError(f"{name} must not be empty")
    if len(raw) > MAX_COMPLETION_BYTES:
        raise ContractError(f"{name} exceeds {MAX_COMPLETION_BYTES} bytes")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, item in pairs:
            if key in out:
                raise ContractError(f"{name} contains duplicate JSON key {key!r}")
            out[key] = item
        return out

    def reject_constant(item: str) -> object:
        raise ContractError(f"{name} must not contain non-finite JSON number {item!r}")

    try:
        decoded = json.loads(
            raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=reject_constant
        )
    except UnicodeDecodeError as error:
        raise ContractError(f"{name} must be UTF-8 JSON") from error
    except (json.JSONDecodeError, RecursionError) as error:
        raise ContractError(f"{name} must be valid JSON") from error
    _json_depth(decoded, name)
    return decoded


class _CanonicalCodec:
    """Small shared codec surface; concrete classes still own their schemas."""

    _schema_name: ClassVar[str]

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_json(cls: type[Self], value: object, name: str) -> Self:
        raise NotImplementedError

    def to_bytes(self) -> bytes:
        payload = self.to_json()
        encoded = canonical_json(payload).encode("utf-8")
        if len(encoded) > MAX_COMPLETION_BYTES:
            raise ContractError(f"{self._schema_name} exceeds {MAX_COMPLETION_BYTES} bytes")
        _json_depth(self.to_json(), self._schema_name)
        return encoded

    @classmethod
    def from_bytes(
        cls: type[Self], value: bytes | bytearray | memoryview, name: str | None = None
    ) -> Self:
        return cls.from_json(
            _decode_bytes(value, name or cls._schema_name), name or cls._schema_name
        )


class _HashDocument(_CanonicalCodec):
    """Codec base for documents whose identity is their full canonical payload."""

    def content_hash(self) -> str:
        return content_hash_of(self.to_json())


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPinV1(_CanonicalCodec):
    """A non-authorising projection of an already resolved typed reference."""

    id: str
    revision: int
    content_hash: str

    _schema_name: ClassVar[str] = "completion_pin"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", identifier(self.id, "completion_pin.id"))
        object.__setattr__(
            self, "revision", index(self.revision, "completion_pin.revision", minimum=1)
        )
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "completion_pin.content_hash")
        )
        _validate_document(self.to_json(), self._schema_name)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision, "content_hash": self.content_hash}

    @classmethod
    def from_json(cls, value: object, name: str = "completion_pin") -> Self:
        data = fields_of(value, name, required=("id", "revision", "content_hash"))
        return cls(id=data["id"], revision=data["revision"], content_hash=data["content_hash"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanRevisionPinV1(_HashDocument):
    """The exact adopted plan revision used when a completion scope was compiled."""

    revision: int
    snapshot_hash: str

    _schema_name: ClassVar[str] = "completion_plan_ref"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revision", index(self.revision, "completion_plan_ref.revision", minimum=1)
        )
        object.__setattr__(
            self, "snapshot_hash", hash_hex(self.snapshot_hash, "completion_plan_ref.snapshot_hash")
        )
        _validate_document(self.to_json(), self._schema_name)

    def to_json(self) -> dict[str, Any]:
        return {"revision": self.revision, "snapshot_hash": self.snapshot_hash}

    @classmethod
    def from_json(cls, value: object, name: str = "completion_plan_ref") -> Self:
        data = fields_of(value, name, required=("revision", "snapshot_hash"))
        return cls(revision=data["revision"], snapshot_hash=data["snapshot_hash"])


@dataclass(frozen=True, slots=True, kw_only=True)
class RequiredEffectSlotV1(_HashDocument):
    effect_key: str
    obligation_id: str
    criterion_ids: tuple[str, ...]
    required_milestone: str
    milestone_policy_ref: CompletionPinV1
    evidence_policy_ref: CompletionPinV1
    source_slot_key: str

    _schema_name: ClassVar[str] = "required_effect_slot"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "effect_key", identifier(self.effect_key, "effect_slot.effect_key")
        )
        object.__setattr__(
            self, "obligation_id", identifier(self.obligation_id, "effect_slot.obligation_id")
        )
        object.__setattr__(
            self, "criterion_ids", identifiers(self.criterion_ids, "effect_slot.criterion_ids")
        )
        if not self.criterion_ids:
            raise ContractError("effect_slot.criterion_ids must not be empty")
        object.__setattr__(
            self,
            "required_milestone",
            identifier(self.required_milestone, "effect_slot.required_milestone"),
        )
        if not isinstance(self.milestone_policy_ref, CompletionPinV1):
            raise ContractError("effect_slot.milestone_policy_ref must be a CompletionPinV1")
        if not isinstance(self.evidence_policy_ref, CompletionPinV1):
            raise ContractError("effect_slot.evidence_policy_ref must be a CompletionPinV1")
        object.__setattr__(
            self, "source_slot_key", identifier(self.source_slot_key, "effect_slot.source_slot_key")
        )
        _validate_document(self.to_json(), self._schema_name)

    def to_json(self) -> dict[str, Any]:
        return {
            "effect_key": self.effect_key,
            "obligation_id": self.obligation_id,
            "criterion_ids": list(self.criterion_ids),
            "required_milestone": self.required_milestone,
            "milestone_policy_ref": self.milestone_policy_ref.to_json(),
            "evidence_policy_ref": self.evidence_policy_ref.to_json(),
            "source_slot_key": self.source_slot_key,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "required_effect_slot") -> Self:
        data = fields_of(
            value,
            name,
            required=(
                "effect_key",
                "obligation_id",
                "criterion_ids",
                "required_milestone",
                "milestone_policy_ref",
                "evidence_policy_ref",
                "source_slot_key",
            ),
        )
        return cls(
            effect_key=data["effect_key"],
            obligation_id=data["obligation_id"],
            criterion_ids=identifiers(data["criterion_ids"], f"{name}.criterion_ids"),
            required_milestone=data["required_milestone"],
            milestone_policy_ref=CompletionPinV1.from_json(
                data["milestone_policy_ref"], f"{name}.milestone_policy_ref"
            ),
            evidence_policy_ref=CompletionPinV1.from_json(
                data["evidence_policy_ref"], f"{name}.evidence_policy_ref"
            ),
            source_slot_key=data["source_slot_key"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationCompletionRequirementsV1(_HashDocument):
    schema_version: int
    mission_id: str
    requirements_ref: CompletionPinV1
    mode: CompletionMode
    content_criterion_ids: tuple[str, ...]
    effects: tuple[RequiredEffectSlotV1, ...]

    _schema_name: ClassVar[str] = "operation_completion_requirements"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "completion_spec.schema_version",
                expected=OPERATION_COMPLETION_SCHEMA_VERSION,
            ),
        )
        object.__setattr__(
            self, "mission_id", identifier(self.mission_id, "completion_spec.mission_id")
        )
        if not isinstance(self.requirements_ref, CompletionPinV1):
            raise ContractError("completion_spec.requirements_ref must be a CompletionPinV1")
        object.__setattr__(self, "mode", enum_of(CompletionMode, self.mode, "completion_spec.mode"))
        object.__setattr__(
            self,
            "content_criterion_ids",
            identifiers(self.content_criterion_ids, "completion_spec.content_criterion_ids"),
        )
        effects = _effect_slots(self.effects, "completion_spec.effects")
        object.__setattr__(self, "effects", effects)
        keys = [item.effect_key for item in effects]
        slots = [item.source_slot_key for item in effects]
        if len(set(keys)) != len(keys):
            raise ContractError("completion_spec.effects must not repeat effect_key")
        if len(set(slots)) != len(slots):
            raise ContractError("completion_spec.effects must not repeat source_slot_key")
        effect_criteria = {criterion for item in effects for criterion in item.criterion_ids}
        if set(self.content_criterion_ids) & effect_criteria:
            raise ContractError("completion_spec content and effect criterion ids must not overlap")
        if self.mode is CompletionMode.CONTENT_ONLY:
            if effects or not self.content_criterion_ids:
                raise ContractError("CONTENT_ONLY requires content criteria and no effects")
        elif not effects:
            raise ContractError("REQUIRED_EFFECTS requires at least one effect")
        _validate_document(self.to_json(), self._schema_name)

    @property
    def spec_id(self) -> str:
        return _derive_id(
            "op-completion-spec",
            self.mission_id,
            self.requirements_ref.revision,
            self.requirements_ref.content_hash,
        )

    def effect(self, effect_key: str) -> RequiredEffectSlotV1:
        key = identifier(effect_key, "effect_key")
        for effect in self.effects:
            if effect.effect_key == key:
                return effect
        raise ContractError(f"completion_spec has no effect_key {key!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "requirements_ref": self.requirements_ref.to_json(),
            "mode": str(self.mode),
            "content_criterion_ids": list(self.content_criterion_ids),
            "effects": [item.to_json() for item in self.effects],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "operation_completion_requirements") -> Self:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "mission_id",
                "requirements_ref",
                "mode",
                "content_criterion_ids",
                "effects",
            ),
        )
        return cls(
            schema_version=data["schema_version"],
            mission_id=data["mission_id"],
            requirements_ref=CompletionPinV1.from_json(
                data["requirements_ref"], f"{name}.requirements_ref"
            ),
            mode=data["mode"],
            content_criterion_ids=identifiers(
                data["content_criterion_ids"], f"{name}.content_criterion_ids"
            ),
            effects=_effect_slots(data["effects"], f"{name}.effects"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OccurrenceCompletionScopeV1(_HashDocument):
    schema_version: int
    mission_id: str
    requirements_ref: CompletionPinV1
    spec_hash: str
    plan_ref: PlanRevisionPinV1
    occurrence_id: str
    task_ref: CompletionPinV1
    obligation_id: str
    role: CompletionScopeRole
    content_criterion_ids: tuple[str, ...]
    required_effect_keys: tuple[str, ...]
    owned_effect_keys: tuple[str, ...]

    _schema_name: ClassVar[str] = "occurrence_completion_scope"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "completion_scope.schema_version",
                expected=OPERATION_COMPLETION_SCHEMA_VERSION,
            ),
        )
        object.__setattr__(
            self, "mission_id", identifier(self.mission_id, "completion_scope.mission_id")
        )
        if not isinstance(self.requirements_ref, CompletionPinV1):
            raise ContractError("completion_scope.requirements_ref must be a CompletionPinV1")
        object.__setattr__(
            self, "spec_hash", hash_hex(self.spec_hash, "completion_scope.spec_hash")
        )
        if not isinstance(self.plan_ref, PlanRevisionPinV1):
            raise ContractError("completion_scope.plan_ref must be a PlanRevisionPinV1")
        object.__setattr__(
            self, "occurrence_id", identifier(self.occurrence_id, "completion_scope.occurrence_id")
        )
        if not isinstance(self.task_ref, CompletionPinV1):
            raise ContractError("completion_scope.task_ref must be a CompletionPinV1")
        object.__setattr__(
            self, "obligation_id", identifier(self.obligation_id, "completion_scope.obligation_id")
        )
        object.__setattr__(
            self, "role", enum_of(CompletionScopeRole, self.role, "completion_scope.role")
        )
        object.__setattr__(
            self,
            "content_criterion_ids",
            identifiers(self.content_criterion_ids, "completion_scope.content_criterion_ids"),
        )
        object.__setattr__(
            self,
            "required_effect_keys",
            identifiers(self.required_effect_keys, "completion_scope.required_effect_keys"),
        )
        object.__setattr__(
            self,
            "owned_effect_keys",
            identifiers(self.owned_effect_keys, "completion_scope.owned_effect_keys"),
        )
        if not set(self.owned_effect_keys).issubset(self.required_effect_keys):
            raise ContractError(
                "completion_scope.owned_effect_keys must be a subset of required_effect_keys"
            )
        if self.role is CompletionScopeRole.CONTENT:
            if self.required_effect_keys or self.owned_effect_keys:
                raise ContractError("CONTENT scope must not require or own effects")
            if not self.content_criterion_ids:
                raise ContractError("CONTENT scope must cover at least one content criterion")
        elif self.role is CompletionScopeRole.MIXED:
            if not self.required_effect_keys:
                raise ContractError("MIXED scope must require at least one effect")
        elif not self.required_effect_keys:
            raise ContractError("AGGREGATE scope must require at least one effect")
        _validate_document(self.to_json(), self._schema_name)

    @property
    def scope_id(self) -> str:
        return _derive_id(
            "op-completion-scope", self.mission_id, self.plan_ref.revision, self.occurrence_id
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "requirements_ref": self.requirements_ref.to_json(),
            "spec_hash": self.spec_hash,
            "plan_ref": self.plan_ref.to_json(),
            "occurrence_id": self.occurrence_id,
            "task_ref": self.task_ref.to_json(),
            "obligation_id": self.obligation_id,
            "role": str(self.role),
            "content_criterion_ids": list(self.content_criterion_ids),
            "required_effect_keys": list(self.required_effect_keys),
            "owned_effect_keys": list(self.owned_effect_keys),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "occurrence_completion_scope") -> Self:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "mission_id",
                "requirements_ref",
                "spec_hash",
                "plan_ref",
                "occurrence_id",
                "task_ref",
                "obligation_id",
                "role",
                "content_criterion_ids",
                "required_effect_keys",
                "owned_effect_keys",
            ),
        )
        return cls(
            schema_version=data["schema_version"],
            mission_id=data["mission_id"],
            requirements_ref=CompletionPinV1.from_json(
                data["requirements_ref"], f"{name}.requirements_ref"
            ),
            spec_hash=data["spec_hash"],
            plan_ref=PlanRevisionPinV1.from_json(data["plan_ref"], f"{name}.plan_ref"),
            occurrence_id=data["occurrence_id"],
            task_ref=CompletionPinV1.from_json(data["task_ref"], f"{name}.task_ref"),
            obligation_id=data["obligation_id"],
            role=data["role"],
            content_criterion_ids=identifiers(
                data["content_criterion_ids"], f"{name}.content_criterion_ids"
            ),
            required_effect_keys=identifiers(
                data["required_effect_keys"], f"{name}.required_effect_keys"
            ),
            owned_effect_keys=identifiers(data["owned_effect_keys"], f"{name}.owned_effect_keys"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceContributionScopeV1(_HashDocument):
    schema_version: int
    mission_id: str
    acceptance_id: str
    completion_scope_id: str
    spec_hash: str
    kind: ContributionKind
    content_criterion_ids: tuple[str, ...]
    effect_keys: tuple[str, ...]
    output_artifact_refs: tuple[CompletionPinV1, ...]
    outcome_binding_id: str | None
    delivery_receipt_ref: CompletionPinV1 | None

    _schema_name: ClassVar[str] = "acceptance_contribution_scope"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "contribution.schema_version",
                expected=OPERATION_COMPLETION_SCHEMA_VERSION,
            ),
        )
        object.__setattr__(
            self, "mission_id", identifier(self.mission_id, "contribution.mission_id")
        )
        object.__setattr__(
            self, "acceptance_id", identifier(self.acceptance_id, "contribution.acceptance_id")
        )
        object.__setattr__(
            self,
            "completion_scope_id",
            identifier(self.completion_scope_id, "contribution.completion_scope_id"),
        )
        object.__setattr__(self, "spec_hash", hash_hex(self.spec_hash, "contribution.spec_hash"))
        object.__setattr__(self, "kind", enum_of(ContributionKind, self.kind, "contribution.kind"))
        object.__setattr__(
            self,
            "content_criterion_ids",
            identifiers(self.content_criterion_ids, "contribution.content_criterion_ids"),
        )
        object.__setattr__(
            self, "effect_keys", identifiers(self.effect_keys, "contribution.effect_keys")
        )
        object.__setattr__(
            self,
            "output_artifact_refs",
            _pins(self.output_artifact_refs, "contribution.output_artifact_refs"),
        )
        if self.outcome_binding_id is not None:
            object.__setattr__(
                self,
                "outcome_binding_id",
                identifier(self.outcome_binding_id, "contribution.outcome_binding_id"),
            )
        if self.delivery_receipt_ref is not None and not isinstance(
            self.delivery_receipt_ref, CompletionPinV1
        ):
            raise ContractError(
                "contribution.delivery_receipt_ref must be a CompletionPinV1 or null"
            )
        if self.kind is ContributionKind.OPERATION_EFFECT:
            if (
                len(self.effect_keys) != 1
                or self.content_criterion_ids
                or self.output_artifact_refs
            ):
                raise ContractError(
                    "OPERATION_EFFECT must have exactly one effect and no content/artifact refs"
                )
            if self.outcome_binding_id is None:
                raise ContractError("OPERATION_EFFECT requires outcome_binding_id")
        else:
            if (
                self.effect_keys
                or self.outcome_binding_id is not None
                or self.delivery_receipt_ref is not None
            ):
                raise ContractError(
                    "CONTENT/PREPARATION must not carry effect outcome or delivery references"
                )
            if not self.content_criterion_ids and not self.output_artifact_refs:
                raise ContractError("CONTENT/PREPARATION must cover reviewed content or artifacts")
        _validate_document(self.to_json(), self._schema_name)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "acceptance_id": self.acceptance_id,
            "completion_scope_id": self.completion_scope_id,
            "spec_hash": self.spec_hash,
            "kind": str(self.kind),
            "content_criterion_ids": list(self.content_criterion_ids),
            "effect_keys": list(self.effect_keys),
            "output_artifact_refs": [item.to_json() for item in self.output_artifact_refs],
            "outcome_binding_id": self.outcome_binding_id,
            "delivery_receipt_ref": None
            if self.delivery_receipt_ref is None
            else self.delivery_receipt_ref.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "acceptance_contribution_scope") -> Self:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "mission_id",
                "acceptance_id",
                "completion_scope_id",
                "spec_hash",
                "kind",
                "content_criterion_ids",
                "effect_keys",
                "output_artifact_refs",
                "outcome_binding_id",
                "delivery_receipt_ref",
            ),
        )
        return cls(
            schema_version=data["schema_version"],
            mission_id=data["mission_id"],
            acceptance_id=data["acceptance_id"],
            completion_scope_id=data["completion_scope_id"],
            spec_hash=data["spec_hash"],
            kind=data["kind"],
            content_criterion_ids=identifiers(
                data["content_criterion_ids"], f"{name}.content_criterion_ids"
            ),
            effect_keys=identifiers(data["effect_keys"], f"{name}.effect_keys"),
            output_artifact_refs=_pins(
                data["output_artifact_refs"], f"{name}.output_artifact_refs"
            ),
            outcome_binding_id=data["outcome_binding_id"],
            delivery_receipt_ref=(
                None
                if data["delivery_receipt_ref"] is None
                else CompletionPinV1.from_json(
                    data["delivery_receipt_ref"], f"{name}.delivery_receipt_ref"
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationOutcomeReviewBindingV1(_HashDocument):
    schema_version: int
    mission_id: str
    spec_hash: str
    effect_key: str
    completion_scope_id: str
    intent_id: str
    operation_id: str
    operation_occurrence_id: str
    action_key: str
    action_version: int
    request_hash: str
    candidate_file_hash: str
    parameters_content_hash: str
    params_hash: str
    effect_contract_hash: str
    link_hash: str
    connector_profile_hash: str
    namespace_hash: str
    target_identity_hash: str
    milestone_policy_ref: CompletionPinV1
    observed_milestone: str
    covered_handoff_ids: tuple[str, ...]
    source_receipt_refs: tuple[CompletionPinV1, ...]

    _schema_name: ClassVar[str] = "operation_outcome_review_binding"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            schema_version(
                self.schema_version,
                "outcome_binding.schema_version",
                expected=OPERATION_COMPLETION_SCHEMA_VERSION,
            ),
        )
        for name in (
            "mission_id",
            "effect_key",
            "completion_scope_id",
            "intent_id",
            "operation_id",
            "operation_occurrence_id",
            "action_key",
            "observed_milestone",
        ):
            object.__setattr__(
                self, name, identifier(getattr(self, name), f"outcome_binding.{name}")
            )
        object.__setattr__(self, "spec_hash", hash_hex(self.spec_hash, "outcome_binding.spec_hash"))
        object.__setattr__(
            self,
            "action_version",
            index(self.action_version, "outcome_binding.action_version", minimum=1),
        )
        for name in (
            "request_hash",
            "candidate_file_hash",
            "parameters_content_hash",
            "params_hash",
            "effect_contract_hash",
            "link_hash",
            "connector_profile_hash",
            "namespace_hash",
            "target_identity_hash",
        ):
            object.__setattr__(self, name, hash_hex(getattr(self, name), f"outcome_binding.{name}"))
        if not isinstance(self.milestone_policy_ref, CompletionPinV1):
            raise ContractError("outcome_binding.milestone_policy_ref must be a CompletionPinV1")
        handoffs = identifiers(self.covered_handoff_ids, "outcome_binding.covered_handoff_ids")
        if not handoffs:
            raise ContractError("outcome_binding.covered_handoff_ids must not be empty")
        if handoffs != tuple(sorted(handoffs)):
            raise ContractError("outcome_binding.covered_handoff_ids must be sorted")
        object.__setattr__(self, "covered_handoff_ids", handoffs)
        refs = _pins(self.source_receipt_refs, "outcome_binding.source_receipt_refs", minimum=1)
        object.__setattr__(
            self, "source_receipt_refs", _sorted_pins(refs, "outcome_binding.source_receipt_refs")
        )
        _validate_document(self.to_json(), self._schema_name)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "spec_hash": self.spec_hash,
            "effect_key": self.effect_key,
            "completion_scope_id": self.completion_scope_id,
            "intent_id": self.intent_id,
            "operation_id": self.operation_id,
            "operation_occurrence_id": self.operation_occurrence_id,
            "action_key": self.action_key,
            "action_version": self.action_version,
            "request_hash": self.request_hash,
            "candidate_file_hash": self.candidate_file_hash,
            "parameters_content_hash": self.parameters_content_hash,
            "params_hash": self.params_hash,
            "effect_contract_hash": self.effect_contract_hash,
            "link_hash": self.link_hash,
            "connector_profile_hash": self.connector_profile_hash,
            "namespace_hash": self.namespace_hash,
            "target_identity_hash": self.target_identity_hash,
            "milestone_policy_ref": self.milestone_policy_ref.to_json(),
            "observed_milestone": self.observed_milestone,
            "covered_handoff_ids": list(self.covered_handoff_ids),
            "source_receipt_refs": [item.to_json() for item in self.source_receipt_refs],
        }

    @classmethod
    def from_json(cls, value: object, name: str = "operation_outcome_review_binding") -> Self:
        required = (
            "schema_version",
            "mission_id",
            "spec_hash",
            "effect_key",
            "completion_scope_id",
            "intent_id",
            "operation_id",
            "operation_occurrence_id",
            "action_key",
            "action_version",
            "request_hash",
            "candidate_file_hash",
            "parameters_content_hash",
            "params_hash",
            "effect_contract_hash",
            "link_hash",
            "connector_profile_hash",
            "namespace_hash",
            "target_identity_hash",
            "milestone_policy_ref",
            "observed_milestone",
            "covered_handoff_ids",
            "source_receipt_refs",
        )
        data = fields_of(value, name, required=required)
        return cls(
            **{
                field: data[field]
                for field in required
                if field
                not in {"milestone_policy_ref", "covered_handoff_ids", "source_receipt_refs"}
            },
            milestone_policy_ref=CompletionPinV1.from_json(
                data["milestone_policy_ref"], f"{name}.milestone_policy_ref"
            ),
            covered_handoff_ids=identifiers(
                data["covered_handoff_ids"], f"{name}.covered_handoff_ids"
            ),
            source_receipt_refs=_pins(
                data["source_receipt_refs"], f"{name}.source_receipt_refs", minimum=1
            ),
        )


def _derive_id(role: str, *parts: object) -> str:
    """Use the existing canonical derive shape without importing planner modules."""

    return f"{role}-{content_hash_of([role, *[str(part) for part in parts]])[:32]}"


def derive_outcome_binding_id(
    *,
    intent_id: str,
    spec_hash: str,
    effect_key: str,
    completion_scope_hash: str,
    source_manifest_hash: str,
) -> str:
    """Derive an outcome-review identity from producer-supplied frozen hashes.

    The caller must build ``source_manifest_hash`` from the actual receipt adapter,
    source receipts and covered handoffs.  This contract module has no adapter
    registry and must not substitute a partial manifest.
    """

    return _derive_id(
        "op-outcome-review",
        identifier(intent_id, "outcome_binding.intent_id"),
        hash_hex(spec_hash, "outcome_binding.spec_hash"),
        identifier(effect_key, "outcome_binding.effect_key"),
        hash_hex(completion_scope_hash, "outcome_binding.completion_scope_hash"),
        hash_hex(source_manifest_hash, "outcome_binding.source_manifest_hash"),
    )


def validate_scope_owners(
    spec: OperationCompletionRequirementsV1,
    scopes: Sequence[OccurrenceCompletionScopeV1],
) -> None:
    """Require exactly one declared owner for every approved required effect."""

    expected = {effect.effect_key for effect in spec.effects}
    owners: dict[str, list[str]] = {key: [] for key in expected}
    for scope in scopes:
        if scope.mission_id != spec.mission_id or scope.requirements_ref != spec.requirements_ref:
            raise ContractError("completion scope does not belong to the Spec mission/requirements")
        if scope.spec_hash != spec.content_hash():
            raise ContractError("completion scope spec_hash does not match the Spec")
        unknown = set(scope.required_effect_keys) - expected
        if unknown:
            raise ContractError(f"completion scope requires unknown effect keys {sorted(unknown)}")
        for effect_key in scope.owned_effect_keys:
            owners[effect_key].append(scope.scope_id)
    missing = sorted(key for key, values in owners.items() if not values)
    duplicate = sorted(key for key, values in owners.items() if len(values) != 1)
    if missing or duplicate:
        raise ContractError(
            f"completion effect owners must be unique; missing={missing}, nonunique={duplicate}"
        )


def validate_spec_scope_coverage(
    spec: OperationCompletionRequirementsV1,
    scopes: Sequence[OccurrenceCompletionScopeV1],
    *,
    root_occurrence_id: str,
    local_content_criterion_ids_by_occurrence: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """Pure compile-time checks for approved requirement coverage in frozen scopes.

    ``root_occurrence_id`` comes from the already adopted plan.  An arbitrary
    aggregate-shaped scope cannot stand in for that root, and a primitive MIXED
    root is valid without introducing a fake compound.
    """

    if not scopes:
        raise ContractError("completion scopes must not be empty")
    root_id = identifier(root_occurrence_id, "root_occurrence_id")
    expected_content = set(spec.content_criterion_ids)
    expected_effects = {effect.effect_key for effect in spec.effects}
    # Supplied only by the registry-backed compiler, from exact Task contracts and
    # the fixed local review policy. Local review is never Spec satisfaction.
    local_by_occurrence = {
        identifier(occurrence, "local_content.occurrence_id"): set(
            identifiers(values, "local_content.criterion_ids")
        )
        for occurrence, values in (local_content_criterion_ids_by_occurrence or {}).items()
    }
    if set(local_by_occurrence) - {scope.occurrence_id for scope in scopes}:
        raise ContractError("local content projection names an absent occurrence")
    if any(values & expected_content for values in local_by_occurrence.values()):
        raise ContractError("local content projection must not redefine a Spec criterion")
    scope_ids: set[str] = set()
    plan_refs: set[tuple[int, str]] = set()
    content_coverage: set[str] = set()
    root_scope: OccurrenceCompletionScopeV1 | None = None
    for scope in scopes:
        if scope.mission_id != spec.mission_id or scope.requirements_ref != spec.requirements_ref:
            raise ContractError("completion scope does not belong to the Spec mission/requirements")
        if scope.spec_hash != spec.content_hash():
            raise ContractError("completion scope spec_hash does not match the Spec")
        if scope.scope_id in scope_ids:
            raise ContractError("completion scopes must not repeat an occurrence identity")
        scope_ids.add(scope.scope_id)
        plan_refs.add((scope.plan_ref.revision, scope.plan_ref.snapshot_hash))
        if scope.occurrence_id == root_id:
            if root_scope is not None:
                raise ContractError("completion scopes must have exactly one root occurrence")
            root_scope = scope
        local_ids = local_by_occurrence.get(scope.occurrence_id, set())
        if set(scope.content_criterion_ids) - expected_content - local_ids:
            raise ContractError(
                "completion scope covers criterion ids absent from the Spec content set"
            )
        if set(scope.required_effect_keys) - expected_effects:
            raise ContractError("completion scope requires effect keys absent from the Spec")
        content_coverage.update(set(scope.content_criterion_ids) & expected_content)
    if len(plan_refs) != 1:
        raise ContractError("completion scopes must share one adopted plan revision")
    if root_scope is None:
        raise ContractError("completion scopes omit the adopted root occurrence")
    if set(root_scope.content_criterion_ids) & expected_content != expected_content:
        raise ContractError("the adopted root scope must cover every Spec content criterion")
    if content_coverage != expected_content:
        raise ContractError("completion scopes must cover every and only Spec content criterion")
    if spec.mode is CompletionMode.CONTENT_ONLY:
        if any(scope.required_effect_keys or scope.owned_effect_keys for scope in scopes):
            raise ContractError("CONTENT_ONLY Spec cannot compile effect scopes")
        return
    if root_scope.role not in {CompletionScopeRole.MIXED, CompletionScopeRole.AGGREGATE}:
        raise ContractError("a REQUIRED_EFFECTS root must be MIXED or AGGREGATE")
    if set(root_scope.required_effect_keys) != expected_effects:
        raise ContractError("the adopted root scope must require every Spec effect")
    validate_scope_owners(spec, scopes)


__all__ = (
    "OPERATION_COMPLETION_SCHEMA_VERSION",
    "MAX_COMPLETION_BYTES",
    "MAX_COMPLETION_DEPTH",
    "MAX_EFFECT_SLOTS",
    "CompletionMode",
    "CompletionScopeRole",
    "ContributionKind",
    "CompletionPinV1",
    "PlanRevisionPinV1",
    "RequiredEffectSlotV1",
    "OperationCompletionRequirementsV1",
    "OccurrenceCompletionScopeV1",
    "AcceptanceContributionScopeV1",
    "OperationOutcomeReviewBindingV1",
    "derive_outcome_binding_id",
    "validate_scope_owners",
    "validate_spec_scope_coverage",
)
