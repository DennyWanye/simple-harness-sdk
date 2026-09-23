# SPDX-License-Identifier: Apache-2.0
"""Read the installed derivation of a verified HTN wiring delivery.

The build manifest records actual upstream evidence and the complete derived
package inventory. It is not a TaskGraph test verdict or a planning grant.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from simple_harness.contracts import canonical_json

from ..graph.revision_records import SourceRef
from ..runtime.planning_operations import SourceUnavailable
from ..storage.store import Store
from .taskgraph_policy import InstalledGraphPolicy


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class InstalledHtnWiringAcceptance:
    """Fixed package-owned source; no IPC argument or environment bypass."""
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.manifest = Path(__file__).resolve().with_name("taskgraph_deployment_manifest.json")

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or set(value) != {
                    "schema", "deployment_id", "upstream", "source_files", "taskgraph_acceptance"}
                    or value["schema"] != "taskgraph-htn-wiring-derivation-v1"
                    or not _digest(value["deployment_id"])):
                raise ValueError("invalid deployment manifest")
            upstream = value["upstream"]
            if (not isinstance(upstream, dict) or set(upstream) != {
                    "status", "scope", "wheel_sha256", "manifest_sha256", "source_inputs_sha256",
                    "mission_id", "mission_receipt_sha256", "cold_replay_receipt_sha256", "semantic_replay"}
                    or upstream["status"] != "READY_FOR_TASKGRAPH_WIRING"
                    or upstream["scope"] != "CORE_INTEGRATION_E2E"
                    or upstream["semantic_replay"] != "PARTIAL"
                    or not isinstance(upstream["mission_id"], str) or not upstream["mission_id"]
                    or any(not _digest(v) for k, v in upstream.items() if k.endswith("_sha256"))):
                raise ValueError("actual HTN wiring evidence is missing")
            sources = value["source_files"]
            if not isinstance(sources, dict) or not sources:
                raise ValueError("installed source inventory is missing")
            actual_names = {p.relative_to(self.root).as_posix()
                for namespace in ("agent_orchestrator", "simple_harness")
                for p in (self.root / namespace).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p != self.manifest
                and p.suffix not in {".pyc", ".pyo"}}
            if actual_names != set(sources):
                raise ValueError("installed source inventory changed")
            for name, digest in sources.items():
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts
                        or path.parts[0] not in {"agent_orchestrator", "simple_harness"}
                        or not _digest(digest) or _hash((self.root / name).read_bytes()) != digest):
                    raise ValueError("installed source bytes changed")
            identity = {k: v for k, v in value.items() if k != "deployment_id"}
            if _hash(canonical_json(identity).encode()) != value["deployment_id"]:
                raise ValueError("deployment identity changed")
            # This source explicitly allows validation of the derived TaskGraph
            # package. It never converts NOT_RUN into an acceptance PASS.
            if value["taskgraph_acceptance"] not in {"NOT_RUN", "VALIDATED"}:
                raise ValueError("invalid TaskGraph validation scope")
            return value
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise SourceUnavailable("taskgraph_deployed_source_unverified") from error

    def __call__(self, store: Store, mission_id: str, policy: InstalledGraphPolicy) -> SourceRef:
        if not store.connection.in_transaction or store.get_mission(mission_id) is None:
            raise SourceUnavailable("taskgraph_deployment_requires_original_mission_transaction")
        if not isinstance(policy, InstalledGraphPolicy):
            raise SourceUnavailable("taskgraph_deployment_policy_missing")
        value = self._read()
        return SourceRef(channel="h1h_deployment_acceptance", identity=value["deployment_id"],
                         revision=1, digest=_hash(canonical_json(value).encode()))
