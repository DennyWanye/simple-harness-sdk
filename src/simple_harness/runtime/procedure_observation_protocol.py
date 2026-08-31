# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-owned authority for observed Procedure lifecycle transitions.

Only :class:`ProcedureObservationAuthorityRef` is accepted across the public
consumer boundary.  The complete terminal receipt and transition commitment
are returned by the injected Host resolver; model supplied receipt strings or
outcomes have no authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, cast

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
from .evidence_protocol import EvidenceSpanRef
from .memory_protocol import ProcedureLifecycleState, ProcedureRiskLevel

PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION = 1
PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION = 2


class ProcedureObservationKind(StrEnum):
    TERMINAL_OUTCOME = "terminal_outcome"
    APPLICABILITY_SNAPSHOT = "applicability_snapshot"


class ProcedureObservationOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ProcedureHazard(StrEnum):
    NONE = "none"
    PUBLISH = "publish"
    DELETE = "delete"
    PAYMENT = "payment"
    PERMISSION = "permission"


_SAFE_SUCCESS_TRANSITIONS: Final[
    dict[ProcedureLifecycleState, frozenset[ProcedureLifecycleState]]
] = {
    ProcedureLifecycleState.DRAFT: frozenset(
        {
            ProcedureLifecycleState.DRAFT,
            ProcedureLifecycleState.ELIGIBLE_FOR_ACTIVATION,
        }
    ),
    ProcedureLifecycleState.ELIGIBLE_FOR_ACTIVATION: frozenset(
        {
            ProcedureLifecycleState.ELIGIBLE_FOR_ACTIVATION,
            ProcedureLifecycleState.ACTIVE,
        }
    ),
    ProcedureLifecycleState.ACTIVE: frozenset(
        {ProcedureLifecycleState.ACTIVE, ProcedureLifecycleState.REINFORCED}
    ),
    ProcedureLifecycleState.REINFORCED: frozenset(
        {ProcedureLifecycleState.REINFORCED}
    ),
}


def _schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _scope_from_json(value: object) -> MemoryScopeRef:
    scope = _object(value, "scope")
    _exact_keys(scope, {"kind", "owner_id"}, "MemoryScopeRef")
    return MemoryScopeRef.from_json(cast(Mapping[str, JsonValue], scope))


@dataclass(frozen=True, slots=True)
class ProcedureApplicabilityContext:
    tool_id: str
    environment: str
    tool_version: str
    input_schema_hash: str
    fingerprint_version: int = PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.tool_id, "tool_id")
        _identifier(self.environment, "environment")
        _identifier(self.tool_version, "tool_version")
        _digest(self.input_schema_hash, "input_schema_hash")
        if self.fingerprint_version != PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION:
            raise ValueError("unsupported Procedure applicability fingerprint_version")
        object.__setattr__(
            self,
            "fingerprint",
            _domain_hash("simple-harness/procedure-applicability/v2", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "tool_id": self.tool_id,
            "environment": self.environment,
            "tool_version": self.tool_version,
            "input_schema_hash": self.input_schema_hash,
            "fingerprint_version": self.fingerprint_version,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProcedureApplicabilityContext:
        _exact_keys(
            value,
            {
                "tool_id",
                "environment",
                "tool_version",
                "input_schema_hash",
                "fingerprint_version",
            },
            "ProcedureApplicabilityContext",
        )
        return cls(
            _identifier(value["tool_id"], "tool_id"),
            _identifier(value["environment"], "environment"),
            _identifier(value["tool_version"], "tool_version"),
            _digest(value["input_schema_hash"], "input_schema_hash"),
            _positive_int(value["fingerprint_version"], "fingerprint_version"),
        )


@dataclass(frozen=True, slots=True)
class ProcedureObservationIntent:
    """Exact authority-free commitment for one trusted Procedure observation."""

    observation_id: str
    subject: str
    scope: MemoryScopeRef
    target_memory_id: str
    target_revision: int
    kind: ProcedureObservationKind
    applicability: ProcedureApplicabilityContext
    risk_level: ProcedureRiskLevel
    hazard: ProcedureHazard
    task_scope_id: str
    evidence_span: EvidenceSpanRef
    terminal_receipt_id: str | None
    terminal_receipt_hash: str | None
    outcome: ProcedureObservationOutcome | None
    attributable: bool
    observed_at: float
    transition_from: ProcedureLifecycleState
    transition_to: ProcedureLifecycleState
    run_id: str
    operation_id: str
    schema_version: int = PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION
    intent_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProcedureObservationIntent schema_version")
        for value, name in (
            (self.observation_id, "observation_id"),
            (self.subject, "subject"),
            (self.target_memory_id, "target_memory_id"),
            (self.task_scope_id, "task_scope_id"),
            (self.run_id, "run_id"),
            (self.operation_id, "operation_id"),
        ):
            _identifier(value, name)
        if type(self.scope) is not MemoryScopeRef:
            raise TypeError("scope must use MemoryScopeRef")
        _positive_int(self.target_revision, "target_revision")
        kind = ProcedureObservationKind(self.kind)
        if type(self.applicability) is not ProcedureApplicabilityContext:
            raise TypeError("applicability must use ProcedureApplicabilityContext")
        risk = ProcedureRiskLevel(self.risk_level)
        hazard = ProcedureHazard(self.hazard)
        if type(self.evidence_span) is not EvidenceSpanRef:
            raise TypeError("evidence_span must use EvidenceSpanRef")
        if not isinstance(self.attributable, bool):
            raise TypeError("attributable must be a boolean")
        observed_at = _non_negative_number(self.observed_at, "observed_at")
        transition_from = ProcedureLifecycleState(self.transition_from)
        transition_to = ProcedureLifecycleState(self.transition_to)
        receipt_id = _optional_identifier(self.terminal_receipt_id, "terminal_receipt_id")
        receipt_hash = (
            None
            if self.terminal_receipt_hash is None
            else _digest(self.terminal_receipt_hash, "terminal_receipt_hash")
        )
        outcome = None if self.outcome is None else ProcedureObservationOutcome(self.outcome)
        if kind is ProcedureObservationKind.TERMINAL_OUTCOME:
            if receipt_id is None or receipt_hash is None or outcome is None:
                raise ValueError("terminal outcome requires an exact terminal receipt and outcome")
            if outcome is ProcedureObservationOutcome.FAILURE and self.attributable and (
                transition_to is not ProcedureLifecycleState.REVISED
            ):
                raise ValueError("attributable terminal failure must transition to revised")
            if outcome is ProcedureObservationOutcome.FAILURE and not self.attributable and (
                transition_to is not transition_from
            ):
                raise ValueError("non-attributable terminal failure cannot change lifecycle state")
            if outcome is ProcedureObservationOutcome.SUCCESS and (
                risk is not ProcedureRiskLevel.LOW or hazard is not ProcedureHazard.NONE
            ) and transition_to is not transition_from:
                raise ValueError("non-low-risk observation cannot change Procedure lifecycle")
            safe_targets = _SAFE_SUCCESS_TRANSITIONS.get(transition_from)
            if outcome is ProcedureObservationOutcome.SUCCESS and (
                safe_targets is None or transition_to not in safe_targets
            ):
                raise ValueError("successful observation has an invalid expected transition")
        elif receipt_id is not None or receipt_hash is not None or outcome is not None:
            raise ValueError("applicability snapshot cannot carry a terminal outcome")
        if kind is ProcedureObservationKind.APPLICABILITY_SNAPSHOT and self.attributable:
            raise ValueError("applicability snapshot cannot claim outcome attribution")
        if kind is ProcedureObservationKind.APPLICABILITY_SNAPSHOT and transition_to not in {
            transition_from,
            ProcedureLifecycleState.INAPPLICABLE,
        }:
            raise ValueError("applicability snapshot has an invalid expected transition")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(self, "hazard", hazard)
        object.__setattr__(self, "terminal_receipt_id", receipt_id)
        object.__setattr__(self, "terminal_receipt_hash", receipt_hash)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "transition_from", transition_from)
        object.__setattr__(self, "transition_to", transition_to)
        object.__setattr__(
            self,
            "intent_hash",
            _domain_hash("simple-harness/procedure-observation-intent/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "subject": self.subject,
            "scope": self.scope.to_json(),
            "target_memory_id": self.target_memory_id,
            "target_revision": self.target_revision,
            "kind": self.kind.value,
            "applicability": self.applicability.to_json(),
            "applicability_fingerprint": self.applicability.fingerprint,
            "risk_level": self.risk_level.value,
            "hazard": self.hazard.value,
            "task_scope_id": self.task_scope_id,
            "evidence_span": self.evidence_span.to_json(),
            "evidence_span_hash": self.evidence_span.span_hash,
            "terminal_receipt_id": self.terminal_receipt_id,
            "terminal_receipt_hash": self.terminal_receipt_hash,
            "outcome": None if self.outcome is None else self.outcome.value,
            "attributable": self.attributable,
            "observed_at": self.observed_at,
            "transition_from": self.transition_from.value,
            "transition_to": self.transition_to.value,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ProcedureObservationIntent:
        _exact_keys(
            value,
            {
                "schema_version", "observation_id", "subject", "scope",
                "target_memory_id", "target_revision", "kind", "applicability",
                "applicability_fingerprint", "risk_level", "hazard", "task_scope_id",
                "evidence_span", "evidence_span_hash", "terminal_receipt_id",
                "terminal_receipt_hash", "outcome", "attributable", "observed_at",
                "transition_from", "transition_to", "run_id", "operation_id",
            },
            "ProcedureObservationIntent",
        )
        applicability = ProcedureApplicabilityContext.from_json(
            _object(value["applicability"], "applicability")
        )
        if _digest(value["applicability_fingerprint"], "applicability_fingerprint") != (
            applicability.fingerprint
        ):
            raise ValueError("Procedure applicability fingerprint differs")
        span = EvidenceSpanRef.from_json(_object(value["evidence_span"], "evidence_span"))
        if _digest(value["evidence_span_hash"], "evidence_span_hash") != span.span_hash:
            raise ValueError("Procedure evidence span hash differs")
        outcome_value = value["outcome"]
        outcome = (
            None
            if outcome_value is None
            else ProcedureObservationOutcome(outcome_value)  # type: ignore[arg-type]
        )
        attributable = value["attributable"]
        if not isinstance(attributable, bool):
            raise TypeError("attributable must be a boolean")
        return cls(
            observation_id=_identifier(value["observation_id"], "observation_id"),
            subject=_identifier(value["subject"], "subject"),
            scope=_scope_from_json(value["scope"]),
            target_memory_id=_identifier(value["target_memory_id"], "target_memory_id"),
            target_revision=_positive_int(value["target_revision"], "target_revision"),
            kind=ProcedureObservationKind(value["kind"]),  # type: ignore[arg-type]
            applicability=applicability,
            risk_level=ProcedureRiskLevel(value["risk_level"]),  # type: ignore[arg-type]
            hazard=ProcedureHazard(value["hazard"]),  # type: ignore[arg-type]
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            evidence_span=span,
            terminal_receipt_id=_optional_identifier(
                value["terminal_receipt_id"], "terminal_receipt_id"
            ),
            terminal_receipt_hash=(
                None
                if value["terminal_receipt_hash"] is None
                else _digest(value["terminal_receipt_hash"], "terminal_receipt_hash")
            ),
            outcome=outcome,
            attributable=attributable,
            observed_at=_non_negative_number(value["observed_at"], "observed_at"),
            transition_from=ProcedureLifecycleState(value["transition_from"]),  # type: ignore[arg-type]
            transition_to=ProcedureLifecycleState(value["transition_to"]),  # type: ignore[arg-type]
            run_id=_identifier(value["run_id"], "run_id"),
            operation_id=_identifier(value["operation_id"], "operation_id"),
            schema_version=_schema_version(value["schema_version"], "ProcedureObservationIntent"),
        )


@dataclass(frozen=True, slots=True)
class ProcedureObservationAuthority:
    authority_id: str
    intent: ProcedureObservationIntent
    issued_at: float
    expires_at: float
    nonce: str
    issuer_ref: str
    schema_version: int = PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION
    replay_identity: str = field(init=False)
    authority_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProcedureObservationAuthority schema_version")
        _identifier(self.authority_id, "authority_id")
        if type(self.intent) is not ProcedureObservationIntent:
            raise TypeError("intent must use ProcedureObservationIntent")
        issued_at = _non_negative_number(self.issued_at, "issued_at")
        expires_at = _non_negative_number(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("ProcedureObservationAuthority expires_at must follow issued_at")
        _identifier(self.nonce, "nonce", max_length=1024)
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "replay_identity",
            _domain_hash(
                "simple-harness/procedure-observation-replay-identity/v1",
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
            _domain_hash("simple-harness/procedure-observation-authority/v1", self.to_json()),
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
    def from_json(cls, value: Mapping[str, object]) -> ProcedureObservationAuthority:
        _exact_keys(
            value,
            {"schema_version", "authority_id", "intent", "intent_hash", "issued_at",
             "expires_at", "nonce", "issuer_ref", "replay_identity"},
            "ProcedureObservationAuthority",
        )
        intent = ProcedureObservationIntent.from_json(_object(value["intent"], "intent"))
        if _digest(value["intent_hash"], "intent_hash") != intent.intent_hash:
            raise ValueError("ProcedureObservationAuthority intent_hash differs")
        result = cls(
            authority_id=_identifier(value["authority_id"], "authority_id"),
            intent=intent,
            issued_at=_non_negative_number(value["issued_at"], "issued_at"),
            expires_at=_non_negative_number(value["expires_at"], "expires_at"),
            nonce=_identifier(value["nonce"], "nonce", max_length=1024),
            issuer_ref=_identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            schema_version=_schema_version(
                value["schema_version"], "ProcedureObservationAuthority"
            ),
        )
        if _digest(value["replay_identity"], "replay_identity") != result.replay_identity:
            raise ValueError("ProcedureObservationAuthority replay_identity differs")
        return result


@dataclass(frozen=True, slots=True)
class ProcedureObservationAuthorityRef:
    authority_id: str
    authority_hash: str
    issuer_ref: str
    replay_identity: str
    schema_version: int = PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION
    ref_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION:
            raise ValueError("unsupported ProcedureObservationAuthorityRef schema_version")
        _identifier(self.authority_id, "authority_id")
        _digest(self.authority_hash, "authority_hash")
        _identifier(self.issuer_ref, "issuer_ref", max_length=1024)
        _digest(self.replay_identity, "replay_identity")
        object.__setattr__(
            self,
            "ref_hash",
            _domain_hash("simple-harness/procedure-observation-authority-ref/v1", self.to_json()),
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
    def from_json(cls, value: Mapping[str, object]) -> ProcedureObservationAuthorityRef:
        _exact_keys(
            value,
            {"schema_version", "authority_id", "authority_hash", "issuer_ref", "replay_identity"},
            "ProcedureObservationAuthorityRef",
        )
        return cls(
            _identifier(value["authority_id"], "authority_id"),
            _digest(value["authority_hash"], "authority_hash"),
            _identifier(value["issuer_ref"], "issuer_ref", max_length=1024),
            _digest(value["replay_identity"], "replay_identity"),
            _schema_version(value["schema_version"], "ProcedureObservationAuthorityRef"),
        )

    @classmethod
    def from_authority(
        cls, authority: ProcedureObservationAuthority
    ) -> ProcedureObservationAuthorityRef:
        if type(authority) is not ProcedureObservationAuthority:
            raise TypeError("authority must use ProcedureObservationAuthority")
        return cls(
            authority.authority_id,
            authority.authority_hash,
            authority.issuer_ref,
            authority.replay_identity,
        )


class ProcedureObservationAuthorityPort(Protocol):
    async def resolve_procedure_observation_authority(
        self, reference: ProcedureObservationAuthorityRef
    ) -> ProcedureObservationAuthority: ...


def issue_procedure_observation_authority(
    intent: ProcedureObservationIntent,
    *,
    authority_id: str,
    issued_at: float,
    expires_at: float,
    nonce: str,
    issuer_ref: str,
) -> ProcedureObservationAuthority:
    if type(intent) is not ProcedureObservationIntent:
        raise TypeError("intent must use ProcedureObservationIntent")
    return ProcedureObservationAuthority(
        authority_id, intent, issued_at, expires_at, nonce, issuer_ref
    )


async def verify_procedure_observation_authority(
    reference: ProcedureObservationAuthorityRef,
    authority: ProcedureObservationAuthorityPort,
    *,
    current_time: float,
) -> ProcedureObservationAuthority:
    """Resolve exactly once; the Memory consumer must atomically fence replay."""

    if type(reference) is not ProcedureObservationAuthorityRef:
        raise TypeError("reference must use ProcedureObservationAuthorityRef")
    now = _non_negative_number(current_time, "current_time")
    resolved = await authority.resolve_procedure_observation_authority(reference)
    if type(resolved) is not ProcedureObservationAuthority:
        raise TypeError("procedure observation authority port returned an invalid authority")
    expected_ref = ProcedureObservationAuthorityRef.from_authority(resolved)
    if expected_ref != reference or expected_ref.ref_hash != reference.ref_hash:
        raise ValueError("resolved ProcedureObservationAuthority differs from reference")
    if now < resolved.issued_at:
        raise ValueError("ProcedureObservationAuthority is not yet valid")
    if now >= resolved.expires_at:
        raise ValueError("ProcedureObservationAuthority is expired")
    return resolved


__all__ = (
    "PROCEDURE_APPLICABILITY_FINGERPRINT_VERSION",
    "PROCEDURE_OBSERVATION_AUTHORITY_SCHEMA_VERSION",
    "ProcedureApplicabilityContext",
    "ProcedureHazard",
    "ProcedureObservationAuthority",
    "ProcedureObservationAuthorityPort",
    "ProcedureObservationAuthorityRef",
    "ProcedureObservationIntent",
    "ProcedureObservationKind",
    "ProcedureObservationOutcome",
    "issue_procedure_observation_authority",
    "verify_procedure_observation_authority",
)
