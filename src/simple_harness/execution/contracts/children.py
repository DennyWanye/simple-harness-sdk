# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable ticket, child-command, and parent-signal records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Mapping

from simple_harness.contracts import FrozenJsonValue, JsonValue, canonical_json


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256(value: object, name: str) -> str:
    text = _required(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def child_launch_fingerprint(value: Mapping[str, JsonValue]) -> str:
    """Bind every trusted launch field to the one-use ticket."""

    if not isinstance(value, dict):
        raise TypeError("launch request must be a JSON object")
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ProfileLaunchTicketState(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AttachmentPolicy(StrEnum):
    ATTACHED = "attached"
    DETACHED = "detached"
    ROOT_TERMINAL_CHILD = "root_terminal_child"


class ChildCommandState(StrEnum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    ACKED = "acked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChildSignalState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACKED = "acked"


@dataclass(frozen=True, slots=True)
class ProfileLaunchTicket:
    ticket_id: str
    parent_run_id: str
    profile_key: str
    catalog_generation: int
    fingerprint: str
    state: ProfileLaunchTicketState = ProfileLaunchTicketState.ISSUED
    child_run_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("ticket_id", "parent_run_id", "profile_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if isinstance(self.catalog_generation, bool) or self.catalog_generation < 1:
            raise ValueError("catalog_generation must be a positive integer")
        object.__setattr__(self, "fingerprint", _sha256(self.fingerprint, "fingerprint"))
        object.__setattr__(self, "state", ProfileLaunchTicketState(self.state))
        if self.child_run_id is not None:
            object.__setattr__(self, "child_run_id", _required(self.child_run_id, "child_run_id"))
        if (self.state is ProfileLaunchTicketState.CLAIMED) != (self.child_run_id is not None):
            raise ValueError("only claimed tickets must name their child run")


@dataclass(frozen=True, slots=True)
class ChildCommandRecord:
    command_id: str
    parent_run_id: str
    child_run_id: str
    ticket_id: str
    state: ChildCommandState


@dataclass(frozen=True, slots=True)
class ChildSignalRecord:
    signal_id: str
    parent_run_id: str
    child_run_id: str
    payload: FrozenJsonValue
    state: ChildSignalState
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ChildSignalState(self.state))


@dataclass(frozen=True, slots=True)
class ChildLaunchResult:
    ticket: ProfileLaunchTicket
    command: ChildCommandRecord
    child_run_id: str


__all__ = (
    "AttachmentPolicy",
    "ChildCommandRecord",
    "ChildCommandState",
    "ChildLaunchResult",
    "ChildSignalRecord",
    "ChildSignalState",
    "ProfileLaunchTicket",
    "ProfileLaunchTicketState",
    "child_launch_fingerprint",
)
