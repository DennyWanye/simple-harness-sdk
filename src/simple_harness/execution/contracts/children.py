# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable ticket, child-command, and parent-signal records."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from simple_harness.contracts import (
    FrozenJsonValue,
    JsonValue,
    canonical_json,
    freeze_json,
    thaw_json,
)


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256(value: object, name: str) -> str:
    text = _required(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
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
        object.__setattr__(
            self, "fingerprint", _sha256(self.fingerprint, "fingerprint")
        )
        object.__setattr__(self, "state", ProfileLaunchTicketState(self.state))
        if self.child_run_id is not None:
            object.__setattr__(
                self, "child_run_id", _required(self.child_run_id, "child_run_id")
            )
        if (self.state is ProfileLaunchTicketState.CLAIMED) != (
            self.child_run_id is not None
        ):
            raise ValueError("only claimed tickets must name their child run")


@dataclass(frozen=True, slots=True)
class ChildCommandRecord:
    command_id: str
    parent_run_id: str
    child_run_id: str
    ticket_id: str
    state: ChildCommandState

    def __post_init__(self) -> None:
        for name in ("command_id", "parent_run_id", "child_run_id", "ticket_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "state", ChildCommandState(self.state))


@dataclass(frozen=True, slots=True)
class ChildSignalRecord:
    signal_id: str
    parent_run_id: str
    child_run_id: str
    payload: FrozenJsonValue
    state: ChildSignalState
    version: int
    claimed_by: str | None = None
    claimed_at: float | None = None
    claim_expires_at: float | None = None
    claim_epoch: int = 0
    acked_at: float | None = None
    ack_receipt_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("signal_id", "parent_run_id", "child_run_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a JSON object")
        mutable_payload = thaw_json(self.payload)
        if not isinstance(mutable_payload, dict):
            raise TypeError("payload must be a JSON object")
        object.__setattr__(self, "payload", freeze_json(mutable_payload))
        object.__setattr__(self, "state", ChildSignalState(self.state))
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("version must be a non-negative integer")
        if isinstance(self.claim_epoch, bool) or self.claim_epoch < 0:
            raise ValueError("claim_epoch must be a non-negative integer")

        claim_values = (self.claimed_by, self.claimed_at, self.claim_expires_at)
        if self.state is ChildSignalState.PENDING:
            if any(value is not None for value in claim_values):
                raise ValueError("pending child signal cannot have a claim lease")
            if self.claim_epoch != 0 or self.acked_at is not None:
                raise ValueError(
                    "pending child signal cannot have claim or ack history"
                )
            if self.ack_receipt_id is not None:
                raise ValueError("pending child signal cannot have an ack receipt")
            return

        if any(value is None for value in claim_values):
            raise ValueError("claimed child signal requires owner and lease timestamps")
        object.__setattr__(self, "claimed_by", _required(self.claimed_by, "claimed_by"))
        for name in ("claimed_at", "claim_expires_at"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a number")
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.claim_expires_at <= self.claimed_at:  # type: ignore[operator]
            raise ValueError("claim_expires_at must be later than claimed_at")
        if self.claim_epoch < 1:
            raise ValueError("claimed child signal requires a positive claim_epoch")

        if self.state is ChildSignalState.CLAIMED:
            if self.acked_at is not None or self.ack_receipt_id is not None:
                raise ValueError("claimed child signal cannot have ack metadata")
            return

        if not isinstance(self.acked_at, (int, float)) or isinstance(
            self.acked_at, bool
        ):
            raise TypeError("acked_at must be a number")
        acked_at = float(self.acked_at)
        if not math.isfinite(acked_at) or acked_at < 0:
            raise ValueError("acked_at must be finite and non-negative")
        if acked_at < self.claimed_at:  # type: ignore[operator]
            raise ValueError("acked_at cannot be earlier than claimed_at")
        object.__setattr__(self, "acked_at", acked_at)
        object.__setattr__(
            self, "ack_receipt_id", _required(self.ack_receipt_id, "ack_receipt_id")
        )


@dataclass(frozen=True, slots=True)
class ChildSignalAckReceipt:
    receipt_id: str
    signal_id: str
    parent_run_id: str
    owner_id: str
    claim_epoch: int
    continuation_id: str
    event_id: str
    continuation_payload_hash: str
    event_payload_hash: str
    created_at: float

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "signal_id",
            "parent_run_id",
            "owner_id",
            "continuation_id",
            "event_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if isinstance(self.claim_epoch, bool) or self.claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        for name in ("continuation_payload_hash", "event_payload_hash"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if not isinstance(self.created_at, (int, float)) or isinstance(
            self.created_at, bool
        ):
            raise TypeError("created_at must be a number")
        created_at = float(self.created_at)
        if not math.isfinite(created_at) or created_at < 0:
            raise ValueError("created_at must be finite and non-negative")
        object.__setattr__(self, "created_at", created_at)


@dataclass(frozen=True, slots=True)
class ChildSignalAckResult:
    signal: ChildSignalRecord
    receipt: ChildSignalAckReceipt

    def __post_init__(self) -> None:
        if self.signal.state is not ChildSignalState.ACKED:
            raise ValueError("ack result requires an acknowledged child signal")
        if self.signal.signal_id != self.receipt.signal_id:
            raise ValueError("ack receipt belongs to another child signal")
        if self.signal.parent_run_id != self.receipt.parent_run_id:
            raise ValueError("ack receipt belongs to another parent run")
        if self.signal.claimed_by != self.receipt.owner_id:
            raise ValueError("ack receipt owner does not match the signal claim")
        if self.signal.claim_epoch != self.receipt.claim_epoch:
            raise ValueError("ack receipt epoch does not match the signal claim")
        if self.signal.ack_receipt_id != self.receipt.receipt_id:
            raise ValueError("ack receipt identity does not match the signal")


@dataclass(frozen=True, slots=True)
class ChildLaunchResult:
    ticket: ProfileLaunchTicket
    command: ChildCommandRecord
    child_run_id: str


@dataclass(frozen=True, slots=True)
class ChildTerminalReceipt:
    receipt_id: str
    command_id: str
    child_run_id: str
    terminal_state: str
    outcome_hash: str
    signal_id: str | None
    event_id: str
    owner_id: str
    runtime_lease_epoch: int
    fence_epoch: int

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "command_id",
            "child_run_id",
            "event_id",
            "owner_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.signal_id is not None:
            object.__setattr__(
                self, "signal_id", _required(self.signal_id, "signal_id")
            )
        terminal_state = self.terminal_state.strip()
        if terminal_state not in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal_state must be terminal")
        object.__setattr__(self, "terminal_state", terminal_state)
        object.__setattr__(
            self, "outcome_hash", _sha256(self.outcome_hash, "outcome_hash")
        )
        for name in ("runtime_lease_epoch", "fence_epoch"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ChildTerminalResult:
    run_id: str
    terminal_state: str
    receipt: ChildTerminalReceipt
    signal: ChildSignalRecord | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required(self.run_id, "run_id"))
        if self.run_id != self.receipt.child_run_id:
            raise ValueError("terminal receipt belongs to another child Run")
        if self.terminal_state != self.receipt.terminal_state:
            raise ValueError("terminal state differs from its receipt")
        if (self.signal is None) != (self.receipt.signal_id is None):
            raise ValueError("terminal signal differs from its receipt")
        if self.signal is not None and self.signal.signal_id != self.receipt.signal_id:
            raise ValueError("terminal signal identity differs from its receipt")


__all__ = (
    "AttachmentPolicy",
    "ChildCommandRecord",
    "ChildCommandState",
    "ChildLaunchResult",
    "ChildSignalAckReceipt",
    "ChildSignalAckResult",
    "ChildSignalRecord",
    "ChildSignalState",
    "ChildTerminalReceipt",
    "ChildTerminalResult",
    "ProfileLaunchTicket",
    "ProfileLaunchTicketState",
    "child_launch_fingerprint",
)
