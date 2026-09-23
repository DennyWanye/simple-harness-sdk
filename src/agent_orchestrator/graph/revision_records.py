# SPDX-License-Identifier: Apache-2.0
"""Strict immutable contracts for TaskGraph revision history records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, TypeAlias

from ..contracts.models import ContractError
from .execution_contracts import PreviewBindingV1
from .network_codec import NetworkDocumentV1
from .revision_pins import RevisionPins

_HASH = frozenset("0123456789abcdef")


def _bad(message: str) -> NoReturn:
    raise ContractError(f"TASKGRAPH_REVISION_INVALID: {message}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _bad(f"{field} must be a nonempty identifier of at most 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _bad(f"{field} must be UTF-8")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _HASH:
        _bad(f"{field} must be a lowercase SHA-256")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        _bad(f"{field} must be a nonnegative safe integer")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRef:
    channel: str
    identity: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        _text(self.channel, "source_ref.channel")
        _text(self.identity, "source_ref.identity")
        _integer(self.revision, "source_ref.revision")
        _hash(self.digest, "source_ref.digest")

    def to_json(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "identity": self.identity,
            "revision": self.revision,
            "digest": self.digest,
        }

    @classmethod
    def from_json(cls, value: object) -> SourceRef:
        if not isinstance(value, Mapping) or set(value) != {
            "channel", "identity", "revision", "digest"
        }:
            _bad("SourceRef has missing or unknown fields")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanAdmissionCertificate:
    preview: PreviewBindingV1
    commit_receipt_ref: SourceRef
    admission_check_ref: SourceRef
    version: int = 1
    kind: str = "PLAN_ADMISSION"

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1 or self.kind != "PLAN_ADMISSION":
            _bad("PLAN_ADMISSION certificate discriminator is invalid")
        if not isinstance(self.preview, PreviewBindingV1):
            _bad("preview must be PreviewBindingV1")

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "kind": self.kind, "preview": self.preview.to_json(),
                "commit_receipt_ref": self.commit_receipt_ref.to_json(),
                "admission_check_ref": self.admission_check_ref.to_json()}


@dataclass(frozen=True, slots=True, kw_only=True)
class CapturedBaselineCertificate:
    captured_through_seq: int
    baseline_command_ref: SourceRef
    quiescence_ref: SourceRef
    policy_ref: SourceRef
    codec_manifest_hash: str
    version: int = 1
    kind: str = "CAPTURED_BASELINE"

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1 or self.kind != "CAPTURED_BASELINE":
            _bad("CAPTURED_BASELINE certificate discriminator is invalid")
        _integer(self.captured_through_seq, "captured_through_seq")
        _hash(self.codec_manifest_hash, "codec_manifest_hash")

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "kind": self.kind,
                "captured_through_seq": self.captured_through_seq,
                "baseline_command_ref": self.baseline_command_ref.to_json(),
                "quiescence_ref": self.quiescence_ref.to_json(),
                "policy_ref": self.policy_ref.to_json(),
                "codec_manifest_hash": self.codec_manifest_hash}


RevisionCertificate: TypeAlias = PlanAdmissionCertificate | CapturedBaselineCertificate


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineProofContext:
    """Exact capture identity a historical quiescence receipt must bind."""
    mission_id: str
    revision: int
    command_id: str
    enabling_command_id: str
    manifest_hash: str
    captured_through_seq: int


def certificate_from_json(value: object) -> RevisionCertificate:
    if not isinstance(value, Mapping):
        _bad("certificate must be an object")
    kind = value.get("kind")
    if kind == "PLAN_ADMISSION":
        expected = {"version", "kind", "preview", "commit_receipt_ref", "admission_check_ref"}
        if set(value) != expected:
            _bad("PLAN_ADMISSION certificate has missing or unknown fields")
        return PlanAdmissionCertificate(version=value["version"], kind=value["kind"],
            preview=PreviewBindingV1.from_json(value["preview"]),
            commit_receipt_ref=SourceRef.from_json(value["commit_receipt_ref"]),
            admission_check_ref=SourceRef.from_json(value["admission_check_ref"]))
    if kind == "CAPTURED_BASELINE":
        expected = {"version", "kind", "captured_through_seq", "baseline_command_ref",
                    "quiescence_ref", "policy_ref", "codec_manifest_hash"}
        if set(value) != expected:
            _bad("CAPTURED_BASELINE certificate has missing or unknown fields")
        return CapturedBaselineCertificate(version=value["version"], kind=value["kind"],
            captured_through_seq=value["captured_through_seq"],
            baseline_command_ref=SourceRef.from_json(value["baseline_command_ref"]),
            quiescence_ref=SourceRef.from_json(value["quiescence_ref"]),
            policy_ref=SourceRef.from_json(value["policy_ref"]),
            codec_manifest_hash=value["codec_manifest_hash"])
    _bad("certificate kind is unsupported")


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisionRecord:
    document: NetworkDocumentV1
    manifest_hash: str
    sdk_snapshot_hash: str
    source_kind: str
    parent_revision: int | None
    parent_manifest_hash: str | None
    certificate: RevisionCertificate
    admission_check_id: str | None
    event_id: str
    command_id: str
    created_at: float
    pins: RevisionPins


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalRevision:
    record: RevisionRecord
    executable: bool = False
    reason: str = "HISTORICAL_STRUCTURE_NON_EXECUTABLE"


__all__ = ["CapturedBaselineCertificate", "HistoricalRevision", "PlanAdmissionCertificate",
           "RevisionCertificate", "RevisionRecord", "SourceRef", "certificate_from_json"]
