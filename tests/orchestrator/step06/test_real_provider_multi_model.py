# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · real multi-pool load report (opt-in ``--run-real-provider``).

Two Missions at once through ``demo --scenario multi-mission --provider env``: two
execution pools of the model named in ``SH_MODEL`` (this program's real runs use
deepseek-flash for both, per the user's instruction), Workers on ``small``, Planner /
Manager / Critic on ``large``, escalation small → large on failure, a bounded
verification queue.  The key comes from the environment and is never printed; the
report line carries ids, profiles, echoed models and outcomes only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_orchestrator.__main__ import main
from agent_orchestrator.observability.secrets import find_secrets

pytestmark = pytest.mark.real_provider


def test_real_multi_mission_on_two_flash_pools(tmp_path, capsys):
    if not (
        os.environ.get("SH_BASEURL") and os.environ.get("SH_APIKEY") and os.environ.get("SH_MODEL")
    ):
        pytest.skip("SH_BASEURL / SH_APIKEY / SH_MODEL not set")
    evidence = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    code = main(
        [
            "demo",
            "--scenario",
            "multi-mission",
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
    summary = json.loads((evidence / "multi-mission.json").read_text(encoding="utf-8"))
    model = os.environ["SH_MODEL"]
    report = {
        "model": model,
        "exit_code": code,
        "elapsed_seconds": summary["elapsed_seconds"],
        "missions": [
            {
                "mission_id": m["mission_id"],
                "status": m["status"],
                "stop_reason": m["stop_reason"],
                "tasks": len(m["tasks"]),
                "attempts": [
                    {
                        k: a[k]
                        for k in (
                            "attempt_id",
                            "status",
                            "runtime_profile_id",
                            "echoed_models",
                            "retry_of",
                            "failure",
                            "only_in_own_pool",
                        )
                    }
                    for a in m["attempts"]
                ],
                "services": [
                    {k: s[k] for k in ("kind", "runtime_profile_id")} for s in m["services"]
                ],
            }
            for m in summary["missions"]
        ],
        "global_account": {
            k: summary["global_account"][k]
            for k in ("settled_tokens", "reserved_tokens", "attempts_created")
        },
        "backpressure_log": (summary["backpressure"] or {}).get("log"),
        "profile_health": summary["profile_health"],
    }
    print("REAL_MULTI_MODEL_REPORT", json.dumps(report, ensure_ascii=False))
    # every Attempt ran on a configured pool and its echo is the configured model (no silent substitution)
    for mission in summary["missions"]:
        for attempt in mission["attempts"]:
            assert attempt["runtime_profile_id"] in {"small", "large"}
            if attempt["echoed_models"]:
                assert attempt["echoed_models"] == [model]
            # review P1-7: both pools echo the same model name, so the physical route is proven
            # by the Agent living only in its own pool's execution library
            if attempt["only_in_own_pool"] is not None:
                assert attempt["only_in_own_pool"] is True
        assert all(
            s["runtime_profile_id"] == "large"
            for s in mission["services"]
            if s["kind"] in {"plan", "critic", "manager"}
        )
    assert summary["global_account"]["reserved_tokens"] == 0
    for path in evidence.rglob("*"):  # every file, every pattern
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert find_secrets(text) == [], path.name
    assert all(m["status"] == "COMPLETED" for m in summary["missions"]), report
