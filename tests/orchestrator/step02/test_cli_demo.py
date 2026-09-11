# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · C4: ``python -m agent_orchestrator`` demo on fixtures writes the full evidence
directory (ORCH §14.3), later-step scenarios report ``not_implemented`` (exit 3), and the
operator commands read back the same library."""

from __future__ import annotations

import json
from pathlib import Path

from agent_orchestrator.__main__ import EXIT_NOT_IMPLEMENTED, EXIT_OK, main

EVIDENCE_FILES = {
    "baseline.json",
    "events.jsonl",
    "final_state.json",
    "verification.json",
    "costs.json",
    "test-report.json",
}


def test_demo_single_task_on_fixtures_writes_evidence(tmp_path, capsys):
    evidence = Path(tmp_path) / "evidence" / "s2"
    code = main(
        [
            "demo",
            "--scenario",
            "single-task",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(evidence),
            "--idempotency-key",
            "demo-1",
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    report = json.loads(out)
    assert report["status"] == "COMPLETED" and report["stop_reason"] == "verification_passed"
    present = {p.name for p in evidence.iterdir() if p.is_file()}
    assert EVIDENCE_FILES <= present
    events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
    assert events[0]["type"] == "MissionCreated" and events[-1]["type"] == "MissionCompleted"
    final = json.loads((evidence / "final_state.json").read_text())
    assert final["mission"]["status"] == "COMPLETED" and len(final["attempts"]) == 2
    costs = json.loads((evidence / "costs.json").read_text())
    assert costs["accounts"] and all(r["state"] == "SETTLED" for r in costs["reservations"])
    verification = json.loads((evidence / "verification.json").read_text())
    assert [r["verdict"] for r in verification["results"]] == ["FAIL", "PASS"]
    artifacts = list((evidence / "artifacts").rglob("parse_kv.py"))
    assert artifacts, "accepted artifact copied into the evidence directory"
    baseline = json.loads((evidence / "baseline.json").read_text())
    assert baseline["provider_kind"] == "fixtures" and "SH_APIKEY" not in json.dumps(baseline)
    # operator read-back on the same library
    mission_id = report["mission_id"]
    assert main(["mission", "get", "--evidence-dir", str(evidence), mission_id]) == EXIT_OK
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["mission"]["id"] == mission_id
    assert main(["mission", "events", "--evidence-dir", str(evidence), mission_id]) == EXIT_OK
    assert len(json.loads(capsys.readouterr().out)) == len(events)
    attempt_id = snapshot["attempts"][1]["id"]
    assert main(["attempt", "get", "--evidence-dir", str(evidence), attempt_id]) == EXIT_OK
    attempt = json.loads(capsys.readouterr().out)
    assert attempt["result"]["verdict"] == "PASS"
    artifact_id = snapshot["tasks"][0]["accepted_artifacts"][0]
    assert main(["artifact", "show", "--evidence-dir", str(evidence), artifact_id]) == EXIT_OK
    shown = json.loads(capsys.readouterr().out)
    assert "def parse_kv" in shown["content"]


def test_later_step_scenarios_are_not_implemented(tmp_path, capsys):
    code = main(
        [
            "demo",
            "--scenario",
            "multi-mission",
            "--provider",
            "fixtures",
            "--evidence-dir",
            str(tmp_path / "e"),
        ]
    )
    assert code == EXIT_NOT_IMPLEMENTED
    assert json.loads(capsys.readouterr().out)["status"] == "not_implemented"


def test_mission_create_validates_and_is_idempotent(tmp_path, capsys):
    evidence = Path(tmp_path) / "evidence"
    spec_path = Path(tmp_path) / "spec.json"
    spec_path.write_text(json.dumps({"goal": "x", "success_criteria": [], "idempotency_key": "k"}))
    import pytest

    from agent_orchestrator.api.missions import MissionRequestError

    with pytest.raises(MissionRequestError):
        main(
            [
                "mission",
                "create",
                "--evidence-dir",
                str(evidence),
                "--spec",
                str(spec_path),
                "--tenant",
                "t",
            ]
        )
    spec_path.write_text(
        json.dumps(
            {
                "goal": "x",
                "success_criteria": ["file:a.py"],
                "idempotency_key": "k",
                "budget": {"max_attempts": 1},
            }
        )
    )
    assert (
        main(
            [
                "mission",
                "create",
                "--evidence-dir",
                str(evidence),
                "--spec",
                str(spec_path),
                "--tenant",
                "t",
            ]
        )
        == EXIT_OK
    )
    first = json.loads(capsys.readouterr().out)
    assert first["created"] is True
    assert (
        main(
            [
                "mission",
                "create",
                "--evidence-dir",
                str(evidence),
                "--spec",
                str(spec_path),
                "--tenant",
                "t",
            ]
        )
        == EXIT_OK
    )
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False and second["mission_id"] == first["mission_id"]
