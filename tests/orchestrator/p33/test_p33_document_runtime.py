"""C01/C02/C03/C08: actual SDK dispatch → file tools → record → verify → accept.

Uses a deterministic Provider, real SQLite and CAS. It does not claim real-model
quality, a finished Mission report or native Host UI acceptance (slice G).
"""

import asyncio
from dataclasses import replace

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step
from graph_helpers7 import node, spec

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.context.retrieval import knowledge_view
from agent_orchestrator.contracts import ClaimStatus, ContractError, TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import Store


@pytest.mark.parametrize(
    "case_name,expected",
    [
        ("quote", "VERIFIED"),
        ("long_quote", "VERIFIED"),
        ("inference", "SUPPORTED"),
        ("reserved_key", "SUPPORTED"),
        ("missing", "UNDER_REVIEW"),
        ("bad_quote", "UNDER_REVIEW"),
        ("one_good_one_bad", "UNDER_REVIEW"),
    ],
)
def test_real_sdk_document_grading_pipeline(tmp_path, case_name, expected):
    path, quote = "sources/a.md", "方案 A 不支持离线。"
    if case_name == "long_quote":
        quote = "甲" * 19_999 + "。"
    version = ""

    def cited(body):
        c = {"path": path, "version": version, "start_line": 1, "end_line": 1, "quote": quote}
        citations = [] if case_name == "missing" else [c]
        if case_name == "bad_quote":
            citations = [{**c, "quote": "方案 A 支持离线。"}]
        if case_name == "one_good_one_bad":
            citations.append({**c, "quote": "方案 A 支持离线。"})
        return {
            **body,
            "evidence": [],
            "claims": [
                {
                    "content": "方案 A 支持离线。"
                    if case_name in {"inference", "reserved_key"}
                    else quote,
                    "confidence": 1.0,
                    "type": "attribution",
                    "key": f"attribution:{version}:1-1"
                    if case_name == "reserved_key"
                    else "world.offline",
                    "stance": "refutes",
                    "citations": citations,
                }
            ],
        }

    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_read_file", {"path": path}),
                (
                    "workspace_write_file",
                    {"path": "report.md", "content": "# 分析\n待系统核验结论。\n"},
                ),
                envelope_step(
                    summary="已写分析", artifacts=["report.md"], claims=[quote], override=cited
                ),
            ]
        }
    )

    async def case():
        nonlocal version
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(
                spec(domain=DOC_DOMAIN, success_criteria=("file:report.md",))
            )
            api = MissionControlV1(
                orch, tenant_id=mission.tenant_id, principal=Principal("importer")
            )
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
                                success_criteria=["file:report.md", "cite:" + path],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            assert await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
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

            result = await asyncio.wait_for(completed(), timeout=15)
            await orch._collect_attempt(intent, result)
            stored = orch.store.find_result_for_attempt(attempt.id)
            assert stored is not None, orch.progress_log[-10:]
            original = stored.envelope.to_json()
            assert await asyncio.wait_for(orch._verify(stored.envelope.id), timeout=15)
            saved = orch.store.get_result(stored.envelope.id)
            [claim] = orch.store.list_claims(stored.envelope.id)
            assert str(claim.status) == expected, (claim.to_json(), orch.progress_log[-10:])
            assert saved.envelope.to_json() == original
            rows = orch.store.list_criterion_assessments(mission.id, result_id=stored.envelope.id)
            if expected == "UNDER_REVIEW":
                assert saved.verdict == "FAIL"
                assert rows == [] and orch.store.list_knowledge(mission.id) == []
                assert claim.confidence_metadata["grade"] == "unsupported"
            else:
                assert saved.verdict == "PASS" and rows
                assert orch.store.get_task(tasks[0].id).status is TaskStatus.COMPLETED
                if expected == "VERIFIED":
                    [knowledge] = orch.store.list_knowledge(mission.id)
                    assert knowledge.key == f"attribution:{version}:1-1"
                    assert knowledge.stance == "affirms" and quote in knowledge.content
                    assert knowledge_view(knowledge)["source_trust"] == "untrusted_external"
                    assert claim.status is ClaimStatus.VERIFIED
                else:
                    assert orch.store.list_knowledge(mission.id) == []
                    assert claim.confidence_metadata["basis"]["type_downgraded"] is True
                    if case_name == "reserved_key":
                        assert claim.key is None
                        assert claim.confidence_metadata["key_downgraded"] is True
            assert provider.by_role == {"worker": 3}
            database = orch.store.path
        if case_name == "long_quote":
            reopened = Store.open_readonly(database)
            try:
                [knowledge] = reopened.list_knowledge(mission.id)
                claim = reopened.get_claim(knowledge.claim_id)
                assert quote in knowledge.content and claim.content == knowledge.content
                for value in (claim, knowledge):
                    with pytest.raises(ContractError, match="exceeds 20000"):
                        replace(value, type="statement")
            finally:
                reopened.close()

    asyncio.run(case())


@pytest.mark.parametrize("target,expected_ok", [(None, False), ("b", False), ("a", True)])
def test_doc_action_criterion_requires_a_checked_matching_candidate(tmp_path, target, expected_ok):
    import json

    from agent_orchestrator.governance.policies import DeploymentPolicy
    from agent_orchestrator.runtime.connectors import TestConfigService

    async def case():
        config = OrchestratorConfig(
            evidence_root=tmp_path / "run",
            deployment_policy=DeploymentPolicy(enabled_connectors=("test_config",)),
        )
        connector = TestConfigService(tmp_path / "service.json")
        async with Orchestrator(
            config, RoleScriptedProvider({}), connectors={"test_config": connector}
        ) as orch:
            mission = await orch.submit_mission(
                spec(
                    domain=DOC_DOMAIN,
                    success_criteria=("action:test_config.set:a", "action:test_config.set:b"),
                )
            )
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                outputs=["actions/change.json"],
                                success_criteria=["action:test_config.set:a"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            workspace = orch.assembled.workspaces.create("fixture-attempt", seed={})
            if target is not None:
                workspace.write_text(
                    "actions/change.json",
                    json.dumps(
                        {
                            "connector": "test_config",
                            "operation": "set",
                            "target": target,
                            "params": {"value": "on"},
                            "reason": "record",
                        }
                    ),
                )
            artifacts = workspace.snapshot(
                mission_id=mission.id, task_id=tasks[0].id, produced_by="worker"
            )
            problems = orch._action_problems(mission, tasks[0], artifacts, workspace)
            assert problems is not None  # absence of a candidate is not a checked PASS
            assert (problems == []) is expected_ok

    asyncio.run(case())
