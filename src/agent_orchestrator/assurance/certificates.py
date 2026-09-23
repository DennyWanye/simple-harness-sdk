# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Immutable use certificate contract; decoding does not grant authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codec import AssuranceError, array, canonical, fields, integer, one_of, text, unique_texts
from .evidence import ReadItem, canonical_read_set
from .refs import AssuranceRef, Pin

PURPOSES = frozenset({"PLAN", "START", "MAINTAIN", "ACCEPT", "CONTEXT", "DISCLOSE", "RECOVERY"})


@dataclass(frozen=True, slots=True)
class UseCertificate:
    mission_id: str
    consumer_kind: str
    consumer_id: str
    scope_id: str
    principal_id: str
    purpose: str
    truth: str
    freshness: str
    availability: str
    decision: str
    coverage: str
    policy_ref: Pin
    read_set: tuple[ReadItem, ...]
    clean_support_refs: tuple[AssuranceRef, ...]
    issued_at_ms: int
    not_after_ms: int | None
    reasons: tuple[str, ...]
    mission_epoch: int
    environment_epoch: int
    clock_generation: int
    root_incarnation_id: str

    def __post_init__(self) -> None:
        for value in (
            self.mission_id,
            self.consumer_kind,
            self.consumer_id,
            self.scope_id,
            self.principal_id,
            self.root_incarnation_id,
        ):
            text(value)
        one_of(self.purpose, PURPOSES)
        one_of(self.truth, {"TRUE", "FALSE", "UNKNOWN", "CONFLICT"})
        one_of(self.freshness, {"CURRENT", "STALE", "REVOKED"})
        one_of(self.availability, {"READABLE", "REDACTED", "UNAVAILABLE"})
        one_of(self.decision, {"USABLE", "NEEDS_REVIEW", "BLOCKED", "UNAVAILABLE"})
        one_of(self.coverage, {"COMPLETE", "INCOMPLETE"})
        if not isinstance(self.policy_ref, Pin):
            raise AssuranceError("CERTIFICATE_POLICY_INVALID")
        for value in (
            self.issued_at_ms,
            self.mission_epoch,
            self.environment_epoch,
            self.clock_generation,
        ):
            integer(value)
        if self.not_after_ms is not None:
            integer(self.not_after_ms)
        if not self.read_set:
            raise AssuranceError("READSET_INCOMPLETE")
        object.__setattr__(self, "read_set", canonical_read_set(self.read_set))
        if len(self.clean_support_refs) > 20_000 or len(set(self.clean_support_refs)) != len(
            self.clean_support_refs
        ):
            raise AssuranceError("CERTIFICATE_SUPPORT_INVALID")
        object.__setattr__(
            self,
            "clean_support_refs",
            tuple(sorted(self.clean_support_refs, key=lambda ref: ref.key)),
        )
        object.__setattr__(self, "reasons", unique_texts(list(self.reasons), maximum=64))
        if self.decision == "USABLE":
            if (
                self.truth != "TRUE"
                or self.freshness != "CURRENT"
                or self.availability != "READABLE"
                or self.coverage != "COMPLETE"
            ):
                raise AssuranceError("CERTIFICATE_NOT_USABLE")
            if self.not_after_ms is not None and self.not_after_ms <= self.issued_at_ms:
                raise AssuranceError("CERTIFICATE_EXPIRED_AT_ISSUE")
            channels = {row.channel for row in self.read_set}
            if not {"OBJECT", "QUERY_SET", "ACCESS", "POLICY"} <= channels:
                raise AssuranceError("READSET_INCOMPLETE")
        canonical(self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "mission_id": self.mission_id,
            "consumer_kind": self.consumer_kind,
            "consumer_id": self.consumer_id,
            "scope_id": self.scope_id,
            "principal_id": self.principal_id,
            "purpose": self.purpose,
            "truth": self.truth,
            "freshness": self.freshness,
            "availability": self.availability,
            "decision": self.decision,
            "coverage": self.coverage,
            "policy_ref": self.policy_ref.to_json(),
            "read_set": [row.to_json() for row in self.read_set],
            "clean_support_refs": [ref.to_json() for ref in self.clean_support_refs],
            "issued_at_ms": self.issued_at_ms,
            "not_after_ms": self.not_after_ms,
            "reasons": list(self.reasons),
            "mission_epoch": self.mission_epoch,
            "environment_epoch": self.environment_epoch,
            "clock_generation": self.clock_generation,
            "root_incarnation_id": self.root_incarnation_id,
        }

    @classmethod
    def from_json(cls, value: object) -> UseCertificate:
        canonical(value)
        row = fields(
            value,
            {
                "schema_version",
                "mission_id",
                "consumer_kind",
                "consumer_id",
                "scope_id",
                "principal_id",
                "purpose",
                "truth",
                "freshness",
                "availability",
                "decision",
                "coverage",
                "policy_ref",
                "read_set",
                "clean_support_refs",
                "issued_at_ms",
                "not_after_ms",
                "reasons",
                "mission_epoch",
                "environment_epoch",
                "clock_generation",
                "root_incarnation_id",
            },
        )
        if integer(row["schema_version"]) != 2:
            raise AssuranceError("CERTIFICATE_SCHEMA_VERSION")
        data = {key: value for key, value in row.items() if key != "schema_version"}
        data["policy_ref"] = Pin.from_json(row["policy_ref"])
        data["read_set"] = tuple(
            ReadItem.from_json(item) for item in array(row["read_set"], minimum=1, maximum=20_000)
        )
        data["clean_support_refs"] = tuple(
            AssuranceRef.from_json(ref) for ref in array(row["clean_support_refs"], maximum=20_000)
        )
        data["reasons"] = unique_texts(row["reasons"], maximum=64)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class UseIdentity:
    """Actual current caller and consumer identity, supplied by trusted assembly."""

    mission_id: str
    consumer_kind: str
    consumer_id: str
    scope_id: str
    principal_id: str
    purpose: str
    root_incarnation_id: str


def check_certificate_binding(
    certificate: UseCertificate,
    *,
    identity: UseIdentity,
    mission_epoch: int,
    environment_epoch: int,
    clock_generation: int,
    clock_state: str,
    now_ms: int,
    current_access: ReadItem,
    current_policy: ReadItem,
) -> None:
    """Cheap final-lock checks, in addition to source writer barriers/root guard.

    This does not sign certificates, run closure, authenticate callers, or prove
    continuous MAINTAIN coverage. Those are required at the original use writer.
    """
    if certificate.decision != "USABLE":
        raise AssuranceError("CERTIFICATE_NOT_USABLE")
    if (
        UseIdentity(
            certificate.mission_id,
            certificate.consumer_kind,
            certificate.consumer_id,
            certificate.scope_id,
            certificate.principal_id,
            certificate.purpose,
            certificate.root_incarnation_id,
        )
        != identity
    ):
        raise AssuranceError("CERTIFICATE_USE_IDENTITY")
    integer(now_ms)
    for value in (mission_epoch, environment_epoch, clock_generation):
        integer(value)
    one_of(clock_state, {"STABLE", "ROLLBACK"})
    if clock_state != "STABLE" or clock_generation != certificate.clock_generation:
        raise AssuranceError("CLOCK_RECHECK_REQUIRED")
    if (
        mission_epoch != certificate.mission_epoch
        or environment_epoch != certificate.environment_epoch
    ):
        raise AssuranceError("RECHECK_REQUIRED")
    if now_ms < certificate.issued_at_ms or (
        certificate.not_after_ms is not None and now_ms >= certificate.not_after_ms
    ):
        raise AssuranceError("CERTIFICATE_EXPIRED")
    if current_access.channel != "ACCESS" or current_policy.channel != "POLICY":
        raise AssuranceError("ACCESS_POLICY_WITNESS_REQUIRED")
    if current_access not in certificate.read_set or current_policy not in certificate.read_set:
        raise AssuranceError("RECHECK_REQUIRED")
