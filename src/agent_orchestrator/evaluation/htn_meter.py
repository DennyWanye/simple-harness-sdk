# SPDX-License-Identifier: Apache-2.0
"""H8 physical accounting survives a process restart without resetting its budget."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from .experiment import ExecutionCounters
from .metered_provider import MeteredProvider


class DurableMeteredProvider(MeteredProvider):
    def __init__(self, *args: Any, checkpoint: Path, run_identity: str,
                 resume: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not run_identity:
            raise ContractError("durable meter requires a frozen run identity")
        self._checkpoint = checkpoint
        self._binding = {"run_identity": run_identity,
            "provider": self.target.provider_id, "model": self.target.model,
            "budget": asdict(self._budget), "physical_slots": self._physical_slots}
        self._wall_deadline = time.time() + self._budget.seconds
        if checkpoint.exists():
            if not resume:
                raise ContractError("existing physical meter requires explicit recovery")
            self._restore(json.loads(checkpoint.read_text()))
        elif resume:
            raise ContractError("cannot recover an episode with missing physical accounting")
        self._persist()

    def _restore(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or set(envelope) != {"state", "sha256"}:
            raise ContractError("invalid physical accounting checkpoint")
        state = envelope["state"]
        if content_hash_of(state) != envelope["sha256"] or state["binding"] != self._binding:
            raise ContractError("physical accounting identity or content changed")
        counts = ExecutionCounters(**state["counters"])
        rows = state["observations"]
        if not isinstance(rows, list) or [r["ordinal"] for r in rows] != list(range(1, counts.calls + 1)):
            raise ContractError("physical accounting lost a call ordinal")
        known = [r for r in rows if all(k in r for k in ("input_tokens", "output_tokens", "total_tokens"))]
        if (sum(self._tokens(r["input_tokens"], "input") for r in known) != counts.input_tokens
                or sum(self._tokens(r["output_tokens"], "output") for r in known) != counts.output_tokens):
            raise ContractError("physical counters differ from their call receipts")
        self._input, self._output, self._calls = counts.input_tokens, counts.output_tokens, counts.calls
        self._peak = counts.peak_physical_slots
        self.observations = rows
        self.admission_denials = list(state["admission_denials"])
        # A started call may have reached the provider before the process died.
        # Keep that uncertainty; neither retry nor restart restores its allowance.
        self.unknown_usage_calls = max(self._tokens(state["unknown_usage_calls"], "unknown usage"),
                                       len(rows) - len(known))
        self._closed = bool(state["closed"]) or bool(self.unknown_usage_calls)
        self._wall_deadline = float(state["wall_deadline"])
        import math
        if not math.isfinite(self._wall_deadline):
            raise ContractError("invalid physical accounting deadline")
        remaining = max(0.0, min(self._budget.seconds, self._wall_deadline - time.time()))
        self._deadline = time.monotonic() + remaining

    def _persist(self) -> None:
        state: dict[str, Any] = {"binding": self._binding, "counters": asdict(self.counters),
            "wall_deadline": self._wall_deadline, "observations": self.observations,
            "admission_denials": self.admission_denials, "unknown_usage_calls": self.unknown_usage_calls,
            "closed": self._closed}
        content = canonical_json({"state": state, "sha256": content_hash_of(state)})
        self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".meter-", dir=self._checkpoint.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._checkpoint)
            directory = os.open(self._checkpoint.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _publish(self) -> None:
        self._persist()  # The original meter calls this before physical handoff.
        super()._publish()
