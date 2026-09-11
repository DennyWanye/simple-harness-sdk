# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · ORCH §8.4 demo: ``python -m agent_orchestrator demo --scenario multi-mission
--provider fixtures`` runs two Missions at once on two execution pools under a Global
Budget with a bounded verification queue, and writes the evidence per Mission."""

from __future__ import annotations

import json
import re

from agent_orchestrator.__main__ import main


def test_demo_multi_mission_on_fixtures_writes_per_mission_evidence(tmp_path, capsys):
    evidence = tmp_path / "evidence" / "s6"
    code = main(
        [
            "demo",
            "--scenario",
            "multi-mission",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            "demo-s6",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    summary = json.loads((evidence / "multi-mission.json").read_text(encoding="utf-8"))
    assert summary["scenario"] == "multi-mission" and len(summary["missions"]) == 2
    assert all(m["status"] == "COMPLETED" for m in summary["missions"])
    for mission in summary["missions"]:
        # physical routing: Workers on the small pool, proven by its echo; services on the large pool
        workers = mission["attempts"]
        assert workers and all(
            a["runtime_profile_id"] == "small" and a["echoed_models"] == ["fixture-small"]
            for a in workers
        )
        planners = [s for s in mission["services"] if s["kind"] == "plan"]
        critics = [s for s in mission["services"] if s["kind"] == "critic"]
        assert planners and all(
            s["runtime_profile_id"] == "large" and s["model"] == "fixture-large" for s in planners
        )
        assert critics and all(s["runtime_profile_id"] == "large" for s in critics)
        files = set(mission["evidence_files"])
        assert {
            "baseline.json",
            "events.jsonl",
            "final_state.json",
            "verification.json",
            "costs.json",
            "test-report.json",
            "trace.json",
            "metrics.json",
            "scheduler.json",
            "graph_history.json",
            "lineage.json",
        } <= files
        assert any(f.startswith("artifacts/") for f in files)
        trace = json.loads(
            (evidence / "missions" / mission["mission_id"] / "trace.json").read_text()
        )
        assert all(s["model_version"]["echoed_models"] == ["fixture-small"] for s in trace["spans"])
    # both Missions spent from one Global Budget, never beyond it
    glob = summary["global_account"]
    assert glob["scope"] == "global" and glob["settled_tokens"] > 0 and glob["reserved_tokens"] == 0
    assert (
        summary["profiles"]["small"]["model"] == "fixture-small"
        and "provider" not in summary["profiles"]["small"]
    )
    assert summary["backpressure"]["limits"]["max_pending_verifications"] == 2
    # no credential anywhere in the evidence tree
    pattern = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
    for path in evidence.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl"}:
            assert not pattern.search(path.read_text(encoding="utf-8"))
