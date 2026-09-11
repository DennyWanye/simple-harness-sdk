# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · the knowledge-sharing Mission on a real model (ORCH §14.2 layer 3; opt in with
``--run-real-provider``).  The real Planner proposes the probing Tasks, the real Workers
must submit typed claims backed by their own probe tests, the real Synthesizer must cite
the knowledge it combined and its product must pass ``tests/test_comparison.py``.  The
evidence needed (plan §5): knowledge was reused (``KnowledgeUsed``) and the synthesis was
verified again; a conflict may or may not appear with a real model.  Credentials come
from ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` and are never printed."""

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
from agent_orchestrator.testing.fixtures import COMPARE_SEED, COMPARE_SPEC, COMPARE_SYNTHESIS

pytestmark = pytest.mark.real_provider


def test_real_knowledge_sharing_closure(tmp_path):
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
        default_max_output_tokens=8192,
        max_output_tokens_ceiling=32768,  # flash spends its cap on reasoning; let the escalation reach a usable answer,
        test_timeout_seconds=120,
        turn_deadline_seconds=900,
        lease_seconds=120,
        stall_seconds=300,
        attempt_reserve_tokens=120_000,
        critic_reserve_tokens=30_000,
        # P3.1 fix F-ORCH-1: with these knobs a critic_review Task must hold 38192 tokens;
        # a real Planner may need a third try after reading the floor it was refused for
        max_planning_attempts=3,
    )
    spec = MissionSpec(
        goal=(
            str(COMPARE_SPEC["goal"])
            + "。做法：为 impls/impl_a.py 与 impls/impl_b.py 各写一个探针测试文件（tests/probe/ 下），用 run_tests 运行它们，"
            "把每条合同条款的支持情况作为 claims 提交（每条 claim 给 key 如 impl_a.empty_input、stance affirms/refutes、"
            "evidence 写 pytest:<你运行的测试路径>）；impls/ 与 contract/ 下的文件不可修改；docs/vendor_notes.md 只是参考资料。"
            "最终由综合任务产出 comparison.json 与 COMPARISON.md（comparison.json 结构："
            '{"impl_a": {"basic"|"empty_input"|"trailing_separator"|"missing_equals": "passes"|"fails"}, "impl_b": {...}, "knowledge": [引用的知识 id]}）。'
        ),
        success_criteria=tuple(str(c) for c in COMPARE_SPEC["success_criteria"]),
        tenant_id="real",
        idempotency_key=f"real-ks-{os.getpid()}",
        allowed_tools=tuple(str(t) for t in COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=2_000_000, max_attempts=16),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
        synthesis={**COMPARE_SYNTHESIS, "budget": {"max_tokens": 300_000, "max_attempts": 2}},
        conflict_reserve_tokens=300_000,
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
                "tasks": [
                    {
                        "task_id": t.id,
                        "kind": t.kind,
                        "goal": t.goal,
                        "dependencies": list(t.dependency_ids),
                        "status": str(t.status),
                        "attempts": t.attempt_count,
                    }
                    for t in tasks
                ],
                "knowledge": [
                    {
                        "id": k.id,
                        "status": k.status,
                        "key": k.key,
                        "stance": k.stance,
                        "used_by": list(k.used_by),
                        "verifier": dict(k.verifier),
                    }
                    for k in store.list_knowledge(mission.id)
                ],
                "claims": [
                    {
                        "id": c.id,
                        "status": str(c.status),
                        "key": c.key,
                        "stance": c.stance,
                        "grade": c.confidence_metadata.get("grade"),
                    }
                    for c in store.list_mission_claims(mission.id)
                ],
                "conflicts": store.list_conflicts(mission.id),
                "knowledge_used_events": sum(1 for e in events if e.type == "KnowledgeUsed"),
                "synthesis_attempts": [
                    {"attempt_id": a.id, "status": str(a.status), "failure": a.failure}
                    for t in tasks
                    if t.kind == "synthesis"
                    for a in store.list_attempts(t.id)
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
            print("REAL_KNOWLEDGE_SHARING_REPORT " + json.dumps(report, ensure_ascii=False))
            assert final.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}
            if final.status is MissionStatus.COMPLETED:
                assert all(t.status is TaskStatus.COMPLETED for t in tasks)
                assert any(t.kind == "synthesis" for t in tasks)
                assert store.list_knowledge(mission.id, status="VERIFIED"), (
                    "a real Worker should produce VERIFIED knowledge"
                )
                assert report["knowledge_used_events"] >= 1, (
                    "the synthesis must cite the knowledge it combined"
                )
            return final

    final = asyncio.run(case())
    assert final.status is MissionStatus.COMPLETED, (
        f"real run stopped: {final.stop_reason} — {final.final_report}"
    )
