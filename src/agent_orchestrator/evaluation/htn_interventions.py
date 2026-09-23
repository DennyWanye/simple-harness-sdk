# SPDX-License-Identifier: Apache-2.0
"""Frozen H8 tool faults and untrusted evidence changes at actual runtime calls.

Faults happen before handoff, so no uncertain external effect is called retryable.
The same controller wraps the original gateway in all four arms. It never supplies
a Planner decision, Acceptance, authorization, model reply, or an oracle score.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json
from simple_harness.tools import ToolResult
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from .htn_matrix import EvidenceFile, H8Run
from .htn_oracles import write_evidence

READ_TOOLS = {"workspace_read_file", "workspace_list", "run_tests", "drone_sim_telemetry", "appworld_execute"}
EFFECT_TOOLS = {"workspace_write_file", "drone_sim_command", "appworld_execute"}


class ToolIntervention:
    def __init__(self, run: H8Run, root: Path):
        self.run, self.root = run, root
        self.spec = dict(run.scenario.intervention or {})
        self.identity = content_hash_of([run.run_id, self.spec])
        self.path = root / "intervention-state.json"
        self.state: dict[str, Any] = {"identity": self.identity, "calls": 0,
            "injections": [], "source_reads": [], "changed": False}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            if raw.get("sha256") != content_hash_of(raw.get("state")) or raw["state"].get("identity") != self.identity:
                raise ContractError("intervention checkpoint identity/content changed")
            self.state = raw["state"]
        self.gateways: list[Any] = []

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(canonical_json({"state": self.state, "sha256": content_hash_of(self.state)}))
        temporary.replace(self.path)

    def configure(self, config: Any, domain: Any) -> Any:
        self.domain = domain
        return config

    def _inject(self, name: str) -> bool:
        count = len(self.state["injections"])
        action = self.spec["action"]
        if action == "one_transport_failure":
            return count == 0
        if action == "observer_unavailable_twice":
            return name in READ_TOOLS and count < 2
        if action == "one_write_conflict":
            return name in EFFECT_TOOLS and count == 0
        if action == "one_resource_unavailable":
            return self.state["calls"] >= 2 and count == 0
        return False

    def _install(self, gateway: Any) -> None:
        if any(existing is gateway for existing in self.gateways):
            return
        self.gateways.append(gateway)
        original = gateway.execute

        async def execute(call: Any, context: Any) -> ToolResult:
            self.state["calls"] += 1
            if self._inject(call.name):
                failure = {"call_id": str(call.call_id), "tool": call.name,
                    "ordinal": self.state["calls"], "action": self.spec["action"],
                    "phase": "before_original_gateway_handoff", "external_effect_performed": False}
                self.state["injections"].append(failure)
                self._save()
                return ToolResult.failed(call.call_id, "h8_injected_" + self.spec["action"],
                    "Frozen experiment fault before execution; no external effect occurred. Retry or choose another valid approach.",
                    retryable=True)
            result = await original(call, context)
            if call.name == "workspace_read_file" and str(result.outcome) == "succeeded":
                name = str(call.arguments.get("path", ""))
                sources = self.run.scenario.fixture.get("evidence_sources", {})
                if name in sources:
                    self.state["source_reads"].append({"path": name, "call_id": str(call.call_id)})
                    if self.spec["action"] == "change_cached_source" and not self.state["changed"]:
                        binding = gateway._bindings[str(context["run_id"])]
                        workspace = gateway._workspace(binding).root.resolve()
                        path = (workspace / name).resolve(strict=True)
                        if not path.is_relative_to(workspace) or not path.is_relative_to(self.root.resolve()):
                            raise ContractError("evidence intervention escaped its local episode workspace")
                        before = path.read_bytes()
                        after = canonical_json({"source": "cached-note-revised", "verified": False,
                            "claim": "prior_cached_claim_withdrawn", "reason": "external cache revision"}).encode()
                        path.write_bytes(after)
                        self.state["injections"].append({"action": "change_cached_source", "path": name,
                            "call_id": str(call.call_id), "before_sha256": hashlib.sha256(before).hexdigest(),
                            "after_sha256": hashlib.sha256(after).hexdigest(), "phase": "after_original_read"})
                        self.state["changed"] = True
            self._save()
            return result

        gateway.execute = execute

    async def before_cycle(self, loop: Any, mission: Any) -> None:
        self._install(loop.assembled.gateway)

    async def before_single_tick(self, gateway: Any, agent: Any, workspace: Path) -> None:
        self._install(gateway)

    def receipt(self, root: Path) -> EvidenceFile:
        action = self.spec["action"]
        if action == "contradictory_cached_claims":
            triggered = {r["path"] for r in self.state["source_reads"]} >= set(self.run.scenario.fixture["evidence_sources"])
        elif action == "observer_unavailable_twice":
            triggered = len(self.state["injections"]) == 2
        else:
            triggered = bool(self.state["injections"])
        return write_evidence(root, "intervention.json", {"run_id": self.run.run_id,
            "specification": self.spec, "triggered": triggered, "state": self.state,
            "scope": "tool_fault_or_untrusted_cached_claim; not a forged HTN authority/Observation"})
