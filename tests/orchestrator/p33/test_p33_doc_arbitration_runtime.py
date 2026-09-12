"""E05/E06/E07: real document conflict reaches a human on its first attempt."""

import asyncio

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


@pytest.mark.parametrize(
    "ruling", ["contextual", "keep", "unresolved", "hard_fail", "legacy_review", "legacy_reject"]
)
def test_document_conflict_first_human_wait_reopens_and_rules_without_upgrading(
    tmp_path, ruling, monkeypatch
):
    versions = {}
    quotes = {"a": "资料甲适用室内环境。", "b": "资料乙适用室外环境。"}

    def cited(body, key):
        selected = "ab" if key == "arb" else key
        return {
            **body,
            "evidence": [],
            "claims": [
                {
                    "content": "实际离线能力需要人核查。",
                    "confidence": 0.8,
                    "key": "world.offline",
                    "stance": "refutes" if key == "b" else "affirms",
                    "citations": [
                        {
                            "path": f"sources/{k}.md",
                            "version": versions[k],
                            "start_line": 1,
                            "end_line": 1,
                            "quote": quotes[k],
                        }
                        for k in selected
                    ],
                }
            ],
        }

    def work(key):
        return [
            ("workspace_read_file", {"path": f"sources/{key}.md"}),
            ("workspace_write_file", {"path": f"{key}.md", "content": f"分析 {key}。"}),
            envelope_step(
                summary="候选世界陈述",
                artifacts=[f"{key}.md"],
                claims=["主张"],
                override=lambda body: cited(body, key),
            ),
        ]

    provider = RoleScriptedProvider(
        {
            "worker": work("a") + work("b"),
            "arbiter": [
                ("workspace_read_file", {"path": "sources/a.md"}),
                ("workspace_read_file", {"path": "sources/b.md"}),
                (
                    "workspace_write_file",
                    {
                        "path": "arbitration/world.offline/report.md",
                        "content": "条件不同，请人工审阅。",
                    },
                ),
                envelope_step(
                    summary="交由人判断",
                    artifacts=["arbitration/world.offline/report.md"],
                    claims=["建议"],
                    override=lambda body: cited(body, "arb"),
                ),
            ],
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )
    if ruling == "hard_fail":
        provider.scripts["arbiter"] *= 2
        provider.scripts["critic"] = [
            critic_step(verdict="FAIL", criteria_met=False, blocker="仲裁材料缺少必要依据")
        ] * 2

    async def drive(orch, task):
        assert await orch._next_attempt(
            orch.store.get_mission(task.mission_id), task, orch.store.list_attempts(task.id)
        )
        attempt = orch.store.list_attempts(task.id)[-1]
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
        stored = orch.store.find_result_for_attempt(attempt.id)
        assert stored is not None, "\n".join(orch.progress_log[-10:])
        assert await asyncio.wait_for(orch._verify(stored.envelope.id), 15)
        return stored.envelope.id

    async def case():
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider, owner="e-human") as first:
            mission = await first.submit_mission(
                spec(
                    domain=DOC_DOMAIN,
                    success_criteria=("file:b.md",),
                    conflict_reserve_tokens=20000,
                )
            )
            api = MissionControlV1(first, tenant_id=mission.tenant_id, principal=Principal("human"))
            for key, quote in quotes.items():
                api.register_source(
                    {
                        "mission_id": mission.id,
                        "path": f"sources/{key}.md",
                        "content": quote + "\n",
                        "kind": "markdown",
                        "idempotency_key": key,
                    }
                )
                versions[key] = first.store.get_source(mission.id, f"sources/{key}.md")[
                    "version_hash"
                ]
            planning = first.commit.begin_planning(mission.id)
            first.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node("A", success_criteria=["file:a.md", "cite:sources/a.md"]),
                            node("B", ["A"], success_criteria=["file:b.md", "cite:sources/b.md"]),
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            for task in first.store.list_tasks(mission.id):
                await drive(first, first.store.get_task(task.id))
                assert first.store.get_task(task.id).status is TaskStatus.COMPLETED
            [conflict] = first.store.list_conflicts(mission.id)
            assert conflict["state"] == "OPEN"
            conflict_task = first.store.get_task(conflict["task_id"])
            assert (
                "human_review" in conflict_task.verification_policy
                and "code_test" not in conflict_task.verification_policy
            )
            if ruling.startswith("legacy_"):
                # Simulate D's published writer branch; actual SDK checks and decisions.
                with monkeypatch.context() as patch:
                    patch.setattr(first.commit, "_is_document_conflict", lambda task: False)
                    rid = await drive(first, conflict_task)
            else:
                rid = await drive(first, conflict_task)
            if ruling == "hard_fail":
                assert first.store.get_result(rid).verdict == "FAIL"
                rid = await drive(first, first.store.get_task(conflict_task.id))
                assert first.store.get_result(rid).verdict == "FAIL"
                assert await first._next_attempt(
                    first.store.get_mission(mission.id),
                    first.store.get_task(conflict_task.id),
                    first.store.list_attempts(conflict_task.id),
                )
                assert first.store.list_approvals(mission.id) == []
                assert first.store.get_task(conflict_task.id).status is TaskStatus.FAILED
                assert len(first.store.list_attempts(conflict_task.id)) == 2
                assert provider.by_role == {"worker": 6, "arbiter": 8, "critic": 2}
                return
            assert first.store.get_result(rid).verification_state == "SUSPENDED"
            [request] = first.store.list_approvals(mission.id)
            if ruling.startswith("legacy_"):
                assert request["kind"] == "review"
                api.decide(
                    request["request_id"],
                    "review_fail" if ruling == "legacy_reject" else "review_pass",
                    note="旧版普通审核",
                    nonce="legacy-decision",
                )
            else:
                assert (request["kind"], request.get("topic")) == ("arbitration", "conflict")
                assert "contextual" in request["options"]
            side_ids = list(conflict["claim_ids"])
            side_before = {cid: first.store.get_claim(cid).to_json() for cid in side_ids}
            assert all(row["status"] == "DISPUTED" for row in side_before.values())
            assert first.store.list_knowledge(mission.id) == []
            assert len(first.store.list_attempts(conflict_task.id)) == 1
            assert provider.by_role == {"worker": 6, "arbiter": 4, "critic": 1}
        idle = RoleScriptedProvider({})
        async with Orchestrator(config, idle, owner="e-human") as second:
            if ruling.startswith("legacy_"):
                await second.recover()
                assert await asyncio.wait_for(second._verify(rid), 15)
                if ruling == "legacy_reject":
                    assert second.store.get_result(rid).verdict == "FAIL"
                    assert not any(
                        r["kind"] == "arbitration" for r in second.store.list_approvals(mission.id)
                    )
                    assert idle.by_role == {}
                    return
                pending = [
                    r for r in second.store.list_approvals(mission.id) if r["state"] == "PENDING"
                ]
                assert len(pending) == 1 and pending[0]["kind"] == "arbitration"
                request = pending[0]
                assert second.store.get_result(rid).verification_state == "SUSPENDED"
                assert second.store.get_task(conflict_task.id).accepted_result_id is None
            else:
                await asyncio.wait_for(second.run(), 15)
            assert second.store.get_approval(request["request_id"])["state"] == "PENDING"
            api = MissionControlV1(
                second, tenant_id=mission.tenant_id, principal=Principal("reviewer")
            )
            choice = (
                "keep:" + side_ids[0]
                if ruling == "keep"
                else "contextual"
                if ruling == "legacy_review"
                else ruling
            )
            api.decide(
                request["request_id"],
                "arbitrate",
                ruling=choice,
                basis="核对双方原文，仅在其条件内适用。",
                nonce="ruling",
            )
            events = len(second.store.list_events(mission.id))
            api.decide(
                request["request_id"],
                "arbitrate",
                ruling=choice,
                basis="核对双方原文，仅在其条件内适用。",
                nonce="ruling",
            )
            assert len(second.store.list_events(mission.id)) == events
            assert {cid: second.store.get_claim(cid).to_json() for cid in side_ids} == side_before
            assert second.store.list_knowledge(mission.id) == []
            assert second.store.get_task(conflict_task.id).accepted_result_id is None
            if ruling != "unresolved":
                assert (
                    second.store.get_conflict(conflict["conflict_id"])["state"]
                    == "RESOLVED_BY_HUMAN"
                )
                assert second.store.get_task(conflict_task.id).status is TaskStatus.CANCELLED
                tasks = {t.id: t for t in second.store.list_tasks(mission.id)}
                gathered = second._gather_knowledge(
                    second.store.get_mission(mission.id), tasks[conflict_task.id], tasks
                )
                assert all(
                    "human_arbitration" in c for c in gathered.disputed if c["claim_id"] in side_ids
                )
            assert idle.by_role == {}

    asyncio.run(case())
