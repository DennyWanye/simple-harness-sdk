# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · S2-01 on a real model (ORCH §14.2 layer 3; opt in with ``--run-real-provider``).

The Planner, Worker and Critic are real LLM calls; the tools and pytest are real.
The assertion is about the *closure* (a verified, delivered result or an honest,
visible stop), never about the exact wording of a stochastic model.  Credentials
come from ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` or the Host ``.env`` and never
reach the evidence directory.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from real_provider_config import build_real_provider, resolve_real_provider

from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import DEMO_SEED

pytestmark = pytest.mark.real_provider


def test_real_single_task_closure(tmp_path):
    config = resolve_real_provider()
    if config is None:
        pytest.skip("no real provider configured (SH_BASEURL/SH_APIKEY or Host .env)")
    provider = build_real_provider(config, timeout=240.0)
    evidence = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    orchestrator_config = OrchestratorConfig(
        evidence_root=evidence,
        model=config.model,
        max_concurrency=1,
        max_concurrent_model_calls=1,
        default_max_output_tokens=4096,
        test_timeout_seconds=120,
        turn_deadline_seconds=600,
    )
    spec = MissionSpec(
        goal="在隔离工作区实现字符串解析函数 parse_kv(text) -> dict，键值对以 ';' 分隔、键和值以 '=' 分隔，空字符串返回空字典；必须通过 tests/test_parse_kv.py",
        success_criteria=("pytest:tests/test_parse_kv.py", "空字符串输入返回空字典"),
        tenant_id="real",
        idempotency_key=f"real-{os.getpid()}",
        allowed_tools=(
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ),
        budget=Budget(max_tokens=600_000, max_attempts=3),
        workspace_seed=DEMO_SEED,
    )

    async def case():
        async with Orchestrator(
            orchestrator_config, provider, critic_wait_seconds=600
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None
            report = {
                "model": config.model,
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "progress": orchestrator.progress_log,
            }
            write_evidence(
                directory=evidence,
                store=orchestrator.store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline={
                    "model": config.model,
                    "spec": spec.to_json(),
                    "config": orchestrator_config.to_json(),
                },
                workspaces_root=orchestrator_config.workspaces_root,
                test_report=report,
            )
            print("REAL_SINGLE_TASK_REPORT " + json.dumps(report, ensure_ascii=False))
            assert final.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}
            assert final.stop_reason is not None
            if final.status is MissionStatus.COMPLETED:
                task = orchestrator.store.list_tasks(mission.id)[0]
                accepted = orchestrator.store.get_artifact(task.accepted_artifacts[0])
                assert accepted is not None and Path(accepted.storage_uri).is_file()
            return final

    final = asyncio.run(case())
    assert final.status is MissionStatus.COMPLETED, (
        f"real run stopped: {final.stop_reason} — {final.final_report}"
    )
