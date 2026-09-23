# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Internal ``Pin`` four-tuple (kind / id / revision / content_hash) — FIELD-CONTRACTS §2.

A Pin is a precise reference, never an authorisation token: every field that holds
one narrows ``kind`` to its own subset, and a resolver checks root/tenant/owner
before id + revision + hash.  ``revision == 0`` is only for original immutable
objects that genuinely have no version (a binding row, a run event); it is never a
default for a missing field.  The public ``TypedRef`` of the orchestrator is not
changed by this type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import ArpError
from .strict import digest

_HASH = re.compile(r"^[0-9a-f]{64}$")

PIN_KINDS = frozenset(
    {
        "agent", "agent_turn", "session", "profile", "policy", "schema", "capability",
        "provider", "deployment", "tool", "skill", "workflow", "artifact", "source",
        "journal_record", "journal_group", "input_manifest", "receipt", "context",
        "retrieval", "tool_snapshot", "skill_use", "occurrence", "completion_scope",
        "task", "requirements", "method", "acceptance", "review", "resolution",
        "operation", "operation_intent", "scope", "principal", "authority",
        "reservation", "invocation", "catalogue", "index_snapshot", "index_generation",
        "dependency_lock", "evaluation", "restore", "use_certificate",
    }
)


@dataclass(frozen=True, slots=True)
class Pin:
    kind: str
    id: str
    revision: int
    content_hash: str

    def __post_init__(self) -> None:
        if self.kind not in PIN_KINDS:
            raise ArpError("REF_KIND_MISMATCH", field_path="kind")
        if type(self.id) is not str or not 1 <= len(self.id) <= 256:
            raise ArpError("STRING_LIMIT", field_path="id")
        if type(self.revision) is not int or self.revision < 0:
            raise ArpError("NUMBER_LIMIT", field_path="revision")
        if type(self.content_hash) is not str or not _HASH.match(self.content_hash):
            raise ArpError("STRING_PATTERN", field_path="content_hash")

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "revision": self.revision,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json(cls, value: object, *, kinds: Iterable[str] | None = None) -> Pin:
        if not isinstance(value, Mapping) or set(value) != {"kind", "id", "revision", "content_hash"}:
            raise ArpError("STRUCTURE_INVALID", "Pin must have exactly kind/id/revision/content_hash")
        pin = cls(value["kind"], value["id"], value["revision"], value["content_hash"])
        if kinds is not None and pin.kind not in set(kinds):
            raise ArpError("REF_KIND_MISMATCH", field_path="kind")
        return pin

    def require_kind(self, *kinds: str) -> Pin:
        if self.kind not in kinds:
            raise ArpError("REF_KIND_MISMATCH", f"expected {kinds}, got {self.kind}")
        return self

    def matches(self, other: Pin) -> bool:
        return self == other

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}:{self.revision}:{self.content_hash}"


def pin_for(kind: str, id: str, revision: int, body: object) -> Pin:
    """Pin an in-memory body: ``content_hash`` is the SHA-256 of its canonical bytes."""

    return Pin(kind, id, revision, digest(body))


def original_receipt_pin(id: str, body: object) -> Pin:
    """A ``receipt`` Pin to an original immutable SDK object (revision 0 by contract)."""

    return Pin("receipt", id, 0, digest(body))


__all__ = ("PIN_KINDS", "Pin", "original_receipt_pin", "pin_for")
