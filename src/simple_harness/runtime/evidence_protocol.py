# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Sanitized evidence and auditable main-model analysis contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from simple_harness.contracts import FrozenJsonValue, JsonValue

from .disclosure_protocol import (
    HUMAN_MEMORY_SCHEMA_VERSION,
    DisclosureContext,
    _canonical_hash,
    _digest,
    _exact_keys,
    _freeze_object,
    _identifier,
    _non_negative_number,
    _object,
    _objects,
    _optional_identifier,
    _positive_int,
    _schema_version,
    _strings,
    _thaw_object,
)


class EvidenceSourceKind(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    PROVIDER_RECORD = "provider_record"
    RUNTIME_EVENT = "runtime_event"
    TYPED_OBSERVATION = "typed_observation"


class RemovedSpanType(StrEnum):
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"
    COOKIE = "cookie"
    AUTHORIZATION_HEADER = "authorization_header"
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    HIDDEN_REASONING = "hidden_reasoning"
    OTHER_CREDENTIAL = "other_credential"


class EvidenceReasonCode(StrEnum):
    SANITIZED_AND_ACCEPTED = "evidence_sanitized_and_accepted"
    SOURCE_HASH_MISMATCH = "evidence_source_hash_mismatch"
    SANITIZED_HASH_MISMATCH = "evidence_sanitized_hash_mismatch"
    FILTER_POLICY_UNSUPPORTED = "evidence_filter_policy_unsupported"
    CREDENTIAL_CANARY_REJECTED = "evidence_credential_canary_rejected"
    HIDDEN_REASONING_REJECTED = "evidence_hidden_reasoning_rejected"
    ORDERED_EVIDENCE_MISMATCH = "analysis_ordered_evidence_mismatch"
    VALIDATOR_ACCEPTED = "analysis_validator_accepted"
    VALIDATOR_REJECTED = "analysis_validator_rejected"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    content_hash: str
    ordinal: int

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence_id")
        _digest(self.content_hash, "content_hash")
        _positive_int(self.ordinal, "ordinal")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> EvidenceRef:
        _exact_keys(value, {"evidence_id", "content_hash", "ordinal"}, "EvidenceRef")
        return cls(
            _identifier(value["evidence_id"], "evidence_id"),
            _digest(value["content_hash"], "content_hash"),
            _positive_int(value["ordinal"], "ordinal"),
        )


def _evidence_refs(value: object, name: str = "evidence_refs") -> tuple[EvidenceRef, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, EvidenceRef) for item in value
    ):
        raise TypeError(f"{name} must contain EvidenceRef values")
    refs = tuple(value)
    if len({ref.evidence_id for ref in refs}) != len(refs):
        raise ValueError(f"{name} evidence_id values must be unique")
    if refs and [ref.ordinal for ref in refs] != list(range(1, len(refs) + 1)):
        raise ValueError(f"{name} ordinals must be contiguous and start at 1")
    return refs


def _refs_from_json(value: object, name: str = "evidence_refs") -> tuple[EvidenceRef, ...]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise TypeError(f"{name} must be an array of objects")
    return _evidence_refs(tuple(EvidenceRef.from_json(item) for item in value), name)


@dataclass(frozen=True, slots=True)
class RemovedSpanSummary:
    span_type: RemovedSpanType
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "span_type", RemovedSpanType(self.span_type))
        _positive_int(self.count, "count")

    def to_json(self) -> dict[str, JsonValue]:
        return {"span_type": self.span_type.value, "count": self.count}

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RemovedSpanSummary:
        _exact_keys(value, {"span_type", "count"}, "RemovedSpanSummary")
        return cls(RemovedSpanType(value["span_type"]), _positive_int(value["count"], "count"))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SanitizedEvidenceEnvelope:
    evidence_id: str
    run_id: str
    subject: str
    source_kind: EvidenceSourceKind
    source_ref: str
    source_hash: str
    sanitized_payload: Mapping[str, FrozenJsonValue]
    sanitized_hash: str
    filter_policy_version: str
    removed_spans: tuple[RemovedSpanSummary, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    envelope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported SanitizedEvidenceEnvelope schema_version")
        _identifier(self.evidence_id, "evidence_id")
        _identifier(self.run_id, "run_id")
        _identifier(self.subject, "subject")
        object.__setattr__(self, "source_kind", EvidenceSourceKind(self.source_kind))
        _identifier(self.source_ref, "source_ref", max_length=1024)
        _digest(self.source_hash, "source_hash")
        _digest(self.sanitized_hash, "sanitized_hash")
        _identifier(self.filter_policy_version, "filter_policy_version")
        payload = _freeze_object(self.sanitized_payload, "sanitized_payload")
        if _canonical_hash(_thaw_object(payload)) != self.sanitized_hash:
            raise ValueError(EvidenceReasonCode.SANITIZED_HASH_MISMATCH.value)
        spans = tuple(self.removed_spans)
        if not all(isinstance(span, RemovedSpanSummary) for span in spans):
            raise TypeError("removed_spans must contain RemovedSpanSummary values")
        if len({span.span_type for span in spans}) != len(spans):
            raise ValueError("removed_spans span types must be unique")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        object.__setattr__(self, "sanitized_payload", payload)
        object.__setattr__(self, "removed_spans", spans)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "envelope_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "source_kind": self.source_kind.value,
            "source_ref": self.source_ref,
            "source_hash": self.source_hash,
            "sanitized_payload": _thaw_object(self.sanitized_payload),
            "sanitized_hash": self.sanitized_hash,
            "filter_policy_version": self.filter_policy_version,
            "removed_spans": [span.to_json() for span in self.removed_spans],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SanitizedEvidenceEnvelope:
        _exact_keys(
            value,
            {
                "schema_version",
                "evidence_id",
                "run_id",
                "subject",
                "source_kind",
                "source_ref",
                "source_hash",
                "sanitized_payload",
                "sanitized_hash",
                "filter_policy_version",
                "removed_spans",
                "disclosure_context",
                "evidence_refs",
            },
            "SanitizedEvidenceEnvelope",
        )
        return cls(
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            source_kind=EvidenceSourceKind(value["source_kind"]),  # type: ignore[arg-type]
            source_ref=_identifier(value["source_ref"], "source_ref", max_length=1024),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_payload=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["sanitized_payload"], "sanitized_payload"),
            ),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            filter_policy_version=_identifier(
                value["filter_policy_version"], "filter_policy_version"
            ),
            removed_spans=tuple(
                RemovedSpanSummary.from_json(item)
                for item in _objects(value["removed_spans"], "removed_spans")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            schema_version=_schema_version(
                value["schema_version"], "SanitizedEvidenceEnvelope"
            ),
        )


@dataclass(frozen=True, slots=True)
class SanitizedEvidenceReceipt:
    receipt_id: str
    run_id: str
    subject: str
    evidence_id: str
    envelope_hash: str
    source_hash: str
    sanitized_hash: str
    filter_policy_version: str
    accepted: bool
    reason_codes: tuple[EvidenceReasonCode, ...]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    admitted_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported SanitizedEvidenceReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.evidence_id, "evidence_id"),
            (self.filter_policy_version, "filter_policy_version"),
        ):
            _identifier(value, name)
        for value, name in (
            (self.envelope_hash, "envelope_hash"),
            (self.source_hash, "source_hash"),
            (self.sanitized_hash, "sanitized_hash"),
        ):
            _digest(value, name)
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a boolean")
        reasons = tuple(EvidenceReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        if self.accepted != (EvidenceReasonCode.SANITIZED_AND_ACCEPTED in reasons):
            raise ValueError("accepted and reason_codes differ")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        admitted_at = _non_negative_number(self.admitted_at, "admitted_at")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "admitted_at", admitted_at)
        object.__setattr__(self, "receipt_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "evidence_id": self.evidence_id,
            "envelope_hash": self.envelope_hash,
            "source_hash": self.source_hash,
            "sanitized_hash": self.sanitized_hash,
            "filter_policy_version": self.filter_policy_version,
            "accepted": self.accepted,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "admitted_at": self.admitted_at,
        }

    def verify(self, envelope: SanitizedEvidenceEnvelope) -> None:
        if not self.accepted:
            raise ValueError("sanitized evidence receipt is not accepted")
        expected = (
            self.run_id,
            self.subject,
            self.evidence_id,
            self.envelope_hash,
            self.source_hash,
            self.sanitized_hash,
            self.filter_policy_version,
        )
        actual = (
            envelope.run_id,
            envelope.subject,
            envelope.evidence_id,
            envelope.envelope_hash,
            envelope.source_hash,
            envelope.sanitized_hash,
            envelope.filter_policy_version,
        )
        if expected != actual:
            raise ValueError("sanitized evidence receipt does not bind the envelope")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SanitizedEvidenceReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "run_id",
                "subject",
                "evidence_id",
                "envelope_hash",
                "source_hash",
                "sanitized_hash",
                "filter_policy_version",
                "accepted",
                "reason_codes",
                "disclosure_context",
                "evidence_refs",
                "admitted_at",
            },
            "SanitizedEvidenceReceipt",
        )
        accepted = value["accepted"]
        if not isinstance(accepted, bool):
            raise TypeError("accepted must be a boolean")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            evidence_id=_identifier(value["evidence_id"], "evidence_id"),
            envelope_hash=_digest(value["envelope_hash"], "envelope_hash"),
            source_hash=_digest(value["source_hash"], "source_hash"),
            sanitized_hash=_digest(value["sanitized_hash"], "sanitized_hash"),
            filter_policy_version=_identifier(
                value["filter_policy_version"], "filter_policy_version"
            ),
            accepted=accepted,
            reason_codes=tuple(
                EvidenceReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            admitted_at=_non_negative_number(value["admitted_at"], "admitted_at"),
            schema_version=_schema_version(
                value["schema_version"], "SanitizedEvidenceReceipt"
            ),
        )


class ExecutionEvidenceKind(StrEnum):
    PROVIDER_INVOCATION = "provider_invocation"
    TOOL_INVOCATION = "tool_invocation"
    CONTEXT_SNAPSHOT = "context_snapshot"
    ROUTE_DECISION = "route_decision"
    RUN_TERMINAL = "run_terminal"


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    event_id: str
    run_id: str
    subject: str
    kind: ExecutionEvidenceKind
    public_payload: Mapping[str, FrozenJsonValue]
    disclosure_context: DisclosureContext
    evidence_refs: tuple[EvidenceRef, ...]
    idempotency_key: str
    occurred_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported ExecutionEvidence schema_version")
        for value, name in (
            (self.event_id, "event_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        object.__setattr__(self, "kind", ExecutionEvidenceKind(self.kind))
        payload = _freeze_object(self.public_payload, "public_payload")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        refs = _evidence_refs(self.evidence_refs)
        occurred_at = _non_negative_number(self.occurred_at, "occurred_at")
        object.__setattr__(self, "public_payload", payload)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "evidence_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "kind": self.kind.value,
            "public_payload": _thaw_object(self.public_payload),
            "disclosure_context": self.disclosure_context.to_json(),
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ExecutionEvidence:
        _exact_keys(
            value,
            {
                "schema_version",
                "event_id",
                "run_id",
                "subject",
                "kind",
                "public_payload",
                "disclosure_context",
                "evidence_refs",
                "idempotency_key",
                "occurred_at",
            },
            "ExecutionEvidence",
        )
        return cls(
            event_id=_identifier(value["event_id"], "event_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            kind=ExecutionEvidenceKind(value["kind"]),  # type: ignore[arg-type]
            public_payload=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["public_payload"], "public_payload"),
            ),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            evidence_refs=_refs_from_json(value["evidence_refs"]),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            occurred_at=_non_negative_number(value["occurred_at"], "occurred_at"),
            schema_version=_schema_version(value["schema_version"], "ExecutionEvidence"),
        )


@dataclass(frozen=True, slots=True)
class AnalysisBudget:
    max_input_tokens: int
    max_output_tokens: int
    deadline_ms: int
    max_cost_microunits: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "deadline_ms"):
            _positive_int(getattr(self, name), name)
        if (
            isinstance(self.max_cost_microunits, bool)
            or not isinstance(self.max_cost_microunits, int)
            or self.max_cost_microunits < 0
        ):
            raise ValueError("max_cost_microunits must be a non-negative integer")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_ms": self.deadline_ms,
            "max_cost_microunits": self.max_cost_microunits,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> AnalysisBudget:
        _exact_keys(
            value,
            {"max_input_tokens", "max_output_tokens", "deadline_ms", "max_cost_microunits"},
            "AnalysisBudget",
        )
        cost = value["max_cost_microunits"]
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError("max_cost_microunits must be a non-negative integer")
        return cls(
            _positive_int(value["max_input_tokens"], "max_input_tokens"),
            _positive_int(value["max_output_tokens"], "max_output_tokens"),
            _positive_int(value["deadline_ms"], "deadline_ms"),
            cost,
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisRequest:
    job_id: str
    run_id: str
    subject: str
    ordered_evidence_refs: tuple[EvidenceRef, ...]
    prompt_version: str
    result_schema_version: str
    policy_version: str
    provider_id: str
    model_id: str
    model_config_hash: str
    attempt: int
    budget: AnalysisBudget
    disclosure_context: DisclosureContext
    idempotency_key: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisRequest schema_version")
        for value, name in (
            (self.job_id, "job_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.prompt_version, "prompt_version"),
            (self.result_schema_version, "result_schema_version"),
            (self.policy_version, "policy_version"),
            (self.provider_id, "provider_id"),
            (self.model_id, "model_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(value, name)
        _digest(self.model_config_hash, "model_config_hash")
        _positive_int(self.attempt, "attempt")
        refs = _evidence_refs(self.ordered_evidence_refs, "ordered_evidence_refs")
        if not refs:
            raise ValueError("ordered_evidence_refs must be non-empty")
        if not isinstance(self.budget, AnalysisBudget):
            raise TypeError("budget must use AnalysisBudget")
        if not isinstance(self.disclosure_context, DisclosureContext):
            raise TypeError("disclosure_context must use DisclosureContext")
        if self.disclosure_context.run_id != self.run_id:
            raise ValueError("disclosure_context run_id differs")
        object.__setattr__(self, "ordered_evidence_refs", refs)
        object.__setattr__(self, "request_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "ordered_evidence_refs": [ref.to_json() for ref in self.ordered_evidence_refs],
            "prompt_version": self.prompt_version,
            "result_schema_version": self.result_schema_version,
            "policy_version": self.policy_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_config_hash": self.model_config_hash,
            "attempt": self.attempt,
            "budget": self.budget.to_json(),
            "disclosure_context": self.disclosure_context.to_json(),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisRequest:
        _exact_keys(
            value,
            {
                "schema_version",
                "job_id",
                "run_id",
                "subject",
                "ordered_evidence_refs",
                "prompt_version",
                "result_schema_version",
                "policy_version",
                "provider_id",
                "model_id",
                "model_config_hash",
                "attempt",
                "budget",
                "disclosure_context",
                "idempotency_key",
            },
            "MemoryAnalysisRequest",
        )
        return cls(
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            ordered_evidence_refs=_refs_from_json(
                value["ordered_evidence_refs"], "ordered_evidence_refs"
            ),
            prompt_version=_identifier(value["prompt_version"], "prompt_version"),
            result_schema_version=_identifier(
                value["result_schema_version"], "result_schema_version"
            ),
            policy_version=_identifier(value["policy_version"], "policy_version"),
            provider_id=_identifier(value["provider_id"], "provider_id"),
            model_id=_identifier(value["model_id"], "model_id"),
            model_config_hash=_digest(value["model_config_hash"], "model_config_hash"),
            attempt=_positive_int(value["attempt"], "attempt"),
            budget=AnalysisBudget.from_json(_object(value["budget"], "budget")),
            disclosure_context=DisclosureContext.from_json(
                _object(value["disclosure_context"], "disclosure_context")
            ),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisRequest"),
        )


class AnalysisValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class MemoryAnalysisResult:
    job_id: str
    run_id: str
    request_hash: str
    provider_response_id: str | None
    structured_result: Mapping[str, FrozenJsonValue]
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    latency_ms: int
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    result_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisResult schema_version")
        _identifier(self.job_id, "job_id")
        _identifier(self.run_id, "run_id")
        _digest(self.request_hash, "request_hash")
        _optional_identifier(self.provider_response_id, "provider_response_id")
        result = _freeze_object(self.structured_result, "structured_result")
        for name in ("input_tokens", "output_tokens", "cost_microunits", "latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "structured_result", result)
        object.__setattr__(self, "result_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "request_hash": self.request_hash,
            "provider_response_id": self.provider_response_id,
            "structured_result": _thaw_object(self.structured_result),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_microunits": self.cost_microunits,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisResult:
        _exact_keys(
            value,
            {
                "schema_version",
                "job_id",
                "run_id",
                "request_hash",
                "provider_response_id",
                "structured_result",
                "input_tokens",
                "output_tokens",
                "cost_microunits",
                "latency_ms",
            },
            "MemoryAnalysisResult",
        )
        counts: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens", "cost_microunits", "latency_ms"):
            count = value[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            counts[name] = count
        return cls(
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            provider_response_id=_optional_identifier(
                value["provider_response_id"], "provider_response_id"
            ),
            structured_result=cast(
                Mapping[str, FrozenJsonValue],
                _object(value["structured_result"], "structured_result"),
            ),
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            cost_microunits=counts["cost_microunits"],
            latency_ms=counts["latency_ms"],
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisResult"),
        )


def _analysis_domain_hash(domain: str, payload: dict[str, JsonValue]) -> str:
    return _canonical_hash({"protocol": domain, "payload": payload})


@dataclass(frozen=True, slots=True)
class MemoryAnalysisDeliveryReceipt:
    """Host record proving durable delivery of one exact provider result.

    This receipt is separate from :class:`MemoryAnalysisReceipt`, which records
    Memory-side validation/application. Its public hashes are audit material,
    not authority; consumers must call ``MemoryAnalysisDeliveryAuthorityPort``.
    """

    receipt_id: str
    issuer_id: str
    run_id: str
    job_id: str
    request_hash: str
    result_hash: str
    attempt: int
    provider_response_id: str | None
    provider_response_hash: str
    issued_at: float
    host_receipt_id: str
    host_receipt_hash: str
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisDeliveryReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.issuer_id, "issuer_id"),
            (self.run_id, "run_id"),
            (self.job_id, "job_id"),
            (self.host_receipt_id, "host_receipt_id"),
        ):
            _identifier(value, name)
        _optional_identifier(self.provider_response_id, "provider_response_id")
        for value, name in (
            (self.request_hash, "request_hash"),
            (self.result_hash, "result_hash"),
            (self.provider_response_hash, "provider_response_hash"),
            (self.host_receipt_hash, "host_receipt_hash"),
        ):
            _digest(value, name)
        _positive_int(self.attempt, "attempt")
        issued_at = _non_negative_number(self.issued_at, "issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(
            self,
            "receipt_hash",
            _analysis_domain_hash("memory-analysis/delivery-receipt/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "issuer_id": self.issuer_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "request_hash": self.request_hash,
            "result_hash": self.result_hash,
            "attempt": self.attempt,
            "provider_response_id": self.provider_response_id,
            "provider_response_hash": self.provider_response_hash,
            "issued_at": self.issued_at,
            "host_receipt_id": self.host_receipt_id,
            "host_receipt_hash": self.host_receipt_hash,
        }

    def _verify_result_link(self, result: MemoryAnalysisResult) -> None:
        if not isinstance(result, MemoryAnalysisResult):
            raise TypeError("result must use MemoryAnalysisResult")
        exact = (
            (self.job_id, result.job_id),
            (self.run_id, result.run_id),
            (self.request_hash, result.request_hash),
            (self.result_hash, result.result_hash),
            (self.provider_response_id, result.provider_response_id),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Memory analysis delivery differs from result")

    def verify_result(
        self, request: MemoryAnalysisRequest, result: MemoryAnalysisResult
    ) -> None:
        if not isinstance(request, MemoryAnalysisRequest):
            raise TypeError("request must use MemoryAnalysisRequest")
        self._verify_result_link(result)
        exact = (
            (self.job_id, request.job_id),
            (self.run_id, request.run_id),
            (self.request_hash, request.request_hash),
            (self.attempt, request.attempt),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Memory analysis delivery differs from request or result")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisDeliveryReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "issuer_id",
                "run_id",
                "job_id",
                "request_hash",
                "result_hash",
                "attempt",
                "provider_response_id",
                "provider_response_hash",
                "issued_at",
                "host_receipt_id",
                "host_receipt_hash",
            },
            "MemoryAnalysisDeliveryReceipt",
        )
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            issuer_id=_identifier(value["issuer_id"], "issuer_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            job_id=_identifier(value["job_id"], "job_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            result_hash=_digest(value["result_hash"], "result_hash"),
            attempt=_positive_int(value["attempt"], "attempt"),
            provider_response_id=_optional_identifier(
                value["provider_response_id"], "provider_response_id"
            ),
            provider_response_hash=_digest(
                value["provider_response_hash"], "provider_response_hash"
            ),
            issued_at=_non_negative_number(value["issued_at"], "issued_at"),
            host_receipt_id=_identifier(value["host_receipt_id"], "host_receipt_id"),
            host_receipt_hash=_digest(value["host_receipt_hash"], "host_receipt_hash"),
            schema_version=_schema_version(
                value["schema_version"], "MemoryAnalysisDeliveryReceipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisResultEnvelope:
    result: MemoryAnalysisResult
    delivery_receipt: MemoryAnalysisDeliveryReceipt
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    envelope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisResultEnvelope schema_version")
        if not isinstance(self.result, MemoryAnalysisResult):
            raise TypeError("result must use MemoryAnalysisResult")
        if not isinstance(self.delivery_receipt, MemoryAnalysisDeliveryReceipt):
            raise TypeError("delivery_receipt must use MemoryAnalysisDeliveryReceipt")
        self.delivery_receipt._verify_result_link(self.result)
        object.__setattr__(
            self,
            "envelope_hash",
            _analysis_domain_hash("memory-analysis/result-envelope/v1", self.to_json()),
        )

    def verify_request(self, request: MemoryAnalysisRequest) -> None:
        self.delivery_receipt.verify_result(request, self.result)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "result": self.result.to_json(),
            "delivery_receipt": self.delivery_receipt.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisResultEnvelope:
        _exact_keys(
            value,
            {"schema_version", "result", "delivery_receipt"},
            "MemoryAnalysisResultEnvelope",
        )
        return cls(
            result=MemoryAnalysisResult.from_json(_object(value["result"], "result")),
            delivery_receipt=MemoryAnalysisDeliveryReceipt.from_json(
                _object(value["delivery_receipt"], "delivery_receipt")
            ),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisResultEnvelope"),
        )


@dataclass(frozen=True, slots=True)
class MemoryAnalysisReceipt:
    receipt_id: str
    job_id: str
    run_id: str
    request_hash: str
    result_hash: str
    validator_version: str
    validation_status: AnalysisValidationStatus
    reason_codes: tuple[EvidenceReasonCode, ...]
    committed_revision: int | None
    committed_at: float
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported MemoryAnalysisReceipt schema_version")
        for value, name in (
            (self.receipt_id, "receipt_id"),
            (self.job_id, "job_id"),
            (self.run_id, "run_id"),
            (self.validator_version, "validator_version"),
        ):
            _identifier(value, name)
        _digest(self.request_hash, "request_hash")
        _digest(self.result_hash, "result_hash")
        object.__setattr__(
            self, "validation_status", AnalysisValidationStatus(self.validation_status)
        )
        reasons = tuple(EvidenceReasonCode(reason) for reason in self.reason_codes)
        if not reasons or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be non-empty and unique")
        if self.validation_status is AnalysisValidationStatus.ACCEPTED:
            if EvidenceReasonCode.VALIDATOR_ACCEPTED not in reasons:
                raise ValueError("accepted analysis requires analysis_validator_accepted")
            if self.committed_revision is None:
                raise ValueError("accepted analysis requires committed_revision")
        elif self.committed_revision is not None:
            raise ValueError("non-accepted analysis cannot commit a revision")
        if self.committed_revision is not None:
            _positive_int(self.committed_revision, "committed_revision")
        committed_at = _non_negative_number(self.committed_at, "committed_at")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "committed_at", committed_at)
        object.__setattr__(self, "receipt_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "request_hash": self.request_hash,
            "result_hash": self.result_hash,
            "validator_version": self.validator_version,
            "validation_status": self.validation_status.value,
            "reason_codes": [reason.value for reason in self.reason_codes],
            "committed_revision": self.committed_revision,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> MemoryAnalysisReceipt:
        _exact_keys(
            value,
            {
                "schema_version",
                "receipt_id",
                "job_id",
                "run_id",
                "request_hash",
                "result_hash",
                "validator_version",
                "validation_status",
                "reason_codes",
                "committed_revision",
                "committed_at",
            },
            "MemoryAnalysisReceipt",
        )
        revision = value["committed_revision"]
        if revision is not None:
            revision = _positive_int(revision, "committed_revision")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            job_id=_identifier(value["job_id"], "job_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            result_hash=_digest(value["result_hash"], "result_hash"),
            validator_version=_identifier(value["validator_version"], "validator_version"),
            validation_status=AnalysisValidationStatus(value["validation_status"]),  # type: ignore[arg-type]
            reason_codes=tuple(
                EvidenceReasonCode(item) for item in _strings(value["reason_codes"], "reason_codes")
            ),
            committed_revision=revision,
            committed_at=_non_negative_number(value["committed_at"], "committed_at"),
            schema_version=_schema_version(value["schema_version"], "MemoryAnalysisReceipt"),
        )


class MemoryAnalysisExecutorPort(Protocol):
    async def analyze_memory(
        self, request: MemoryAnalysisRequest
    ) -> MemoryAnalysisResultEnvelope: ...


class MemoryAnalysisDeliveryAuthorityPort(Protocol):
    """Verifies an exact delivery against Host durable state or authenticated proof."""

    async def verify_analysis_delivery(
        self, request: MemoryAnalysisRequest, envelope: MemoryAnalysisResultEnvelope
    ) -> None: ...


__all__ = (
    "AnalysisBudget",
    "AnalysisValidationStatus",
    "EvidenceReasonCode",
    "EvidenceRef",
    "EvidenceSourceKind",
    "ExecutionEvidence",
    "ExecutionEvidenceKind",
    "MemoryAnalysisDeliveryAuthorityPort",
    "MemoryAnalysisDeliveryReceipt",
    "MemoryAnalysisExecutorPort",
    "MemoryAnalysisReceipt",
    "MemoryAnalysisRequest",
    "MemoryAnalysisResult",
    "MemoryAnalysisResultEnvelope",
    "RemovedSpanSummary",
    "RemovedSpanType",
    "SanitizedEvidenceEnvelope",
    "SanitizedEvidenceReceipt",
)
