# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · code review round 1, P2-5: ``replay`` and ``evaluate`` answer a bad call with
an error and exit 2 — never a traceback — and an incomplete replay exits 1."""

from __future__ import annotations

import json
from pathlib import Path

from agent_orchestrator.__main__ import main


def test_review_p2_5_cli_errors_are_answers_not_tracebacks(tmp_path, capsys):
    empty = Path(tmp_path) / "empty"
    empty.mkdir()
    assert main(["replay", "--evidence-dir", str(empty), "mission-x"]) == 2
    assert (
        main(
            [
                "replay",
                "--evidence-dir",
                str(empty),
                "mission-x",
                "--events",
                str(empty / "none.jsonl"),
            ]
        )
        == 2
    )
    evidence = Path(tmp_path) / "single"
    assert (
        main(
            [
                "demo",
                "--scenario",
                "single-task",
                "--provider",
                "fixtures",
                "--evidence-dir",
                str(evidence),
                "--idempotency-key",
                "cli-1",
            ]
        )
        == 0
    )
    mission_id = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))[
        "mission_id"
    ]
    assert main(["replay", "--evidence-dir", str(evidence), "mission-unknown"]) == 2
    assert main(["replay", "--evidence-dir", str(evidence), mission_id]) == 0
    lines = (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
    partial = Path(tmp_path) / "partial.jsonl"
    partial.write_text(
        "".join(line + "\n" for line in lines if json.loads(line)["type"] != "MissionCompleted"),
        encoding="utf-8",
    )
    assert (
        main(["replay", "--evidence-dir", str(evidence), mission_id, "--events", str(partial)]) == 1
    )  # a gap is not a clean replay
    only_events = Path(tmp_path) / "only-events"
    only_events.mkdir()
    assert (
        main(
            [
                "replay",
                "--evidence-dir",
                str(only_events),
                mission_id,
                "--events",
                str(partial),
                "--attribution",
            ]
        )
        == 2
    )  # attribution needs the library
    assert (
        main(
            [
                "evaluate",
                "--plan",
                str(empty / "missing.json"),
                "--evidence-dir",
                str(tmp_path / "e1"),
            ]
        )
        == 2
    )
    plan = Path(tmp_path) / "plan.json"
    plan.write_text(
        json.dumps({"name": "p", "cases": ["nope"], "strategies": [{"name": "s"}]}),
        encoding="utf-8",
    )
    assert main(["evaluate", "--plan", str(plan), "--evidence-dir", str(tmp_path / "e2")]) == 2
    out = capsys.readouterr().out
    assert "Traceback" not in out and "unknown evaluation cases" in out
