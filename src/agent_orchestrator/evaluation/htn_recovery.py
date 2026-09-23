# SPDX-License-Identifier: Apache-2.0
"""H8 process-kill checkpoints, preserving original stores and physical accounting.

The batch supervisor kills the stopped worker; the remote AppWorld service stays
alive. The legacy controlled-reopen helper is retained for focused local checks
and is explicitly rejected as H8 process-recovery acceptance evidence.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from .htn_matrix import EvidenceFile, H8Run
from .htn_oracles import write_evidence


class RuntimeRestartRequested(Exception):
    """Unwind and close the original SDK runtime before opening its stores again."""


class RuntimeRecovery:
    def __init__(self, run: H8Run, root: Path, *, process_recovery: bool = False,
                 manifest_hash: str = ""):
        self.process_recovery, self.manifest_hash = process_recovery, manifest_hash
        self.run, self.root = run, root
        self.path = root / "recovery-state.json"
        self.identity = content_hash_of([run.run_id, run.scenario.to_json()])
        self.state: dict[str, Any] = {"identity": self.identity, "phase": "ARMED"}
        if self.path.exists():
            envelope = json.loads(self.path.read_text())
            if (envelope.get("sha256") != content_hash_of(envelope.get("state"))
                    or envelope["state"].get("identity") != self.identity):
                raise ContractError("recovery checkpoint identity/content changed")
            self.state = envelope["state"]
        self.meter: Any = None

    def configure(self, config: Any, domain: Any) -> Any:
        self.domain = domain
        return config

    def bind_meter(self, meter: Any) -> None:
        self.meter = meter

    def _save(self) -> None:
        from .appworld_resume import durable_document
        durable_document(self.path, self.state)

    def _check(self, gateway: Any) -> None:
        if self.meter is None:
            raise ContractError("recovery has no original physical meter")
        if self.state["phase"] != "ARMED" or self.meter._active:
            return
        action = (self.run.scenario.intervention or {}).get("at")
        settled = [r for r in self.meter.observations if r.get("status") != "started"]
        calls = [c for c in gateway.calls if c.get("outcome") == "succeeded"]
        ready = bool(settled) if action == "first_settled_provider_call" else bool(calls)
        if not ready:
            return
        if self.process_recovery:
            import os
            import signal
            from .appworld_resume import checkpoint_episode
            if self.domain.episode is not None:
                checkpoint_episode(self.domain.episode, self.root / "appworld-resume.json")
            self.state.update(phase="KILL_READY", counts_before=asdict(self.meter.counters),
                call_ids=[str(c["call_id"]) for c in calls], trigger=action,
                mechanism="supervisor_sigkill_and_cold_process_restart",
                worker_pid=os.getpid(), parent_pid=os.getppid(), manifest_hash=self.manifest_hash)
            self._save()
            # No context manager unwinds, no runtime/remote-world close runs.
            # The owning parent kills this stopped child and verifies wait status.
            os.kill(os.getpid(), signal.SIGSTOP)
            raise ContractError("stopped recovery worker was unexpectedly resumed")
        self.state.update(phase="RESTART_REQUESTED", counts_before=asdict(self.meter.counters),
            call_ids=[str(c["call_id"]) for c in calls], trigger=action,
            mechanism="original_sdk_runtime_shutdown_and_reopen")
        self._save()
        raise RuntimeRestartRequested()

    async def before_cycle(self, loop: Any, mission: Any) -> None:
        self._check(loop.assembled.gateway)

    async def before_single_tick(self, gateway: Any, agent: Any, workspace: Path) -> None:
        self._check(gateway)

    def reopened(self, meter: Any) -> None:
        if self.process_recovery:
            import os
            import signal
            if (self.state.get("phase") != "KILLED"
                    or self.state.get("kill_returncode") != -signal.SIGKILL
                    or self.state.get("killed_pid") == os.getpid()
                    or self.state.get("manifest_hash") != self.manifest_hash):
                raise ContractError("missing original process-kill receipt")
            self.state["resumed_pid"] = os.getpid()
        elif self.state["phase"] != "RESTART_REQUESTED":
            raise ContractError("runtime recovery was not requested")
        if asdict(meter.counters) != self.state["counts_before"]:
            # Shutdown can settle an already started call. Preserve this evidence
            # rather than pretending the pre-shutdown snapshot is the final one.
            self.state["counts_after_shutdown"] = asdict(meter.counters)
        if meter.counters.calls < self.state["counts_before"]["calls"]:
            raise ContractError("recovery reset physical call accounting")
        self.state["phase"] = "REOPENED"
        self.bind_meter(meter)
        self._save()

    def receipt(self, root: Path) -> EvidenceFile:
        return write_evidence(root, "intervention.json", {**self.state,
            "triggered": self.state["phase"] == "REOPENED", "kind": (
                "process_kill_recovery" if self.process_recovery else "controlled_runtime_restart")})
