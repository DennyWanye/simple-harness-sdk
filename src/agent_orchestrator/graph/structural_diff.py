# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Pure structural differences for already authorized historical documents.

This module does not read a Store or grant access to a Mission. The eventual
read facade must authorize both revisions and verify their persisted records.
Only frozen object content is compared; runtime overlays are not accepted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, cast

from simple_harness.contracts import canonical_json
from simple_harness.contracts.json import JsonValue

from ..contracts.models import ContractError
from .network_codec import NetworkDocumentV1, decode

_JS_MAX = 2**53 - 1
_MAX_CHANGES = 16384
_CHANGE_FIELDS = {"kind", "identity", "before_hash", "after_hash"}
_DIFF_FIELDS = {
    "schema_version",
    "mission_id",
    "from_revision",
    "to_revision",
    "from_manifest_hash",
    "to_manifest_hash",
    "changes",
    "complete",
}


def _invalid(reason: str) -> NoReturn:
    raise ContractError(f"TASKGRAPH_DIFF_INVALID: {reason}")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _invalid(f"{name} must be a nonempty identifier of at most 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(f"{name} must be UTF-8")
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        _invalid(f"{name} must be a lowercase SHA-256")
    return value


def _revision(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _JS_MAX:
        _invalid(f"{name} must be a nonnegative safe integer")
    return value


def _object(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _invalid(f"{name} has missing or unknown fields")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralChange:
    kind: str
    identity: str
    before_hash: str | None
    after_hash: str | None

    def __post_init__(self) -> None:
        _identifier(self.kind, "kind")
        _identifier(self.identity, "identity")
        if self.before_hash is not None:
            _digest(self.before_hash, "before_hash")
        if self.after_hash is not None:
            _digest(self.after_hash, "after_hash")
        if self.before_hash == self.after_hash:
            _invalid("a change must have different hashes and at least one object")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }

    @classmethod
    def from_json(cls, value: object) -> StructuralChange:
        data = _object(value, _CHANGE_FIELDS, "change")
        return cls(
            kind=cast(str, data["kind"]),
            identity=cast(str, data["identity"]),
            before_hash=cast(str | None, data["before_hash"]),
            after_hash=cast(str | None, data["after_hash"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskGraphDiffV1:
    mission_id: str
    from_revision: int
    to_revision: int
    from_manifest_hash: str
    to_manifest_hash: str
    changes: tuple[StructuralChange, ...]
    complete: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        _revision(self.from_revision, "from_revision")
        _revision(self.to_revision, "to_revision")
        _digest(self.from_manifest_hash, "from_manifest_hash")
        _digest(self.to_manifest_hash, "to_manifest_hash")
        if type(self.schema_version) is not int or self.schema_version != 1:
            _invalid("schema_version must be 1")
        if self.complete is not True:
            _invalid("partial structural differences are unsupported")
        if not isinstance(self.changes, (tuple, list)) or len(self.changes) > _MAX_CHANGES:
            _invalid("changes must be a bounded sequence")
        if not all(isinstance(change, StructuralChange) for change in self.changes):
            _invalid("changes must contain StructuralChange values")
        changes = tuple(sorted(self.changes, key=lambda change: (change.kind, change.identity)))
        if len({(change.kind, change.identity) for change in changes}) != len(changes):
            _invalid("duplicate change identity")
        if self.from_revision == self.to_revision and (
            self.from_manifest_hash != self.to_manifest_hash or changes
        ):
            _invalid("one immutable revision cannot have two structures")
        if self.from_manifest_hash == self.to_manifest_hash and (
            self.from_revision != self.to_revision or changes
        ):
            _invalid("identical documents must have the same revision and no changes")
        object.__setattr__(self, "changes", changes)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "from_manifest_hash": self.from_manifest_hash,
            "to_manifest_hash": self.to_manifest_hash,
            "changes": [change.to_json() for change in self.changes],
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, value: object) -> TaskGraphDiffV1:
        data = _object(value, _DIFF_FIELDS, "diff")
        raw_changes = data["changes"]
        if not isinstance(raw_changes, list) or len(raw_changes) > _MAX_CHANGES:
            _invalid("changes must be a bounded array")
        return cls(
            schema_version=cast(int, data["schema_version"]),
            mission_id=cast(str, data["mission_id"]),
            from_revision=cast(int, data["from_revision"]),
            to_revision=cast(int, data["to_revision"]),
            from_manifest_hash=cast(str, data["from_manifest_hash"]),
            to_manifest_hash=cast(str, data["to_manifest_hash"]),
            changes=tuple(StructuralChange.from_json(change) for change in raw_changes),
            complete=cast(bool, data["complete"]),
        )


def diff_documents(before: NetworkDocumentV1, after: NetworkDocumentV1) -> TaskGraphDiffV1:
    """Compare complete decoded objects, retaining top-level document hashes.

    Set changes (roots/adoption/required obligations) and requirements references
    are bound by the two document hashes, not represented as invented object kinds.
    No current Task, Method state, witness, or execution permission is consulted.
    """
    if not isinstance(before, NetworkDocumentV1) or not isinstance(after, NetworkDocumentV1):
        _invalid("both inputs must be NetworkDocumentV1")
    if before.mission_id != after.mission_id:
        _invalid("source missions differ")
    # Directly constructed dataclasses are not evidence of decoder validation.
    left = decode(before.to_json()).document
    right = decode(after.to_json()).document
    old = {(item.kind, item.identity): item.sha256 for item in left.objects}
    new = {(item.kind, item.identity): item.sha256 for item in right.objects}
    changed_keys = [key for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]
    if len(changed_keys) > _MAX_CHANGES:
        _invalid("complete difference exceeds the public contract capacity")
    changes = tuple(
        StructuralChange(
            kind=kind,
            identity=identity,
            before_hash=old.get((kind, identity)),
            after_hash=new.get((kind, identity)),
        )
        for kind, identity in changed_keys
    )
    return TaskGraphDiffV1(
        mission_id=left.mission_id,
        from_revision=left.revision,
        to_revision=right.revision,
        from_manifest_hash=hashlib.sha256(
            canonical_json(left.to_json()).encode("utf-8")
        ).hexdigest(),
        to_manifest_hash=hashlib.sha256(
            canonical_json(right.to_json()).encode("utf-8")
        ).hexdigest(),
        changes=changes,
        complete=True,
    )


__all__ = ("StructuralChange", "TaskGraphDiffV1", "diff_documents")
