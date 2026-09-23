# SPDX-License-Identifier: Apache-2.0
"""Typed proof input for a settled-terminal ORDER edge, never an execution grant."""
from dataclasses import dataclass

from .notification_contracts import _hash, _integer, _text


@dataclass(frozen=True, slots=True, kw_only=True)
class SettledTerminalOccurrence:
    mission_id: str
    plan_revision: int
    occurrence_id: str
    contract_revision: int
    dispatch_generation: int
    outcome: str
    source_digest: str

    def __post_init__(self) -> None:
        _text(self.mission_id, "mission_id")
        _text(self.occurrence_id, "occurrence_id")
        for name in ("plan_revision", "contract_revision", "dispatch_generation"):
            _integer(getattr(self, name), name)
        _hash(self.source_digest, "source_digest")
        if self.outcome not in {"ACCEPTED", "FAILED", "CANCELLED", "SETTLED_OTHER"}:
            raise ValueError("settlement requires a terminal outcome")
