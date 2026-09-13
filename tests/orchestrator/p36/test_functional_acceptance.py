# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P36 functional sidecar: one uncovered public evidence-export boundary."""

from __future__ import annotations

import asyncio

from helpers_step06 import config, only, spec

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.observability.secrets import find_secrets
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import demo_dynamic_dag_provider


def test_p36_a08_support_evidence_redacts_test_report_payload(tmp_path, monkeypatch):
    """The public evidence writer must redact a support report before it is persisted."""

    canary = "fixture-support-secret-0123456789"
    provider = demo_dynamic_dag_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p36-a08-report", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            monkeypatch.setenv("SH_APIKEY", canary)
            evidence = tmp_path / "support-evidence"
            receipt = write_evidence(
                directory=evidence,
                store=orchestrator.store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline={"note": "clean"},
                workspaces_root=orchestrator.assembled.workspaces.root,
                test_report={"provider_failure": {"detail": canary}},
            )
            return evidence, receipt

    evidence, receipt = asyncio.run(case())
    assert {"file": "test-report.json", "patterns": ["env_value"]} in receipt["redactions"]
    assert canary not in (evidence / "test-report.json").read_text(encoding="utf-8")
    for path in evidence.rglob("*"):
        if path.is_file():
            assert find_secrets(path.read_text(encoding="utf-8"), extra=[canary]) == [], path
