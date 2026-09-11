# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 5 · the recorder Mission on a real model (ORCH §14.2 layer 3; opt in with
``--run-real-provider``): the Planner plans, the Workers execute, and whenever a Worker
reports blocked / no_progress / failure / proposed sub-tasks the real Manager may change
the formal graph through the same Commit path.  The evidence needed (plan §5 附加门槛):
the Mission completes and any graph change (if the model triggered one) kept the
completed Tasks intact.  Credentials come from ``SH_BASEURL`` / ``SH_APIKEY`` /
``SH_MODEL`` and are never printed."""

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
from agent_orchestrator.testing.fixtures import RECORDER_SEED, RECORDER_SPEC

pytestmark = pytest.mark.real_provider


def test_real_dynamic_dag_closure(tmp_path):
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
        attempt_reserve_tokens=120_000,
        critic_reserve_tokens=30_000,
        manager_reserve_tokens=30_000,
    )
    spec = MissionSpec(
        goal=(
            str(RECORDER_SPEC["goal"])
            + "。做法：先分析输入（analysis.md），再实现 recorder.py，再独立验证（VERIFY.md）与文档检查（DOCS.md）。"
            "任何任务如果缺少必要的前置结论（例如某个格式没有被明确定义），不要猜：返回 outcome=blocked，"
            "并在 proposed_tasks 里提出需要先完成的前置任务，由系统调整计划。tests/ 下的测试不可修改。"
        ),
        success_criteria=tuple(str(c) for c in RECORDER_SPEC["success_criteria"]),
        tenant_id="real",
        idempotency_key=f"real-dyn-{os.getpid()}",
        allowed_tools=tuple(str(t) for t in RECORDER_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=2_000_000, max_attempts=24),
        workspace_seed=RECORDER_SEED,
    )

    async def case():
        async with Orchestrator(
            orchestrator_config, provider, critic_wait_seconds=900
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final is not None
            tasks = store.list_tasks(mission.id)
            events = store.list_events(mission.id)
            report = {
                "model": config.model,
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "graph_version": (final.final_report or {}).get("graph_version"),
                "tasks": [
                    {
                        "task_id": t.id,
                        "kind": t.kind,
                        "goal": t.goal,
                        "dependencies": list(t.dependency_ids),
                        "status": str(t.status),
                        "attempts": t.attempt_count,
                        "role": t.context.get("role", "worker"),
                        "supersedes": t.context.get("supersedes_task"),
                    }
                    for t in tasks
                ],
                "graph_changes": store.list_graph_changes(mission.id),
                "management": [
                    {"type": e.type, "task": e.task_id, "payload": e.payload}
                    for e in events
                    if e.type
                    in {
                        "OutcomeRecorded",
                        "ManagementRequested",
                        "ManagementDecided",
                        "TaskGraphChangeRejected",
                    }
                ],
                "progress": orchestrator.progress_log,
            }
            write_evidence(
                directory=evidence,
                store=store,
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
            print("REAL_DYNAMIC_DAG_REPORT " + json.dumps(report, ensure_ascii=False))
            assert final.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}
            if final.status is MissionStatus.COMPLETED:
                live = [t for t in tasks if t.status is not TaskStatus.CANCELLED]
                assert live and all(t.status is TaskStatus.COMPLETED for t in live)
                for change in store.list_graph_changes(mission.id):
                    for old in change.get("superseded", {}):
                        assert store.get_task(old).status is TaskStatus.CANCELLED
            return final

    final = asyncio.run(case())
    assert final.status is MissionStatus.COMPLETED, (
        f"real run stopped: {final.stop_reason} — {final.final_report}"
    )
