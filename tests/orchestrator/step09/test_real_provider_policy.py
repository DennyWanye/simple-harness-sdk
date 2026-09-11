# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 9 · real model gate (opt-in ``--run-real-provider``).

A person's candidate is evaluated against the ACTIVE version on the ``parse-kv`` case
with the model named in ``SH_MODEL`` (this program's real runs use deepseek-flash, per
the user's instruction), two trials per side.  The gates ask for three samples per case
and per side on real evidence, so the honest verdict is INSUFFICIENT — the report says
so and nothing can be promoted on it.  The key comes from the environment and is never
printed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_orchestrator.__main__ import main
from agent_orchestrator.api.policies import PolicyApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import resolve_params
from agent_orchestrator.observability.secrets import find_secrets
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import Store

pytestmark = pytest.mark.real_provider


def test_real_gate_on_flash_is_honest_about_its_samples(tmp_path, capsys):
    if not (
        os.environ.get("SH_BASEURL") and os.environ.get("SH_APIKEY") and os.environ.get("SH_MODEL")
    ):
        pytest.skip("SH_BASEURL / SH_APIKEY / SH_MODEL not set")
    root = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    production = root / "production"
    config = OrchestratorConfig(
        evidence_root=production, model=os.environ["SH_MODEL"], max_concurrency=1
    )
    store = Store.open(config.orchestrator_db)
    commit = CommitService(store)
    commit.set_library_role("production")
    commit.seed_policy(resolve_params(config), detail={"config": "real gate test"})
    proposal = PolicyApi(commit, Principal("tester"), max_concurrency=1).propose(
        {"no_progress_limit": 3}, note="真实门槛试验"
    )
    store.close()
    code = main(
        [
            "policy",
            "evaluate",
            proposal["proposal_id"],
            "--evidence-dir",
            str(production),
            "--eval-dir",
            str(root / "evaluation"),
            "--provider",
            "env",
            "--unpriced",
            "--trials",
            "2",
            "--case",
            "parse-kv",
            "--test-timeout",
            "120",
            "--timeout",
            "1800",
        ]
    )
    printed = capsys.readouterr().out
    gate = json.loads((root / "evaluation" / "gate.json").read_text(encoding="utf-8"))
    report = json.loads((root / "evaluation" / "evaluation.json").read_text(encoding="utf-8"))
    summary = {
        "model": os.environ["SH_MODEL"],
        "exit_code": code,
        "verdict": gate["verdict"],
        "reasons": gate["reasons"],
        "evidence_kind": gate["evidence_kind"],
        "runs": [
            {
                k: r.get(k)
                for k in (
                    "strategy",
                    "trial",
                    "mission_id",
                    "category",
                    "wall_seconds",
                    "tokens",
                    "attempts",
                    "oracle",
                )
            }
            for r in report["runs"]
        ],
        "per_case": report["comparisons"][0]["per_case"],
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
    assert find_secrets(text) == [] and find_secrets(printed) == []
    print("REAL_POLICY_GATE " + text)
    assert gate["evidence_kind"] == "real" and len(report["runs"]) == 4
    assert gate["verdict"] == "INSUFFICIENT" and code == 1  # two samples per side decide nothing
    assert any("至少 3" in r or "脚手架错误" in r for r in gate["reasons"])
