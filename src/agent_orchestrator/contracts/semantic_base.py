# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Structural primitives shared by the FULL-TARGET-1.4 semantic contracts (P1.1).

Every object in :mod:`.htn`, :mod:`.obligations`, :mod:`.evidence_state` and
:mod:`.resolution` is a frozen dataclass validated in ``__post_init__`` with
:class:`~.models.ContractError`, and round-trips through ``to_json`` /
``from_json`` over canonical JSON so content hashes are stable.  This module
holds the pieces all four need: the boundary validators, the two typed-reference
shapes used by the plan schemas (``evidence_ref`` with seven kinds) and by the
AER annex schemas (twelve kinds), and the versioned reference.

The validators are deliberately strict: an unknown key, a ``str`` where an
integer belongs, or a non-finite number is a ``ContractError``.  That strictness
is what lets the schema fixtures' negative samples be rejected by the codecs
themselves instead of by a JSON-Schema engine the SDK does not depend on.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from .models import ContractError, sha256_hex

MAX_ID = 512
MAX_TEXT = 20_000
MAX_REASON = 10_000
MAX_LIST = 256
MAX_JSON_INT = 9_007_199_254_740_991

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

T = TypeVar("T")
E = TypeVar("E", bound=StrEnum)


def identifier(value: object, name: str, *, limit: int = MAX_ID) -> str:
    """A stable id: non-blank printable text within ``limit`` characters."""

    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    if not value.strip():
        raise ContractError(f"{name} must not be blank")
    if len(value) > limit:
        raise ContractError(f"{name} exceeds {limit} characters")
    if _CONTROL_RE.search(value):
        raise ContractError(f"{name} must not contain control characters")
    return value


def optional_identifier(value: object, name: str, *, limit: int = MAX_ID) -> str | None:
    return None if value is None else identifier(value, name, limit=limit)


def text(value: object, name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    if not value.strip():
        raise ContractError(f"{name} must not be blank")
    if len(value) > limit:
        raise ContractError(f"{name} exceeds {limit} characters")
    return value


def hash_hex(value: object, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def optional_hash_hex(value: object, name: str) -> str | None:
    return None if value is None else hash_hex(value, name)


def index(value: object, name: str, *, minimum: int = 0) -> int:
    """A revision / counter.  ``bool`` is not an integer here, and neither is ``"1"``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    if value < minimum:
        raise ContractError(f"{name} must be >= {minimum}")
    if value > MAX_JSON_INT:
        raise ContractError(f"{name} exceeds the safe JSON integer range")
    return value


def optional_index(value: object, name: str, *, minimum: int = 0) -> int | None:
    return None if value is None else index(value, name, minimum=minimum)


def flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a boolean")
    return value


def json_value(value: object, name: str, *, depth: int = 0) -> Any:
    """Plain JSON only: no callables, no dataclasses, no sets, no NaN."""

    if depth > 32:
        raise ContractError(f"{name} nests deeper than 32 levels")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ContractError(f"{name} must be a finite number")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{name} keys must be strings")
            out[key] = json_value(item, f"{name}.{key}", depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [json_value(item, f"{name}[]", depth=depth + 1) for item in value]
    raise ContractError(f"{name} must be a JSON value, not {type(value).__name__}")


def json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return dict(json_value(value, name))


def reject_executable(value: object, name: str) -> None:
    """A registry / condition entry may never carry code.

    ``eval``-style payloads reach contracts in two shapes: an actual callable, or
    a string that a naive interpreter would hand to ``eval`` / a SQL driver.  Both
    are refused here; the safe interpreter in ``planning.htn.applicability`` only
    ever walks structured AST nodes.
    """

    if callable(value) or isinstance(value, (staticmethod, classmethod)):
        raise ContractError(f"{name} must not be a callable")
    if isinstance(value, str) and _looks_like_code(value):
        raise ContractError(f"{name} must not contain executable code or a query fragment")
    if isinstance(value, Mapping):
        for key, item in value.items():
            reject_executable(key, f"{name}.key")
            reject_executable(item, f"{name}.{key}")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            reject_executable(item, f"{name}[]")


_CODE_MARKERS = (
    "eval(",
    "exec(",
    "lambda ",
    "__import__",
    "import os",
    "subprocess",
    "select ",
    "insert into",
    "update ",
    "delete from",
    "drop table",
    "union all",
    "--;",
)


def _looks_like_code(value: str) -> bool:
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _CODE_MARKERS)


def sequence_of(
    value: object,
    name: str,
    item: Callable[[object, str], T],
    *,
    limit: int = MAX_LIST,
    minimum: int = 0,
) -> tuple[T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name} must be a list")
    items = tuple(item(entry, f"{name}[{position}]") for position, entry in enumerate(value))
    if len(items) < minimum:
        raise ContractError(f"{name} must have at least {minimum} entries")
    if len(items) > limit:
        raise ContractError(f"{name} has more than {limit} entries")
    return items


def identifiers(value: object, name: str, *, limit: int = MAX_LIST) -> tuple[str, ...]:
    items = sequence_of(value, name, lambda entry, where: identifier(entry, where), limit=limit)
    if len(set(items)) != len(items):
        raise ContractError(f"{name} must not contain duplicates")
    return items


def enum_of(kind: type[E], value: object, name: str) -> E:
    if isinstance(value, kind):
        return value
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    try:
        return kind(value)
    except ValueError as error:
        allowed = sorted(str(member) for member in kind)
        raise ContractError(f"{name} must be one of {allowed}") from error


def fields_of(
    value: object,
    name: str,
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
) -> dict[str, Any]:
    """Strict object decoding: every required key present, no unknown key tolerated.

    Unknown keys are the shape every negative fixture in the plan and AER packs
    takes (``model_says_approved``, ``unexpected_authority_override``), and an
    unchecked extra key is exactly how an unchecked authority claim would ride
    into a contract object.
    """

    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    data = dict(value)
    for key in data:
        if not isinstance(key, str):
            raise ContractError(f"{name} keys must be strings")
    missing = [key for key in required if key not in data]
    if missing:
        raise ContractError(f"{name} is missing required fields: {sorted(missing)}")
    unknown = sorted(set(data) - set(required) - set(optional))
    if unknown:
        raise ContractError(f"{name} has unknown fields: {unknown}")
    return data


def schema_version(value: object, name: str, *, expected: int) -> int:
    got = index(value, name, minimum=1)
    if got != expected:
        raise ContractError(f"{name} must be {expected}")
    return got


class Provenance(StrEnum):
    """Who produced the thing a reference points at (AER §8.1).

    This is an attribution, not a verdict: ``SYSTEM`` and ``TOOL`` say a receipt
    came from the orchestrator or from a real dispatched tool, and a model may not
    award itself either one.  ``from_model_json`` is the ingress that enforces it;
    system-side code binds the stronger values after the fact.
    """

    SYSTEM = "system"
    TOOL = "tool"
    MODEL = "model"
    HUMAN = "human"


#: The only attributions a proposal may carry for itself (§18.3, ADR-04).
MODEL_SUBMITTABLE_PROVENANCE = frozenset({Provenance.MODEL, Provenance.HUMAN})


def reject_model_claimed_provenance(value: object, name: str) -> None:
    """Refuse a model-submitted payload that attributes anything to SYSTEM or TOOL.

    Walks the whole payload rather than one level: a fabricated ``tool_receipt``
    attribution three refs deep would otherwise arrive looking like real execution
    evidence.  Attribution is bound by the system after dispatch, never accepted
    from the proposal side.
    """

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "produced_by" and isinstance(item, str):
                try:
                    claimed = Provenance(item)
                except ValueError as error:
                    raise ContractError(f"{name}.produced_by is not a known attribution") from error
                if claimed not in MODEL_SUBMITTABLE_PROVENANCE:
                    raise ContractError(
                        f"{name}.produced_by may not claim {item!r}; a proposal may "
                        "attribute only 'model' or 'human' (the system binds the rest)"
                    )
            else:
                reject_model_claimed_provenance(item, f"{name}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for position, item in enumerate(value):
            reject_model_claimed_provenance(item, f"{name}[{position}]")


class EvidenceRefKind(StrEnum):
    """``evidence_ref.kind`` of the plan's four v1 schemas (§18.1)."""

    OBSERVATION = "observation"
    TOOL_RECEIPT = "tool_receipt"
    ARTIFACT = "artifact"
    KNOWLEDGE = "knowledge"
    REVIEW = "review"
    SOURCE = "source"
    OPERATION = "operation"


class TypedRefKind(StrEnum):
    """``ref.kind`` of the AER annex schemas — a superset of :class:`EvidenceRefKind`."""

    TASK = "task"
    METHOD = "method"
    REQUIREMENTS = "requirements"
    ARTIFACT = "artifact"
    SOURCE = "source"
    OBSERVATION = "observation"
    REVIEW = "review"
    ACCEPTANCE = "acceptance"
    RESOLUTION = "resolution"
    OPERATION = "operation"
    TOOL_RECEIPT = "tool_receipt"
    KNOWLEDGE = "knowledge"


@dataclass(frozen=True, slots=True)
class VersionedRef:
    """An immutable definition reference: id + version + content hash."""

    id: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", identifier(self.id, "versioned_ref.id"))
        object.__setattr__(self, "version", index(self.version, "versioned_ref.version", minimum=1))
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "versioned_ref.content_hash")
        )

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "content_hash": self.content_hash}

    @classmethod
    def from_json(cls, value: object, name: str = "versioned_ref") -> VersionedRef:
        data = fields_of(value, name, required=("id", "version", "content_hash"))
        return cls(
            id=data["id"],
            version=data["version"],
            content_hash=data["content_hash"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A reference to a piece of evidence at a known revision (plan v1 schemas)."""

    kind: EvidenceRefKind
    id: str
    revision: int
    content_hash: str
    produced_by: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", enum_of(EvidenceRefKind, self.kind, "evidence_ref.kind"))
        object.__setattr__(self, "id", identifier(self.id, "evidence_ref.id"))
        object.__setattr__(self, "revision", index(self.revision, "evidence_ref.revision"))
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "evidence_ref.content_hash")
        )
        if self.produced_by is not None:
            object.__setattr__(
                self,
                "produced_by",
                enum_of(Provenance, self.produced_by, "evidence_ref.produced_by"),
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": str(self.kind),
            "id": self.id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }
        # Omitted when unset so an unattributed reference keeps the bytes — and the
        # content hash — it had before this field existed.
        if self.produced_by is not None:
            payload["produced_by"] = str(self.produced_by)
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "evidence_ref") -> EvidenceRef:
        data = fields_of(
            value,
            name,
            required=("kind", "id", "revision", "content_hash"),
            optional=("produced_by",),
        )
        return cls(
            kind=data["kind"],
            id=data["id"],
            revision=data["revision"],
            content_hash=data["content_hash"],
            produced_by=data.get("produced_by"),
        )

    @classmethod
    def from_model_json(cls, value: object, name: str = "evidence_ref") -> EvidenceRef:
        """Decode a reference submitted by a model: it may not claim SYSTEM or TOOL."""

        reject_model_claimed_provenance(value, name)
        return cls.from_json(value, name)


@dataclass(frozen=True, slots=True)
class TypedRef:
    """A discriminated reference used by the AER contracts.

    The ``kind`` is carried on the wire precisely so a role cannot be claimed by
    an id prefix: a slot that wants a task will not accept ``{"kind":
    "obligation"}`` or a bare string, whatever the string starts with.
    """

    kind: TypedRefKind
    id: str
    revision: int
    content_hash: str
    produced_by: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", enum_of(TypedRefKind, self.kind, "typed_ref.kind"))
        object.__setattr__(self, "id", identifier(self.id, "typed_ref.id"))
        object.__setattr__(self, "revision", index(self.revision, "typed_ref.revision"))
        object.__setattr__(
            self, "content_hash", hash_hex(self.content_hash, "typed_ref.content_hash")
        )
        if self.produced_by is not None:
            object.__setattr__(
                self, "produced_by", enum_of(Provenance, self.produced_by, "typed_ref.produced_by")
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": str(self.kind),
            "id": self.id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }
        if self.produced_by is not None:
            payload["produced_by"] = str(self.produced_by)
        return payload

    @classmethod
    def from_json(cls, value: object, name: str = "typed_ref") -> TypedRef:
        data = fields_of(
            value,
            name,
            required=("kind", "id", "revision", "content_hash"),
            optional=("produced_by",),
        )
        return cls(
            kind=data["kind"],
            id=data["id"],
            revision=data["revision"],
            content_hash=data["content_hash"],
            produced_by=data.get("produced_by"),
        )

    @classmethod
    def from_model_json(cls, value: object, name: str = "typed_ref") -> TypedRef:
        """Decode a reference submitted by a model: it may not claim SYSTEM or TOOL."""

        reject_model_claimed_provenance(value, name)
        return cls.from_json(value, name)

    @classmethod
    def of_kind(cls, expected: TypedRefKind, value: object, name: str) -> TypedRef:
        ref = cls.from_json(value, name)
        if ref.kind is not expected:
            raise ContractError(f"{name}.kind must be {expected!s}, not {ref.kind!s}")
        return ref


def content_hash_of(payload: object) -> str:
    """The canonical-JSON SHA-256 of a contract payload (stable across processes)."""

    return sha256_hex(payload)


__all__ = (
    "MAX_ID",
    "MODEL_SUBMITTABLE_PROVENANCE",
    "MAX_JSON_INT",
    "MAX_LIST",
    "MAX_REASON",
    "MAX_TEXT",
    "ContractError",
    "EvidenceRef",
    "EvidenceRefKind",
    "Provenance",
    "TypedRef",
    "TypedRefKind",
    "VersionedRef",
    "content_hash_of",
    "enum_of",
    "fields_of",
    "flag",
    "hash_hex",
    "identifier",
    "identifiers",
    "index",
    "json_object",
    "json_value",
    "optional_hash_hex",
    "optional_identifier",
    "optional_index",
    "reject_executable",
    "reject_model_claimed_provenance",
    "schema_version",
    "sequence_of",
    "text",
)
