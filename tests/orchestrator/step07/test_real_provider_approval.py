# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 7 · real model run (opt-in ``--run-real-provider``).

``demo --scenario approval-action --provider env``: a real Planner and Worker (the model
named in ``SH_MODEL`` — this program's real runs use deepseek-flash, per the user's
instruction) write the action candidate; the system asks for approval, the demo operator
approves, the executor hands it off to the local test configuration service and checks
the receipt.  The key comes from the environment and is never printed; the report line
carries ids, hashes, states and outcomes only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.secrets import find_secrets

pytestmark = pytest.mark.real_provider


def test_real_approval_action_on_flash(tmp_path, capsys):
    if not (
        os.environ.get("SH_BASEURL") and os.environ.get("SH_APIKEY") and os.environ.get("SH_MODEL")
    ):
        pytest.skip("SH_BASEURL / SH_APIKEY / SH_MODEL not set")
    evidence = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    code = main(
        [
            "demo",
            "--scenario",
            "approval-action",
            "--provider",
            "env",
            "--unpriced",
            "--evidence-dir",
            str(evidence),
            "--test-timeout",
            "120",
            "--as",
            "demo-operator",
        ]
    )
    capsys.readouterr()
    report = json.loads((evidence / "test-report.json").read_text(encoding="utf-8"))
    summary = {
        "model": os.environ["SH_MODEL"],
        "exit_code": code,
        "status": report["status"],
        "stop_reason": report["stop_reason"],
        "elapsed_seconds": report["elapsed_seconds"],
        "waiting_on": report["waiting_on"],
        "chain": report["chain"],
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    assert find_secrets(text) == []
    print("REAL_APPROVAL_REPORT " + text)
    assert report["status"] == "COMPLETED", text
    [action] = [a for a in report["chain"]["actions"] if a["state"] == "SUCCEEDED"]
    assert action["handoffs"] >= 1 and action["receipt_hash"] and action["decision_receipts"]
    assert report["chain"]["service"]["config"] == {"feature_flags.new_ui": "on"}
