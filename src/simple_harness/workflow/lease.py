# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Fenced workflow lease contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowLease:
    run_id: str
    owner_id: str
    epoch: int
    expires_at: float
    runtime_lease_epoch: int
    namespace: str = "native"

    def __post_init__(self) -> None:
        if not self.run_id or not self.owner_id or self.epoch < 1:
            raise ValueError("invalid workflow lease identity")
        if not math.isfinite(self.expires_at):
            raise ValueError("lease expiry must be finite")
        if self.runtime_lease_epoch < 1:
            raise ValueError("runtime lease epoch must be positive")
        if not self.namespace:
            raise ValueError("workflow lease namespace is required")


__all__ = ("WorkflowLease",)
