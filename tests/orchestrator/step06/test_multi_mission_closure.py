# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · ORCH §8.4 demo: ``python -m agent_orchestrator demo --scenario multi-mission
--provider fixtures`` runs two Missions at once on two execution pools under a Global
Budget with a bounded verification queue, and writes the evidence per Mission."""

from __future__ import annotations

import json

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.secrets import find_secrets


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
    escalations = []
    for mission in summary["missions"]:
        # physical routing: each Attempt's echo is its own pool's model and its Agent lives
        # only in that pool's execution library; services run on the large pool
        workers = mission["attempts"]
        assert workers
        for a in workers:
            expected = "fixture-small" if a["runtime_profile_id"] == "small" else "fixture-large"
            assert a["echoed_models"] == [expected] and a["only_in_own_pool"] is True
            if a["runtime_profile_id"] == "large":
                escalations.append(a)
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
        for span in trace["spans"]:
            pool = span["model_version"]["runtime_profile_id"]
            assert span["model_version"]["echoed_models"] == [f"fixture-{pool}"]
    # the demo's first Critic verdict is a FAIL: that Attempt climbed small → large (§9.3)
    assert escalations and all(a["retry_of"] for a in escalations)
    # backpressure really rose (one slow Verifier, max 2 waiting) and was cleared again
    levels = [t["level"] for t in summary["backpressure"]["log"]]
    assert "RAISED" in levels and levels[-1] == "NORMAL"
    # the two Missions progressed together: their Attempts interleave on the one timeline
    created = []
    for mission in summary["missions"]:
        for line in (
            (evidence / "missions" / mission["mission_id"] / "events.jsonl")
            .read_text()
            .splitlines()
        ):
            event = json.loads(line)
            if event["type"] == "AttemptCreated":
                created.append((event["created_at"], mission["mission_id"]))
    order = [m for _t, m in sorted(created)]
    assert len(set(order)) == 2 and order != sorted(order) and order != sorted(order, reverse=True)
    # both Missions spent from one Global Budget, never beyond it
    glob = summary["global_account"]
    assert glob["scope"] == "global" and glob["settled_tokens"] > 0 and glob["reserved_tokens"] == 0
    assert (
        summary["profiles"]["small"]["model"] == "fixture-small"
        and "provider" not in summary["profiles"]["small"]
    )
    assert summary["backpressure"]["limits"]["max_pending_verifications"] == 2
    # no credential anywhere in the evidence tree
    for path in evidence.rglob("*"):  # every file, every pattern
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert find_secrets(text) == [], path.name
