# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strict Host-authority contracts for append-only workspace bindings.

Public DTOs are durable records, never capabilities by construction. A Host
must verify exact durable records through :class:`WorkspaceBindingAuthorityPort`
and return a grant before a binding-set transaction may commit.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from simple_harness.contracts import JsonValue

from .disclosure_protocol import (
    _canonical_hash,
    _digest,
    _exact_keys,
    _identifier,
    _positive_int,
    _schema_version,
)

WORKSPACE_BINDING_SCHEMA_VERSION = 1
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _domain_hash(domain: str, payload: dict[str, JsonValue]) -> str:
    return _canonical_hash({"protocol": domain, "payload": payload})


def _canonical_path(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be an exact non-empty path")
    if len(value.encode("utf-8")) > 32_768:
        raise ValueError(f"{name} exceeds its byte limit")
    if not (value.startswith("/") or value.startswith("\\\\") or _WINDOWS_ABSOLUTE.match(value)):
        raise ValueError(f"{name} must be absolute")
    if any(part in {".", ".."} for part in value.replace("\\", "/").split("/")):
        raise ValueError(f"{name} must not contain dot segments")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


class FilesystemIdentityKind(StrEnum):
    POSIX_INODE = "posix_inode"
    WINDOWS_FILE_ID = "windows_file_id"


class WorkspaceBindingMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class WorkspaceBindingAuthorizationChannel(StrEnum):
    USER_CONFIRMATION = "user_confirmation"
    PROJECT_PICKER = "project_picker"


class WorkspaceBindingAuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class WorkspaceBindingGrantSource(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class FilesystemIdentity:
    kind: FilesystemIdentityKind
    volume_id: str
    object_id: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    identity_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported FilesystemIdentity schema_version")
        object.__setattr__(self, "kind", FilesystemIdentityKind(self.kind))
        _identifier(self.volume_id, "volume_id", max_length=512)
        _identifier(self.object_id, "object_id", max_length=512)
        object.__setattr__(
            self,
            "identity_hash",
            _domain_hash("workspace-binding/filesystem-identity/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "volume_id": self.volume_id,
            "object_id": self.object_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> FilesystemIdentity:
        _exact_keys(
            value,
            {"schema_version", "kind", "volume_id", "object_id"},
            "FilesystemIdentity",
        )
        return cls(
            kind=FilesystemIdentityKind(value["kind"]),  # type: ignore[arg-type]
            volume_id=_identifier(value["volume_id"], "volume_id", max_length=512),
            object_id=_identifier(value["object_id"], "object_id", max_length=512),
            schema_version=_schema_version(value["schema_version"], "FilesystemIdentity"),
        )


@dataclass(frozen=True, slots=True)
class CanonicalWorkspaceRoot:
    root_id: str
    canonical_path: str
    filesystem_identity: FilesystemIdentity
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    path_hash: str = field(init=False)
    root_identity_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported CanonicalWorkspaceRoot schema_version")
        _identifier(self.root_id, "root_id")
        _canonical_path(self.canonical_path, "canonical_path")
        if not isinstance(self.filesystem_identity, FilesystemIdentity):
            raise TypeError("filesystem_identity must use FilesystemIdentity")
        path_hash = _domain_hash(
            "workspace-binding/canonical-path/v1", {"canonical_path": self.canonical_path}
        )
        object.__setattr__(self, "path_hash", path_hash)
        object.__setattr__(
            self,
            "root_identity_hash",
            _domain_hash("workspace-binding/canonical-root/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "root_id": self.root_id,
            "canonical_path": self.canonical_path,
            "path_hash": self.path_hash,
            "filesystem_identity": self.filesystem_identity.to_json(),
            "filesystem_identity_hash": self.filesystem_identity.identity_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> CanonicalWorkspaceRoot:
        _exact_keys(
            value,
            {
                "schema_version",
                "root_id",
                "canonical_path",
                "path_hash",
                "filesystem_identity",
                "filesystem_identity_hash",
            },
            "CanonicalWorkspaceRoot",
        )
        raw_identity = value["filesystem_identity"]
        if not isinstance(raw_identity, Mapping):
            raise TypeError("filesystem_identity must be an object")
        result = cls(
            root_id=_identifier(value["root_id"], "root_id"),
            canonical_path=_canonical_path(value["canonical_path"], "canonical_path"),
            filesystem_identity=FilesystemIdentity.from_json(raw_identity),
            schema_version=_schema_version(value["schema_version"], "CanonicalWorkspaceRoot"),
        )
        if value["path_hash"] != result.path_hash:
            raise ValueError("canonical workspace path hash differs")
        if value["filesystem_identity_hash"] != result.filesystem_identity.identity_hash:
            raise ValueError("filesystem identity hash differs")
        return result


@dataclass(frozen=True, slots=True)
class WorkspaceBindingProposal:
    """A non-authoritative append intent. It deliberately has no mode field."""

    proposal_id: str
    run_id: str
    subject: str
    task_scope_id: str
    root: CanonicalWorkspaceRoot
    base_binding_set_revision: int
    idempotency_key: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    proposal_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported WorkspaceBindingProposal schema_version")
        for item, name in (
            (self.proposal_id, "proposal_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identifier(item, name)
        if not isinstance(self.root, CanonicalWorkspaceRoot):
            raise TypeError("root must use CanonicalWorkspaceRoot")
        _non_negative_int(self.base_binding_set_revision, "base_binding_set_revision")
        object.__setattr__(
            self,
            "proposal_hash",
            _domain_hash("workspace-binding/append-proposal/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "root": self.root.to_json(),
            "base_binding_set_revision": self.base_binding_set_revision,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> WorkspaceBindingProposal:
        _exact_keys(
            value,
            {
                "schema_version",
                "proposal_id",
                "run_id",
                "subject",
                "task_scope_id",
                "root",
                "base_binding_set_revision",
                "idempotency_key",
            },
            "WorkspaceBindingProposal",
        )
        raw_root = value["root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("root must be an object")
        return cls(
            proposal_id=_identifier(value["proposal_id"], "proposal_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            root=CanonicalWorkspaceRoot.from_json(raw_root),
            base_binding_set_revision=_non_negative_int(
                value["base_binding_set_revision"], "base_binding_set_revision"
            ),
            idempotency_key=_identifier(value["idempotency_key"], "idempotency_key"),
            schema_version=_schema_version(value["schema_version"], "WorkspaceBindingProposal"),
        )


def _verify_proposal_fields(record: object, proposal: WorkspaceBindingProposal, name: str) -> None:
    if not isinstance(proposal, WorkspaceBindingProposal):
        raise TypeError("proposal must use WorkspaceBindingProposal")
    exact = (
        (getattr(record, "proposal_id"), proposal.proposal_id),
        (getattr(record, "proposal_hash"), proposal.proposal_hash),
        (getattr(record, "run_id"), proposal.run_id),
        (getattr(record, "subject"), proposal.subject),
        (getattr(record, "task_scope_id"), proposal.task_scope_id),
        (getattr(record, "root"), proposal.root),
        (getattr(record, "base_binding_set_revision"), proposal.base_binding_set_revision),
    )
    if any(left != right for left, right in exact):
        raise ValueError(f"{name} differs from proposal")


@dataclass(frozen=True, slots=True)
class ManualWorkspaceBindingChallenge:
    challenge_id: str
    proposal_id: str
    proposal_hash: str
    run_id: str
    subject: str
    task_scope_id: str
    root: CanonicalWorkspaceRoot
    base_binding_set_revision: int
    authorization_nonce: str
    authorization_channel: WorkspaceBindingAuthorizationChannel
    authorization_evidence_id: str
    authorization_evidence_hash: str
    interaction_event_id: str
    issued_at_millis: int
    not_before_millis: int
    expires_at_millis: int
    host_challenge_ref: str
    host_challenge_hash: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    sdk_challenge_hash: str = field(init=False)
    challenge_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported ManualWorkspaceBindingChallenge schema_version")
        for item, name in (
            (self.challenge_id, "challenge_id"),
            (self.proposal_id, "proposal_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.authorization_nonce, "authorization_nonce"),
            (self.authorization_evidence_id, "authorization_evidence_id"),
            (self.interaction_event_id, "interaction_event_id"),
            (self.host_challenge_ref, "host_challenge_ref"),
        ):
            _identifier(item, name)
        _digest(self.proposal_hash, "proposal_hash")
        if not isinstance(self.root, CanonicalWorkspaceRoot):
            raise TypeError("root must use CanonicalWorkspaceRoot")
        _non_negative_int(self.base_binding_set_revision, "base_binding_set_revision")
        object.__setattr__(
            self,
            "authorization_channel",
            WorkspaceBindingAuthorizationChannel(self.authorization_channel),
        )
        _digest(self.authorization_evidence_hash, "authorization_evidence_hash")
        _digest(self.host_challenge_hash, "host_challenge_hash")
        _non_negative_int(self.issued_at_millis, "issued_at_millis")
        _non_negative_int(self.not_before_millis, "not_before_millis")
        _non_negative_int(self.expires_at_millis, "expires_at_millis")
        if not self.issued_at_millis <= self.not_before_millis < self.expires_at_millis:
            raise ValueError("Manual challenge validity interval is invalid")
        sdk_hash = _domain_hash("workspace-binding/manual-challenge/v1", self.sdk_challenge_json())
        object.__setattr__(self, "sdk_challenge_hash", sdk_hash)
        object.__setattr__(
            self,
            "challenge_hash",
            _domain_hash("workspace-binding/manual-challenge-record/v1", self.to_json()),
        )

    def sdk_challenge_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "challenge_id": self.challenge_id,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "root": self.root.to_json(),
            "base_binding_set_revision": self.base_binding_set_revision,
            "authorization_nonce": self.authorization_nonce,
            "authorization_channel": self.authorization_channel.value,
            "authorization_evidence_id": self.authorization_evidence_id,
            "authorization_evidence_hash": self.authorization_evidence_hash,
            "interaction_event_id": self.interaction_event_id,
            "issued_at_millis": self.issued_at_millis,
            "not_before_millis": self.not_before_millis,
            "expires_at_millis": self.expires_at_millis,
        }

    def to_json(self) -> dict[str, JsonValue]:
        return {
            **self.sdk_challenge_json(),
            "sdk_challenge_hash": self.sdk_challenge_hash,
            "host_challenge_ref": self.host_challenge_ref,
            "host_challenge_hash": self.host_challenge_hash,
        }

    def verify_proposal(self, proposal: WorkspaceBindingProposal) -> None:
        _verify_proposal_fields(self, proposal, "Manual challenge")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ManualWorkspaceBindingChallenge:
        expected = {
            "schema_version",
            "challenge_id",
            "proposal_id",
            "proposal_hash",
            "run_id",
            "subject",
            "task_scope_id",
            "root",
            "base_binding_set_revision",
            "authorization_nonce",
            "authorization_channel",
            "authorization_evidence_id",
            "authorization_evidence_hash",
            "interaction_event_id",
            "issued_at_millis",
            "not_before_millis",
            "expires_at_millis",
            "sdk_challenge_hash",
            "host_challenge_ref",
            "host_challenge_hash",
        }
        _exact_keys(value, expected, "ManualWorkspaceBindingChallenge")
        raw_root = value["root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("root must be an object")
        result = cls(
            challenge_id=_identifier(value["challenge_id"], "challenge_id"),
            proposal_id=_identifier(value["proposal_id"], "proposal_id"),
            proposal_hash=_digest(value["proposal_hash"], "proposal_hash"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            root=CanonicalWorkspaceRoot.from_json(raw_root),
            base_binding_set_revision=_non_negative_int(
                value["base_binding_set_revision"], "base_binding_set_revision"
            ),
            authorization_nonce=_identifier(value["authorization_nonce"], "authorization_nonce"),
            authorization_channel=WorkspaceBindingAuthorizationChannel(
                value["authorization_channel"]  # type: ignore[arg-type]
            ),
            authorization_evidence_id=_identifier(
                value["authorization_evidence_id"], "authorization_evidence_id"
            ),
            authorization_evidence_hash=_digest(
                value["authorization_evidence_hash"], "authorization_evidence_hash"
            ),
            interaction_event_id=_identifier(value["interaction_event_id"], "interaction_event_id"),
            issued_at_millis=_non_negative_int(value["issued_at_millis"], "issued_at_millis"),
            not_before_millis=_non_negative_int(value["not_before_millis"], "not_before_millis"),
            expires_at_millis=_non_negative_int(value["expires_at_millis"], "expires_at_millis"),
            host_challenge_ref=_identifier(value["host_challenge_ref"], "host_challenge_ref"),
            host_challenge_hash=_digest(value["host_challenge_hash"], "host_challenge_hash"),
            schema_version=_schema_version(
                value["schema_version"], "ManualWorkspaceBindingChallenge"
            ),
        )
        if value["sdk_challenge_hash"] != result.sdk_challenge_hash:
            raise ValueError("Manual challenge SDK hash differs")
        return result


@dataclass(frozen=True, slots=True)
class ManualWorkspaceBindingAuthorizationReceipt:
    receipt_id: str
    challenge_id: str
    sdk_challenge_hash: str
    proposal_id: str
    proposal_hash: str
    run_id: str
    subject: str
    task_scope_id: str
    root: CanonicalWorkspaceRoot
    base_binding_set_revision: int
    authorization_nonce: str
    authorization_channel: WorkspaceBindingAuthorizationChannel
    decided_by_actor_id: str
    authorization_evidence_id: str
    authorization_evidence_hash: str
    interaction_event_id: str
    challenge_issued_at_millis: int
    challenge_not_before_millis: int
    challenge_expires_at_millis: int
    decision: WorkspaceBindingAuthorizationDecision
    decided_at_millis: int
    host_receipt_ref: str
    host_receipt_hash: str
    host_bound_sdk_challenge_hash: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported Manual authorization receipt schema_version")
        for item, name in (
            (self.receipt_id, "receipt_id"),
            (self.challenge_id, "challenge_id"),
            (self.proposal_id, "proposal_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.authorization_nonce, "authorization_nonce"),
            (self.decided_by_actor_id, "decided_by_actor_id"),
            (self.authorization_evidence_id, "authorization_evidence_id"),
            (self.interaction_event_id, "interaction_event_id"),
            (self.host_receipt_ref, "host_receipt_ref"),
        ):
            _identifier(item, name)
        for item, name in (
            (self.sdk_challenge_hash, "sdk_challenge_hash"),
            (self.proposal_hash, "proposal_hash"),
            (self.authorization_evidence_hash, "authorization_evidence_hash"),
            (self.host_receipt_hash, "host_receipt_hash"),
            (self.host_bound_sdk_challenge_hash, "host_bound_sdk_challenge_hash"),
        ):
            _digest(item, name)
        if self.sdk_challenge_hash != self.host_bound_sdk_challenge_hash:
            raise ValueError("Host receipt is not bound to the SDK challenge")
        if not isinstance(self.root, CanonicalWorkspaceRoot):
            raise TypeError("root must use CanonicalWorkspaceRoot")
        _non_negative_int(self.base_binding_set_revision, "base_binding_set_revision")
        object.__setattr__(
            self,
            "authorization_channel",
            WorkspaceBindingAuthorizationChannel(self.authorization_channel),
        )
        object.__setattr__(self, "decision", WorkspaceBindingAuthorizationDecision(self.decision))
        for value, name in (
            (self.challenge_issued_at_millis, "challenge_issued_at_millis"),
            (self.challenge_not_before_millis, "challenge_not_before_millis"),
            (self.challenge_expires_at_millis, "challenge_expires_at_millis"),
            (self.decided_at_millis, "decided_at_millis"),
        ):
            _non_negative_int(value, name)
        if not (
            self.challenge_issued_at_millis
            <= self.challenge_not_before_millis
            < self.challenge_expires_at_millis
        ):
            raise ValueError("Manual receipt challenge validity interval is invalid")
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash("workspace-binding/manual-decision/v1", self.to_json()),
        )

    @property
    def authorized(self) -> bool:
        return self.decision is WorkspaceBindingAuthorizationDecision.ALLOW

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "challenge_id": self.challenge_id,
            "sdk_challenge_hash": self.sdk_challenge_hash,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "root": self.root.to_json(),
            "base_binding_set_revision": self.base_binding_set_revision,
            "authorization_nonce": self.authorization_nonce,
            "authorization_channel": self.authorization_channel.value,
            "decided_by_actor_id": self.decided_by_actor_id,
            "authorization_evidence_id": self.authorization_evidence_id,
            "authorization_evidence_hash": self.authorization_evidence_hash,
            "interaction_event_id": self.interaction_event_id,
            "challenge_issued_at_millis": self.challenge_issued_at_millis,
            "challenge_not_before_millis": self.challenge_not_before_millis,
            "challenge_expires_at_millis": self.challenge_expires_at_millis,
            "decision": self.decision.value,
            "decided_at_millis": self.decided_at_millis,
            "host_receipt_ref": self.host_receipt_ref,
            "host_receipt_hash": self.host_receipt_hash,
            "host_bound_sdk_challenge_hash": self.host_bound_sdk_challenge_hash,
        }

    def verify_challenge(self, challenge: ManualWorkspaceBindingChallenge) -> None:
        if not isinstance(challenge, ManualWorkspaceBindingChallenge):
            raise TypeError("challenge must use ManualWorkspaceBindingChallenge")
        exact = (
            (self.challenge_id, challenge.challenge_id),
            (self.sdk_challenge_hash, challenge.sdk_challenge_hash),
            (self.proposal_id, challenge.proposal_id),
            (self.proposal_hash, challenge.proposal_hash),
            (self.run_id, challenge.run_id),
            (self.subject, challenge.subject),
            (self.task_scope_id, challenge.task_scope_id),
            (self.root, challenge.root),
            (self.base_binding_set_revision, challenge.base_binding_set_revision),
            (self.authorization_nonce, challenge.authorization_nonce),
            (self.authorization_channel, challenge.authorization_channel),
            (self.authorization_evidence_id, challenge.authorization_evidence_id),
            (self.authorization_evidence_hash, challenge.authorization_evidence_hash),
            (self.interaction_event_id, challenge.interaction_event_id),
            (self.challenge_issued_at_millis, challenge.issued_at_millis),
            (self.challenge_not_before_millis, challenge.not_before_millis),
            (self.challenge_expires_at_millis, challenge.expires_at_millis),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Manual receipt differs from challenge")
        if not challenge.not_before_millis <= self.decided_at_millis < challenge.expires_at_millis:
            raise ValueError("Manual decision is outside the challenge validity interval")
        if not self.authorized:
            raise ValueError("Manual decision does not authorize binding")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ManualWorkspaceBindingAuthorizationReceipt:
        expected = {
            "schema_version",
            "receipt_id",
            "challenge_id",
            "sdk_challenge_hash",
            "proposal_id",
            "proposal_hash",
            "run_id",
            "subject",
            "task_scope_id",
            "root",
            "base_binding_set_revision",
            "authorization_nonce",
            "authorization_channel",
            "decided_by_actor_id",
            "authorization_evidence_id",
            "authorization_evidence_hash",
            "interaction_event_id",
            "challenge_issued_at_millis",
            "challenge_not_before_millis",
            "challenge_expires_at_millis",
            "decision",
            "decided_at_millis",
            "host_receipt_ref",
            "host_receipt_hash",
            "host_bound_sdk_challenge_hash",
        }
        _exact_keys(value, expected, "ManualWorkspaceBindingAuthorizationReceipt")
        raw_root = value["root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("root must be an object")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            challenge_id=_identifier(value["challenge_id"], "challenge_id"),
            sdk_challenge_hash=_digest(value["sdk_challenge_hash"], "sdk_challenge_hash"),
            proposal_id=_identifier(value["proposal_id"], "proposal_id"),
            proposal_hash=_digest(value["proposal_hash"], "proposal_hash"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            root=CanonicalWorkspaceRoot.from_json(raw_root),
            base_binding_set_revision=_non_negative_int(
                value["base_binding_set_revision"], "base_binding_set_revision"
            ),
            authorization_nonce=_identifier(value["authorization_nonce"], "authorization_nonce"),
            authorization_channel=WorkspaceBindingAuthorizationChannel(
                value["authorization_channel"]  # type: ignore[arg-type]
            ),
            decided_by_actor_id=_identifier(value["decided_by_actor_id"], "decided_by_actor_id"),
            authorization_evidence_id=_identifier(
                value["authorization_evidence_id"], "authorization_evidence_id"
            ),
            authorization_evidence_hash=_digest(
                value["authorization_evidence_hash"], "authorization_evidence_hash"
            ),
            interaction_event_id=_identifier(value["interaction_event_id"], "interaction_event_id"),
            challenge_issued_at_millis=_non_negative_int(
                value["challenge_issued_at_millis"], "challenge_issued_at_millis"
            ),
            challenge_not_before_millis=_non_negative_int(
                value["challenge_not_before_millis"], "challenge_not_before_millis"
            ),
            challenge_expires_at_millis=_non_negative_int(
                value["challenge_expires_at_millis"], "challenge_expires_at_millis"
            ),
            decision=WorkspaceBindingAuthorizationDecision(value["decision"]),  # type: ignore[arg-type]
            decided_at_millis=_non_negative_int(value["decided_at_millis"], "decided_at_millis"),
            host_receipt_ref=_identifier(value["host_receipt_ref"], "host_receipt_ref"),
            host_receipt_hash=_digest(value["host_receipt_hash"], "host_receipt_hash"),
            host_bound_sdk_challenge_hash=_digest(
                value["host_bound_sdk_challenge_hash"], "host_bound_sdk_challenge_hash"
            ),
            schema_version=_schema_version(
                value["schema_version"], "ManualWorkspaceBindingAuthorizationReceipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class RunBindingModeSnapshotRequest:
    request_id: str
    run_id: str
    subject: str
    run_revision: int
    task_scope_id: str
    binding_set_revision: int
    context_snapshot_id: str
    context_snapshot_revision: int
    context_snapshot_hash: str
    configured_workspace_root: CanonicalWorkspaceRoot
    configuration_revision: int
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported RunBindingModeSnapshotRequest schema_version")
        for item, name in (
            (self.request_id, "request_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.context_snapshot_id, "context_snapshot_id"),
        ):
            _identifier(item, name)
        _positive_int(self.run_revision, "run_revision")
        _non_negative_int(self.binding_set_revision, "binding_set_revision")
        _positive_int(self.context_snapshot_revision, "context_snapshot_revision")
        _positive_int(self.configuration_revision, "configuration_revision")
        _digest(self.context_snapshot_hash, "context_snapshot_hash")
        if not isinstance(self.configured_workspace_root, CanonicalWorkspaceRoot):
            raise TypeError("configured_workspace_root must use CanonicalWorkspaceRoot")
        object.__setattr__(
            self,
            "request_hash",
            _domain_hash("workspace-binding/mode-request/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "run_revision": self.run_revision,
            "task_scope_id": self.task_scope_id,
            "binding_set_revision": self.binding_set_revision,
            "context_snapshot_id": self.context_snapshot_id,
            "context_snapshot_revision": self.context_snapshot_revision,
            "context_snapshot_hash": self.context_snapshot_hash,
            "configured_workspace_root": self.configured_workspace_root.to_json(),
            "configuration_revision": self.configuration_revision,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunBindingModeSnapshotRequest:
        expected = {
            "schema_version",
            "request_id",
            "run_id",
            "subject",
            "run_revision",
            "task_scope_id",
            "binding_set_revision",
            "context_snapshot_id",
            "context_snapshot_revision",
            "context_snapshot_hash",
            "configured_workspace_root",
            "configuration_revision",
        }
        _exact_keys(value, expected, "RunBindingModeSnapshotRequest")
        raw_root = value["configured_workspace_root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("configured_workspace_root must be an object")
        return cls(
            request_id=_identifier(value["request_id"], "request_id"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            run_revision=_positive_int(value["run_revision"], "run_revision"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            binding_set_revision=_non_negative_int(
                value["binding_set_revision"], "binding_set_revision"
            ),
            context_snapshot_id=_identifier(value["context_snapshot_id"], "context_snapshot_id"),
            context_snapshot_revision=_positive_int(
                value["context_snapshot_revision"], "context_snapshot_revision"
            ),
            context_snapshot_hash=_digest(value["context_snapshot_hash"], "context_snapshot_hash"),
            configured_workspace_root=CanonicalWorkspaceRoot.from_json(raw_root),
            configuration_revision=_positive_int(
                value["configuration_revision"], "configuration_revision"
            ),
            schema_version=_schema_version(
                value["schema_version"], "RunBindingModeSnapshotRequest"
            ),
        )


@dataclass(frozen=True, slots=True)
class HostIssuedRunBindingModeSnapshot:
    snapshot_id: str
    request_id: str
    request_hash: str
    run_id: str
    subject: str
    run_revision: int
    task_scope_id: str
    binding_set_revision: int
    context_snapshot_id: str
    context_snapshot_revision: int
    context_snapshot_hash: str
    configured_workspace_root: CanonicalWorkspaceRoot
    configuration_revision: int
    mode: WorkspaceBindingMode
    issued_at_millis: int
    expires_at_millis: int
    authority_receipt_ref: str
    authority_receipt_hash: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported HostIssuedRunBindingModeSnapshot schema_version")
        for item, name in (
            (self.snapshot_id, "snapshot_id"),
            (self.request_id, "request_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.context_snapshot_id, "context_snapshot_id"),
            (self.authority_receipt_ref, "authority_receipt_ref"),
        ):
            _identifier(item, name)
        _digest(self.request_hash, "request_hash")
        _digest(self.context_snapshot_hash, "context_snapshot_hash")
        _digest(self.authority_receipt_hash, "authority_receipt_hash")
        _positive_int(self.run_revision, "run_revision")
        _non_negative_int(self.binding_set_revision, "binding_set_revision")
        _positive_int(self.context_snapshot_revision, "context_snapshot_revision")
        _positive_int(self.configuration_revision, "configuration_revision")
        if not isinstance(self.configured_workspace_root, CanonicalWorkspaceRoot):
            raise TypeError("configured_workspace_root must use CanonicalWorkspaceRoot")
        object.__setattr__(self, "mode", WorkspaceBindingMode(self.mode))
        _non_negative_int(self.issued_at_millis, "issued_at_millis")
        _non_negative_int(self.expires_at_millis, "expires_at_millis")
        if self.issued_at_millis >= self.expires_at_millis:
            raise ValueError("Run binding-mode snapshot validity interval is invalid")
        object.__setattr__(
            self,
            "snapshot_hash",
            _domain_hash("workspace-binding/host-mode-snapshot/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "run_revision": self.run_revision,
            "task_scope_id": self.task_scope_id,
            "binding_set_revision": self.binding_set_revision,
            "context_snapshot_id": self.context_snapshot_id,
            "context_snapshot_revision": self.context_snapshot_revision,
            "context_snapshot_hash": self.context_snapshot_hash,
            "configured_workspace_root": self.configured_workspace_root.to_json(),
            "configuration_revision": self.configuration_revision,
            "mode": self.mode.value,
            "issued_at_millis": self.issued_at_millis,
            "expires_at_millis": self.expires_at_millis,
            "authority_receipt_ref": self.authority_receipt_ref,
            "authority_receipt_hash": self.authority_receipt_hash,
        }

    def verify_request(self, request: RunBindingModeSnapshotRequest, *, now_millis: int) -> None:
        if not isinstance(request, RunBindingModeSnapshotRequest):
            raise TypeError("request must use RunBindingModeSnapshotRequest")
        _non_negative_int(now_millis, "now_millis")
        exact = (
            (self.request_id, request.request_id),
            (self.request_hash, request.request_hash),
            (self.run_id, request.run_id),
            (self.subject, request.subject),
            (self.run_revision, request.run_revision),
            (self.task_scope_id, request.task_scope_id),
            (self.binding_set_revision, request.binding_set_revision),
            (self.context_snapshot_id, request.context_snapshot_id),
            (self.context_snapshot_revision, request.context_snapshot_revision),
            (self.context_snapshot_hash, request.context_snapshot_hash),
            (self.configured_workspace_root, request.configured_workspace_root),
            (self.configuration_revision, request.configuration_revision),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Run binding-mode snapshot differs from request")
        if self.mode is not WorkspaceBindingMode.AUTO:
            raise ValueError("Run binding-mode snapshot does not authorize Auto mode")
        if not self.issued_at_millis <= now_millis < self.expires_at_millis:
            raise ValueError("Run binding-mode snapshot is not currently valid")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> HostIssuedRunBindingModeSnapshot:
        expected = {
            "schema_version",
            "snapshot_id",
            "request_id",
            "request_hash",
            "run_id",
            "subject",
            "run_revision",
            "task_scope_id",
            "binding_set_revision",
            "context_snapshot_id",
            "context_snapshot_revision",
            "context_snapshot_hash",
            "configured_workspace_root",
            "configuration_revision",
            "mode",
            "issued_at_millis",
            "expires_at_millis",
            "authority_receipt_ref",
            "authority_receipt_hash",
        }
        _exact_keys(value, expected, "HostIssuedRunBindingModeSnapshot")
        raw_root = value["configured_workspace_root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("configured_workspace_root must be an object")
        return cls(
            snapshot_id=_identifier(value["snapshot_id"], "snapshot_id"),
            request_id=_identifier(value["request_id"], "request_id"),
            request_hash=_digest(value["request_hash"], "request_hash"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            run_revision=_positive_int(value["run_revision"], "run_revision"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            binding_set_revision=_non_negative_int(
                value["binding_set_revision"], "binding_set_revision"
            ),
            context_snapshot_id=_identifier(value["context_snapshot_id"], "context_snapshot_id"),
            context_snapshot_revision=_positive_int(
                value["context_snapshot_revision"], "context_snapshot_revision"
            ),
            context_snapshot_hash=_digest(value["context_snapshot_hash"], "context_snapshot_hash"),
            configured_workspace_root=CanonicalWorkspaceRoot.from_json(raw_root),
            configuration_revision=_positive_int(
                value["configuration_revision"], "configuration_revision"
            ),
            mode=WorkspaceBindingMode(value["mode"]),  # type: ignore[arg-type]
            issued_at_millis=_non_negative_int(value["issued_at_millis"], "issued_at_millis"),
            expires_at_millis=_non_negative_int(value["expires_at_millis"], "expires_at_millis"),
            authority_receipt_ref=_identifier(
                value["authority_receipt_ref"], "authority_receipt_ref"
            ),
            authority_receipt_hash=_digest(
                value["authority_receipt_hash"], "authority_receipt_hash"
            ),
            schema_version=_schema_version(
                value["schema_version"], "HostIssuedRunBindingModeSnapshot"
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceBindingAuthorityGrant:
    grant_id: str
    source: WorkspaceBindingGrantSource
    proposal_id: str
    proposal_hash: str
    run_id: str
    subject: str
    task_scope_id: str
    root: CanonicalWorkspaceRoot
    base_binding_set_revision: int
    source_authority_ref: str
    source_authority_hash: str
    host_grant_ref: str
    host_grant_hash: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    grant_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported WorkspaceBindingAuthorityGrant schema_version")
        for item, name in (
            (self.grant_id, "grant_id"),
            (self.proposal_id, "proposal_id"),
            (self.run_id, "run_id"),
            (self.subject, "subject"),
            (self.task_scope_id, "task_scope_id"),
            (self.source_authority_ref, "source_authority_ref"),
            (self.host_grant_ref, "host_grant_ref"),
        ):
            _identifier(item, name)
        object.__setattr__(self, "source", WorkspaceBindingGrantSource(self.source))
        _digest(self.proposal_hash, "proposal_hash")
        _digest(self.source_authority_hash, "source_authority_hash")
        _digest(self.host_grant_hash, "host_grant_hash")
        if not isinstance(self.root, CanonicalWorkspaceRoot):
            raise TypeError("root must use CanonicalWorkspaceRoot")
        _non_negative_int(self.base_binding_set_revision, "base_binding_set_revision")
        object.__setattr__(
            self,
            "grant_hash",
            _domain_hash("workspace-binding/authority-grant/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "source": self.source.value,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "run_id": self.run_id,
            "subject": self.subject,
            "task_scope_id": self.task_scope_id,
            "root": self.root.to_json(),
            "base_binding_set_revision": self.base_binding_set_revision,
            "source_authority_ref": self.source_authority_ref,
            "source_authority_hash": self.source_authority_hash,
            "host_grant_ref": self.host_grant_ref,
            "host_grant_hash": self.host_grant_hash,
        }

    def verify_proposal(self, proposal: WorkspaceBindingProposal) -> None:
        _verify_proposal_fields(self, proposal, "Workspace binding authority grant")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> WorkspaceBindingAuthorityGrant:
        expected = {
            "schema_version",
            "grant_id",
            "source",
            "proposal_id",
            "proposal_hash",
            "run_id",
            "subject",
            "task_scope_id",
            "root",
            "base_binding_set_revision",
            "source_authority_ref",
            "source_authority_hash",
            "host_grant_ref",
            "host_grant_hash",
        }
        _exact_keys(value, expected, "WorkspaceBindingAuthorityGrant")
        raw_root = value["root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("root must be an object")
        return cls(
            grant_id=_identifier(value["grant_id"], "grant_id"),
            source=WorkspaceBindingGrantSource(value["source"]),  # type: ignore[arg-type]
            proposal_id=_identifier(value["proposal_id"], "proposal_id"),
            proposal_hash=_digest(value["proposal_hash"], "proposal_hash"),
            run_id=_identifier(value["run_id"], "run_id"),
            subject=_identifier(value["subject"], "subject"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            root=CanonicalWorkspaceRoot.from_json(raw_root),
            base_binding_set_revision=_non_negative_int(
                value["base_binding_set_revision"], "base_binding_set_revision"
            ),
            source_authority_ref=_identifier(value["source_authority_ref"], "source_authority_ref"),
            source_authority_hash=_digest(value["source_authority_hash"], "source_authority_hash"),
            host_grant_ref=_identifier(value["host_grant_ref"], "host_grant_ref"),
            host_grant_hash=_digest(value["host_grant_hash"], "host_grant_hash"),
            schema_version=_schema_version(
                value["schema_version"], "WorkspaceBindingAuthorityGrant"
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceBindingSetReceipt:
    receipt_id: str
    binding_id: str
    task_scope_id: str
    base_binding_set_revision: int
    binding_set_revision: int
    parent_receipt_id: str | None
    parent_receipt_hash: str | None
    previous_root_set_digest: str
    root_set_digest: str
    appended_root: CanonicalWorkspaceRoot
    grant_id: str
    grant_hash: str
    host_receipt_ref: str
    host_receipt_hash: str
    schema_version: int = WORKSPACE_BINDING_SCHEMA_VERSION
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_BINDING_SCHEMA_VERSION:
            raise ValueError("unsupported WorkspaceBindingSetReceipt schema_version")
        for item, name in (
            (self.receipt_id, "receipt_id"),
            (self.binding_id, "binding_id"),
            (self.task_scope_id, "task_scope_id"),
            (self.grant_id, "grant_id"),
            (self.host_receipt_ref, "host_receipt_ref"),
        ):
            _identifier(item, name)
        _digest(self.previous_root_set_digest, "previous_root_set_digest")
        _digest(self.root_set_digest, "root_set_digest")
        _digest(self.grant_hash, "grant_hash")
        _digest(self.host_receipt_hash, "host_receipt_hash")
        _non_negative_int(self.base_binding_set_revision, "base_binding_set_revision")
        _positive_int(self.binding_set_revision, "binding_set_revision")
        if self.binding_set_revision != self.base_binding_set_revision + 1:
            raise ValueError("binding-set receipt must advance base revision exactly once")
        if self.base_binding_set_revision == 0:
            if self.parent_receipt_id is not None or self.parent_receipt_hash is not None:
                raise ValueError("genesis binding-set receipt must not claim a parent")
        else:
            if self.parent_receipt_id is None or self.parent_receipt_hash is None:
                raise ValueError("non-genesis binding-set receipt must identify its parent")
            _identifier(self.parent_receipt_id, "parent_receipt_id")
            _digest(self.parent_receipt_hash, "parent_receipt_hash")
        if self.previous_root_set_digest == self.root_set_digest:
            raise ValueError("binding-set receipt must change the root-set digest")
        if not isinstance(self.appended_root, CanonicalWorkspaceRoot):
            raise TypeError("appended_root must use CanonicalWorkspaceRoot")
        object.__setattr__(
            self,
            "receipt_hash",
            _domain_hash("workspace-binding/binding-set-receipt/v1", self.to_json()),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "binding_id": self.binding_id,
            "task_scope_id": self.task_scope_id,
            "base_binding_set_revision": self.base_binding_set_revision,
            "binding_set_revision": self.binding_set_revision,
            "parent_receipt_id": self.parent_receipt_id,
            "parent_receipt_hash": self.parent_receipt_hash,
            "previous_root_set_digest": self.previous_root_set_digest,
            "root_set_digest": self.root_set_digest,
            "appended_root": self.appended_root.to_json(),
            "grant_id": self.grant_id,
            "grant_hash": self.grant_hash,
            "host_receipt_ref": self.host_receipt_ref,
            "host_receipt_hash": self.host_receipt_hash,
        }

    def verify_grant(self, grant: WorkspaceBindingAuthorityGrant) -> None:
        if not isinstance(grant, WorkspaceBindingAuthorityGrant):
            raise TypeError("grant must use WorkspaceBindingAuthorityGrant")
        exact = (
            (self.grant_id, grant.grant_id),
            (self.grant_hash, grant.grant_hash),
            (self.task_scope_id, grant.task_scope_id),
            (self.appended_root, grant.root),
            (self.base_binding_set_revision, grant.base_binding_set_revision),
        )
        if any(left != right for left, right in exact):
            raise ValueError("Workspace binding-set receipt differs from grant")

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> WorkspaceBindingSetReceipt:
        expected = {
            "schema_version",
            "receipt_id",
            "binding_id",
            "task_scope_id",
            "base_binding_set_revision",
            "binding_set_revision",
            "parent_receipt_id",
            "parent_receipt_hash",
            "previous_root_set_digest",
            "root_set_digest",
            "appended_root",
            "grant_id",
            "grant_hash",
            "host_receipt_ref",
            "host_receipt_hash",
        }
        _exact_keys(value, expected, "WorkspaceBindingSetReceipt")
        raw_root = value["appended_root"]
        if not isinstance(raw_root, Mapping):
            raise TypeError("appended_root must be an object")
        return cls(
            receipt_id=_identifier(value["receipt_id"], "receipt_id"),
            binding_id=_identifier(value["binding_id"], "binding_id"),
            task_scope_id=_identifier(value["task_scope_id"], "task_scope_id"),
            base_binding_set_revision=_non_negative_int(
                value["base_binding_set_revision"], "base_binding_set_revision"
            ),
            binding_set_revision=_positive_int(
                value["binding_set_revision"], "binding_set_revision"
            ),
            parent_receipt_id=(
                None
                if value["parent_receipt_id"] is None
                else _identifier(value["parent_receipt_id"], "parent_receipt_id")
            ),
            parent_receipt_hash=(
                None
                if value["parent_receipt_hash"] is None
                else _digest(value["parent_receipt_hash"], "parent_receipt_hash")
            ),
            previous_root_set_digest=_digest(
                value["previous_root_set_digest"], "previous_root_set_digest"
            ),
            root_set_digest=_digest(value["root_set_digest"], "root_set_digest"),
            appended_root=CanonicalWorkspaceRoot.from_json(raw_root),
            grant_id=_identifier(value["grant_id"], "grant_id"),
            grant_hash=_digest(value["grant_hash"], "grant_hash"),
            host_receipt_ref=_identifier(value["host_receipt_ref"], "host_receipt_ref"),
            host_receipt_hash=_digest(value["host_receipt_hash"], "host_receipt_hash"),
            schema_version=_schema_version(value["schema_version"], "WorkspaceBindingSetReceipt"),
        )


class WorkspaceBindingAuthorityPort(Protocol):
    """Injected Host authority backed by durable exact records.

    Implementations must enforce nonce replay/idempotency and reject changed
    payloads for an already-consumed nonce. Manual verification, Auto snapshot
    issuance/authorization, and grant verification must use Host durable lookup
    or a fixed authenticated proof; public DTO hashes alone are never authority.
    """

    def verify_manual_authorization(
        self,
        proposal: WorkspaceBindingProposal,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
    ) -> Awaitable[WorkspaceBindingAuthorityGrant]: ...

    def issue_run_binding_mode_snapshot(
        self, request: RunBindingModeSnapshotRequest
    ) -> Awaitable[HostIssuedRunBindingModeSnapshot]: ...

    def authorize_auto_binding(
        self,
        proposal: WorkspaceBindingProposal,
        snapshot: HostIssuedRunBindingModeSnapshot,
    ) -> Awaitable[WorkspaceBindingAuthorityGrant]: ...

    def verify_binding_grant(
        self, proposal: WorkspaceBindingProposal, grant: WorkspaceBindingAuthorityGrant
    ) -> Awaitable[None]: ...


__all__ = (
    "WORKSPACE_BINDING_SCHEMA_VERSION",
    "CanonicalWorkspaceRoot",
    "FilesystemIdentity",
    "FilesystemIdentityKind",
    "HostIssuedRunBindingModeSnapshot",
    "ManualWorkspaceBindingAuthorizationReceipt",
    "ManualWorkspaceBindingChallenge",
    "RunBindingModeSnapshotRequest",
    "WorkspaceBindingAuthorizationChannel",
    "WorkspaceBindingAuthorizationDecision",
    "WorkspaceBindingAuthorityGrant",
    "WorkspaceBindingAuthorityPort",
    "WorkspaceBindingGrantSource",
    "WorkspaceBindingMode",
    "WorkspaceBindingProposal",
    "WorkspaceBindingSetReceipt",
)
