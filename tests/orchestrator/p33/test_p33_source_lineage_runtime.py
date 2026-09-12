"""E03/E04/E09: actual document branches and synthesis, then source invalidation."""

import asyncio
import json

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step
from graph_helpers7 import node, spec

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


@pytest.mark.parametrize("change", ["revoke", "supersede"])
def test_real_synthesis_inherits_upstream_sources_and_context_excludes_stale(tmp_path, change):
    versions = {}
    upstream = []
    source = {key: (f"sources/{key}.md", f"资料 {key} 记载了独立的测试条件。") for key in "abs"}

    def output(key):
        def amend(body):
            path, quote = source[key]
            return {
                **body,
                "used_knowledge": list(upstream) if key == "s" else [],
                "claims": [
                    {
                        "content": quote,
                        "confidence": 1.0,
                        "citations": [
                            {
                                "path": path,
                                "version": versions[key],
                                "start_line": 1,
                                "end_line": 1,
                                "quote": quote,
                            }
                        ],
                    }
                ],
            }

        return amend

    def script(key):
        return [
            ("workspace_read_file", {"path": source[key][0]}),
            ("workspace_write_file", {"path": f"{key}.md", "content": f"分析 {key}。"}),
            envelope_step(
                summary=f"历史分析 {key}。",
                artifacts=[f"{key}.md"],
                claims=[source[key][1]],
                override=output(key),
            ),
        ]

    provider = RoleScriptedProvider(
        {
            "worker": script("a") + script("b"),
            "synthesizer": script("s"),
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )

    async def case():
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(
                spec(
                    domain=DOC_DOMAIN,
                    success_criteria=("file:s.md",),
                    synthesis={
                        "goal": "组合两份资料",
                        "success_criteria": ["file:s.md", "cite:sources/s.md"],
                        "outputs": ["s.md"],
                        "budget": {"max_tokens": 30000, "max_attempts": 2},
                    },
                )
            )
            api = MissionControlV1(orch, tenant_id=mission.tenant_id, principal=Principal("person"))
            for key, (path, quote) in source.items():
                api.register_source(
                    {
                        "mission_id": mission.id,
                        "path": path,
                        "content": quote + "\n",
                        "kind": "markdown",
                        "idempotency_key": key,
                    }
                )
                versions[key] = orch.store.get_source(mission.id, path)["version_hash"]
            planning = orch.commit.begin_planning(mission.id)
            orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                key.upper(),
                                success_criteria=[f"file:{key}.md", f"cite:sources/{key}.md"],
                            )
                            for key in "ab"
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            tasks = orch.store.list_tasks(mission.id)
            assert [t.kind for t in tasks] == ["work", "work", "synthesis"]
            assert "code_test" not in tasks[-1].verification_policy
            assert "critic_review" in tasks[-1].verification_policy
            for original in tasks:
                task = orch.store.get_task(original.id)
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

                await orch._collect_attempt(intent, await asyncio.wait_for(completed(), 15))
                result = orch.store.find_result_for_attempt(attempt.id)
                assert result is not None, orch.progress_log[-10:]
                assert await asyncio.wait_for(orch._verify(result.envelope.id), 15)
                assert orch.store.get_task(task.id).status is TaskStatus.COMPLETED, (
                    orch.progress_log[-10:]
                )
                if task.kind == "work":
                    upstream.append(orch.store.list_claims(result.envelope.id)[0].id)
            records = orch.store.list_knowledge(mission.id)
            synthesis = next(r for r in records if r.source_task == tasks[-1].id)
            assert set(synthesis.dependencies) == set(upstream)
            assert synthesis.to_json().get("source_versions") == {
                source[k][0]: [versions[k]] for k in "abs"
            }

            def snapshot():
                return {
                    "claims": [c.to_json() for c in orch.store.list_mission_claims(mission.id)],
                    "knowledge": [r.to_json() for r in orch.store.list_knowledge(mission.id)],
                    "assessments": orch.store.list_criterion_assessments(mission.id),
                }

            before = snapshot()
            command = {
                "mission_id": mission.id,
                "path": source["a"][0],
                "expected_version_hash": versions["a"],
                "idempotency_key": "change",
            }
            if change == "revoke":
                pending = api.revoke_source({**command, "reason": "资料撤回"})
            else:
                pending = api.supersede_source(
                    {**command, "content": "新版资料。\n", "kind": "markdown"}
                )
            api.decide(pending["request_id"], "approve", nonce="approve-change")
            assert snapshot() == before
            fresh = orch.store.get_mission(mission.id)
            current_tasks = {t.id: t for t in orch.store.list_tasks(mission.id)}
            gathered = orch._gather_knowledge(fresh, current_tasks[tasks[-1].id], current_tasks)
            offered = {r["id"] for r in gathered.verified}
            assert upstream[0] not in offered and synthesis.id not in offered
            assert upstream[1] in offered
            assert "stale" in json.dumps(gathered.retrieval.to_json())
            for summary in (gathered.branch_summary, gathered.global_summary):
                if summary:
                    assert not (
                        {row["id"] for row in summary["knowledge"]} & {upstream[0], synthesis.id}
                    )
                    assert "stale" in json.dumps(summary)
            assert snapshot() == before
            assert provider.by_role == {"worker": 6, "synthesizer": 3, "critic": 1}

    asyncio.run(case())
