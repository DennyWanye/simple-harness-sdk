# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Evidence directory writer (ORCH-BUILD §14.3): ``baseline.json``, ``events.jsonl``,
``final_state.json``, ``artifacts/``, ``verification.json``, ``costs.json``,
``test-report.json``, plus (step 4) ``knowledge.json`` and ``lineage.json``.  Never
contains credentials: only ids, hashes and orchestrator state are written."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..orchestrator.commit_service import CommitService
from ..storage.store import Store
from .graph_history import graph_history
from .lineage import lineage
from .metrics import metrics
from .secrets import guard_text
from .trace import trace


def _dump(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    guard_text(text, where=path.name)  # step 6 (S6-09): a file with a credential is never written
    path.write_text(text, encoding="utf-8")


def write_evidence(
    *,
    directory: Path,
    store: Store,
    commit: CommitService,
    mission_id: str,
    baseline: Mapping[str, Any],
    workspaces_root: Path,
    test_report: Mapping[str, Any],
    echoes: Mapping[str, Sequence[str]] | None = None,
    unpriced: bool = True,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    _dump(directory / "baseline.json", dict(baseline))
    lines = "".join(
        json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
        for event in store.list_events(mission_id)
    )
    guard_text(lines, where="events.jsonl")
    (directory / "events.jsonl").write_text(lines, encoding="utf-8")
    snapshot = store.snapshot(mission_id)
    _dump(directory / "final_state.json", snapshot)
    _dump(
        directory / "verification.json",
        {
            "results": [
                {
                    "result_id": item["envelope"]["id"],
                    "attempt_id": item["envelope"]["attempt_id"],
                    "verdict": item["verdict"],
                    "layers": item["verifications"],
                }
                for item in snapshot["results"]
            ],
            "claims": snapshot["claims"],
        },
    )
    with store.transaction():
        costs = commit.ledger.costs_report(mission_id)
    _dump(directory / "costs.json", costs)
    _dump(  # step 4: the Blackboard layers and the final result's lineage
        directory / "knowledge.json",
        {
            "knowledge": snapshot.get("knowledge", []),
            "conflicts": snapshot.get("conflicts", []),
            "summaries": snapshot.get("summaries", []),
        },
    )
    _dump(directory / "lineage.json", lineage(store, mission_id))
    _dump(directory / "graph_history.json", graph_history(store, mission_id))  # step 5
    _dump(  # step 6 (D6-2'): the scheduler's durable signals — the transition log is the truth
        directory / "scheduler.json",
        {
            "backpressure": store.get_scheduler_state("backpressure"),
            "profile_health": store.get_scheduler_state("profile_health"),
        },
    )
    _dump(directory / "trace.json", trace(store, mission_id, echoes=echoes))  # step 6 (S6-09)
    _dump(directory / "metrics.json", metrics(store, mission_id, unpriced=unpriced))
    artifacts_dir = directory / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    for artifact in snapshot["artifacts"]:
        source = workspaces_root / artifact["attempt_id"] / artifact["path"]
        if source.is_file():
            target = artifacts_dir / artifact["attempt_id"] / artifact["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    _dump(directory / "test-report.json", dict(test_report))
    return {
        "mission": snapshot["mission"],
        "files": sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()),
    }


__all__ = ("write_evidence",)
