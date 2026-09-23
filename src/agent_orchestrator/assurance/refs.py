# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Exact internal references. These are deliberately separate from public TypedRef."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codec import AssuranceError, digest, fields, fingerprint, integer, one_of, text

REF_KINDS = frozenset(
    {
        "requirements",
        "task",
        "method",
        "method_instance",
        "artifact",
        "source",
        "observation",
        "review",
        "acceptance",
        "resolution",
        "operation",
        "tool_receipt",
        "policy",
        "authority",
        "capability",
        "input_manifest",
        "completion_scope",
        "completion_spec",
        "result",
        "commit_receipt",
        "reservation_fact",
        "agent_turn_receipt",
        "check_binding",
        "check_spec",
        "check_policy",
        "local_check_receipt",
        "execution_receipt",
        "disclosure_receipt",
        "review_package",
    }
)


@dataclass(frozen=True, slots=True)
class Pin:
    id: str
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        text(self.id)
        integer(self.revision)
        digest(self.content_hash)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision, "content_hash": self.content_hash}

    @classmethod
    def from_json(cls, value: object) -> Pin:
        row = fields(value, {"id", "revision", "content_hash"})
        return cls(row["id"], row["revision"], row["content_hash"])


@dataclass(frozen=True, slots=True)
class AssuranceRef:
    kind: str
    pin: Pin

    def __post_init__(self) -> None:
        one_of(self.kind, REF_KINDS)
        if not isinstance(self.pin, Pin):
            raise AssuranceError("REF_PIN_INVALID")

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "pin": self.pin.to_json()}

    @property
    def key(self) -> str:
        return fingerprint(self.to_json())

    @classmethod
    def from_json(cls, value: object, *, kinds: set[str] | None = None) -> AssuranceRef:
        row = fields(value, {"kind", "pin"})
        result = cls(row["kind"], Pin.from_json(row["pin"]))
        if kinds is not None and result.kind not in kinds:
            raise AssuranceError("REF_KIND_FORBIDDEN")
        return result
