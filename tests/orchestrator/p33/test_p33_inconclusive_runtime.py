"""D07/D08: real dispatch, CAS, verification, acceptance and Mission judgment."""

import asyncio
import json

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step
from graph_helpers7 import node, spec

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import Store


@pytest.mark.parametrize(
    "total,uncertain,extra,expected,root_arbitration",
    [
        (3, 1, 0, "COMPLETED", False),
        (2, 1, 0, "COMPLETED", False),
        (3, 2, 0, "FAILED", False),
        (3, 2, 20, "FAILED", False),
        (1, 0, 0, "COMPLETED", True),
    ],
)
def test_mission_uncertainty_uses_original_denominator_before_judge(
    tmp_path, total, uncertain, extra, expected, root_arbitration
):
    from agent_orchestrator.verification.assessments import (
        criterion_id,
        mission_criterion_catalog,
        task_contract_revision,
    )
    from agent_orchestrator.verification.mission_coverage import mission_coverage

    path = "sources/a.md"
    criteria = tuple(f"方案在环境 {i} 可运行。" for i in range(total))
    quotes = [
        f"来源对环境 {i} 尚无明确结论。" if i < uncertain else criteria[i] for i in range(total)
    ]
    version, task_ids, mission_ids = "", [], []

    def output(body):
        claims, limitations = [], []
        for i, quote in enumerate(quotes):
            proposal = {
                "content": quote,
                "confidence": 1.0,
                "citations": [
                    {
                        "path": path,
                        "version": version,
                        "start_line": i + 1,
                        "end_line": i + 1,
                        "quote": quote,
                    }
                ],
            }
            if i < uncertain:
                proposal.update(criterion_ids=[task_ids[i]], mission_criterion_ids=[mission_ids[i]])
                limitations.append(
                    {
                        "criterion_id": task_ids[i],
                        "claim_id": f"claim:{i + 1}",
                        "missing": f"缺少环境 {i} 的实际运行依据",
                    }
                )
            claims.append(proposal)
        return {**body, "claims": claims, "limitations": limitations, "evidence": []}

    provider = RoleScriptedProvider(
        {
            "critic": [
                "<critic_verdict>"
                + json.dumps(
                    {
                        "verdict": "PASS",
                        "findings": [],
                        "mission_criteria": [
                            {
                                "criterion": "arbitration:topic",
                                "met": True,
                                "reason": "independent fixture judgment",
                            }
                        ],
                    }
                )
                + "</critic_verdict>"
            ]
            if root_arbitration
            else [],
            "worker": [
                ("workspace_read_file", {"path": path}),
                (
                    "workspace_write_file",
                    {"path": "report.md", "content": "分析与局限另由系统核验。"},
                ),
                *[
                    ("workspace_write_file", {"path": f"extra-{i}.md", "content": "small item"})
                    for i in range(extra)
                ],
                envelope_step(
                    summary="提交来源与局限",
                    artifacts=["report.md"],
                    claims=["待核验"],
                    override=output,
                ),
            ],
        }
    )

    async def run():
        nonlocal version, task_ids, mission_ids
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(
                spec(
                    domain=DOC_DOMAIN,
                    success_criteria=("arbitration:topic",) if root_arbitration else criteria,
                )
            )
            api = MissionControlV1(orch, tenant_id=mission.tenant_id, principal=Principal("human"))
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": path,
                    "content": "\n".join(quotes) + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            version = orch.store.get_source(mission.id, path)["version_hash"]
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                outputs=["report.md"],
                                success_criteria=[
                                    *criteria,
                                    *[f"file:extra-{i}.md" for i in range(extra)],
                                ],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            task = tasks[0]
            revision = task_contract_revision(task.to_json())
            task_ids = [criterion_id(revision, i + 1, text) for i, text in enumerate(criteria)]
            mission_ids = [item["criterion_id"] for item in mission_criterion_catalog(mission)]
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
            stored = orch.store.find_result_for_attempt(attempt.id)
            assert stored is not None, orch.progress_log[-10:]
            assert await asyncio.wait_for(orch._verify(stored.envelope.id), 15)
            task = orch.store.get_task(task.id)
            assert task.status is TaskStatus.COMPLETED, orch.progress_log[-10:]
            before = mission_coverage(orch.store, mission, orch.commit.domain_for(mission.id))
            assert before["numerator"] == uncertain and before["denominator"] == total
            assert before["insufficient"] is (uncertain / total > 0.5)

            async def forbidden_critic(*args, **kwargs):
                pytest.fail("document deterministic Mission criteria must precede judge Critic")

            if not root_arbitration:
                orch._run_critic = forbidden_critic
            assert await orch._decide(orch.store.get_mission(mission.id))
            final = orch.store.get_mission(mission.id)
            assert str(final.status) == expected
            assert final.final_report["document_coverage"]["hash"] == before["hash"]
            if final.status is MissionStatus.FAILED:
                assert final.stop_reason == "insufficient_evidence"
                assert final.final_report["result"] == "INSUFFICIENT"
            else:
                assert all(item["met"] for item in final.final_report["success_criteria"])
            expected_calls = {"worker": 3 + extra, **({"critic": 1} if root_arbitration else {})}
            assert provider.by_role == expected_calls
            if root_arbitration:
                assert final.final_report["success_criteria"][0]["judge"] == "critic_review"
            database, mid = orch.store.path, mission.id
            original = final.to_json()
            domain = orch.commit.domain_for(mid)
        reopened = Store.open_readonly(database)
        try:
            assert reopened.get_mission(mid).to_json() == original
            assert (
                mission_coverage(reopened, reopened.get_mission(mid), domain)["hash"]
                == before["hash"]
            )
        finally:
            reopened.close()

    asyncio.run(run())


@pytest.mark.parametrize("tamper_binding", [False, True], ids=["human-reopen", "damaged-binding"])
def test_adapter_human_review_survives_actual_runtime_close_and_reopen(
    tmp_path, monkeypatch, tamper_binding
):
    from helpers_step07 import ALICE

    from agent_orchestrator.verification import adapters
    from agent_orchestrator.verification.assessments import criterion_id, task_contract_revision

    path, quote = "sources/a.md", "资料未报告真实生产结果。"
    version, candidate_id = "", ""

    def output(body):
        return {
            **body,
            "evidence": [],
            "claims": [
                {
                    "content": quote,
                    "confidence": 1.0,
                    "criterion_ids": [candidate_id],
                    "citations": [
                        {
                            "path": path,
                            "version": version,
                            "start_line": 1,
                            "end_line": 1,
                            "quote": quote,
                        }
                    ],
                }
            ],
            "limitations": [
                {"criterion_id": candidate_id, "claim_id": "claim:1", "missing": "缺少真实生产验证"}
            ],
        }

    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_read_file", {"path": path}),
                ("workspace_write_file", {"path": "report.md", "content": "分析及局限待审阅。"}),
                envelope_step(
                    summary="证据不足", artifacts=["report.md"], claims=[quote], override=output
                ),
            ]
        }
    )
    config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)

    async def run():
        nonlocal version, candidate_id
        async with Orchestrator(config, provider, owner="d-human") as first:
            mission = await first.submit_mission(
                spec(domain=DOC_DOMAIN, success_criteria=("file:report.md",))
            )
            api = MissionControlV1(first, tenant_id=mission.tenant_id, principal=Principal("human"))
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": path,
                    "content": quote + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            version = first.store.get_source(mission.id, path)["version_hash"]
            planning = first.commit.begin_planning(mission.id)
            tasks, _ = first.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                outputs=["report.md"],
                                success_criteria=["确认真实生产适用性。"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            task = tasks[0]
            candidate_id = criterion_id(
                task_contract_revision(task.to_json()), 1, task.success_criteria[0]
            )
            assert await first._next_attempt(first.store.get_mission(mission.id), task, [])
            [attempt] = first.store.list_attempts(task.id)
            intent = first.store.get_intent_for_subject(attempt.id)
            assert await first._dispatch(intent)
            intent = first.store.get_intent(intent.intent_id)

            async def completed():
                while True:
                    result = await first.bridge_for(intent).result(
                        agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.01)

            await first._collect_attempt(intent, await asyncio.wait_for(completed(), 15))
            stored = first.store.find_result_for_attempt(attempt.id)
            if tamper_binding:
                frozen = first.store.get_intent(intent.intent_id)
                bad = dict(frozen.config)
                for key in ("check_spec_ids", "mission_contract_revision", "mission_criteria"):
                    bad.pop(key)
                # Fault injection: normal CAS deliberately cannot mutate frozen config.
                with first.store.transaction() as connection:
                    connection.execute(
                        "UPDATE dispatch_intents SET config_json = ? WHERE intent_id = ?",
                        (json.dumps(bad), frozen.intent_id),
                    )
                assert await first._verify(stored.envelope.id)
                rule = next(
                    r
                    for r in first.store.list_verifications(stored.envelope.id)
                    if r["layer"] == "rule_check"
                )
                assert rule["status"] == "ERROR"
                assert first.store.get_result(stored.envelope.id).verdict == "FAIL"
                assert first.store.list_criterion_assessments(mission.id) == []
                return
            with monkeypatch.context() as patch:
                patch.setattr(adapters, "coverage_verdict", lambda **kwargs: "NEEDS_HUMAN")
                assert await asyncio.wait_for(first._verify(stored.envelope.id), 15)
            rid = stored.envelope.id
            assert first.store.get_result(rid).verification_state == "SUSPENDED"
            [request] = first.store.list_approvals(mission.id)
            rule_before = next(
                v for v in first.store.list_verifications(rid) if v["layer"] == "rule_check"
            )
            assert rule_before["status"] == "NEEDS_HUMAN"
            assert provider.by_role == {"worker": 3}
        idle_provider = RoleScriptedProvider({})
        async with Orchestrator(config, idle_provider, owner="d-human") as second:
            await asyncio.wait_for(second.run(), 15)
            assert second.store.get_result(rid).verification_state == "SUSPENDED"
            assert second.store.get_task(task.id).status is TaskStatus.VERIFYING
            second.commit.review_result(
                request["request_id"],
                principal=ALICE,
                verdict="pass",
                note="局限已核对",
                nonce="d-human",
            )

            def forbidden(**kwargs):
                pytest.fail(
                    "resuming an authenticated coverage receipt must not rerun its decision"
                )

            with monkeypatch.context() as patch:
                patch.setattr(adapters, "coverage_verdict", forbidden)
                await asyncio.wait_for(second.run(), 15)
            assert second.store.get_task(task.id).status is TaskStatus.COMPLETED
            assert second.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            [claim] = second.store.list_claims(rid)
            assert claim.confidence_metadata["grade"] == "insufficient_evidence"
            rule_after = next(
                v for v in second.store.list_verifications(rid) if v["layer"] == "rule_check"
            )
            assert rule_after == rule_before
            assert idle_provider.by_role == {}

    asyncio.run(run())


def test_actual_runtime_stops_missing_limitations_after_one_rework_without_manager(tmp_path):
    from agent_orchestrator.verification.assessments import criterion_id, task_contract_revision

    path, quote, version, candidate = "sources/a.md", "现有资料尚未验证生产效果。", "", ""

    def output(body):
        return {
            **body,
            "evidence": [],
            "claims": [
                {
                    "content": quote,
                    "confidence": 0.5,
                    "criterion_ids": [candidate],
                    "citations": [
                        {
                            "path": path,
                            "version": version,
                            "start_line": 1,
                            "end_line": 1,
                            "quote": quote,
                        }
                    ],
                }
            ],
        }

    steps = [
        ("workspace_read_file", {"path": path}),
        ("workspace_write_file", {"path": "report.md", "content": "尚需补证据"}),
        envelope_step(
            summary="缺少局限字段", artifacts=["report.md"], claims=[quote], override=output
        ),
    ]
    provider = RoleScriptedProvider({"worker": steps * 2})

    async def run():
        nonlocal version, candidate
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(
                spec(domain=DOC_DOMAIN, success_criteria=("file:report.md",))
            )
            api = MissionControlV1(orch, tenant_id=mission.tenant_id, principal=Principal("human"))
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": path,
                    "content": quote + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            version = orch.store.get_source(mission.id, path)["version_hash"]
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                outputs=["report.md"],
                                success_criteria=["确认真实生产适用性。"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            task = tasks[0]
            candidate = criterion_id(
                task_contract_revision(task.to_json()), 1, task.success_criteria[0]
            )
            await asyncio.wait_for(orch.run(), 20)
            final = orch.store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED, orch.progress_log[-12:]
            assert final.stop_reason == "insufficient_evidence"
            attempts = orch.store.list_attempts(task.id)
            assert len(attempts) == 2
            assert all(a.failure["reason"] == "inconclusive" for a in attempts)
            assert attempts[1].retry_of == attempts[0].id
            assert provider.by_role == {"worker": 6}
            assert not any(i.kind == "manager" for i in orch.store.list_intents(mission.id))
            events = orch.store.count_events(mission.id)
            await asyncio.wait_for(orch.run(), 5)
            assert orch.store.count_events(mission.id) == events

    asyncio.run(run())
