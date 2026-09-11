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
from .secrets import environment_secrets, find_secrets, redact_text
from .trace import trace


def _dump(path: Path, value: Any, redactions: list[dict[str, Any]] | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # step 6 (S6-09 / review P2-10): a credential never reaches the evidence — it is replaced
    # by a marker naming the pattern, and the redaction itself is reported
    text, found = redact_text(text)
    if found and redactions is not None:
        redactions.append({"file": path.name, "patterns": sorted(set(found))})
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
    redactions: list[dict[str, Any]] = []

    def dump(path: Path, value: Any) -> None:
        _dump(path, value, redactions)

    dump(directory / "baseline.json", dict(baseline))
    lines = "".join(
        json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
        for event in store.iter_events(mission_id)
    )
    lines, found = redact_text(lines)
    if found:
        redactions.append({"file": "events.jsonl", "patterns": sorted(set(found))})
    (directory / "events.jsonl").write_text(lines, encoding="utf-8")
    snapshot = store.snapshot(mission_id)
    dump(directory / "final_state.json", snapshot)
    dump(
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
    dump(directory / "costs.json", costs)
    dump(  # step 4: the Blackboard layers and the final result's lineage
        directory / "knowledge.json",
        {
            "knowledge": snapshot.get("knowledge", []),
            "conflicts": snapshot.get("conflicts", []),
            "summaries": snapshot.get("summaries", []),
        },
    )
    dump(directory / "lineage.json", lineage(store, mission_id))
    dump(directory / "graph_history.json", graph_history(store, mission_id))  # step 5
    requests = snapshot.get("approvals", [])
    dump(  # step 7 (D7-11): the action ledger and the human record
        directory / "actions.json",
        {"actions": snapshot.get("actions", []), "overrides": snapshot.get("human_overrides", [])},
    )
    dump(
        directory / "approvals.json",
        {
            "requests": requests,
            "decisions": {r["request_id"]: store.list_decisions(r["request_id"]) for r in requests},
            "waiting_on": snapshot.get("waiting_on", []),
            "human_time": [
                {
                    "request_id": r["request_id"],
                    "kind": r["kind"],
                    "state": r["state"],
                    "waited_seconds": round(
                        float(r.get("closed_at") or store.now) - float(r.get("created_at") or 0.0),
                        3,
                    ),
                }
                for r in requests
            ],
        },
    )
    dump(  # step 6 (D6-2'): the scheduler's durable signals — the transition log is the truth
        directory / "scheduler.json",
        {
            "backpressure": store.get_scheduler_state("backpressure"),
            "profile_health": store.get_scheduler_state("profile_health"),
        },
    )
    dump(directory / "trace.json", trace(store, mission_id, echoes=echoes))  # step 6 (S6-09)
    dump(directory / "metrics.json", metrics(store, mission_id, unpriced=unpriced))
    artifacts_dir = directory / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    withheld: list[dict[str, Any]] = []
    for artifact in snapshot["artifacts"]:
        source = workspaces_root / artifact["attempt_id"] / artifact["path"]
        if source.is_file():
            data = source.read_bytes()
            try:  # review P1-5: work product is scanned too; a binary file is listed, not read
                found_here = find_secrets(data.decode("utf-8"), extra=environment_secrets())
            except UnicodeDecodeError:
                found_here = []
                withheld.append(
                    {"artifact_id": artifact["id"], "reason": "binary_not_scanned", "copied": True}
                )
            if found_here:  # a credential-bearing artifact is withheld, never copied
                withheld.append(
                    {"artifact_id": artifact["id"], "patterns": found_here, "copied": False}
                )
                continue
            target = artifacts_dir / artifact["attempt_id"] / artifact["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    dump(directory / "test-report.json", dict(test_report))
    return {
        "redactions": redactions,
        "withheld_artifacts": withheld,
        "mission": snapshot["mission"],
        "files": sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()),
    }


__all__ = ("write_evidence",)
