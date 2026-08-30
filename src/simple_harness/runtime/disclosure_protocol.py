# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Disclosure authority carried by Human Memory protocol messages.

Natural-language and model supplied values are proposals only.  They may make a
trusted context narrower, but cannot manufacture a recipient or audit grant.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)

HUMAN_MEMORY_SCHEMA_VERSION = 1


def _identifier(value: object, name: str, *, max_length: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > max_length
    ):
        raise ValueError(f"{name} must be non-blank, bounded, and contain no NUL")
    return value


def _optional_identifier(value: object, name: str, *, max_length: int = 256) -> str | None:
    if value is None:
        return None
    return _identifier(value, name, max_length=max_length)


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _bounded_text(value: object, name: str, *, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ValueError(f"{name} must be non-blank, bounded, and contain no NUL")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{name} fields differ; missing={missing}, extra={extra}")


def _schema_version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} schema_version must be an integer")
    if value != HUMAN_MEMORY_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")
    return value


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return value


def _objects(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise TypeError(f"{name} must be an array of objects")
    return tuple(value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{name} must be an array of strings")
    return tuple(value)


def _canonical_hash(value: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _freeze_object(value: object, name: str) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    frozen = freeze_json(dict(value))
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return frozen


def _thaw_object(value: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    thawed = thaw_json(cast(FrozenJsonValue, value))
    if not isinstance(thawed, dict):
        raise TypeError("frozen value must thaw to a JSON object")
    return thawed


class DeliveryRecipient(StrEnum):
    USER_SELF = "user_self"
    HOUSEHOLD = "household"
    TASK_COLLABORATOR = "task_collaborator"
    EXTERNAL_PARTY = "external_party"
    PUBLIC = "public"
    AUDIT_REVIEWER = "audit_reviewer"
    UNKNOWN = "unknown"


class IntendedAudience(StrEnum):
    USER_SELF = "user_self"
    HOUSEHOLD = "household"
    TASK_COLLABORATORS = "task_collaborators"
    EXTERNAL = "external"
    PUBLIC = "public"
    AUDITOR = "auditor"
    UNKNOWN = "unknown"


class DisclosurePurpose(StrEnum):
    TASK_EXECUTION = "task_execution"
    PERSONALIZATION = "personalization"
    TASK_RESUME = "task_resume"
    USER_REVIEW = "user_review"
    AUDIT = "audit"
    EXPORT = "export"
    UNKNOWN = "unknown"


class DisclosureSource(StrEnum):
    AUTHENTICATED_HOST = "authenticated_host"
    AUTHENTICATED_UI = "authenticated_ui"
    TRUSTED_TOOL_DESTINATION = "trusted_tool_destination"
    AUDIT_ACCESS_DECISION = "audit_access_decision"
    USER_NATURAL_LANGUAGE = "user_natural_language"
    LLM_PROPOSAL = "llm_proposal"
    UNKNOWN = "unknown"


class DisclosureTrust(StrEnum):
    TRUSTED_AUTHORITY = "trusted_authority"
    TRUSTED_METADATA = "trusted_metadata"
    UNTRUSTED_PROPOSAL = "untrusted_proposal"
    UNKNOWN = "unknown"


class DisclosureGeneration(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"


class DisclosureReasonCode(StrEnum):
    MINIMUM_NECESSARY = "disclosure_minimum_necessary"
    UNKNOWN_RECIPIENT = "disclosure_unknown_recipient"
    UNKNOWN_PURPOSE = "disclosure_unknown_purpose"
    UNTRUSTED_SOURCE = "disclosure_untrusted_source"
    STALE_CONTEXT = "disclosure_stale_context"
    CONFLICTED_CONTEXT = "disclosure_conflicted_context"
    AUDIT_GRANT_REQUIRED = "disclosure_audit_grant_required"
    SENSITIVE_EXTERNAL_DENIED = "disclosure_sensitive_external_denied"


_AUTHORITY_SOURCES = {
    DisclosureSource.AUTHENTICATED_HOST,
    DisclosureSource.AUTHENTICATED_UI,
    DisclosureSource.AUDIT_ACCESS_DECISION,
}


@dataclass(frozen=True, slots=True)
class DisclosureContext:
    run_id: str
    subject: str
    recipient: DeliveryRecipient
    recipient_id: str | None
    intended_audience: IntendedAudience
    purpose: DisclosurePurpose
    source: DisclosureSource
    trust: DisclosureTrust
    generation: DisclosureGeneration
    authority_ref: str | None
    reason_codes: tuple[DisclosureReasonCode, ...]
    schema_version: int = HUMAN_MEMORY_SCHEMA_VERSION
    context_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != HUMAN_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported DisclosureContext schema_version")
        _identifier(self.run_id, "run_id")
        _identifier(self.subject, "subject")
        object.__setattr__(self, "recipient", DeliveryRecipient(self.recipient))
        object.__setattr__(self, "intended_audience", IntendedAudience(self.intended_audience))
        object.__setattr__(self, "purpose", DisclosurePurpose(self.purpose))
        object.__setattr__(self, "source", DisclosureSource(self.source))
        object.__setattr__(self, "trust", DisclosureTrust(self.trust))
        object.__setattr__(self, "generation", DisclosureGeneration(self.generation))
        _optional_identifier(self.recipient_id, "recipient_id")
        _optional_identifier(self.authority_ref, "authority_ref")
        reasons = tuple(DisclosureReasonCode(reason) for reason in self.reason_codes)
        if len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must be unique")
        object.__setattr__(self, "reason_codes", reasons)
        if self.trust is DisclosureTrust.TRUSTED_AUTHORITY:
            if self.source not in _AUTHORITY_SOURCES or self.authority_ref is None:
                raise ValueError(
                    "trusted disclosure authority requires a trusted source and authority_ref"
                )
        if self.source in {
            DisclosureSource.LLM_PROPOSAL,
            DisclosureSource.USER_NATURAL_LANGUAGE,
            DisclosureSource.UNKNOWN,
        } and self.trust is not DisclosureTrust.UNTRUSTED_PROPOSAL:
            raise ValueError("natural-language and model sources cannot grant disclosure authority")
        if self.purpose is DisclosurePurpose.AUDIT and (
            self.source is not DisclosureSource.AUDIT_ACCESS_DECISION
            or self.recipient is not DeliveryRecipient.AUDIT_REVIEWER
        ):
            raise ValueError("audit disclosure requires an AuditAccessDecision and audit recipient")
        if self.recipient is DeliveryRecipient.UNKNOWN and (
            DisclosureReasonCode.UNKNOWN_RECIPIENT not in reasons
        ):
            raise ValueError("unknown recipient requires disclosure_unknown_recipient")
        if self.purpose is DisclosurePurpose.UNKNOWN and (
            DisclosureReasonCode.UNKNOWN_PURPOSE not in reasons
        ):
            raise ValueError("unknown purpose requires disclosure_unknown_purpose")
        object.__setattr__(self, "context_hash", _canonical_hash(self.to_json()))

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "recipient": self.recipient.value,
            "recipient_id": self.recipient_id,
            "intended_audience": self.intended_audience.value,
            "purpose": self.purpose.value,
            "source": self.source.value,
            "trust": self.trust.value,
            "generation": self.generation.value,
            "authority_ref": self.authority_ref,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> DisclosureContext:
        _exact_keys(
            value,
            {
                "schema_version",
                "run_id",
                "subject",
                "recipient",
                "recipient_id",
                "intended_audience",
                "purpose",
                "source",
                "trust",
                "generation",
                "authority_ref",
                "reason_codes",
            },
            "DisclosureContext",
        )
        reasons = value["reason_codes"]
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise TypeError("reason_codes must be an array of strings")
        schema_version = _schema_version(value["schema_version"], "DisclosureContext")
        return cls(
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            recipient=DeliveryRecipient(value["recipient"]),  # type: ignore[arg-type]
            recipient_id=_optional_identifier(value["recipient_id"], "recipient_id"),
            intended_audience=IntendedAudience(value["intended_audience"]),  # type: ignore[arg-type]
            purpose=DisclosurePurpose(value["purpose"]),  # type: ignore[arg-type]
            source=DisclosureSource(value["source"]),  # type: ignore[arg-type]
            trust=DisclosureTrust(value["trust"]),  # type: ignore[arg-type]
            generation=DisclosureGeneration(value["generation"]),  # type: ignore[arg-type]
            authority_ref=_optional_identifier(value["authority_ref"], "authority_ref"),
            reason_codes=tuple(DisclosureReasonCode(item) for item in reasons),
            schema_version=schema_version,
        )


__all__ = (
    "HUMAN_MEMORY_SCHEMA_VERSION",
    "DeliveryRecipient",
    "DisclosureContext",
    "DisclosureGeneration",
    "DisclosurePurpose",
    "DisclosureReasonCode",
    "DisclosureSource",
    "DisclosureTrust",
    "IntendedAudience",
)
