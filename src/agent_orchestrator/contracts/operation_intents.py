# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Strict authenticated input contract for completion-bound operation intent V2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from .models import ContractError
from .semantic_base import TypedRef, TypedRefKind, fields_of, hash_hex, identifier, sequence_of


class OperationIntentSourceKind(StrEnum):
    USER_COMMAND = "USER_COMMAND"
    AUTHORIZED_SLOT = "AUTHORIZED_SLOT"


@dataclass(frozen=True, slots=True)
class OperationIntentSourceV2:
    kind: OperationIntentSourceKind
    origin_receipt_id: str | None = None
    slot_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", OperationIntentSourceKind(str(self.kind)))
        if self.kind is OperationIntentSourceKind.USER_COMMAND:
            if self.origin_receipt_id is not None or self.slot_key is not None:
                raise ContractError("USER_COMMAND cannot carry origin authority")
        elif self.origin_receipt_id is None or self.slot_key is None:
            raise ContractError("AUTHORIZED_SLOT requires origin_receipt_id and slot_key")
        else:
            object.__setattr__(
                self,
                "origin_receipt_id",
                identifier(self.origin_receipt_id, "origin_receipt_id"),
            )
            object.__setattr__(self, "slot_key", identifier(self.slot_key, "slot_key"))

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": str(self.kind)}
        if self.kind is OperationIntentSourceKind.AUTHORIZED_SLOT:
            value.update(origin_receipt_id=self.origin_receipt_id, slot_key=self.slot_key)
        return value

    @classmethod
    def from_json(cls, value: object, name: str = "intent_source") -> Self:
        data = fields_of(
            value, name, required=("kind",), optional=("origin_receipt_id", "slot_key")
        )
        if data["kind"] == "USER_COMMAND" and set(data) != {"kind"}:
            raise ContractError("USER_COMMAND cannot carry origin authority fields")
        return cls(data["kind"], data.get("origin_receipt_id"), data.get("slot_key"))


@dataclass(frozen=True, slots=True)
class CompletionSlotV2:
    spec_hash: str
    effect_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "spec_hash", hash_hex(self.spec_hash, "completion_slot.spec_hash"))
        object.__setattr__(
            self, "effect_key", identifier(self.effect_key, "completion_slot.effect_key")
        )

    def to_json(self) -> dict[str, Any]:
        return {"spec_hash": self.spec_hash, "effect_key": self.effect_key}

    @classmethod
    def from_json(cls, value: object, name: str = "completion_slot") -> Self:
        data = fields_of(value, name, required=("spec_hash", "effect_key"))
        return cls(data["spec_hash"], data["effect_key"])


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitOperationIntentV2:
    schema_version: int
    mission_id: str
    idempotency_key: str
    intent_source: OperationIntentSourceV2
    candidate_artifact_ref: TypedRef
    prepared_acceptance_refs: tuple[TypedRef, ...]
    supersedes_intent_id: str | None
    completion_slot: CompletionSlotV2

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ContractError("operation intent schema_version must be 2")
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "mission_id"))
        object.__setattr__(
            self, "idempotency_key", identifier(self.idempotency_key, "idempotency_key")
        )
        if not isinstance(self.intent_source, OperationIntentSourceV2):
            raise ContractError("intent_source must be OperationIntentSourceV2")
        if self.candidate_artifact_ref.kind is not TypedRefKind.ARTIFACT:
            raise ContractError("candidate_artifact_ref must be an artifact ref")
        refs = tuple(self.prepared_acceptance_refs)
        if not refs or any(ref.kind is not TypedRefKind.ACCEPTANCE for ref in refs):
            raise ContractError("prepared_acceptance_refs must be nonempty acceptance refs")
        if len({(ref.id, ref.revision, ref.content_hash) for ref in refs}) != len(refs):
            raise ContractError("prepared_acceptance_refs must not repeat")
        object.__setattr__(self, "prepared_acceptance_refs", refs)
        if self.supersedes_intent_id is not None:
            object.__setattr__(
                self,
                "supersedes_intent_id",
                identifier(self.supersedes_intent_id, "supersedes_intent_id"),
            )
        if not isinstance(self.completion_slot, CompletionSlotV2):
            raise ContractError("completion_slot must be CompletionSlotV2")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "mission_id": self.mission_id,
            "idempotency_key": self.idempotency_key,
            "intent_source": self.intent_source.to_json(),
            "candidate_artifact_ref": self.candidate_artifact_ref.to_json(),
            "prepared_acceptance_refs": [ref.to_json() for ref in self.prepared_acceptance_refs],
            "supersedes_intent_id": self.supersedes_intent_id,
            "completion_slot": self.completion_slot.to_json(),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "submit_operation_intent_v2") -> Self:
        data = fields_of(
            value,
            name,
            required=(
                "schema_version",
                "mission_id",
                "idempotency_key",
                "intent_source",
                "candidate_artifact_ref",
                "prepared_acceptance_refs",
                "supersedes_intent_id",
                "completion_slot",
            ),
        )
        refs = sequence_of(
            data["prepared_acceptance_refs"],
            f"{name}.prepared_acceptance_refs",
            TypedRef.from_json,
            minimum=1,
        )
        return cls(
            schema_version=data["schema_version"],
            mission_id=data["mission_id"],
            idempotency_key=data["idempotency_key"],
            intent_source=OperationIntentSourceV2.from_json(data["intent_source"]),
            candidate_artifact_ref=TypedRef.from_json(data["candidate_artifact_ref"]),
            prepared_acceptance_refs=refs,
            supersedes_intent_id=data["supersedes_intent_id"],
            completion_slot=CompletionSlotV2.from_json(data["completion_slot"]),
        )


__all__ = (
    "CompletionSlotV2",
    "OperationIntentSourceKind",
    "OperationIntentSourceV2",
    "SubmitOperationIntentV2",
)
