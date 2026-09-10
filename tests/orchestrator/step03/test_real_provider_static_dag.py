# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · S3-01 on a real model (ORCH §14.2 layer 3; opt in with ``--run-real-provider``).

The real Planner must propose a DAG for the textkit Mission and the real Workers must
deliver it through the same Commit / Frontier / verification path as the fixtures.
Credentials come from ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` or the Host ``.env``
and are never printed; the evidence directory is written under ``ORCH_EVIDENCE_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from real_provider_config import build_real_provider, resolve_real_provider

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import DEMO_DAG_SPEC, TEXTKIT_SEED

pytestmark = pytest.mark.real_provider


def test_real_static_dag_closure(tmp_path):
    config = resolve_real_provider()
    if config is None:
        pytest.skip("no real provider configured (SH_BASEURL/SH_APIKEY or Host .env)")
    provider = build_real_provider(config, timeout=300.0)
    evidence = Path(os.environ.get("ORCH_EVIDENCE_DIR", str(tmp_path / "evidence")))
    orchestrator_config = OrchestratorConfig(
        evidence_root=evidence,
        model=config.model,
        max_concurrency=2,
        max_concurrent_model_calls=2,
        default_max_output_tokens=4096,
        test_timeout_seconds=120,
        turn_deadline_seconds=900,
        lease_seconds=120,
        stall_seconds=300,
    )
    spec = MissionSpec(
        goal=(
            str(DEMO_DAG_SPEC["goal"])
            + "。约束：textkit/__init__.py 必须导出 slugify 与 word_count；slugify 把文本转小写、非字母数字替换为 '-'、折叠连续的 '-' 并去掉首尾的 '-'；"
            "word_count 按空白分词计数、空串为 0；tests/ 下的测试文件不可修改；最后写 DELIVERY.md 交付说明。"
        ),
        success_criteria=tuple(str(c) for c in DEMO_DAG_SPEC["success_criteria"]),
        tenant_id="real",
        idempotency_key=f"real-dag-{os.getpid()}",
        allowed_tools=tuple(str(t) for t in DEMO_DAG_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=1_500_000, max_attempts=12),
        workspace_seed=TEXTKIT_SEED,
    )

    async def case():
        async with Orchestrator(
            orchestrator_config, provider, critic_wait_seconds=900
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None
            tasks = orchestrator.store.list_tasks(mission.id)
            report = {
                "model": config.model,
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "tasks": [
                    {
                        "task_id": t.id,
                        "goal": t.goal,
                        "dependencies": list(t.dependency_ids),
                        "outputs": list(t.outputs),
                        "status": str(t.status),
                        "attempts": t.attempt_count,
                    }
                    for t in tasks
                ],
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
            print("REAL_STATIC_DAG_REPORT " + json.dumps(report, ensure_ascii=False))
            assert final.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}
            if final.status is MissionStatus.COMPLETED:
                assert len(tasks) >= 2, "a real Planner should decompose this Mission"
                assert all(t.status is TaskStatus.COMPLETED for t in tasks)
            return final

    final = asyncio.run(case())
    assert final.status is MissionStatus.COMPLETED, (
        f"real run stopped: {final.stop_reason} — {final.final_report}"
    )
