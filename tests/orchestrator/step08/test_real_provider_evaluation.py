# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 8 · real model evaluation (opt-in ``--run-real-provider``).

``demo --scenario evaluate-policies --provider env``: the ``parse-kv`` case under the full
policy and without the Critic, two independent real trials each (the model named in
``SH_MODEL`` — this program's real runs use deepseek-flash, per the user's instruction).
The report is a real trial, reported apart from the fixture regression, and with two
samples a comparison can only say "evidence insufficient".  The key comes from the
environment and is never printed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.secrets import find_secrets

pytestmark = pytest.mark.real_provider


def test_real_evaluation_on_flash(tmp_path, capsys):
    if not (
        os.environ.get("SH_BASEURL") and os.environ.get("SH_APIKEY") and os.environ.get("SH_MODEL")
    ):
        pytest.skip("SH_BASEURL / SH_APIKEY / SH_MODEL not set")
    evidence = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    code = main(
        [
            "demo",
            "--scenario",
            "evaluate-policies",
            "--provider",
            "env",
            "--unpriced",
            "--evidence-dir",
            str(evidence),
            "--test-timeout",
            "120",
        ]
    )
    capsys.readouterr()
    report = json.loads((evidence / "evaluation.json").read_text(encoding="utf-8"))
    summary = {
        "model": os.environ["SH_MODEL"],
        "exit_code": code,
        "note": report["note"],
        "runs": [
            {
                k: r.get(k)
                for k in (
                    "strategy",
                    "trial",
                    "mission_id",
                    "category",
                    "stop_reason",
                    "wall_seconds",
                    "tokens",
                    "attempts",
                    "oracle",
                    "ablated_policy_pass",
                    "reason",
                )
            }
            for r in report["runs"]
        ],
        "summary": {
            k: {
                f: v[f]
                for f in (
                    "samples",
                    "successes",
                    "success_rate",
                    "wilson95",
                    "harness_errors",
                    "tokens",
                    "duration_seconds",
                    "verification_misjudgment",
                )
            }
            for k, v in report["summary"].items()
        },
        "comparisons": [
            {k: c[k] for k in ("verdict", "success", "duration", "tokens")}
            for c in report["comparisons"]
        ],
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
    assert find_secrets(text) == []
    print("REAL_EVALUATION_REPORT " + text)
    assert len(report["runs"]) == 4 and report["note"].startswith("真实模型试验")
    assert all(
        c["verdict"].startswith("证据不足") for c in report["comparisons"]
    )  # two samples decide nothing
    assert (
        sum(v["samples"] for v in report["summary"].values()) >= 2
    )  # most runs finished either way
