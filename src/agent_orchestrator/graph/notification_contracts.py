# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""TaskGraph notification wire values; none of these values grants authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, cast

from simple_harness.contracts.json import JsonValue

from ..contracts.models import ContractError


def _invalid(reason: str) -> NoReturn:
    raise ContractError(f"TASKGRAPH_NOTIFICATION_INVALID: {reason}")


def _text(value: object, name: str, *, maximum: int = 512, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        _invalid(f"{name} has an invalid string value")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{name} must be UTF-8")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        _invalid(f"{name} must be a safe integer")
    return value


def _hash(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        _invalid(f"{name} must be a lowercase SHA-256")
    return value


def _fields(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _invalid(f"{name} has missing or unknown fields")
    return value


class FollowupKind(StrEnum):
    REEVALUATE = "REEVALUATE"
    CONVERGE = "CONVERGE"
    REQUEST_COMPOSITION = "REQUEST_COMPOSITION"


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupCauseRef:
    kind: str
    id: str
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        _text(self.kind, "cause.kind")
        _text(self.id, "cause.id")
        _integer(self.revision, "cause.revision")
        _hash(self.content_hash, "cause.content_hash")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "id": self.id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json(cls, value: object) -> FollowupCauseRef:
        row = _fields(value, {"kind", "id", "revision", "content_hash"}, "cause_ref")
        return cls(
            kind=cast(str, row["kind"]),
            id=cast(str, row["id"]),
            revision=cast(int, row["revision"]),
            content_hash=cast(str, row["content_hash"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupV1:
    mission_id: str
    source_event_id: str
    kind: FollowupKind
    subject_key: str
    source_revision: int
    cause_ref: FollowupCauseRef
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("mission_id", "source_event_id", "subject_key"):
            _text(getattr(self, name), name)
        _integer(self.source_revision, "source_revision")
        if type(self.schema_version) is not int or self.schema_version != 1:
            _invalid("schema_version must be 1")
        try:
            object.__setattr__(self, "kind", FollowupKind(self.kind))
        except (ValueError, TypeError):
            _invalid("unsupported followup kind")
        if not isinstance(self.cause_ref, FollowupCauseRef):
            _invalid("cause_ref must be a FollowupCauseRef")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "source_event_id": self.source_event_id,
            "kind": str(self.kind),
            "subject_key": self.subject_key,
            "source_revision": self.source_revision,
            "cause_ref": self.cause_ref.to_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> FollowupV1:
        row = _fields(
            value,
            {
                "schema_version",
                "mission_id",
                "source_event_id",
                "kind",
                "subject_key",
                "source_revision",
                "cause_ref",
            },
            "followup",
        )
        return cls(
            schema_version=cast(int, row["schema_version"]),
            mission_id=cast(str, row["mission_id"]),
            source_event_id=cast(str, row["source_event_id"]),
            kind=cast(FollowupKind, row["kind"]),
            subject_key=cast(str, row["subject_key"]),
            source_revision=cast(int, row["source_revision"]),
            cause_ref=FollowupCauseRef.from_json(row["cause_ref"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphErrorV1:
    origin: str
    stage: str
    code: str
    detail: str
    retry_kind: str
    source_identity: str | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _invalid("schema_version must be 1")
        if self.origin not in ("MODEL", "SYSTEM", "RUNTIME"):
            _invalid("unsupported error origin")
        if self.stage not in (
            "SOURCE",
            "PREVIEW",
            "ADMISSION",
            "CONVERGENCE",
            "COMMIT",
            "DISPATCH",
            "REPLAY",
            "READ",
        ):
            _invalid("unsupported error stage")
        if self.retry_kind not in (
            "NONE",
            "REQUERY",
            "RECONCILE",
            "NEW_PLANNER_REQUEST",
            "OPERATOR_REPAIR",
        ):
            _invalid("unsupported retry kind")
        _text(self.code, "code")
        _text(self.detail, "detail", maximum=2000, empty=True)
        if self.source_identity is not None:
            _text(self.source_identity, "source_identity")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "origin": self.origin,
            "stage": self.stage,
            "code": self.code,
            "detail": self.detail,
            "retry_kind": self.retry_kind,
            "source_identity": self.source_identity,
        }

    @classmethod
    def from_json(cls, value: object) -> TaskGraphErrorV1:
        row = _fields(
            value,
            {
                "schema_version",
                "origin",
                "stage",
                "code",
                "detail",
                "retry_kind",
                "source_identity",
            },
            "error",
        )
        return cls(
            schema_version=cast(int, row["schema_version"]),
            origin=cast(str, row["origin"]),
            stage=cast(str, row["stage"]),
            code=cast(str, row["code"]),
            detail=cast(str, row["detail"]),
            retry_kind=cast(str, row["retry_kind"]),
            source_identity=cast(str | None, row["source_identity"]),
        )


__all__ = ("FollowupKind", "FollowupCauseRef", "FollowupV1", "TaskGraphErrorV1")
