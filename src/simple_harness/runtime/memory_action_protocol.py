# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned authority for changing an existing cognitive memory.

The model may carry only :class:`MemoryActionAuthorityRef`.  A reference has
no authority by itself: Memory must resolve it once through the injected Host
port, verify the exact intent and expiry, then atomically consume the returned
``replay_identity`` with the mutation transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from simple_harness.contracts import JsonValue

from .disclosure_protocol import (
    _digest,
    _domain_hash,
    _exact_keys,
    _identifier,
    _non_negative_number,
    _object,
    _objects,
    _positive_int,
)
from .evidence_protocol import EvidenceRef, _evidence_refs

MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION = 1


class MemoryActionKind(StrEnum):
    REVISE = "revise"
    SUPERSEDE = "supersede"
    SUPPRESS = "suppress"


def _action_schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _action_evidence_refs_from_json(value: object) -> tuple[EvidenceRef, ...]:
    refs = tuple(
        EvidenceRef.from_json(item)
        for item in _objects(value, "evidence_refs")
    )
    return _evidence_refs(refs)


def _canonical_span_hashes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise TypeError("evidence_span_hashes must be an array of strings")
    hashes = tuple(_digest(item, "evidence_span_hash") for item in value)
    if not hashes or len(hashes) > 64:
        raise ValueError("evidence_span_hashes must contain 1 to 64 values")
    if len(set(hashes)) != len(hashes):
        raise ValueError("evidence_span_hashes must be unique")
    return tuple(sorted(hashes))


@dataclass(frozen=True, slots=True)
class MemoryActionIntent:
    """Exact, authority-free commitment for one existing-memory change."""

    subject: str
    action: MemoryActionKind
    target_memory_id: str
    target_revision: int
    evidence_refs: tuple[EvidenceRef, ...]
    evidence_span_hashes: tuple[str, ...]
    run_id: str
    turn_id: str
    plan_id: str
    operation_id: str
    operation_intent_hash: str
    schema_version: int = MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION
    intent_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryActionIntent schema_version")
        for value, name in (
            (self.subject, "subject"),
            (self.target_memory_id, "target_memory_id"),
            (self.run_id, "run_id"),
            (self.turn_id, "turn_id"),
            (self.plan_id, "plan_id"),
            (self.operation_id, "operation_id"),
        ):
            _identifier(value, name)
        action = MemoryActionKind(self.action)
        _positive_int(self.target_revision, "target_revision")
        refs = _evidence_refs(self.evidence_refs)
        if not refs:
            raise ValueError("MemoryActionIntent requires evidence_refs")
        if len(refs) > 64:
            raise ValueError("MemoryActionIntent evidence_refs exceeds the item limit")
        span_hashes = _canonical_span_hashes(self.evidence_span_hashes)
        _digest(self.operation_intent_hash, "operation_intent_hash")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "evidence_span_hashes", span_hashes)
        object.__setattr__(
            self,
            "intent_hash",
            _domain_hash("simple-harness/memory-action-intent/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "subject": self.subject,
            "action": self.action.value,
            "target_memory_id": self.target_memory_id,
            "target_revision": self.target_revision,
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
            "evidence_span_hashes": list(self.evidence_span_hashes),
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "plan_id": self.plan_id,
            "operation_id": self.operation_id,
            "operation_intent_hash": self.operation_intent_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryActionIntent:
        _exact_keys(
            value,
            {
                "schema_version",
                "subject",
                "action",
                "target_memory_id",
                "target_revision",
                "evidence_refs",
                "evidence_span_hashes",
                "run_id",
                "turn_id",
                "plan_id",
                "operation_id",
                "operation_intent_hash",
            },
            "MemoryActionIntent",
        )
        return cls(
            subject=_identifier(value["subject"], "subject"),
            action=MemoryActionKind(value["action"]),  # type: ignore[arg-type]
            target_memory_id=_identifier(value["target_memory_id"], "target_memory_id"),
            target_revision=_positive_int(value["target_revision"], "target_revision"),
            evidence_refs=_action_evidence_refs_from_json(value["evidence_refs"]),
            evidence_span_hashes=_canonical_span_hashes(value["evidence_span_hashes"]),
            run_id=_identifier(value["run_id"], "run_id"),
            turn_id=_identifier(value["turn_id"], "turn_id"),
            plan_id=_identifier(value["plan_id"], "plan_id"),
            operation_id=_identifier(value["operation_id"], "operation_id"),
            operation_intent_hash=_digest(
                value["operation_intent_hash"], "operation_intent_hash"
            ),
            schema_version=_action_schema_version(
                value["schema_version"], "MemoryActionIntent"
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryActionAuthority:
    """Host-issued action grant returned only by the trusted authority port."""

    authority_id: str
    intent: MemoryActionIntent
    issued_at: float
    expires_at: float
    nonce: str
    issuer_ref: str
    schema_version: int = MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION
    replay_identity: str = field(init=False)
    authority_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryActionAuthority schema_version")
        _identifier(self.authority_id, "authority_id")
        if type(self.intent) is not MemoryActionIntent:
            raise TypeError("intent must use MemoryActionIntent")
        issued_at = _non_negative_number(self.issued_at, "issued_at")
        expires_at = _non_negative_number(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("MemoryActionAuthority expires_at must follow issued_at")
        _identifier(self.nonce, "nonce", max_length=1024)
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "replay_identity",
            _domain_hash(
                "simple-harness/memory-action-replay-identity/v1",
                {
                    "authority_id": self.authority_id,
                    "intent_hash": self.intent.intent_hash,
                    "nonce": self.nonce,
                    "issuer_ref": self.issuer_ref,
                },
            ),
        )
        object.__setattr__(
            self,
            "authority_hash",
            _domain_hash("simple-harness/memory-action-authority/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "intent": self.intent.to_json(),
            "intent_hash": self.intent.intent_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "issuer_ref": self.issuer_ref,
            "replay_identity": self.replay_identity,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryActionAuthority:
        """Decode only the current strict Host wire; no legacy migration exists."""

        _exact_keys(
            value,
            {
                "schema_version",
                "authority_id",
                "intent",
                "intent_hash",
                "issued_at",
                "expires_at",
                "nonce",
                "issuer_ref",
                "replay_identity",
            },
            "MemoryActionAuthority",
        )
        intent = MemoryActionIntent.from_json(_object(value["intent"], "intent"))
        if _digest(value["intent_hash"], "intent_hash") != intent.intent_hash:
            raise ValueError("MemoryActionAuthority intent_hash differs")
        authority = cls(
            authority_id=_identifier(value["authority_id"], "authority_id"),
            intent=intent,
            issued_at=_non_negative_number(value["issued_at"], "issued_at"),
            expires_at=_non_negative_number(value["expires_at"], "expires_at"),
            nonce=_identifier(value["nonce"], "nonce", max_length=1024),
            issuer_ref=_identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            schema_version=_action_schema_version(
                value["schema_version"], "MemoryActionAuthority"
            ),
        )
        if _digest(value["replay_identity"], "replay_identity") != authority.replay_identity:
            raise ValueError("MemoryActionAuthority replay_identity differs")
        return authority


@dataclass(frozen=True, slots=True)
class MemoryActionAuthorityRef:
    """Untrusted wire reference; authority exists only after Host resolution."""

    authority_id: str
    authority_hash: str
    issuer_ref: str
    replay_identity: str
    schema_version: int = MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION
    ref_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryActionAuthorityRef schema_version")
        _identifier(self.authority_id, "authority_id")
        _digest(self.authority_hash, "authority_hash")
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        _digest(self.replay_identity, "replay_identity")
        object.__setattr__(
            self,
            "ref_hash",
            _domain_hash("simple-harness/memory-action-authority-ref/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "authority_hash": self.authority_hash,
            "issuer_ref": self.issuer_ref,
            "replay_identity": self.replay_identity,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryActionAuthorityRef:
        _exact_keys(
            value,
            {
                "schema_version",
                "authority_id",
                "authority_hash",
                "issuer_ref",
                "replay_identity",
            },
            "MemoryActionAuthorityRef",
        )
        return cls(
            authority_id=_identifier(value["authority_id"], "authority_id"),
            authority_hash=_digest(value["authority_hash"], "authority_hash"),
            issuer_ref=_identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            replay_identity=_digest(value["replay_identity"], "replay_identity"),
            schema_version=_action_schema_version(
                value["schema_version"], "MemoryActionAuthorityRef"
            ),
        )

    @classmethod
    def from_authority(cls, authority: MemoryActionAuthority) -> MemoryActionAuthorityRef:
        if type(authority) is not MemoryActionAuthority:
            raise TypeError("authority must use MemoryActionAuthority")
        return cls(
            authority_id=authority.authority_id,
            authority_hash=authority.authority_hash,
            issuer_ref=authority.issuer_ref,
            replay_identity=authority.replay_identity,
        )


class MemoryActionAuthorityPort(Protocol):
    """Host durable lookup; it does not own Memory's atomic replay fence.

    The Memory repository must consume ``replay_identity`` uniquely in the
    same transaction as the mutation.  An idempotent replay of the same
    committed receipt may return that receipt; a changed payload using the
    consumed identity must fail closed.
    """

    async def resolve_memory_action_authority(
        self, reference: MemoryActionAuthorityRef
    ) -> MemoryActionAuthority: ...


def issue_memory_action_authority(
    intent: MemoryActionIntent,
    *,
    authority_id: str,
    issued_at: float,
    expires_at: float,
    nonce: str,
    issuer_ref: str,
) -> MemoryActionAuthority:
    """Construct the exact Host record to persist before exposing its ref."""

    if type(intent) is not MemoryActionIntent:
        raise TypeError("intent must use MemoryActionIntent")
    return MemoryActionAuthority(
        authority_id=authority_id,
        intent=intent,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        issuer_ref=issuer_ref,
    )


async def verify_memory_action_authority(
    intent: MemoryActionIntent,
    reference: MemoryActionAuthorityRef,
    authority: MemoryActionAuthorityPort,
    *,
    current_time: float,
) -> MemoryActionAuthority:
    """Resolve exactly once and return the same verified Host authority."""

    if type(intent) is not MemoryActionIntent:
        raise TypeError("intent must use MemoryActionIntent")
    if type(reference) is not MemoryActionAuthorityRef:
        raise TypeError("reference must use MemoryActionAuthorityRef")
    now = _non_negative_number(current_time, "current_time")
    resolved = await authority.resolve_memory_action_authority(reference)
    if type(resolved) is not MemoryActionAuthority:
        raise TypeError("action authority port returned an invalid authority")
    expected_ref = MemoryActionAuthorityRef.from_authority(resolved)
    if expected_ref != reference or expected_ref.ref_hash != reference.ref_hash:
        raise ValueError("resolved MemoryActionAuthority differs from reference")
    if resolved.intent != intent or resolved.intent.intent_hash != intent.intent_hash:
        raise ValueError("MemoryActionAuthority intent differs")
    if now < resolved.issued_at:
        raise ValueError("MemoryActionAuthority is not yet valid")
    if now >= resolved.expires_at:
        raise ValueError("MemoryActionAuthority is expired")
    return resolved


__all__ = (
    "MEMORY_ACTION_AUTHORITY_SCHEMA_VERSION",
    "MemoryActionAuthority",
    "MemoryActionAuthorityPort",
    "MemoryActionAuthorityRef",
    "MemoryActionIntent",
    "MemoryActionKind",
    "issue_memory_action_authority",
    "verify_memory_action_authority",
)
