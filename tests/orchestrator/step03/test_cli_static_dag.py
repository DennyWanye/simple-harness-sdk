# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · D3-15: ``python -m agent_orchestrator demo --scenario static-dag`` on fixtures
runs the whole A→(B‖C)→D→E graph and writes the evidence directory; the operator
commands read back the graph."""

from __future__ import annotations

import json
from pathlib import Path

from agent_orchestrator.__main__ import EXIT_OK, main

EVIDENCE_FILES = {
    "baseline.json",
    "events.jsonl",
    "final_state.json",
    "verification.json",
    "costs.json",
    "test-report.json",
}


def test_demo_static_dag_on_fixtures_writes_evidence(tmp_path, capsys):
    evidence = Path(tmp_path) / "evidence" / "s3"
    code = main(
        [
            "demo",
            "--scenario",
            "static-dag",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            "demo-dag-1",
            "--max-concurrency",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    report = json.loads(out)
    assert report["status"] == "COMPLETED" and report["stop_reason"] == "verification_passed"
    assert [t["status"] for t in report["tasks"]] == ["COMPLETED"] * 5
    assert [len(t["dependencies"]) for t in report["tasks"]] == [0, 1, 1, 2, 1]
    present = {p.name for p in evidence.iterdir() if p.is_file()}
    assert EVIDENCE_FILES <= present
    events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    assert types[0] == "MissionCreated" and types[-1] == "MissionCompleted"
    assert types.count("TaskGraphCommitted") == 1 and types.count("TaskCompleted") == 5
    assert types.count("TaskUnblocked") == 4  # B, C, D, E
    final = json.loads((evidence / "final_state.json").read_text())
    assert final["mission"]["status"] == "COMPLETED" and len(final["tasks"]) == 5
    assert final["mission"]["final_report"]["graph_version"] == 1
    assert len(final["mission"]["final_report"]["tasks"]) == 5
    costs = json.loads((evidence / "costs.json").read_text())
    assert all(r["state"] == "SETTLED" for r in costs["reservations"])
    baseline = json.loads((evidence / "baseline.json").read_text())
    assert baseline["config"]["candidates_per_task"] == 1
    assert "SH_APIKEY" not in json.dumps(baseline)
    mission_id = report["mission_id"]
    assert main(["mission", "get", "--evidence-dir", str(evidence), mission_id]) == EXIT_OK
    snapshot = json.loads(capsys.readouterr().out)
    assert [t["status"] for t in snapshot["tasks"]] == ["COMPLETED"] * 5
    delivery = next(
        a for t in snapshot["tasks"] for a in t["accepted_artifacts"] if t["id"].endswith("task-5")
    )
    assert main(["artifact", "show", "--evidence-dir", str(evidence), delivery]) == EXIT_OK
    shown = json.loads(capsys.readouterr().out)
    assert shown["path"] == "DELIVERY.md"
