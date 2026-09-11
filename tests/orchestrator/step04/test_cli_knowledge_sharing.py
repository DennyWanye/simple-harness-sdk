# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · D4-17: ``python -m agent_orchestrator demo --scenario knowledge-sharing`` on
fixtures runs the whole scenario and writes the evidence directory (ORCH §14.3 files plus
``knowledge.json`` / ``lineage.json``)."""

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
    "knowledge.json",
    "lineage.json",
}


def test_demo_knowledge_sharing_on_fixtures_writes_evidence(tmp_path, capsys):
    evidence = Path(tmp_path) / "evidence" / "s4"
    code = main(
        [
            "demo",
            "--scenario",
            "knowledge-sharing",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            "demo-ks-1",
            "--max-concurrency",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    report = json.loads(out)
    assert report["status"] == "COMPLETED" and report["stop_reason"] == "verification_passed"
    kinds = [t["kind"] for t in report["tasks"]]
    assert kinds == ["work", "work", "work", "synthesis", "conflict"]
    assert all(t["status"] == "COMPLETED" for t in report["tasks"])
    assert report["conflicts"] == [
        {
            "conflict_id": report["conflicts"][0]["conflict_id"],
            "key": "impl_a.empty_input",
            "state": "RESOLVED",
        }
    ]
    assert any(k["status"] == "VERIFIED" and k["used_by"] for k in report["knowledge"])
    assert report["lineage"]["knowledge"] and report["lineage"]["agents"]
    present = {p.name for p in evidence.iterdir() if p.is_file()}
    assert EVIDENCE_FILES <= present and (evidence / "artifacts").is_dir()
    knowledge = json.loads((evidence / "knowledge.json").read_text())
    assert (
        knowledge["knowledge"]
        and knowledge["conflicts"][0]["state"] == "RESOLVED"
        and knowledge["summaries"]
    )
    lineage = json.loads((evidence / "lineage.json").read_text())
    assert lineage["terminal_task_id"].endswith(":task-4") and lineage["edges"]
    events = [
        json.loads(line)["type"] for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    for expected in (
        "KnowledgeCommitted",
        "KnowledgeUsed",
        "ClaimDisputed",
        "ConflictOpened",
        "SynthesisGated",
        "ConflictResolved",
        "MissionCompleted",
    ):
        assert expected in events, expected
    final = json.loads((evidence / "final_state.json").read_text())
    contexts = "".join(str(i["config"].get("message")) for i in final["intents"])
    assert (
        "SYSTEM NOTICE" not in contexts
    )  # external content is never inlined into a context package
    assert (
        "SYSTEM NOTICE" not in (evidence / "knowledge.json").read_text()
    )  # nor into the Blackboard
