"""Explicit, bounded recovery witness for a legacy authorization expiry."""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExpiredAuthorizationTerminalRecoveryV1:
    """Read-only eligibility, not a terminal or permission receipt.

    Recovery revalidates every source under its write transaction. Callers cannot
    authorize a repair by constructing this value themselves.
    """

    run_id: str
    decision_id: str
    run_version: int
    source_hash: str
    original_resolved_at: float

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in (self.run_id, self.decision_id)):
            raise ValueError("recovery identity required")
        if type(self.run_version) is not int or self.run_version < 0:
            raise ValueError("recovery version invalid")
        if (
            not isinstance(self.source_hash, str)
            or len(self.source_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.source_hash)
        ):
            raise ValueError("recovery source hash invalid")
        if (
            type(self.original_resolved_at) not in (int, float)
            or not math.isfinite(self.original_resolved_at)
            or self.original_resolved_at < 0
        ):
            raise ValueError("recovery source time invalid")
