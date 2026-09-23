# SPDX-License-Identifier: Apache-2.0
"""Small, strict execution-preview contracts (schema preview-binding-v1)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, NoReturn, TypeVar

from simple_harness.contracts import canonical_json

from ..contracts.models import ContractError
from ..runtime.planning_operations import SourceUnavailable

MAX_JSON_INT = 9_007_199_254_740_991
MAX_IDENTIFIER = 512
_SOURCE_CHANNELS = (
    "request",
    "authorization",
    "network",
    "methods",
    "evidence",
    "capabilities",
    "operations",
    "running_work",
    "acceptances",
    "demand",
    "budget",
    "inputs",
)
_PREVIEW_KEYS = frozenset(
    {
        "schema_version",
        "decision_id",
        "request_id",
        "mission_id",
        "base_revision",
        "decision_hash",
        "network_hash",
        "candidate_hash",
        "delta_hash",
        "read_set_hash",
        "validator_id",
        "validator_version",
        "source_reads",
        "pending_compound_ids",
        "required_convergence_ids",
    }
)
_SOURCE_KEYS = frozenset({"channel", "identity", "digest", "coverage"})


def _fail(field: str, detail: str) -> NoReturn:
    raise ContractError(f"{field}: {detail}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(field, "must be a nonempty string")
    if len(value) > MAX_IDENTIFIER:
        _fail(field, "exceeds 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(field, "must be valid UTF-8")
    return value


def _hash(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        _fail(field, "must be lowercase 64-character hexadecimal")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_JSON_INT:
        _fail(field, "must be an integer from 0 through 9007199254740991")
    return value


def _ids(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(field, "must be an array")
    if len(value) > 4096:
        _fail(field, "exceeds 4096 items")
    items = tuple(_text(item, f"{field}[]") for item in value)
    if len(set(items)) != len(items):
        _fail(field, "must not contain duplicates")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphSourceRead:
    channel: str
    identity: str
    digest: str
    coverage: str

    def __post_init__(self) -> None:
        channel = _text(self.channel, "channel")
        if channel not in _SOURCE_CHANNELS:
            _fail("channel", "is unknown")
        _text(self.identity, "identity")
        _hash(self.digest, "digest")
        if self.coverage != "COMPLETE":
            _fail("coverage", "must be COMPLETE")

    def to_json(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "identity": self.identity,
            "digest": self.digest,
            "coverage": self.coverage,
        }

    @classmethod
    def from_json(cls, value: object) -> GraphSourceRead:
        if not isinstance(value, Mapping):
            _fail("source_read", "must be an object")
        if any(not isinstance(key, str) for key in value):
            _fail("source_read", "keys must be strings")
        unknown = set(value) - _SOURCE_KEYS
        if unknown:
            _fail("source_read", f"unknown fields: {sorted(unknown)!r}")
        if set(value) != _SOURCE_KEYS:
            _fail("source_read", "all fields are required")
        return cls(
            channel=value["channel"],
            identity=value["identity"],
            digest=value["digest"],
            coverage=value["coverage"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreviewBindingV1:
    decision_id: str
    request_id: str
    mission_id: str
    base_revision: int
    decision_hash: str
    network_hash: str
    candidate_hash: str
    delta_hash: str
    read_set_hash: str
    validator_id: str
    validator_version: str
    source_reads: tuple[GraphSourceRead, ...]
    pending_compound_ids: tuple[str, ...]
    required_convergence_ids: tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail("schema_version", "must be integer 1")
        for field in (
            "decision_id",
            "request_id",
            "mission_id",
            "validator_id",
            "validator_version",
        ):
            _text(getattr(self, field), field)
        _integer(self.base_revision, "base_revision")
        for field in (
            "decision_hash",
            "network_hash",
            "candidate_hash",
            "delta_hash",
            "read_set_hash",
        ):
            _hash(getattr(self, field), field)
        if isinstance(self.source_reads, (str, bytes)) or not isinstance(
            self.source_reads, Sequence
        ):
            _fail("source_reads", "must be an array")
        if len(self.source_reads) != len(_SOURCE_CHANNELS):
            _fail("source_reads", "must contain each of the twelve channels exactly once")
        reads = tuple(
            item if isinstance(item, GraphSourceRead) else GraphSourceRead.from_json(item)
            for item in self.source_reads
        )
        channels = tuple(item.channel for item in reads)
        if (
            len(reads) != len(_SOURCE_CHANNELS)
            or set(channels) != set(_SOURCE_CHANNELS)
            or len(set(channels)) != len(channels)
        ):
            _fail("source_reads", "must contain each of the twelve channels exactly once")
        object.__setattr__(
            self, "source_reads", tuple(sorted(reads, key=lambda item: item.channel))
        )
        object.__setattr__(
            self, "pending_compound_ids", _ids(self.pending_compound_ids, "pending_compound_ids")
        )
        object.__setattr__(
            self,
            "required_convergence_ids",
            _ids(self.required_convergence_ids, "required_convergence_ids"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "mission_id": self.mission_id,
            "base_revision": self.base_revision,
            "decision_hash": self.decision_hash,
            "network_hash": self.network_hash,
            "candidate_hash": self.candidate_hash,
            "delta_hash": self.delta_hash,
            "read_set_hash": self.read_set_hash,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "source_reads": [item.to_json() for item in self.source_reads],
            "pending_compound_ids": list(self.pending_compound_ids),
            "required_convergence_ids": list(self.required_convergence_ids),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_json())

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, value: object) -> PreviewBindingV1:
        if not isinstance(value, Mapping):
            _fail("preview", "must be an object")
        if any(not isinstance(key, str) for key in value):
            _fail("preview", "keys must be strings")
        unknown = set(value) - _PREVIEW_KEYS
        if unknown:
            _fail("preview", f"unknown fields: {sorted(unknown)!r}")
        if set(value) != _PREVIEW_KEYS:
            _fail("preview", "all fields are required")
        reads = value["source_reads"]
        if isinstance(reads, (str, bytes)) or not isinstance(reads, Sequence):
            _fail("source_reads", "must be an array")
        return cls(
            schema_version=value["schema_version"],
            decision_id=value["decision_id"],
            request_id=value["request_id"],
            mission_id=value["mission_id"],
            base_revision=value["base_revision"],
            decision_hash=value["decision_hash"],
            network_hash=value["network_hash"],
            candidate_hash=value["candidate_hash"],
            delta_hash=value["delta_hash"],
            read_set_hash=value["read_set_hash"],
            validator_id=value["validator_id"],
            validator_version=value["validator_version"],
            source_reads=tuple(GraphSourceRead.from_json(item) for item in reads),
            pending_compound_ids=value["pending_compound_ids"],
            required_convergence_ids=value["required_convergence_ids"],
        )


__all__ = ["GraphSourceRead", "PreviewBindingV1", "CompleteRead", "SourceUnavailable", "GraphReadToken", "PlanEffectSet"]


# A complete empty source remains distinct from a failed source. Reuse H1's
# existing unavailable branch; do not invent another exception with that name.

SourceValue = TypeVar("SourceValue")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteRead(Generic[SourceValue]):
    value: SourceValue
    source_id: str
    source_digest: str
    through_seq: int

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id")
        _hash(self.source_digest, "source_digest")
        _integer(self.through_seq, "through_seq")


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphReadToken:
    mission_id: str
    plan_revision: int
    manifest_hash: str
    through_seq: int
    validity_epochs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _text(self.mission_id, "mission_id")
        _integer(self.plan_revision, "plan_revision")
        _hash(self.manifest_hash, "manifest_hash")
        _integer(self.through_seq, "through_seq")
        epochs = tuple((_text(scope, "scope_id"), _integer(epoch, "epoch")) for scope, epoch in self.validity_epochs)
        if len(epochs) > 256 or len({scope for scope, _ in epochs}) != len(epochs):
            _fail("validity_epochs", "exceeds bound or has duplicate scopes")
        object.__setattr__(self, "validity_epochs", tuple(sorted(epochs)))

    def to_json(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "plan_revision": self.plan_revision,
                "manifest_hash": self.manifest_hash, "through_seq": self.through_seq,
                "validity_epochs": [{"scope_id": scope, "epoch": epoch} for scope, epoch in self.validity_epochs]}


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanEffectSet:
    retained: tuple[str, ...]
    revalidate: tuple[str, ...]
    retiring: tuple[str, ...]
    newly_materialized: tuple[str, ...]
    shared_retained: tuple[str, ...]
    coverage: str

    def __post_init__(self) -> None:
        for name in ("retained", "revalidate", "retiring", "newly_materialized", "shared_retained"):
            object.__setattr__(self, name, _ids(getattr(self, name), name))
        if self.coverage not in {"COMPLETE", "CONSERVATIVE"}:
            _fail("coverage", "unknown effect coverage")
        if set(self.retained) & set(self.retiring) or set(self.shared_retained) - set(self.retained):
            _fail("effects", "retained and retiring sets contradict")
        if set(self.newly_materialized) & (set(self.retained) | set(self.retiring)):
            _fail("effects", "new and existing identities overlap")

    def to_json(self) -> dict[str, Any]:
        return {"retained": list(self.retained), "revalidate": list(self.revalidate),
                "retiring": list(self.retiring), "newly_materialized": list(self.newly_materialized),
                "shared_retained": list(self.shared_retained), "coverage": self.coverage}
