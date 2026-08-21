# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable lost-wakeup contracts for uncertain outbound work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from simple_harness.contracts import JsonValue, canonical_json


class RecoveryKind(StrEnum):
    PROVIDER = "provider"
    TOOL = "tool"


class ResolutionOutcome(StrEnum):
    COMPLETED = "completed"
    CONFIRMED_NOT_STARTED = "confirmed_not_started"


def recovery_identity(kind: RecoveryKind, ledger_identity: str, handoff_attempt: int) -> str:
    if not ledger_identity.strip() or handoff_attempt < 1:
        raise ValueError("recovery identity requires ledger identity and attempt")
    return hashlib.sha256(
        canonical_json(
            {
                "protocol": "simple-harness-recovery-v1",
                "kind": RecoveryKind(kind).value,
                "ledger_identity": ledger_identity,
                "handoff_attempt": handoff_attempt,
            }
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class WaitBlockerSpec:
    kind: RecoveryKind
    ledger_identity: str
    handoff_attempt: int
    observed_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RecoveryKind(self.kind))
        if not self.ledger_identity.strip():
            raise ValueError("ledger_identity is required")
        if self.handoff_attempt < 1 or self.observed_version < 1:
            raise ValueError("blocker attempt and version must be positive")

    @property
    def blocker_id(self) -> str:
        return recovery_identity(self.kind, self.ledger_identity, self.handoff_attempt)


@dataclass(frozen=True, slots=True)
class WaitBlockerRecord:
    blocker_id: str
    run_id: str
    kind: RecoveryKind
    ledger_identity: str
    handoff_attempt: int
    observed_version: int
    resolution_id: str | None
    wake_consumed: bool
    version: int


@dataclass(frozen=True, slots=True)
class ReconciliationResolution:
    resolution_id: str
    kind: RecoveryKind
    ledger_identity: str
    handoff_attempt: int
    outcome: ResolutionOutcome
    outcome_hash: str
    evidence_ref: str
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class WaitActivationReceipt:
    receipt_id: str
    blocker_id: str
    run_id: str
    owner_id: str
    runtime_lease_epoch: int
    outcome_hash: str


__all__ = (
    "ReconciliationResolution",
    "RecoveryKind",
    "ResolutionOutcome",
    "WaitActivationReceipt",
    "WaitBlockerRecord",
    "WaitBlockerSpec",
    "recovery_identity",
)
