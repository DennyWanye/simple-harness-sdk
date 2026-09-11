# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · slice E (plan D8-9' / D8-10): ``demo --scenario evaluate-policies`` on
fixtures, and the ``replay`` / ``evaluate`` commands a person uses on its output."""

from __future__ import annotations

import json
from pathlib import Path

from agent_orchestrator.__main__ import main


def test_the_evaluate_policies_demo_compares_two_policies_and_explains_one_run_each_way(
    tmp_path, capsys
):
    evidence = Path(tmp_path) / "s8"
    assert (
        main(
            [
                "demo",
                "--scenario",
                "evaluate-policies",
                "--provider",
                "fixtures",
                "--evidence-dir",
                str(evidence),
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = json.loads((evidence / "evaluation.json").read_text(encoding="utf-8"))
    assert len(report["runs"]) == 16 and report["note"].startswith("机制验证")
    by_case = {}
    for run in report["runs"]:
        by_case.setdefault(run["case"], set()).add(run["category"])
    assert by_case == {
        "parse-kv": {"success"},
        "parse-kv-strict": {"success"},
        "parse-kv-bad": {"failure"},
        "textkit": {"success"},
    }
    full, ablated = report["summary"]["full-policy"], report["summary"]["no-critic"]
    assert full["samples"] == ablated["samples"] == 8 and full["harness_errors"] == 0
    assert full["verification_misjudgment"] == {
        "checked": 4,  # parse-kv and parse-kv-strict carry an oracle, textkit does not
        "misjudged": 2,
        "rate": 0.5,
    }  # the strict oracle rejects what passed
    assert ablated["ablated_policy_passes"] > 0 and full["ablated_policy_passes"] == 0
    assert full["failure_reasons"] == {"max_attempts_reached": 2}
    [comparison] = report["comparisons"]
    assert comparison["verdict"].startswith("不适用（fixture）")
    samples = json.loads((evidence / "samples.json").read_text(encoding="utf-8"))
    assert (
        samples["attribution"]["success_path"] is True
        and samples["attribution"]["cost"]["reconciled"]
    )
    kinds = [line["type"] for line in samples["replay"]["failure_timeline"]]
    assert "VerificationFailed" in kinds and kinds[-1] == "MissionFailed"
    assert (
        samples["replay"]["comparison"]["consistent"]
        and samples["replay"]["comparison"]["coverage"] == 1.0
    )
    assert "## 比较" in (evidence / "evaluation.md").read_text(encoding="utf-8")


def test_replay_and_evaluate_from_the_command_line(tmp_path, capsys):
    plan = Path(tmp_path) / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "name": "cli",
                "cases": ["parse-kv"],
                "strategies": [
                    {"name": "baseline"},
                    {"name": "lean", "overrides": {"ablations": ["critic"]}},
                ],
                "trials": 1,
            }
        ),
        encoding="utf-8",
    )
    out = Path(tmp_path) / "eval"
    assert main(["evaluate", "--plan", str(plan), "--evidence-dir", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed["summary"]) == {"baseline", "lean"}
    run = json.loads((out / "evaluation.json").read_text(encoding="utf-8"))["runs"][0]
    assert (
        main(
            [
                "replay",
                "--evidence-dir",
                run["run_dir"],
                run["mission_id"],
                "--attribution",
                "--failures",
                "--out",
                str(Path(tmp_path) / "replay.json"),
            ]
        )
        == 0
    )
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["comparison"]["consistent"] and replayed["comparison"]["coverage"] == 1.0
    assert replayed["attribution"]["final_products"] and replayed["failure_timeline"] == []
    assert (
        json.loads((Path(tmp_path) / "replay.json").read_text(encoding="utf-8"))["mission_id"]
        == run["mission_id"]
    )
    bad = Path(tmp_path) / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "name": "bad",
                "cases": ["parse-kv"],
                "strategies": [{"name": "s", "overrides": {"deployment_policy": {}}}],
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(["evaluate", "--plan", str(bad), "--evidence-dir", str(Path(tmp_path) / "never")]) == 2
    )
    assert "may not change" in json.loads(capsys.readouterr().out)["error"]
