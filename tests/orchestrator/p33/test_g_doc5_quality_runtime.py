# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A real file PASS must not bypass the requested independent quality review.

The Provider is deterministic; this proves dispatch/evidence/decision wiring,
not a real model's ability to judge reports. Both cases read the actual artifact.
"""

import asyncio
import json

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, package_of
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


@pytest.mark.parametrize("has_risk", [False, True])
def test_doc5_critic_reads_real_artifact_and_can_reject_file_pass(tmp_path, has_risk):
    content = "# 比较报告\n" + (
        "风险：当前材料没有覆盖离线运行。\n" if has_risk else "只有标题。\n"
    )
    goal = "提交比较报告并明确列出风险"

    observed = []

    def judge(request):
        package = package_of(request)
        reads = [
            json.loads(message.content)
            for message in request.messages
            if str(message.role) == "tool"
        ]
        actual = reads[-1]["value"]["content"]
        meets = "风险：" in actual
        observed.append({"package": package, "content": actual})
        body = {
            "verdict": "PASS" if meets else "FAIL",
            "findings": [] if meets else [{"severity": "blocker", "detail": "报告没有列出风险"}],
            "mission_criteria": [
                {"criterion": criterion, "met": True, "reason": "Critic已读取实际报告文件"}
                for criterion in package["mission_success_criteria"]
            ],
        }
        return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"

    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_write_file", {"path": "report.md", "content": content}),
                envelope_step(
                    summary="自述质量完全合格",
                    artifacts=["report.md"],
                    claims=["报告已写入 report.md。"],
                ),
            ],
            "critic": [("workspace_read_file", {"path": "report.md"}), judge],
        }
    )

    async def run():
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), provider) as orch:
            mission = await orch.submit_mission(
                spec(domain=DOC_DOMAIN, goal=goal, success_criteria=("file:report.md",))
            )
            planning = orch.commit.begin_planning(mission.id)
            [task], _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                tokens=60_000,
                                goal=goal,
                                outputs=["report.md"],
                                success_criteria=["file:report.md"],
                                verification_policy=["format_check", "rule_check", "critic_review"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "quality-oracle"},
            )
            assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
            [attempt] = orch.store.list_attempts(task.id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)

            async def completed():
                while True:
                    result = await orch.bridge_for(intent).result(
                        agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.01)

            result = await asyncio.wait_for(completed(), 15)
            await orch._collect_attempt(intent, result)
            saved = orch.store.find_result_for_attempt(attempt.id)
            assert saved is not None, (result, orch.progress_log[-12:])
            assert await asyncio.wait_for(orch._verify(saved.envelope.id), 15)
            verified = orch.store.get_result(saved.envelope.id)
            rules = [
                v
                for v in orch.store.list_verifications(saved.envelope.id)
                if v["layer"] == "rule_check"
            ]
            assert rules[0]["status"] == "PASS", json.dumps(rules, ensure_ascii=False)
            assert verified.verdict == ("PASS" if has_risk else "FAIL"), (
                orch.progress_log[-12:],
                orch.store.list_verifications(saved.envelope.id),
            )
            assert (orch.store.get_task(task.id).status == TaskStatus.COMPLETED) is has_risk
            assert provider.by_role == {"worker": 2, "critic": 2}
            assert goal in json.dumps(observed[0]["package"], ensure_ascii=False)
            assert observed[0]["content"] == content

    asyncio.run(run())
