# SPDX-License-Identifier: Apache-2.0
"""Immutable identity of the exact inputs used by one TaskGraph Attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, NoReturn

from ..contracts.models import ContractError


def _bad(message: str) -> NoReturn:
    raise ContractError(f"TASKGRAPH_ATTEMPT_INPUT_INVALID: {message}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _bad(f"{field} must be a nonempty identifier of at most 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _bad(f"{field} must be UTF-8")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        _bad(f"{field} must be a nonnegative safe integer")
    return value


def _hash(value: object, field: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        _bad(f"{field} must be a lowercase SHA-256")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptInputBinding:
    attempt_id: str
    mission_id: str
    task_id: str
    occurrence_id: str
    source_revision: int
    binding_revision: int
    contract_hash: str
    input_binding_revision: int
    dispatch_generation: int
    manifest_hash: str
    intent_id: str
    creation_key: str
    input_id: str
    frozen_input_hash: str
    admission_check_id: str | None
    origin_hash: str
    created_at: float

    def __post_init__(self) -> None:
        for field in ("attempt_id", "mission_id", "task_id", "occurrence_id", "intent_id",
                      "creation_key", "input_id"):
            _text(getattr(self, field), field)
        for field in ("source_revision", "binding_revision", "input_binding_revision",
                      "dispatch_generation"):
            _integer(getattr(self, field), field)
        for field in ("contract_hash", "manifest_hash", "frozen_input_hash", "origin_hash"):
            _hash(getattr(self, field), field)
        if self.admission_check_id is not None:
            _text(self.admission_check_id, "admission_check_id")
        if (isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float))
                or not math.isfinite(self.created_at)):
            _bad("created_at must be a number")

    def identity_json(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in (
            "attempt_id", "mission_id", "task_id", "occurrence_id", "source_revision",
            "binding_revision", "contract_hash", "input_binding_revision",
            "dispatch_generation", "manifest_hash", "intent_id", "creation_key", "input_id",
            "frozen_input_hash", "admission_check_id")}


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenAttemptInputs:
    binding: AttemptInputBinding
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, AttemptInputBinding) or not isinstance(self.manifest, Mapping):
            _bad("FrozenAttemptInputs requires a binding and manifest object")
        object.__setattr__(self, "manifest", _freeze(self.manifest))


__all__ = ["AttemptInputBinding", "FrozenAttemptInputs"]
