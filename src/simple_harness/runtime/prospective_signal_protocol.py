# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned scheduler and event authority for Prospective memory signals.

Memory never treats caller supplied clock, event, registration, or receipt
strings as proof.  A public caller carries only an untrusted reference; the
full signal commitment is returned by the Host resolver and must be replay
fenced atomically with the resulting Memory decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import JsonValue

from .agent_memory import MemoryScopeRef
from .disclosure_protocol import (
    _digest,
    _domain_hash,
    _exact_keys,
    _identifier,
    _non_negative_number,
    _object,
    _optional_identifier,
    _positive_int,
)
from .memory_protocol import (
    ProspectiveEventTrigger,
    ProspectiveLifecycleState,
    ProspectiveTimeTrigger,
    ProspectiveTrigger,
    ProspectiveTriggerKind,
)

PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION = 1


class ProspectiveSignalKind(StrEnum):
    REGISTRATION_ACCEPTED = "registration_accepted"
    REGISTRATION_INVALIDATED = "registration_invalidated"
    TIME_DUE = "time_due"
    EVENT_OCCURRED = "event_occurred"
    EXPIRED = "expired"


def _schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _scope_from_json(value: object) -> MemoryScopeRef:
    scope = _object(value, "scope")
    _exact_keys(scope, {"kind", "owner_id"}, "MemoryScopeRef")
    return MemoryScopeRef.from_json(cast(Mapping[str, JsonValue], scope))


def _trigger_from_json(value: object) -> ProspectiveTrigger:
    trigger = _object(value, "trigger")
    kind = trigger.get("trigger_kind")
    if kind == ProspectiveTriggerKind.TIME.value:
        return ProspectiveTimeTrigger.from_json(trigger)
    if kind == ProspectiveTriggerKind.EVENT.value:
        return ProspectiveEventTrigger.from_json(trigger)
    raise ValueError("Prospective signal trigger has an unknown discriminator")


def prospective_trigger_hash(trigger: ProspectiveTrigger) -> str:
    if not isinstance(trigger, (ProspectiveTimeTrigger, ProspectiveEventTrigger)):
        raise TypeError("trigger must use a typed Prospective trigger")
    return _domain_hash("simple-harness/prospective-trigger/v1", trigger.to_json())


@dataclass(frozen=True, slots=True)
class ProspectiveSignalIntent:
    """Exact authority-free commitment for one Host scheduler/runtime signal."""

    signal_id: str
    subject: str
    scope: MemoryScopeRef
    target_memory_id: str
    target_revision: int
    signal_kind: ProspectiveSignalKind
    trigger: ProspectiveTrigger
    scheduler_registration_ref: str
    registration_revision: int
    signal_receipt_id: str
    signal_receipt_hash: str
    observed_at: float
    transition_from: ProspectiveLifecycleState
    transition_to: ProspectiveLifecycleState
    outbox_id: str | None
    outbox_payload_hash: str | None
    run_id: str
    operation_id: str
    schema_version: int = PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION
    trigger_hash: str = field(init=False)
    occurrence_key: str = field(init=False)
    intent_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProspectiveSignalIntent schema_version")
        for value, name in (
            (self.signal_id, "signal_id"),
            (self.subject, "subject"),
            (self.target_memory_id, "target_memory_id"),
            (self.scheduler_registration_ref, "scheduler_registration_ref"),
            (self.signal_receipt_id, "signal_receipt_id"),
            (self.run_id, "run_id"),
            (self.operation_id, "operation_id"),
        ):
            _identifier(value, name, max_length=1024)
        if type(self.scope) is not MemoryScopeRef:
            raise TypeError("scope must use MemoryScopeRef")
        _positive_int(self.target_revision, "target_revision")
        _positive_int(self.registration_revision, "registration_revision")
        signal_kind = ProspectiveSignalKind(self.signal_kind)
        if not isinstance(self.trigger, (ProspectiveTimeTrigger, ProspectiveEventTrigger)):
            raise TypeError("trigger must use a typed Prospective trigger")
        _digest(self.signal_receipt_hash, "signal_receipt_hash")
        observed_at = _non_negative_number(self.observed_at, "observed_at")
        transition_from = ProspectiveLifecycleState(self.transition_from)
        transition_to = ProspectiveLifecycleState(self.transition_to)
        outbox_id = _optional_identifier(self.outbox_id, "outbox_id", max_length=1024)
        outbox_hash = (
            None
            if self.outbox_payload_hash is None
            else _digest(self.outbox_payload_hash, "outbox_payload_hash")
        )
        ack_kinds = {
            ProspectiveSignalKind.REGISTRATION_ACCEPTED,
            ProspectiveSignalKind.REGISTRATION_INVALIDATED,
        }
        if signal_kind in ack_kinds:
            if outbox_id is None or outbox_hash is None:
                raise ValueError("registration acknowledgement requires exact outbox binding")
            if transition_to is not transition_from:
                raise ValueError("registration acknowledgement cannot change lifecycle state")
        elif outbox_id is not None or outbox_hash is not None:
            raise ValueError("runtime occurrence cannot carry acknowledgement outbox fields")
        if signal_kind is ProspectiveSignalKind.TIME_DUE:
            if not isinstance(self.trigger, ProspectiveTimeTrigger):
                raise ValueError("time_due requires ProspectiveTimeTrigger")
            if transition_from is not ProspectiveLifecycleState.PENDING or (
                transition_to is not ProspectiveLifecycleState.TRIGGERED
            ):
                raise ValueError("time_due must bind pending to triggered")
            if observed_at < self.trigger.trigger_at:
                raise ValueError("time_due observed_at precedes trigger_at")
        elif signal_kind is ProspectiveSignalKind.EVENT_OCCURRED:
            if not isinstance(self.trigger, ProspectiveEventTrigger):
                raise ValueError("event_occurred requires ProspectiveEventTrigger")
            if transition_from is not ProspectiveLifecycleState.PENDING or (
                transition_to is not ProspectiveLifecycleState.TRIGGERED
            ):
                raise ValueError("event_occurred must bind pending to triggered")
        elif signal_kind is ProspectiveSignalKind.EXPIRED:
            if transition_from not in {
                ProspectiveLifecycleState.PENDING,
                ProspectiveLifecycleState.TRIGGERED,
                ProspectiveLifecycleState.IN_PROGRESS,
                ProspectiveLifecycleState.RESCHEDULED,
            } or transition_to is not ProspectiveLifecycleState.EXPIRED:
                raise ValueError("expired signal must bind a live intent to expired")
        trigger_hash = prospective_trigger_hash(self.trigger)
        object.__setattr__(self, "signal_kind", signal_kind)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "transition_from", transition_from)
        object.__setattr__(self, "transition_to", transition_to)
        object.__setattr__(self, "outbox_id", outbox_id)
        object.__setattr__(self, "outbox_payload_hash", outbox_hash)
        object.__setattr__(self, "trigger_hash", trigger_hash)
        object.__setattr__(
            self,
            "occurrence_key",
            _domain_hash(
                "simple-harness/prospective-signal-occurrence/v1",
                {
                    "subject": self.subject,
                    "target_memory_id": self.target_memory_id,
                    "target_revision": self.target_revision,
                    "signal_id": self.signal_id,
                    "signal_kind": signal_kind.value,
                    "signal_receipt_hash": self.signal_receipt_hash,
                    "scheduler_registration_ref": self.scheduler_registration_ref,
                    "registration_revision": self.registration_revision,
                },
            ),
        )
        object.__setattr__(
            self,
            "intent_hash",
            _domain_hash("simple-harness/prospective-signal-intent/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "subject": self.subject,
            "scope": self.scope.to_json(),
            "target_memory_id": self.target_memory_id,
            "target_revision": self.target_revision,
            "signal_kind": self.signal_kind.value,
            "trigger": self.trigger.to_json(),
            "trigger_hash": self.trigger_hash,
            "scheduler_registration_ref": self.scheduler_registration_ref,
            "registration_revision": self.registration_revision,
            "signal_receipt_id": self.signal_receipt_id,
            "signal_receipt_hash": self.signal_receipt_hash,
            "observed_at": self.observed_at,
            "transition_from": self.transition_from.value,
            "transition_to": self.transition_to.value,
            "outbox_id": self.outbox_id,
            "outbox_payload_hash": self.outbox_payload_hash,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "occurrence_key": self.occurrence_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveSignalIntent:
        _exact_keys(
            value,
            {
                "schema_version", "signal_id", "subject", "scope", "target_memory_id",
                "target_revision", "signal_kind", "trigger", "trigger_hash",
                "scheduler_registration_ref", "registration_revision", "signal_receipt_id",
                "signal_receipt_hash", "observed_at", "transition_from", "transition_to",
                "outbox_id", "outbox_payload_hash", "run_id", "operation_id",
                "occurrence_key",
            },
            "ProspectiveSignalIntent",
        )
        trigger = _trigger_from_json(value["trigger"])
        if _digest(value["trigger_hash"], "trigger_hash") != prospective_trigger_hash(trigger):
            raise ValueError("Prospective signal trigger hash differs")
        result = cls(
            signal_id=_identifier(value["signal_id"], "signal_id"),
            subject=_identifier(value["subject"], "subject"),
            scope=_scope_from_json(value["scope"]),
            target_memory_id=_identifier(value["target_memory_id"], "target_memory_id"),
            target_revision=_positive_int(value["target_revision"], "target_revision"),
            signal_kind=ProspectiveSignalKind(value["signal_kind"]),  # type: ignore[arg-type]
            trigger=trigger,
            scheduler_registration_ref=_identifier(
                value["scheduler_registration_ref"], "scheduler_registration_ref",
                max_length=1024,
            ),
            registration_revision=_positive_int(
                value["registration_revision"], "registration_revision"
            ),
            signal_receipt_id=_identifier(value["signal_receipt_id"], "signal_receipt_id"),
            signal_receipt_hash=_digest(value["signal_receipt_hash"], "signal_receipt_hash"),
            observed_at=_non_negative_number(value["observed_at"], "observed_at"),
            transition_from=ProspectiveLifecycleState(value["transition_from"]),  # type: ignore[arg-type]
            transition_to=ProspectiveLifecycleState(value["transition_to"]),  # type: ignore[arg-type]
            outbox_id=_optional_identifier(value["outbox_id"], "outbox_id", max_length=1024),
            outbox_payload_hash=(
                None
                if value["outbox_payload_hash"] is None
                else _digest(value["outbox_payload_hash"], "outbox_payload_hash")
            ),
            run_id=_identifier(value["run_id"], "run_id"),
            operation_id=_identifier(value["operation_id"], "operation_id"),
            schema_version=_schema_version(value["schema_version"], "ProspectiveSignalIntent"),
        )
        if _digest(value["occurrence_key"], "occurrence_key") != result.occurrence_key:
            raise ValueError("Prospective signal occurrence key differs")
        return result


@dataclass(frozen=True, slots=True)
class ProspectiveSignalAuthority:
    authority_id: str
    intent: ProspectiveSignalIntent
    issued_at: float
    expires_at: float
    nonce: str
    issuer_ref: str
    schema_version: int = PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION
    replay_identity: str = field(init=False)
    authority_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProspectiveSignalAuthority schema_version")
        _identifier(self.authority_id, "authority_id")
        if type(self.intent) is not ProspectiveSignalIntent:
            raise TypeError("intent must use ProspectiveSignalIntent")
        issued_at = _non_negative_number(self.issued_at, "issued_at")
        expires_at = _non_negative_number(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("ProspectiveSignalAuthority expires_at must follow issued_at")
        _identifier(self.nonce, "nonce", max_length=1024)
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "replay_identity",
            _domain_hash(
                "simple-harness/prospective-signal-replay-identity/v1",
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
            _domain_hash("simple-harness/prospective-signal-authority/v1", self.to_json()),
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
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveSignalAuthority:
        _exact_keys(
            value,
            {"schema_version", "authority_id", "intent", "intent_hash", "issued_at",
             "expires_at", "nonce", "issuer_ref", "replay_identity"},
            "ProspectiveSignalAuthority",
        )
        intent = ProspectiveSignalIntent.from_json(_object(value["intent"], "intent"))
        if _digest(value["intent_hash"], "intent_hash") != intent.intent_hash:
            raise ValueError("ProspectiveSignalAuthority intent_hash differs")
        result = cls(
            authority_id=_identifier(value["authority_id"], "authority_id"),
            intent=intent,
            issued_at=_non_negative_number(value["issued_at"], "issued_at"),
            expires_at=_non_negative_number(value["expires_at"], "expires_at"),
            nonce=_identifier(value["nonce"], "nonce", max_length=1024),
            issuer_ref=_identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            schema_version=_schema_version(value["schema_version"], "ProspectiveSignalAuthority"),
        )
        if _digest(value["replay_identity"], "replay_identity") != result.replay_identity:
            raise ValueError("ProspectiveSignalAuthority replay_identity differs")
        return result


@dataclass(frozen=True, slots=True)
class ProspectiveSignalAuthorityRef:
    authority_id: str
    authority_hash: str
    issuer_ref: str
    replay_identity: str
    schema_version: int = PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION
    ref_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProspectiveSignalAuthorityRef schema_version")
        _identifier(self.authority_id, "authority_id")
        _digest(self.authority_hash, "authority_hash")
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        _digest(self.replay_identity, "replay_identity")
        object.__setattr__(
            self,
            "ref_hash",
            _domain_hash("simple-harness/prospective-signal-authority-ref/v1", self.to_json()),
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
    def from_json(cls, value: Mapping[str, object]) -> ProspectiveSignalAuthorityRef:
        _exact_keys(
            value,
            {"schema_version", "authority_id", "authority_hash", "issuer_ref", "replay_identity"},
            "ProspectiveSignalAuthorityRef",
        )
        return cls(
            _identifier(value["authority_id"], "authority_id"),
            _digest(value["authority_hash"], "authority_hash"),
            _identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            _digest(value["replay_identity"], "replay_identity"),
            _schema_version(value["schema_version"], "ProspectiveSignalAuthorityRef"),
        )

    @classmethod
    def from_authority(cls, authority: ProspectiveSignalAuthority) -> ProspectiveSignalAuthorityRef:
        if type(authority) is not ProspectiveSignalAuthority:
            raise TypeError("authority must use ProspectiveSignalAuthority")
        return cls(
            authority.authority_id,
            authority.authority_hash,
            authority.issuer_ref,
            authority.replay_identity,
        )


class ProspectiveSignalAuthorityPort(Protocol):
    async def resolve_prospective_signal_authority(
        self, reference: ProspectiveSignalAuthorityRef
    ) -> ProspectiveSignalAuthority: ...


def issue_prospective_signal_authority(
    intent: ProspectiveSignalIntent,
    *,
    authority_id: str,
    issued_at: float,
    expires_at: float,
    nonce: str,
    issuer_ref: str,
) -> ProspectiveSignalAuthority:
    if type(intent) is not ProspectiveSignalIntent:
        raise TypeError("intent must use ProspectiveSignalIntent")
    return ProspectiveSignalAuthority(
        authority_id, intent, issued_at, expires_at, nonce, issuer_ref
    )


async def verify_prospective_signal_authority(
    reference: ProspectiveSignalAuthorityRef,
    authority: ProspectiveSignalAuthorityPort,
    *,
    current_time: float,
) -> ProspectiveSignalAuthority:
    """Resolve exactly once; the Memory consumer must atomically fence replay."""

    if type(reference) is not ProspectiveSignalAuthorityRef:
        raise TypeError("reference must use ProspectiveSignalAuthorityRef")
    now = _non_negative_number(current_time, "current_time")
    resolved = await authority.resolve_prospective_signal_authority(reference)
    if type(resolved) is not ProspectiveSignalAuthority:
        raise TypeError("prospective signal authority port returned an invalid authority")
    expected_ref = ProspectiveSignalAuthorityRef.from_authority(resolved)
    if expected_ref != reference or expected_ref.ref_hash != reference.ref_hash:
        raise ValueError("resolved ProspectiveSignalAuthority differs from reference")
    if now < resolved.issued_at:
        raise ValueError("ProspectiveSignalAuthority is not yet valid")
    if now >= resolved.expires_at:
        raise ValueError("ProspectiveSignalAuthority is expired")
    return resolved


__all__ = (
    "PROSPECTIVE_SIGNAL_AUTHORITY_SCHEMA_VERSION",
    "ProspectiveSignalAuthority",
    "ProspectiveSignalAuthorityPort",
    "ProspectiveSignalAuthorityRef",
    "ProspectiveSignalIntent",
    "ProspectiveSignalKind",
    "issue_prospective_signal_authority",
    "prospective_trigger_hash",
    "verify_prospective_signal_authority",
)
